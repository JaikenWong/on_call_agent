"""把一个长连接上的消息流拆成「本轮的」和「后台任务的」。

为什么需要这个：`receive_response()` 的实现是「遇到第一个 ResultMessage 就 return」，
没有任何关联机制。长连接下 Claude 自己起的后台任务会在轮外产出消息，
其中包括**它自己的 ResultMessage**，于是下一轮一 query 就立刻读到它，
把后台任务的输出当成了本轮答案，而本轮的真实结果被推迟到再下一轮。

实测到的真实序列（见 docs 设计文档 2.9）：一次 query 之后收到两个 ResultMessage，
  第一个 origin={'kind': 'task-notification', ...}  ← 上一轮后台任务的
  第二个 origin=None                                ← 本轮的
所以判据是 origin：`None` 或 `'human'` 才算本轮，其余都不是终止条件。
注意 MessageOrigin 是 TypedDict，实际拿到的是 dict。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    Message,
    ResultMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

log = logging.getLogger(__name__)

# 这些 origin.kind 说明消息不是在回答用户这一轮的提问
HUMAN_ORIGIN_KINDS: set[str | None] = {None, "human"}

_TASK_MESSAGE_TYPES = (
    TaskStartedMessage,
    TaskUpdatedMessage,
    TaskProgressMessage,
    TaskNotificationMessage,
)


def origin_kind(message: Any) -> str | None:
    """取 origin.kind。MessageOrigin 是 TypedDict，所以实际是 dict。"""
    origin = getattr(message, "origin", None)
    if origin is None:
        return None
    if isinstance(origin, dict):
        kind = origin.get("kind")
        return kind if isinstance(kind, str) else None
    kind = getattr(origin, "kind", None)
    return kind if isinstance(kind, str) else None


def is_turn_result(message: Any) -> bool:
    """这个 ResultMessage 是不是本轮提问的答案。"""
    return isinstance(message, ResultMessage) and origin_kind(message) in HUMAN_ORIGIN_KINDS


def is_task_message(message: Any) -> bool:
    return isinstance(message, _TASK_MESSAGE_TYPES)


@dataclass
class BackgroundEvent:
    task_id: str | None
    kind: str
    status: str | None = None
    summary: str | None = None

    def describe(self) -> str:
        label = self.task_id or "后台任务"
        if self.status:
            return f"{label}：{self.status}"
        return f"{label}：{self.kind}"


@dataclass
class TurnStream:
    """过滤出本轮消息，顺带把后台任务动态收集起来。

    终止条件只认 origin 为 None / 'human' 的 ResultMessage —— 这样即使后台任务链
    一直不收尾，本轮也不会卡住。
    """

    background: list[BackgroundEvent] = field(default_factory=list)
    saw_turn_result: bool = False

    async def iterate(self, messages: AsyncIterator[Message]) -> AsyncIterator[Message]:
        # 后台任务链进行中：期间的助手消息属于那条链，不该进本轮正文。
        # 任何 ResultMessage 都会把这个开关复位，所以不会长期误过滤。
        in_background = False

        async for message in messages:
            if is_task_message(message):
                in_background = True
                self._record(message)
                continue

            if isinstance(message, ResultMessage):
                if is_turn_result(message):
                    self.saw_turn_result = True
                    yield message
                    return
                log.info(
                    "忽略非本轮的 ResultMessage：origin.kind=%s num_turns=%s",
                    origin_kind(message),
                    message.num_turns,
                )
                self._record(message)
                in_background = False
                continue

            if in_background or origin_kind(message) not in HUMAN_ORIGIN_KINDS:
                continue

            yield message

    def _record(self, message: Any) -> None:
        event = BackgroundEvent(
            task_id=getattr(message, "task_id", None),
            kind=getattr(message, "subtype", None) or type(message).__name__,
            status=getattr(message, "status", None),
            summary=getattr(message, "summary", None),
        )
        # 同一个任务的多条进度只保留最后一条，免得刷屏
        for index, existing in enumerate(self.background):
            if existing.task_id and existing.task_id == event.task_id:
                self.background[index] = event
                return
        self.background.append(event)

    def summary_note(self) -> str:
        """给卡片用的一行提示。没有后台动态时返回空串。"""
        done = [e for e in self.background if (e.status or "") == "completed"]
        if done:
            names = "、".join(e.task_id or "?" for e in done)
            return f"期间有后台任务完成：{names}（它们的输出不属于本轮回答）"
        if self.background:
            return f"期间有 {len(self.background)} 个后台任务在跑"
        return ""
