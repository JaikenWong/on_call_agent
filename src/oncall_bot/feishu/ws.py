"""飞书长连接线程。

lark.ws.Client.start() 是阻塞调用且使用模块级事件循环，必须放在独立线程里，
并在该线程内把 lark_oapi.ws.client.loop 换成新建的 loop，否则与主循环冲突。
事件回调里只做投递（飞书约 3 秒内要确认，否则重推）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Protocol

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

log = logging.getLogger(__name__)


class EventSink(Protocol):
    def on_message(self, data: P2ImMessageReceiveV1) -> Coroutine[None, None, None]: ...

    def on_card_action(self, data: P2CardActionTrigger) -> None: ...


def start_ws_thread(
    app_id: str,
    app_secret: str,
    main_loop: asyncio.AbstractEventLoop,
    sink: EventSink,
    *,
    on_fatal: Callable[[str], None] | None = None,
) -> threading.Thread:
    def on_message(data: P2ImMessageReceiveV1) -> None:
        try:
            asyncio.run_coroutine_threadsafe(sink.on_message(data), main_loop)
        except Exception:
            log.exception("投递消息事件到主循环失败")

    def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            main_loop.call_soon_threadsafe(sink.on_card_action, data)
        except Exception:
            log.exception("投递卡片回调到主循环失败")
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已收到"}})

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )

    def run() -> None:
        import lark_oapi.ws.client as ws_client

        ws_client.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ws_client.loop)
        log.info("飞书长连接线程启动")
        try:
            lark.ws.Client(
                app_id,
                app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.WARNING,
                auto_reconnect=True,
            ).start()
        except Exception as exc:
            log.exception("飞书长连接线程异常退出")
            if on_fatal is not None:
                main_loop.call_soon_threadsafe(on_fatal, f"长连接异常退出：{exc}")
            return
        # start() 正常返回 = 连接被关闭且不再重连。进程还活着但再也收不到消息，
        # 什么都不做的话只能靠用户发现「机器人不回话了」。
        log.error("飞书长连接已结束（未重连），进程将退出以便外部重启")
        if on_fatal is not None:
            main_loop.call_soon_threadsafe(on_fatal, "长连接已结束")

    thread = threading.Thread(target=run, name="feishu-ws", daemon=True)
    thread.start()
    return thread
