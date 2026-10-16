"""抖音直播弹幕抓取（无头浏览器读 DOM）。

抖音弹幕不在视频像素里，而是网页客户端单独渲染的一层 UI。这里用 Playwright 起无头
Chromium 打开直播间，浏览器原生完成签名 / WebSocket / protobuf，我们只用
MutationObserver 监听 .webcast-chatroom___item 的新增节点，取出"昵称：内容"回调。
好处：对抖音签名/协议变更免疫（浏览器自己处理）。

实现要点：用 Playwright【同步】API 跑在独立线程里——它自带 greenlet 驱动、不依赖
asyncio 子进程，能绕开 uvicorn(uvloop) 的事件循环/child-watcher 冲突。捕获到弹幕后
用 run_coroutine_threadsafe 跨线程回调到主 loop。

找不到弹幕列表不再整场放弃（2026-10-03，9-30 晚班整场 0 条弹幕）：同一个浏览器、同一个页面，
每 RETRY_SEC 秒重新打开一次直播间，直到挂上弹幕列表或场次结束；页面崩了就在同一个浏览器里
新开一个页面。整场都没采到时 status = "missing"，由流水线记到场次上。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Awaitable, Callable

from ..config import get_settings
from .safe_error import safe_error_text, safe_exception_code

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

RETRY_SEC = 90          # 没挂上弹幕列表时，隔多久重新打开一次直播间（复用同一个页面）
HOOK_POLL_SEC = 3       # 多久看一次弹幕列表挂上了没有
CRASH_RETRY_SEC = 5     # 页面出错后隔多久重开
PAGE_TIMEOUT_MS = 30000

_SYS_MARKERS = ("欢迎来到直播间", "抖音严禁", "理性消费", "登录后才可以",
                "主播正在直播", "为你推荐")

_OBSERVER_JS = r"""
() => {
  if (window.__dmHooked) return true;
  const seen = new WeakSet();
  const emit = (el) => {
    try {
      const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if (t) window._pushDanmaku(t);
    } catch (e) {}
  };
  const attach = () => {
    const list = document.querySelector('.webcast-chatroom___list');
    if (!list) { setTimeout(attach, 1000); return; }
    window.__dmHooked = true;
    // 入场即抓屏幕上已有的近期弹幕（复盘也需要），之后增量捕获
    list.querySelectorAll('.webcast-chatroom___item').forEach(e => { seen.add(e); emit(e); });
    const obs = new MutationObserver(muts => {
      muts.forEach(m => m.addedNodes.forEach(n => {
        if (n.nodeType !== 1) return;
        const items = (n.matches && n.matches('.webcast-chatroom___item'))
          ? [n] : [...(n.querySelectorAll ? n.querySelectorAll('.webcast-chatroom___item') : [])];
        items.forEach(it => { if (!seen.has(it)) { seen.add(it); emit(it); } });
      }));
    });
    obs.observe(list, { childList: true, subtree: true });
  };
  attach();
  return true;
}
"""

DanmakuCb = Callable[[str, str], Awaitable[None]]  # (nickname, content)
HookedCb = Callable[[], Awaitable[None]]


class DanmakuScraper:
    def __init__(self, entry_url: str, on_danmaku: DanmakuCb, main_loop,
                 on_hooked: HookedCb | None = None) -> None:
        self.entry_url = entry_url
        self.on_danmaku = on_danmaku
        self.on_hooked = on_hooked
        self.main_loop = main_loop
        self._stop_evt = threading.Event()
        self._recent: list[str] = []
        self._thread: threading.Thread | None = None
        self.hooked = False      # 整场有没有挂上过弹幕列表
        self.captured = 0        # 整场采到几条
        self.attempts = 0        # 打开过几次直播间页面

    @property
    def status(self) -> str:
        """ok：挂上过弹幕列表或采到过弹幕；missing：整场都没采到。"""
        return "ok" if self.hooked or self.captured else "missing"

    def _handle(self, raw: str) -> None:
        """Playwright 线程里同步回调 → 解析 → 调度到主 loop。"""
        text = (raw or "").strip()
        if not text or any(m in text for m in _SYS_MARKERS):
            return
        nick, sep, content = text.partition("：")
        if not sep:
            nick, sep, content = text.partition(":")
        if not sep:
            nick, content = "", text
        content = content.strip()
        if not content:
            return
        key = f"{nick}|{content}"
        if key in self._recent:
            return
        self._recent.append(key)
        if len(self._recent) > 80:
            self._recent = self._recent[-80:]
        self.captured += 1
        try:
            asyncio.run_coroutine_threadsafe(
                self.on_danmaku(nick.strip(), content), self.main_loop)
        except Exception:  # noqa: BLE001
            pass

    def _mark_hooked(self) -> None:
        if self.hooked:
            return
        self.hooked = True
        print(f"[danmaku] 弹幕列表已挂上（第 {self.attempts} 次打开直播间）")
        if self.on_hooked is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.on_hooked(), self.main_loop)
            except Exception:  # noqa: BLE001
                pass

    def _open_page(self, ctx, page):  # noqa: ANN001
        """（重新）打开直播间并装上监听；复用传进来的页面，没有或已关才在同一个浏览器里新开。"""
        try:
            if page is None or page.is_closed():
                page = ctx.new_page()
                page.expose_function("_pushDanmaku", self._handle)   # 换页面导航后仍然有效
            self.attempts += 1
            page.goto(self.entry_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            page.evaluate(_OBSERVER_JS)       # 页面里每秒找一次弹幕列表，找到就挂上
        except Exception as e:  # noqa: BLE001
            print(
                f"[danmaku] 第 {self.attempts} 次打开直播间没成功，{RETRY_SEC} 秒后再试; "
                f"code={safe_exception_code(e, prefix='DANMAKU_OPEN')}; error={safe_error_text(e)}"
            )
        try:
            return None if page is None or page.is_closed() else page
        except Exception:  # noqa: BLE001
            return None

    def _watch(self, ctx) -> None:  # noqa: ANN001
        """没挂上弹幕列表就隔 RETRY_SEC 秒重开一次，直到挂上或场次结束；挂上后只在页面出错时重开。"""
        page = None
        page_hooked = False
        next_open = 0.0
        next_poll = 0.0
        while not self._stop_evt.is_set():
            now = time.monotonic()
            if now >= next_open and (page is None or not page_hooked):
                if page is not None:
                    print(f"[danmaku] {RETRY_SEC} 秒内没找到弹幕列表（可能还没开播或页面结构变了），"
                          "重新打开直播间")
                page = self._open_page(ctx, page)
                page_hooked = False
                next_open = time.monotonic() + RETRY_SEC
                next_poll = 0.0
            if page is None:
                self._stop_evt.wait(1.0)
                continue
            try:
                page.wait_for_timeout(500)   # 泵事件 + 查停止
                if not page_hooked and time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + HOOK_POLL_SEC
                    if page.evaluate("() => !!window.__dmHooked"):
                        page_hooked = True
                        self._mark_hooked()
            except Exception as e:  # noqa: BLE001  页面崩了：关掉，过一会儿在同一个浏览器里重开
                print(
                    f"[danmaku] 直播间页面出错，{CRASH_RETRY_SEC} 秒后重开; "
                    f"code={safe_exception_code(e, prefix='DANMAKU_PAGE')}; error={safe_error_text(e)}"
                )
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
                page = None
                page_hooked = False
                next_open = time.monotonic() + CRASH_RETRY_SEC

    def run_in_thread(self) -> None:
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as pw:
                launch_kwargs = {
                    "headless": True,
                    "args": ["--no-sandbox", "--disable-dev-shm-usage"],
                }
                executable_path = (get_settings().playwright_chromium_executable_path
                                   or os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "")).strip()
                if executable_path:
                    launch_kwargs["executable_path"] = executable_path
                browser = pw.chromium.launch(**launch_kwargs)     # 整场只起这一个浏览器
                try:
                    ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
                    ctx.route("**/*", lambda route: (
                        route.abort() if route.request.resource_type in ("image", "media", "font")
                        else route.continue_()))
                    self._watch(ctx)
                finally:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001  弹幕失败不拖垮主流程
            print(
                "[danmaku] 抓取失败; "
                f"code={safe_exception_code(e, prefix='DANMAKU')}; "
                f"error={safe_error_text(e)}"
            )
        finally:
            if self.status == "missing":
                print(f"[danmaku] 整场没采到弹幕（打开过 {self.attempts} 次直播间）")

    def start(self) -> None:
        """起独立线程跑 Playwright（daemon，避免拖住进程退出）。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run_in_thread, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()

    def join(self, timeout: float = 8) -> None:
        """等线程把 Chromium 关掉再返回（在线程池里调，别阻塞事件循环）。"""
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
