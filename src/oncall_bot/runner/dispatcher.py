"""全局单队列调度。A 模式下只有一条活跃会话，所以只需要一个 worker。

严格串行是硬要求：两个 Claude 进程同时写同一条会话会损坏历史。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from ..config import Config
from ..feishu.api import FeishuApi
from ..feishu.parser import IncomingMessage
from ..state import StateStore
from .connection import SessionConnection
from .session_runner import SessionRunner, TurnHandle

log = logging.getLogger(__name__)


@dataclass
class SubmitResult:
    accepted: bool
    note: str = ""  # 需要告知用户时的提示文本；为空表示无需额外回复


class Dispatcher:
    def __init__(self, cfg: Config, api: FeishuApi, state: StateStore) -> None:
        self._cfg = cfg
        self._api = api
        self._queue: asyncio.Queue[TurnHandle] = asyncio.Queue(maxsize=cfg.runner.max_queue)
        self.connection = SessionConnection(cfg.runner.connection_idle_seconds)
        self._runner = SessionRunner(cfg, api, state, self.connection)
        self._worker: asyncio.Task[None] | None = None
        self._idle_watch: asyncio.Task[None] | None = None
        self._current: TurnHandle | None = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run_worker(), name="turn-worker")
            log.info("调度 worker 已启动（队列上限 %d）", self._cfg.runner.max_queue)
        if self._idle_watch is None and self._cfg.runner.connection_idle_seconds > 0:
            self._idle_watch = asyncio.create_task(self._watch_idle(), name="connection-idle")

    async def shutdown(self) -> None:
        if self._current is not None:
            await self._current.interrupt("桥正在退出")
        for task_attr in ("_idle_watch", "_worker"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                setattr(self, task_attr, None)
        await self.connection.close("桥正在退出")

    async def _watch_idle(self) -> None:
        """连接空闲太久就断开，免得长期占着会话。运行中不动它。"""
        interval = max(5, min(60, self._cfg.runner.connection_idle_seconds // 4 or 5))
        while True:
            await asyncio.sleep(interval)
            if self.is_running():
                continue
            with contextlib.suppress(Exception):
                await self.connection.disconnect_if_idle()

    # ---------- 状态 ----------

    def is_running(self) -> bool:
        return self._current is not None

    def queue_size(self) -> int:
        return self._queue.qsize()

    # ---------- 提交 / 中断 ----------

    async def submit(self, msg: IncomingMessage) -> SubmitResult:
        handle = TurnHandle(msg=msg)
        try:
            self._queue.put_nowait(handle)
        except asyncio.QueueFull:
            return SubmitResult(
                accepted=False,
                note=f"队列已满（{self._cfg.runner.max_queue} 条），请等当前任务跑完再发。",
            )

        ahead = self._queue.qsize() - 1
        if self.is_running():
            if ahead > 0:
                return SubmitResult(True, f"已排队，前面还有 {ahead} 条。")
            return SubmitResult(True, "当前有任务在跑，已排队，马上处理。")
        return SubmitResult(True)

    async def stop_current(self) -> str:
        """清空队列并中断当前轮。返回面向用户的文本。"""
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            dropped += 1

        current = self._current
        if current is None:
            if dropped:
                return f"当前没有运行中的任务，已清空队列里的 {dropped} 条。"
            return "当前没有运行中的任务。"

        ok = await current.interrupt("用户发送 /stop 中断")
        parts = ["已请求中断当前任务。" if ok else "中断请求发送失败，请看日志。"]
        if dropped:
            parts.append(f"另外清空了队列里的 {dropped} 条。")
        return " ".join(parts)

    # ---------- worker ----------

    async def _run_worker(self) -> None:
        while True:
            handle = await self._queue.get()
            self._current = handle
            try:
                await self._runner.run_turn(handle)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker 执行一轮时抛出未捕获异常")
            finally:
                self._current = None
                self._queue.task_done()
