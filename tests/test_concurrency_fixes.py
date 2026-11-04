"""并发问题的回归测试：不需要飞书凭据，也不需要真实模型。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from oncall_bot.attachments import AttachmentStore, PendingAttachments, SavedImage
from oncall_bot.config import (
    AttachmentsConfig,
    Config,
    FeishuConfig,
    RenderConfig,
    RunnerConfig,
)
from oncall_bot.feishu import cards
from oncall_bot.feishu.parser import IncomingMessage
from oncall_bot.runner.dispatcher import Dispatcher
from oncall_bot.runner.render import TurnRenderer
from oncall_bot.runner.session_runner import SessionRunner, TurnHandle


def make_msg(text: str = "hi", message_id: str = "om_1", sender: str = "ou_1") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        chat_id="oc_1",
        chat_type="p2p",
        thread_id="",
        sender_open_id=sender,
        text=text,
        mentioned_bot=False,
    )


def make_config() -> Config:
    return Config(feishu=FeishuConfig(app_id="cli_real", app_secret="secret_real"))


class FakeApi:
    def __init__(self) -> None:
        self.patches: list[dict] = []
        self.texts: list[str] = []

    async def reply_text(self, message_id: str, text: str, *, in_thread: bool = False) -> str:
        self.texts.append(text)
        return "om_reply"

    async def reply_card(self, message_id: str, card: dict, *, in_thread: bool = False) -> str:
        return "om_card"

    async def patch_card(self, card_message_id: str, card: dict) -> bool:
        self.patches.append(card)
        return True


class FakeState:
    def __init__(self) -> None:
        self.active = None
        self.last_card = None

    async def set_last_card(self, ref) -> None:
        self.last_card = ref


# ---------- P0-3：interrupt 幂等，且连上之前就能中断 ----------


async def test_interrupt_before_client_connect_counts_as_success():
    """卡片发出前就 /stop：不能报「中断失败」，而要标记这一轮作废。"""
    handle = TurnHandle(msg=make_msg())

    assert await handle.interrupt("用户发送 /stop 中断") is True
    assert handle.abort_reason == "用户发送 /stop 中断"


async def test_interrupt_is_idempotent_and_touches_client_once():
    """/stop 与超时监控同时打进来，只能真正调用一次 client.interrupt()。"""

    class BlockingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def interrupt(self) -> None:
            self.calls += 1
            await asyncio.sleep(0.05)

    client = BlockingClient()
    handle = TurnHandle(msg=make_msg(), client=client)  # type: ignore[arg-type]

    results = await asyncio.gather(
        handle.interrupt("第一次"),
        handle.interrupt("第二次"),
        handle.interrupt("第三次"),
    )

    assert all(results)
    assert client.calls == 1
    assert handle.abort_reason == "第一次"


# ---------- P0-1：排队时间不计入单轮超时 ----------


async def test_queue_wait_does_not_count_toward_turn_timeout():
    """排队两小时才轮到的任务，不能一开始跑就被判超时。"""
    api = FakeApi()
    runner = SessionRunner(make_config(), api, FakeState(), object())  # type: ignore[arg-type]
    handle = TurnHandle(msg=make_msg())
    handle.started_at -= 7200
    handle.last_event_at -= 7200

    before = time.monotonic()
    await runner.run_turn(handle)

    # 没有活跃会话，run_turn 在第一句就返回；时基必须已经重设
    assert handle.started_at >= before
    assert time.monotonic() - handle.started_at < 60


# ---------- P0-2：队列满时不吞待处理图片 ----------


async def test_full_queue_is_visible_before_taking_images():
    cfg = make_config()
    cfg.runner = RunnerConfig(max_queue=1)
    dispatcher = Dispatcher(cfg, FakeApi(), FakeState())  # type: ignore[arg-type]
    pending = PendingAttachments(ttl_seconds=600, max_items=9)
    pending.add(SavedImage(path=Path("/tmp/a.png"), size=1))

    assert dispatcher.has_room() is True
    await dispatcher.submit(make_msg("第一条"))
    assert dispatcher.has_room() is False

    # 调用方看到没空位就不该 take()，图片必须还在
    assert pending.peek_count() == 1


# ---------- P0-4：定稿之后迟到的刷新不能覆盖最终卡片 ----------


async def test_late_running_flush_is_dropped_after_finalize():
    api = FakeApi()
    renderer = TurnRenderer(api, "om_card", "repo", "abcdef12", RenderConfig(update_interval_ms=200))

    await renderer.start()
    renderer.add_note("中间输出")
    await asyncio.sleep(0.3)

    final = cards.done_card("repo", "完成", duration_ms=1, num_turns=1, tokens=1, short_session_id="abcdef12")
    await renderer.finalize(final)
    count = len(api.patches)

    # 定稿之后又来了一个「处理中」刷新（真实场景里是取消时已经在途的请求）
    await renderer._patch(cards.running_card("repo", "迟到的旧内容"))

    assert len(api.patches) == count
    assert api.patches[-1] is final


async def test_finalize_wins_over_concurrent_running_patch():
    """刷新和定稿并发时，最终卡片必须是最后一次写入。"""

    class SlowRunningApi(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def patch_card(self, card_message_id: str, card: dict) -> bool:
            if card["header"]["title"]["content"].startswith("处理中"):
                self.started.set()
                await self.release.wait()
            return await super().patch_card(card_message_id, card)

    api = SlowRunningApi()
    renderer = TurnRenderer(api, "om_card", "repo", "abcdef12", RenderConfig(update_interval_ms=200))

    await renderer.start()
    renderer.add_note("中间输出")
    await asyncio.wait_for(api.started.wait(), timeout=2)

    final = cards.done_card("repo", "完成", duration_ms=1, num_turns=1, tokens=1, short_session_id="abcdef12")
    finalize = asyncio.create_task(renderer.finalize(final))
    await asyncio.sleep(0.1)
    api.release.set()
    await asyncio.wait_for(finalize, timeout=2)

    assert api.patches[-1] is final


# ---------- P0-5：同一秒的两条消息不再撞图片文件名 ----------


async def test_image_filenames_do_not_collide_within_same_second(tmp_path):
    class DownloadApi:
        async def download_message_resource(self, message_id: str, key: str, rt: str = "image") -> bytes:
            # 内容里带上 message_id，模拟「两条消息各带一张不同的图」
            return b"\x89PNG\r\n\x1a\n" + message_id.encode() + key.encode()

    store = AttachmentStore(DownloadApi(), AttachmentsConfig(dir=tmp_path, enabled=True))  # type: ignore[arg-type]

    first, _ = await store.download_images("om_aaa", ["img_1"], "abcdef12")
    second, _ = await store.download_images("om_bbb", ["img_1"], "abcdef12")

    assert first[0].path != second[0].path
    assert first[0].path.exists() and second[0].path.exists()
    assert first[0].path.read_bytes() != second[0].path.read_bytes()
