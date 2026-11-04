"""跨轮复用的 Claude 连接。

为什么要长连接：实测每轮新建 ClaudeSDKClient 的连接成本是 8 秒量级，
在 19MB 的会话上每轮墙钟 11.75s，复用之后稳定在 1.62s（见 scripts/bench_startup.py）。
会话越大差距越明显，因为每次连接都要重新加载整个历史、重新初始化 MCP 和 skills。

为什么中断后必须丢弃连接：实测 interrupt() 之后同一个连接不可复用，
后续 query() 会拿到 `API Error: 422`。所以 /stop 和超时中断之后一律重连。
"""

from __future__ import annotations

import asyncio
import logging
import time

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

log = logging.getLogger(__name__)


class SessionConnection:
    """同一时刻最多持有一个连接（A 模式只有一条活跃会话）。"""

    def __init__(self, idle_seconds: int) -> None:
        self._idle_seconds = idle_seconds
        self._client: ClaudeSDKClient | None = None
        self._session_id: str | None = None
        self._connected_at = 0.0
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    # ---------- 状态 ----------

    def is_warm_for(self, session_id: str) -> bool:
        return self._client is not None and self._session_id == session_id

    @property
    def idle_seconds(self) -> float:
        if self._client is None:
            return 0.0
        return time.monotonic() - self._last_used

    @property
    def state_text(self) -> str:
        if self._client is None:
            return "未连接（下一条消息会建立连接）"
        alive = time.monotonic() - self._connected_at
        return f"已连接 {alive:.0f}s，空闲 {self.idle_seconds:.0f}s"

    # ---------- 获取 / 释放 ----------

    async def acquire(
        self, session_id: str, options: ClaudeAgentOptions
    ) -> tuple[ClaudeSDKClient, bool]:
        """返回 (client, 本次是否新建连接)。"""
        async with self._lock:
            if self._client is not None and self._session_id == session_id:
                self._last_used = time.monotonic()
                return self._client, False

            if self._client is not None:
                await self._close_locked("活跃会话已切换")

            started = time.monotonic()
            client = ClaudeSDKClient(options=options)
            await client.connect()
            self._client = client
            self._session_id = session_id
            self._connected_at = time.monotonic()
            self._last_used = self._connected_at
            log.info("已建立 Claude 连接（会话 %s，耗时 %.2fs）", session_id[:8], self._last_used - started)
            return client, True

    def touch(self) -> None:
        if self._client is not None:
            self._last_used = time.monotonic()

    async def close(self, reason: str) -> None:
        async with self._lock:
            await self._close_locked(reason)

    async def _close_locked(self, reason: str) -> None:
        if self._client is None:
            return
        session = (self._session_id or "")[:8]
        try:
            await self._client.disconnect()
        except Exception:
            log.warning("断开 Claude 连接时抛异常（会话 %s）", session, exc_info=True)
        self._client = None
        self._session_id = None
        log.info("已断开 Claude 连接（会话 %s）：%s", session, reason or "无原因")

    async def disconnect_if_idle(self) -> bool:
        """空闲超时就断开。idle_seconds 配成 0 表示永不自动断开。"""
        if self._idle_seconds <= 0 or self._client is None:
            return False
        if self.idle_seconds < self._idle_seconds:
            return False
        await self.close(f"空闲超过 {self._idle_seconds}s")
        return True
