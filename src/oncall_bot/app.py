"""组装各模块，承载消息主流程。"""

from __future__ import annotations

import logging
from typing import Any

from .attachments import AttachmentStore, PendingAttachments, ack_text
from .commands import CommandHandler
from .config import Config
from .feishu import cards
from .feishu.api import FeishuApi
from .feishu.parser import IncomingMessage, UnsupportedMessage, parse_message
from .policy import Deduper
from .runner.dispatcher import Dispatcher
from .state import StateStore

log = logging.getLogger(__name__)


class App:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._api = FeishuApi(cfg.feishu.app_id, cfg.feishu.app_secret)
        self._state = StateStore(cfg.state_file)
        self._dispatcher = Dispatcher(cfg, self._api, self._state)
        self._pending = PendingAttachments(
            ttl_seconds=cfg.attachments.pending_ttl_seconds,
            max_items=cfg.attachments.max_pending,
        )
        self._attachments = AttachmentStore(self._api, cfg.attachments)
        self._commands = CommandHandler(
            cfg, self._api, self._state, self._dispatcher, self._pending
        )
        self._dedupe = Deduper()
        self._bot_open_id = ""

    # ---------- 生命周期 ----------

    async def startup(self) -> None:
        await self._state.load()
        self._bot_open_id = await self._api.get_bot_open_id()
        log.info("机器人 open_id=%s", self._bot_open_id)
        await self._recover_stale_card()
        await self._dispatcher.start()
        active = self._state.active
        if active:
            log.info("当前活跃会话 %s（%s）", active.short_id, active.workdir)
        else:
            log.info("当前没有活跃会话，等待 /use")

    async def shutdown(self) -> None:
        await self._dispatcher.shutdown()

    async def _recover_stale_card(self) -> None:
        """上次进程中途退出时留下的「处理中」卡片，刷成状态未知。"""
        ref = self._state.last_card
        if ref is None:
            return
        log.info("发现上次遗留的运行中卡片 %s，刷成「状态未知」", ref.message_id)
        await self._api.patch_card(ref.message_id, cards.stale_card())
        await self._state.set_last_card(None)

    # ---------- 事件入口 ----------

    async def on_message(self, data: Any) -> None:
        try:
            await self._handle_message(data)
        except Exception:
            log.exception("处理消息事件时抛出未捕获异常")

    def on_card_action(self, data: Any) -> None:
        # 第一版的卡片没有按钮；保留通路给 v2 的审批卡片。
        log.debug("收到卡片回调（第一版未使用）：%s", getattr(data, "event", None))

    # ---------- 主流程 ----------

    async def _handle_message(self, data: Any) -> None:
        message_id = _event_message_id(data)
        if message_id and not self._dedupe.check_and_add(message_id):
            log.info("重复推送，丢弃：%s", message_id)
            return

        try:
            msg = parse_message(data, self._bot_open_id)
        except UnsupportedMessage as unsupported:
            if self._should_respond(unsupported.chat_type, unsupported.mentioned_bot) and (
                self._cfg.policy.is_allowed(unsupported.sender_open_id)
            ):
                await self._api.reply_text(
                    unsupported.message_id,
                    f"暂不支持这种消息（收到 {unsupported.message_type}）。"
                    "目前支持文字、图片、文件和富文本。",
                    in_thread=unsupported.in_thread,
                )
            return

        if not self._should_respond(msg.chat_type, msg.mentioned_bot):
            return

        if not self._cfg.policy.is_allowed(msg.sender_open_id):
            log.info("拒绝：非白名单用户 sender_open_id=%s", msg.sender_open_id)
            await self._api.reply_text(msg.message_id, "无权限。", in_thread=msg.in_thread)
            return

        # 这行是第一次配置时拿自己 open_id 的办法：白名单留空启动一次，发条消息看这里
        log.info(
            "收到消息 message_id=%s chat_type=%s chat_id=%s sender_open_id=%s 长度=%d",
            msg.message_id,
            msg.chat_type,
            msg.chat_id,
            msg.sender_open_id,
            len(msg.text),
        )
        log.debug("消息正文：%r", msg.text)

        # 附件先下载。纯附件消息只回执、不跑一轮（飞书里图片 / 文件是独立消息、不能带文字，
        # 人的习惯是先发东西再说需求），图文混排的 post 消息则正常跑一轮。
        if msg.attachments and not await self._handle_attachments(msg):
            return

        if not msg.text:
            await self._api.reply_text(
                msg.message_id, "消息是空的。发 /help 看用法。", in_thread=msg.in_thread
            )
            return

        if self._commands.is_command(msg.text):
            await self._commands.handle(msg)
            return

        if self._state.active is None:
            await self._api.reply_text(
                msg.message_id,
                "还没有选择会话。发 /sessions 看列表，再 /use <序号>。",
                in_thread=msg.in_thread,
            )
            return

        if not self._dispatcher.has_room():
            await self._api.reply_text(
                msg.message_id,
                f"队列已满（{self._cfg.runner.max_queue} 条），请等当前任务跑完再发。"
                "攒着的附件会留着，下一条成功的需求会带上。",
                in_thread=msg.in_thread,
            )
            return

        result = await self._dispatcher.submit(msg, self._pending.take())
        if result.note:
            await self._api.reply_text(msg.message_id, result.note, in_thread=msg.in_thread)

    async def _handle_attachments(self, msg: IncomingMessage) -> bool:
        """处理消息里的图片 / 文件。返回 False 表示本条消息到此为止，不再往下走。"""
        if not self._cfg.attachments.enabled:
            await self._api.reply_text(
                msg.message_id,
                "附件处理已关闭（配置 attachments.enabled）。请把需求用文字描述。",
                in_thread=msg.in_thread,
            )
            return False

        active = self._state.active
        saved, failures = await self._attachments.download(
            msg.message_id, msg.attachments, active.short_id if active else "unbound"
        )
        for item in saved:
            self._pending.add(item)

        if msg.is_attachment_only:
            await self._api.reply_text(
                msg.message_id,
                ack_text(saved, failures, self._pending.peek_count()),
                in_thread=msg.in_thread,
            )
            return False

        if failures:
            await self._api.reply_text(
                msg.message_id,
                "部分附件没能下载：\n" + "\n".join(f"- {r}" for r in failures),
                in_thread=msg.in_thread,
            )
        return True

    def _should_respond(self, chat_type: str, mentioned_bot: bool) -> bool:
        if chat_type != "group":
            return True
        if not self._cfg.policy.require_mention_in_group:
            return True
        return mentioned_bot


def _event_message_id(data: Any) -> str:
    try:
        return data.event.message.message_id or ""
    except AttributeError:
        return ""
