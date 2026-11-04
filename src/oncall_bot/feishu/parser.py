"""飞书消息事件 → IncomingMessage。纯函数，便于单测。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# 文本里的 @ 占位符：@_user_1 / @_all
_MENTION_PLACEHOLDER = re.compile(r"@_(?:user_\d+|all)\s*")


@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str  # "p2p" | "group"
    thread_id: str
    sender_open_id: str
    text: str
    mentioned_bot: bool

    @property
    def in_thread(self) -> bool:
        return bool(self.thread_id)


@dataclass
class UnsupportedMessage(Exception):
    """非文本消息。调用方按同样的 @ / 白名单规则决定是否回复「暂只支持文本」。"""

    message_id: str
    chat_id: str
    chat_type: str
    thread_id: str
    sender_open_id: str
    mentioned_bot: bool
    message_type: str

    @property
    def in_thread(self) -> bool:
        return bool(self.thread_id)


def strip_mentions(text: str) -> str:
    return _MENTION_PLACEHOLDER.sub(" ", text).strip()


def extract_text(message_type: str, content_json: str) -> str | None:
    """从飞书 content（JSON 字符串）里取纯文本。不支持的类型返回 None。"""
    try:
        content: Any = json.loads(content_json or "{}")
    except json.JSONDecodeError:
        return None

    if message_type == "text":
        value = content.get("text")
        return value if isinstance(value, str) else None

    if message_type == "post":
        # {"title": "...", "content": [[{"tag":"text","text":"..."}, ...], ...]}
        parts: list[str] = []
        title = content.get("title")
        if isinstance(title, str) and title.strip():
            parts.append(title.strip())
        blocks = content.get("content")
        if isinstance(blocks, list):
            for line in blocks:
                if not isinstance(line, list):
                    continue
                segs = [
                    seg.get("text", "")
                    for seg in line
                    if isinstance(seg, dict) and seg.get("tag") in ("text", "a", "md")
                ]
                joined = "".join(s for s in segs if isinstance(s, str))
                if joined.strip():
                    parts.append(joined.strip())
        return "\n".join(parts) if parts else None

    return None


def parse_message(event_data: Any, bot_open_id: str) -> IncomingMessage:
    """解析 P2ImMessageReceiveV1。非文本消息抛 UnsupportedMessage。"""
    event = event_data.event
    message = event.message
    sender_open_id = ""
    sender = getattr(event, "sender", None)
    if sender is not None and getattr(sender, "sender_id", None) is not None:
        sender_open_id = getattr(sender.sender_id, "open_id", "") or ""

    message_id = message.message_id or ""
    chat_id = message.chat_id or ""
    thread_id = getattr(message, "thread_id", None) or ""
    message_type = message.message_type or ""

    mentioned_bot = False
    for mention in getattr(message, "mentions", None) or []:
        mention_id = getattr(mention, "id", None)
        if mention_id is not None and getattr(mention_id, "open_id", None) == bot_open_id:
            mentioned_bot = True
            break

    text = extract_text(message_type, message.content or "")
    if text is None:
        raise UnsupportedMessage(
            message_id=message_id,
            chat_id=chat_id,
            chat_type=message.chat_type or "group",
            thread_id=thread_id,
            sender_open_id=sender_open_id,
            mentioned_bot=mentioned_bot,
            message_type=message_type,
        )

    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=message.chat_type or "group",
        thread_id=thread_id,
        sender_open_id=sender_open_id,
        text=strip_mentions(text),
        mentioned_bot=mentioned_bot,
    )
