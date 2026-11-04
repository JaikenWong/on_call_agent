"""单轮执行：用 ClaudeSDKClient resume 既有会话跑一条用户消息。

实测前提（见 docs 设计文档 2.1）：resume=<id> 且 fork_session=False 时
session_id 保持不变、上下文延续。因此每轮新建 client 是安全的。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    ResultError,
    ResultMessage,
    get_session_info,
)

from ..attachments import append_attachments
from ..config import Config
from ..docs_fetch import DocFetcher, FetchOutcome, build_prompt, summarize_outcome
from ..feishu import cards
from ..feishu.api import FeishuApi
from ..feishu.parser import IncomingMessage
from ..state import ActiveSession, CardRef, StateStore
from .connection import SessionConnection
from .render import TurnRenderer, clip_tail, to_feishu_markdown, total_tokens
from .turn_stream import TurnStream

log = logging.getLogger(__name__)

MONITOR_INTERVAL = 5.0
RESULT_INLINE_MAX = 2500


@dataclass
class TurnHandle:
    """一轮的运行时句柄。dispatcher 持有它，用于 /stop 和超时监控。"""

    msg: IncomingMessage
    started_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    card_message_id: str | None = None
    client: ClaudeSDKClient | None = None
    abort_reason: str | None = None
    # 本轮期间后台任务的动态，附在卡片上提醒用户「那些输出不属于本轮回答」
    background_note: str = ""
    # 入队时领走的待处理图片（飞书里先发图、再说需求）
    attachments: list[Path] = field(default_factory=list)

    async def interrupt(self, reason: str) -> bool:
        if self.client is None:
            return False
        self.abort_reason = reason
        try:
            await self.client.interrupt()
        except Exception:
            log.warning("interrupt() 调用失败", exc_info=True)
            return False
        return True


class SessionRunner:
    def __init__(
        self, cfg: Config, api: FeishuApi, state: StateStore, connection: SessionConnection
    ) -> None:
        self._cfg = cfg
        self._api = api
        self._state = state
        self._connection = connection
        self._docs = DocFetcher(api, cfg.docs)

    async def run_turn(self, handle: TurnHandle) -> None:
        msg = handle.msg
        active = self._state.active
        if active is None:
            await self._api.reply_text(
                msg.message_id,
                "还没有选择会话。发 /sessions 看列表，再 /use <序号>。",
                in_thread=msg.in_thread,
            )
            return

        warm = self._connection.is_warm_for(active.session_id)

        # 会话必须仍然存在，否则明确失败。只有 /new 建出来的 pending 会话是例外：
        # 它的文件要跑完第一轮才落盘（实测只 connect 不 query 不会写文件）。
        # 连接还活着也跳过检查 —— 连接活着本身就证明会话是活的。
        if not warm and not active.pending_create:
            info = await asyncio.to_thread(get_session_info, active.session_id, active.workdir)
            if info is None:
                await self._api.reply_card(
                    msg.message_id,
                    cards.failed_card(
                        active.repo_name,
                        f"会话 `{active.short_id}` 在 `{active.workdir}` 下已不存在。",
                        "发 /sessions 看现有会话，再 /use <序号> 选一条。不会自动新建会话。",
                    ),
                    in_thread=msg.in_thread,
                )
                return

        if warm:
            status = "正在处理…"
        elif active.pending_create:
            status = "正在新建会话并连接 Claude…"
        else:
            status = "正在连接 Claude…"
        card_id = await self._api.reply_card(
            msg.message_id,
            cards.running_card(active.repo_name, status),
            in_thread=msg.in_thread,
        )
        if card_id is None:
            log.error("运行中卡片发送失败，本轮放弃（避免无反馈地跑）")
            await self._api.reply_text(
                msg.message_id, "飞书卡片发送失败，本轮已取消，请重试。", in_thread=msg.in_thread
            )
            return

        handle.card_message_id = card_id
        await self._state.set_last_card(CardRef(message_id=card_id, chat_id=msg.chat_id))

        renderer = TurnRenderer(
            self._api, card_id, active.repo_name, active.short_id, self._cfg.render
        )
        await renderer.start()

        try:
            await self._execute(handle, active, renderer)
        finally:
            await self._state.set_last_card(None)

    async def _execute(
        self, handle: TurnHandle, active: ActiveSession, renderer: TurnRenderer
    ) -> None:
        msg = handle.msg
        options = self._build_options(active)
        monitor: asyncio.Task[None] | None = None
        result: ResultMessage | None = None

        # 消息里带飞书云文档链接时，先抓成本地文件，再把路径附在 prompt 后面
        outcome = await self._fetch_docs(msg.text, active)
        prompt = append_attachments(build_prompt(msg.text, outcome), handle.attachments)
        if not outcome.empty:
            renderer.add_note(summarize_outcome(outcome))
        if handle.attachments:
            renderer.add_note(f"带了 {len(handle.attachments)} 张图片")

        try:
            client, created = await self._connection.acquire(active.session_id, options)
            if created:
                renderer.replace_status("正在处理…")
            handle.client = client
            handle.last_event_at = time.monotonic()
            monitor = asyncio.create_task(self._monitor(handle), name="turn-monitor")

            await client.query(prompt)
            # 不能用 receive_response()：它遇到第一个 ResultMessage 就收尾，
            # 长连接下那很可能是上一轮后台任务的结果。详见 turn_stream.py
            stream = TurnStream()
            # 中断后也不要 break：必须读到本轮的 ResultMessage。
            async for message in stream.iterate(client.receive_messages()):
                handle.last_event_at = time.monotonic()
                renderer.feed(message)
                if isinstance(message, ResultMessage):
                    result = message
            handle.background_note = stream.summary_note()
            if handle.background_note:
                log.info("本轮期间的后台任务：%s", handle.background_note)
        except ResultError as exc:
            await self._connection.close("上一轮返回 ResultError")
            await self._fail(renderer, active, f"Claude 返回错误：`{exc}`", "可以直接发消息继续。")
            return
        except ProcessError as exc:
            await self._connection.close("Claude 进程异常退出")
            code = getattr(exc, "exit_code", None)
            await self._fail(
                renderer,
                active,
                f"Claude 进程异常退出（exit={code}）。",
                "会话历史没有丢，下一条消息会自动续上。",
            )
            return
        except CLINotFoundError as exc:
            await self._connection.close("找不到 CLI")
            await self._fail(
                renderer, active, f"找不到 Claude Code CLI：{exc}", "检查配置里的 runner.cli_path。"
            )
            return
        except CLIConnectionError as exc:
            await self._connection.close("连接中断")
            await self._fail(renderer, active, f"与 Claude Code 的连接中断：{exc}", "请重试。")
            return
        except Exception:
            log.exception("单轮执行出现未预期异常")
            await self._connection.close("出现未预期异常")
            await self._fail(renderer, active, "内部错误，详情见本机日志。", "可以重试。")
            return
        finally:
            handle.client = None
            self._connection.touch()
            if monitor is not None:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await monitor

        # 实测 interrupt() 之后同一连接不可复用（后续 query 返回 API Error 422），
        # 所以中断过就必须丢弃连接，下一轮重连。
        if handle.abort_reason:
            await self._connection.close("本轮被中断，连接不可复用")

        await self._finish(handle, active, renderer, result)

    async def _fetch_docs(self, text: str, active: ActiveSession) -> FetchOutcome:
        try:
            return await self._docs.fetch(text, active.short_id)
        except Exception:
            log.exception("云文档抓取环节异常，本轮按无文档继续")
            return FetchOutcome()

    def _build_options(self, active: ActiveSession) -> ClaudeAgentOptions:
        runner = self._cfg.runner
        system_prompt: Any = {"type": "preset", "preset": "claude_code"}
        if runner.system_prompt_append.strip():
            system_prompt["append"] = runner.system_prompt_append

        # inbox 和图片目录都不在仓库里，必须显式授权 Claude 读取
        add_dirs: list[str] = []
        if self._cfg.docs.enabled:
            add_dirs.append(str(self._cfg.docs.inbox_dir))
        if self._cfg.attachments.enabled:
            add_dirs.append(str(self._cfg.attachments.dir))

        # /new 建出来的会话还不存在，要用 session_id= 指定新 ID（实测 SDK 会采用这个值）；
        # 已有会话用 resume= 续上。两者互斥。
        creating = active.pending_create

        return ClaudeAgentOptions(
            cwd=active.workdir,
            add_dirs=add_dirs,
            resume=None if creating else active.session_id,
            session_id=active.session_id if creating else None,
            fork_session=False,
            permission_mode=runner.permission_mode,
            disallowed_tools=list(runner.disallowed_tools),
            setting_sources=list(runner.setting_sources),
            system_prompt=system_prompt,
            max_turns=runner.max_turns,
            model=runner.model,
            env=dict(runner.env),
            cli_path=runner.cli_path,
            stderr=_log_stderr,  # 只写日志：stderr 上有模型别名等噪音，不能当错误
        )

    async def _monitor(self, handle: TurnHandle) -> None:
        """超时监控：总时长 / 无输出时长。超时等同 /stop。"""
        runner = self._cfg.runner
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            now = time.monotonic()
            if now - handle.started_at > runner.turn_timeout_seconds:
                log.warning("单轮总时长超限，中断本轮")
                await handle.interrupt(f"超时中断（单轮超过 {runner.turn_timeout_seconds}s）")
                return
            if now - handle.last_event_at > runner.idle_output_timeout_seconds:
                log.warning("长时间无输出，中断本轮")
                await handle.interrupt(
                    f"超时中断（连续 {runner.idle_output_timeout_seconds}s 无输出）"
                )
                return

    async def _finish(
        self,
        handle: TurnHandle,
        active: ActiveSession,
        renderer: TurnRenderer,
        result: ResultMessage | None,
    ) -> None:
        msg = handle.msg

        if result is None:
            await self._fail(renderer, active, "Claude 没有返回结果消息。", "请重试。")
            return

        # session_id 核对（实测正常 resume 和指定 session_id 新建时都一致）
        if result.session_id and result.session_id != active.session_id:
            log.warning(
                "session_id 变化：%s → %s，已更新状态", active.session_id, result.session_id
            )
            await self._state.update_session_id(result.session_id)

        # 拿到 ResultMessage 就说明会话文件已经落盘，pending 标记可以清掉。
        # 顺带把标题从「新会话」换成 Claude 自己生成的摘要。
        if active.pending_create:
            info = await asyncio.to_thread(
                get_session_info, result.session_id or active.session_id, active.workdir
            )
            title = (info.custom_title or info.summary) if info else None
            await self._state.finish_pending_create(title)
            log.info("新会话 %s 已落盘，标题=%r", active.short_id, title)

        await self._state.touch_active()

        body = renderer.body
        final = (renderer.final_result or "").strip()
        # final 是模型原文，也要过一遍飞书 markdown 转换（body 已经过了）
        inline = (
            to_feishu_markdown(clip_tail(final, self._cfg.render.max_card_chars))
            if final and len(final) <= RESULT_INLINE_MAX
            else body
        )
        if handle.background_note:
            inline = f"*{handle.background_note}*\n\n{inline}"

        if handle.abort_reason or (result.terminal_reason or "").startswith("aborted"):
            reason = handle.abort_reason or f"已中断（{result.terminal_reason}）"
            await renderer.finalize(
                cards.aborted_card(active.repo_name, inline, reason, short_session_id=active.short_id)
            )
            return

        if result.is_error:
            detail = "；".join(result.errors or []) or result.subtype or "未知错误"
            await renderer.finalize(
                cards.failed_card(active.repo_name, f"本轮失败：`{detail}`", "可以直接发消息继续。")
            )
            return

        await renderer.finalize(
            cards.done_card(
                active.repo_name,
                inline,
                duration_ms=result.duration_ms,
                num_turns=result.num_turns,
                tokens=total_tokens(result.usage),
                short_session_id=active.short_id,
            )
        )

        # 结果过长时卡片放摘要，全文用文字消息补发
        if final and len(final) > RESULT_INLINE_MAX:
            await self._api.reply_long_text(msg.message_id, final, in_thread=msg.in_thread)

    async def _fail(
        self, renderer: TurnRenderer, active: ActiveSession, reason: str, next_step: str
    ) -> None:
        await renderer.finalize(cards.failed_card(active.repo_name, reason, next_step))


def _log_stderr(line: str) -> None:
    log.debug("claude stderr: %s", line.rstrip())
