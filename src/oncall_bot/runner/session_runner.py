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

from ..config import Config
from ..docs_fetch import DocFetcher, FetchOutcome, build_prompt, summarize_outcome
from ..feishu import cards
from ..feishu.api import FeishuApi
from ..feishu.parser import IncomingMessage
from ..state import ActiveSession, CardRef, StateStore
from .connection import SessionConnection
from .render import TurnRenderer, clip_tail, to_feishu_markdown, total_tokens

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

        # 会话必须仍然存在。绝不自动新建。
        # 连接还活着就说明会话是活的，没必要再查文件（也避免在持有活连接时误判）。
        if not warm:
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

        card_id = await self._api.reply_card(
            msg.message_id,
            cards.running_card(active.repo_name, "正在处理…" if warm else "正在连接 Claude…"),
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
        prompt = build_prompt(msg.text, outcome)
        if not outcome.empty:
            renderer.add_note(summarize_outcome(outcome))

        try:
            client, created = await self._connection.acquire(active.session_id, options)
            if created:
                renderer.replace_status("正在处理…")
            handle.client = client
            handle.last_event_at = time.monotonic()
            monitor = asyncio.create_task(self._monitor(handle), name="turn-monitor")

            await client.query(prompt)
            # 中断后不要 break：必须把流读完才能拿到 ResultMessage。
            async for message in client.receive_response():
                handle.last_event_at = time.monotonic()
                renderer.feed(message)
                if isinstance(message, ResultMessage):
                    result = message
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

        # inbox 不在仓库里，必须显式授权 Claude 读取
        add_dirs: list[str] = []
        if self._cfg.docs.enabled:
            add_dirs.append(str(self._cfg.docs.inbox_dir))

        return ClaudeAgentOptions(
            cwd=active.workdir,
            add_dirs=add_dirs,
            resume=active.session_id,
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

        # session_id 核对（实测正常 resume 时一致）
        if result.session_id and result.session_id != active.session_id:
            log.warning(
                "session_id 变化：%s → %s，已更新状态", active.session_id, result.session_id
            )
            await self._state.update_session_id(result.session_id)

        await self._state.touch_active()

        body = renderer.body
        final = (renderer.final_result or "").strip()
        # final 是模型原文，也要过一遍飞书 markdown 转换（body 已经过了）
        inline = (
            to_feishu_markdown(clip_tail(final, self._cfg.render.max_card_chars))
            if final and len(final) <= RESULT_INLINE_MAX
            else body
        )

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
