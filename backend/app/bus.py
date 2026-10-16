"""按 session 分发的内存事件总线，用于把实时事件推到 WebSocket 看板。

事件类型（type 字段）：
  partial   实时滚动字幕（不入库）
  sentence  定句 + 校正
  ocr       画面文字 / 弹幕
  flag      句级硬错告警
  segment   段级点评卡片
  summary   整场总结
  status    会话状态变化

慢连接/断线重连（尤其经 Cloudflare，握手要数秒）会错过这期间推送的开场事件，
导致看板一片空白。为此每场保留最近若干事件，新订阅者连上即【回放】，立刻看到现状。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any

_REPLAY_MAX = 200   # 每场回放的最近事件数（覆盖 6-8s 连接间隙的开场内容）


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._streamer_subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=_REPLAY_MAX))

    def subscribe(self, session_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        # 回放最近事件：让慢连接/重连的客户端立刻看到开场状态、主播、已出的句子/点评
        for ev in list(self._recent.get(session_id, ())):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                break
        self._subscribers[session_id].add(q)
        return q

    def unsubscribe(self, session_id: int, q: asyncio.Queue) -> None:
        self._subscribers[session_id].discard(q)
        if not self._subscribers[session_id]:
            self._subscribers.pop(session_id, None)

    def clear_recent(self, session_id: int) -> None:
        """会话彻底结束后清掉回放缓存，避免长期累积。"""
        self._recent.pop(session_id, None)

    async def publish(self, session_id: int, event: dict[str, Any]) -> None:
        self._recent[session_id].append(event)
        for q in list(self._subscribers.get(session_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 看板消费不过来时丢最旧的，保证不阻塞采集主链路
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    def subscribe_streamer(self, streamer_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._streamer_subscribers[streamer_id].add(q)
        return q

    def unsubscribe_streamer(self, streamer_id: int, q: asyncio.Queue) -> None:
        self._streamer_subscribers[streamer_id].discard(q)
        if not self._streamer_subscribers[streamer_id]:
            self._streamer_subscribers.pop(streamer_id, None)

    async def publish_streamer(self, streamer_id: int, event: dict[str, Any]) -> None:
        for q in list(self._streamer_subscribers.get(streamer_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass


bus = EventBus()
