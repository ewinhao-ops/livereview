"""抖音直播链接解析 —— 三策略兜底。

输入：主播分享的"带中文文字"的口令/短链，例如
    「3.14 复制打开抖音，看看XXX的直播 https://v.douyin.com/abcdEfg/ ...」
输出：房间号 + 可用拉流地址（FLV，H.264，实时延迟低）。

策略优先级：
  1) HTTP 解析（已跑通真实直播间）：跟随短链重定向取 web_rid → 先 GET live.douyin.com
     拿 ttwid cookie → 拉直播间页面 → 从内嵌渲染数据解析 flv_pull_url 多档地址。
     关键点（实测）：地址里 \\u0026=&、http 必须改 https（否则 CDN 403）、拉流要带 Referer。
  2) 无头浏览器嗅探（接口预留 sniff_with_browser）：改版导致 1) 失效时的兜底。
  3) 手动兜底：运营直接粘贴拉流地址。

REFERER 必须随拉流请求一起带（capture / live 代理都已带）。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import os
import re
from dataclasses import dataclass, field

import httpx

from ..config import get_settings

URL_RE = re.compile(r"https?://[^\s，。、）)]+")
ROOM_ID_RE = re.compile(r"live\.douyin\.com/(\d+)")
REFLOW_RE = re.compile(r"reflow/(\d+)")
# 结构1（live.douyin.com）：\"flv_pull_url\":{\"HD1\":\"http...\"}
_FLV_BLOCK_RE = re.compile(r'flv_pull_url\\":\{(.*?)\}')
_FLV_PAIR_RE = re.compile(r'\\"([A-Z0-9_]+)\\":\\"(http.*?)\\"')
# 结构2（reflow/amemv）：\"hd\":{\"main\":{\"flv\":\"http...\"
_FLV_MAIN_RE = re.compile(r'\\"(origin|uhd|hd|sd|ld|md)\\":\{\\"main\\":\{\\"flv\\":\\"(http.*?)\\"')
# 兜底：直接抓所有 flv 地址
_ANY_FLV_RE = re.compile(r'https?:[^"\\\s]+?\.flv[^"\\\s]*')
_JSON_STRING = r'((?:\\.|[^"\\]){1,160})'
_OWNER_NICKNAME_PATTERNS = (
    re.compile(r'"owner"\s*:\s*\{.{0,6000}?"nickname"\s*:\s*"' + _JSON_STRING, re.S),
    re.compile(r'"anchor"\s*:\s*\{.{0,3000}?"nickname"\s*:\s*"' + _JSON_STRING, re.S),
)
_ROOM_TITLE_PATTERNS = (
    re.compile(r'"(?:room_title|live_title)"\s*:\s*"' + _JSON_STRING, re.S),
    re.compile(
        r'"(?:room|roomInfo|room_info|room_data)"\s*:\s*\{'
        r'.{0,8000}?"title"\s*:\s*"' + _JSON_STRING,
        re.S,
    ),
)
_INVALID_TITLE_PARTS = (
    "广告投放", "广告推广", "巨量引擎", "seo推广", "seo 标题", "商业推广",
    "抖音直播-", "抖音直播间-", "记录美好生活", "页面不存在", "访问验证",
)
_INVALID_TITLES = {"抖音直播", "抖音直播间", "直播", "直播间", "首页", "home"}

# 画质优先级（2026-07 降码率）：优先 SD(标清 ~540P, H.264) 拉标清源流，
# 省 COS 存储/回看流量约 40-50%，画面回看 + OCR 仍够用（直播画面文字大）；零转码 CPU 开销。
# 无标清档才回退 HD(720P)/原画；LD/MD 最低仅兜底。带 5 的(HEVC)不在列、避开 ffmpeg 解码问题。
_QUALITY_ORDER = ["SD1", "SD", "SD2", "HD1", "HD", "FULL_HD1", "ORIGIN", "UHD", "LD", "MD"]

REFERER = "https://live.douyin.com/"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Referer": REFERER}


class ParseError(Exception):
    pass


@dataclass
class StreamResolution:
    room_id: str = ""
    stream_url: str = ""                       # 最终选用（FLV, https）
    candidates: dict[str, str] = field(default_factory=dict)  # 画质 -> url
    strategy: str = ""                        # http | browser | manual
    title: str = ""
    anchor_nickname: str = ""
    sec_uid: str = ""                          # 主播账号级恒定 ID（解析成功时顺带提取，供自学习）


_SEC_UID_RE = re.compile(r'"sec_uid"\s*:\s*"(MS4wLjAB[^"]+)"')
_OWNER_SEC_RE = re.compile(r'"owner"\s*:\s*\{.{0,2000}?"sec_uid"\s*:\s*"(MS4wLjAB[^"]+)"', re.S)
_WEB_RID_RE = re.compile(r'"web_rid"\s*:\s*"(\d{8,20})"')


def _extract_sec_uid(html: str) -> str:
    """从直播间页面提取主播 sec_uid：owner 邻近优先；否则页面唯一值才取（防拿到连麦/推荐主播）。"""
    m = _OWNER_SEC_RE.search(html)
    if m:
        return m.group(1)
    uids = list(dict.fromkeys(_SEC_UID_RE.findall(html)))
    return uids[0] if len(uids) == 1 else ""


def _decode_identity_text(raw: str) -> str:
    try:
        value = json.loads(f'"{raw}"')
    except (json.JSONDecodeError, TypeError):
        value = raw.replace(r"\/", "/").replace(r'\"', '"')
    value = html_lib.unescape(str(value))
    return " ".join(value.split()).strip(" -_|｜·")


def _valid_native_title(value: str) -> bool:
    lowered = value.lower()
    return bool(
        value
        and len(value) >= 2
        and lowered not in _INVALID_TITLES
        and not any(part.lower() in lowered for part in _INVALID_TITLE_PARTS)
    )


def extract_live_identity(html: str) -> tuple[str, str]:
    """只读取直播结构化数据中的主播昵称/原生标题，绝不使用页面 ``<title>``。"""
    normalized = (html or "").replace(r'\u0022', '"').replace(r'\"', '"')
    nickname = ""
    for pattern in _OWNER_NICKNAME_PATTERNS:
        match = pattern.search(normalized)
        if match:
            nickname = _decode_identity_text(match.group(1))
            if nickname:
                break
    title = ""
    for pattern in _ROOM_TITLE_PATTERNS:
        for match in pattern.finditer(normalized):
            candidate = _decode_identity_text(match.group(1))
            if _valid_native_title(candidate):
                title = candidate
                break
        if title:
            break
    return nickname[:128], title[:160]


def extract_url(text: str) -> str:
    m = URL_RE.search(text or "")
    if not m:
        raise ParseError("未在分享内容里找到链接")
    return m.group(0).rstrip("/")


def _clean(url: str) -> str:
    """页面转义还原 + 强制 https（抖音 CDN 对 http 返回 403）。"""
    url = url.replace("\\u0026", "&").replace("\\u002F", "/").replace("\\/", "/")
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def _clean_manual(url: str) -> str:
    """Decode copied escapes but preserve the caller's explicit URL scheme."""
    return url.replace("\\u0026", "&").replace("\\u002F", "/").replace("\\/", "/")


def _parse_flv_map(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    # 结构1：flv_pull_url 映射（live.douyin.com）
    m = _FLV_BLOCK_RE.search(html)
    if m:
        for key, url in _FLV_PAIR_RE.findall(m.group(1)):
            out[key] = _clean(url)
    # 结构2：data.{quality}.main.flv（reflow/amemv）。带 5 的是 HEVC，跳过
    for q, url in _FLV_MAIN_RE.findall(html):
        if "5.flv" not in url:
            out.setdefault(q.upper(), _clean(url))
    if not out:
        # 兜底：抓任意 flv 地址，按 url 里的画质后缀归类
        for raw in _ANY_FLV_RE.findall(html):
            url = _clean(raw)
            qm = re.search(r"_(uhd|hd|sd|ld|md)\d?\.flv", url)
            out.setdefault(qm.group(1).upper() if qm else "ORIGIN", url)
    return out


def _pick(candidates: dict[str, str]) -> str:
    for q in _QUALITY_ORDER:
        if q in candidates:
            return candidates[q]
    return next(iter(candidates.values())) if candidates else ""


def _parse_html(html: str, room_id: str) -> StreamResolution:
    candidates = _parse_flv_map(html)
    if not candidates:
        raise ParseError("未解析到拉流地址（可能未开播 / 需登录态 / 抖音改版）")
    nickname, native_title = extract_live_identity(html)
    return StreamResolution(
        room_id=room_id, stream_url=_pick(candidates), candidates=candidates,
        strategy="http", title=native_title, anchor_nickname=nickname,
        sec_uid=_extract_sec_uid(html),
    )


def _sniff_sync(url: str, ua: str) -> str:
    """同步 Playwright 渲染直播间页 → 返回 HTML（含内嵌拉流地址 JSON）。"""
    from playwright.sync_api import sync_playwright
    html = ""
    with sync_playwright() as pw:
        launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        exe = (get_settings().playwright_chromium_executable_path
               or os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "")).strip()
        if exe:
            launch_kwargs["executable_path"] = exe
        browser = pw.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(user_agent=ua)
        page = ctx.new_page()
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_timeout(3000)   # 等内嵌数据注入
        except Exception:  # noqa: BLE001
            pass
        html = page.content()
        browser.close()
    return html


async def sniff_with_browser(room_id: str) -> StreamResolution:
    """策略 2：无头浏览器嗅探（HTTP 正则失效时的兜底，浏览器原生完成签名）。"""
    if not room_id:
        raise ParseError("无 room_id，无法用浏览器嗅探")
    ua = get_settings().douyin_ua or _UA
    url = f"https://live.douyin.com/{room_id}"
    try:
        html = await asyncio.to_thread(_sniff_sync, url, ua)
    except Exception as e:  # noqa: BLE001  Playwright 未装/启动失败
        raise ParseError(f"浏览器嗅探失败：{e}") from e
    return _parse_html(html, room_id)   # 复用解析；解析不到会抛 ParseError


async def resolve_stream(
    *, share_link: str = "", manual_url: str = "", cookie: str = ""
) -> StreamResolution:
    """统一入口：manual → http → browser 兜底。"""
    if manual_url.strip():
        m = ROOM_ID_RE.search(manual_url) or REFLOW_RE.search(manual_url)
        return StreamResolution(room_id=m.group(1) if m else "",
                                stream_url=_clean_manual(manual_url.strip()),
                                candidates={"MANUAL": manual_url.strip()}, strategy="manual")

    headers = dict(_HEADERS)
    if get_settings().douyin_ua:
        headers["User-Agent"] = get_settings().douyin_ua
    if cookie:
        headers["Cookie"] = cookie

    rid = ""
    async with httpx.AsyncClient(headers=headers, timeout=20.0,
                                 follow_redirects=True) as client:
        await client.get("https://live.douyin.com/")          # 拿 ttwid cookie
        final = await client.get(extract_url(share_link))      # 跟随短链重定向
        furl = str(final.url)
        web_rid = ""
        mw = ROOM_ID_RE.search(furl)
        if mw:
            web_rid = rid = mw.group(1)
        else:
            mr = REFLOW_RE.search(furl)
            rid = mr.group(1) if mr else ""

        # 1) 直接从落地页解析（reflow 页 / live 页都内嵌地址）
        try:
            return _parse_html(final.text, rid)
        except ParseError:
            pass
        # 2) 落地页没有但有 web_rid → 拉 live.douyin.com 直播间页
        if web_rid:
            resp = await client.get(f"https://live.douyin.com/{web_rid}")
            try:
                return _parse_html(resp.text, web_rid)
            except ParseError:
                pass

    try:
        return await sniff_with_browser(rid)
    except ParseError as e:
        raise ParseError(
            f"自动解析失败（room={rid}）：{e}。可在后台手动粘贴拉流地址兜底。"
        ) from e


async def resolve_by_sec_uid(sec_uid: str, *, cookie: str = "") -> StreamResolution:
    """按账号级恒定 ID 定位当前直播（根治 web_rid 每场变化导致的漏录）：
    主播主页(www.douyin.com/user/<sec_uid>) 在直播中会内嵌当场 web_rid →
    再走 live.douyin.com/<web_rid> 常规解析拿拉流地址。主页 URL 永不变。"""
    if not sec_uid:
        raise ParseError("无 sec_uid")
    headers = dict(_HEADERS)
    if get_settings().douyin_ua:
        headers["User-Agent"] = get_settings().douyin_ua
    if cookie:
        headers["Cookie"] = cookie
    async with httpx.AsyncClient(headers=headers, timeout=20.0,
                                 follow_redirects=True) as client:
        await client.get("https://live.douyin.com/")          # ttwid
        r = await client.get(f"https://www.douyin.com/user/{sec_uid}")
        rids = list(dict.fromkeys(_WEB_RID_RE.findall(r.text)))
        if not rids:
            raise ParseError("账号主页未见直播房间号（未开播，或主页被风控）")
        last: Exception | None = None
        for web_rid in rids[:2]:
            resp = await client.get(f"https://live.douyin.com/{web_rid}")
            try:
                res = _parse_html(resp.text, web_rid)
                res.sec_uid = res.sec_uid or sec_uid
                return res
            except ParseError as e:
                last = e
        raise ParseError(f"主页房间号 {rids[:2]} 均未解析到流地址：{last}")
