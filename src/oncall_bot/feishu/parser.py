"""飞书消息事件 → IncomingMessage。纯函数，便于单测。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
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
    image_keys: list[str] = field(default_factory=list)

    @property
    def in_thread(self) -> bool:
        return bool(self.thread_id)

    @property
    def is_image_only(self) -> bool:
        """只有图片、没有文字 —— 这种不直接跑一轮，先攒着等后续说明。"""
        return bool(self.image_keys) and not self.text.strip()


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


def extract_content(message_type: str, content_json: str) -> tuple[str, list[str]] | None:
    """从飞书 content（JSON 字符串）里取出 (文本, 图片 key 列表)。

    不支持的消息类型返回 None。纯图片消息返回空文本 + 一个 key。
    """
    try:
        content: Any = json.loads(content_json or "{}")
    except json.JSONDecodeError:
        return None

    if message_type == "text":
        value = content.get("text")
        return (value, []) if isinstance(value, str) else None

    if message_type == "image":
        key = content.get("image_key")
        return ("", [key]) if isinstance(key, str) and key else None

    if message_type == "post":
        # {"title": "...", "content": [[{"tag":"text","text":"..."},
        #                               {"tag":"img","image_key":"..."}], ...]}
        parts: list[str] = []
        image_keys: list[str] = []
        title = content.get("title")
        if isinstance(title, str) and title.strip():
            parts.append(title.strip())
        blocks = content.get("content")
        if isinstance(blocks, list):
            for line in blocks:
                if not isinstance(line, list):
                    continue
                segs: list[str] = []
                for seg in line:
                    if not isinstance(seg, dict):
                        continue
                    tag = seg.get("tag")
                    if tag in ("text", "a", "md"):
                        value = seg.get("text")
                        if isinstance(value, str):
                            segs.append(value)
                    elif tag == "img":
                        key = seg.get("image_key")
                        if isinstance(key, str) and key:
                            image_keys.append(key)
                joined = "".join(segs)
                if joined.strip():
                    parts.append(joined.strip())
        if not parts and not image_keys:
            return None
        return ("\n".join(parts), image_keys)

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

    extracted = extract_content(message_type, message.content or "")
    if extracted is None:
        raise UnsupportedMessage(
            message_id=message_id,
            chat_id=chat_id,
            chat_type=message.chat_type or "group",
            thread_id=thread_id,
            sender_open_id=sender_open_id,
            mentioned_bot=mentioned_bot,
            message_type=message_type,
        )
    text, image_keys = extracted

    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=message.chat_type or "group",
        thread_id=thread_id,
        sender_open_id=sender_open_id,
        text=strip_mentions(text),
        mentioned_bot=mentioned_bot,
        image_keys=image_keys,
    )
