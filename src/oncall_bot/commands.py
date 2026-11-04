"""斜杠命令。A 模式：全局一条活跃会话，用 /sessions + /use 切换。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import SDKSessionInfo, get_session_info, list_sessions

from .config import Config
from .feishu.api import FeishuApi
from .feishu.parser import IncomingMessage
from .runner.dispatcher import Dispatcher
from .state import ActiveSession, StateStore, now_iso

log = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# 会话 ID 前缀：至少 4 位十六进制（/sessions 显示的是前 8 位）
_HEX_PREFIX_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{3,34}$")

HELP_TEXT = """可用命令：
/sessions — 列出本机最近的 Claude Code 会话
/use <序号|会话ID> — 切换到某条会话。序号来自最近一次 /sessions；
                     会话 ID 可以是列表里显示的前 8 位，只要能唯一匹配
/status — 当前会话、运行状态、队列长度
/stop — 中断当前任务并清空队列
/help — 这段说明

直接发文字（群里需要 @ 我）就是把需求交给当前会话。
同一时刻只驱动一条会话；切换前请先 /stop。
注意：桥在使用某条会话期间，不要在终端里同时打开同一条会话。"""


class CommandHandler:
    def __init__(
        self, cfg: Config, api: FeishuApi, state: StateStore, dispatcher: Dispatcher
    ) -> None:
        self._cfg = cfg
        self._api = api
        self._state = state
        self._dispatcher = dispatcher
        self._listing: list[SDKSessionInfo] = []  # 最近一次 /sessions 的结果，序号即下标+1

    def is_command(self, text: str) -> bool:
        return text.startswith("/")

    async def handle(self, msg: IncomingMessage) -> None:
        parts = msg.text.split()
        name = parts[0].lower()
        args = parts[1:]

        if name == "/help":
            await self._reply(msg, HELP_TEXT)
        elif name == "/sessions":
            await self._cmd_sessions(msg)
        elif name == "/use":
            await self._cmd_use(msg, args)
        elif name == "/status":
            await self._cmd_status(msg)
        elif name == "/stop":
            await self._reply(msg, await self._dispatcher.stop_current())
        else:
            await self._reply(msg, f"未知命令 `{name}`。发 /help 看可用命令。")

    # ---------- /sessions ----------

    async def _cmd_sessions(self, msg: IncomingMessage) -> None:
        limit = self._cfg.render.session_list_limit
        try:
            sessions: list[SDKSessionInfo] = await asyncio.to_thread(list_sessions, None, limit)
        except Exception:
            log.exception("list_sessions 失败")
            await self._reply(msg, "读取本机会话列表失败，详情见日志。")
            return

        if not sessions:
            await self._reply(
                msg, "本机没有找到任何 Claude Code 会话。请先在终端里跑一次 claude 开一条会话。"
            )
            return

        self._listing = sessions
        active_id = self._state.active.session_id if self._state.active else None

        lines = [f"最近 {len(sessions)} 条会话（/use <序号> 切换）："]
        for idx, s in enumerate(sessions, start=1):
            mark = "  ← 当前" if s.session_id == active_id else ""
            lines.append(f"{idx}. {s.session_id[:8]} · {_title_of(s)}{mark}")
            lines.append(f"    {s.cwd or '(无目录)'} · {s.git_branch or '-'} · {_fmt_ms(s.last_modified)}")
        await self._reply(msg, "\n".join(lines))

    # ---------- /use ----------

    async def _cmd_use(self, msg: IncomingMessage, args: list[str]) -> None:
        if not args:
            await self._reply(msg, "用法：/use <序号> 或 /use <完整会话ID>。先发 /sessions 看列表。")
            return

        if self._dispatcher.is_running():
            await self._reply(msg, "当前有任务在跑，切换会话前请先 /stop。")
            return

        token = args[0].strip()
        session_id: str | None = None

        if _UUID_RE.match(token):
            session_id = token
        elif token.isdigit() and len(token) <= 2:
            # 序号最多两位：列表上限是 50 条
            if not self._listing:
                await self._reply(msg, "还没有列过会话。先发 /sessions，再用序号。")
                return
            index = int(token)
            if not 1 <= index <= len(self._listing):
                await self._reply(
                    msg, f"序号超出范围（当前列表有 {len(self._listing)} 条）。重新发 /sessions 看看。"
                )
                return
            session_id = self._listing[index - 1].session_id
        else:
            # 既可能是写错的序号，也可能是会话 ID 前缀；两种都说清楚
            prefix_error = ""
            if _HEX_PREFIX_RE.match(token):
                session_id, prefix_error = await self._resolve_prefix(token)
            if session_id is None:
                parts: list[str] = []
                if token.isdigit():
                    parts.append(
                        f"`{token}` 不是有效序号（当前列表有 {len(self._listing)} 条）。"
                    )
                parts.append(prefix_error or "这也不像会话 ID 前缀（至少 4 位十六进制）。")
                parts.append(
                    "可用写法：/use 3（序号）、/use 82e9d976（ID 前 8 位）、/use <完整 36 位 ID>。"
                )
                await self._reply(msg, "\n".join(parts))
                return

        try:
            info = await asyncio.to_thread(get_session_info, session_id, None)
        except Exception:
            log.exception("get_session_info 失败：%s", session_id)
            await self._reply(msg, "校验会话时出错，详情见日志。")
            return

        if info is None:
            await self._reply(msg, f"找不到会话 `{session_id[:8]}`。发 /sessions 重新看列表。")
            return
        if not info.cwd:
            await self._reply(msg, f"会话 `{session_id[:8]}` 没有记录工作目录，无法使用。")
            return
        workdir = Path(info.cwd)
        if not workdir.is_dir():
            await self._reply(
                msg, f"会话 `{session_id[:8]}` 的工作目录已不存在：{info.cwd}"
            )
            return

        session = ActiveSession(
            session_id=info.session_id,
            workdir=str(workdir),
            title=_title_of(info),
            switched_by=msg.sender_open_id,
            switched_at=now_iso(),
        )
        # 立刻释放旧连接：桥占着会话时不该让它一直占着，用户可能要去终端里打开它
        await self._dispatcher.connection.close("活跃会话已切换")
        await self._state.set_active(session)
        log.info("活跃会话切换为 %s（%s）", session.short_id, session.workdir)

        await self._reply(
            msg,
            "\n".join(
                [
                    "已切换活跃会话：",
                    f"  会话：{session.short_id} · {session.title}",
                    f"  仓库：{session.workdir}",
                    f"  分支：{info.git_branch or '-'}",
                    f"  最后活动：{_fmt_ms(info.last_modified)}",
                    "",
                    "提醒：桥使用这条会话期间，不要在终端里同时打开它（两边同写会弄乱历史）。",
                ]
            ),
        )

    async def _resolve_prefix(self, token: str) -> tuple[str | None, str]:
        """把 ID 前缀解析成完整 session_id。只有唯一匹配才接受。

        优先在最近一次 /sessions 的结果里找；没列过（比如刚重启）就现查一次，
        这样重启后直接 /use <前8位> 也能用。
        """
        candidates = self._listing
        if not candidates:
            try:
                candidates = await asyncio.to_thread(list_sessions, None, 30)
            except Exception:
                log.exception("按 ID 前缀解析会话时 list_sessions 失败")
                return None, "读取本机会话列表失败，详情见日志。"

        low = token.lower()
        matches = [s for s in candidates if s.session_id.lower().startswith(low)]

        if len(matches) == 1:
            return matches[0].session_id, ""
        if not matches:
            return None, f"没有会话的 ID 以 `{token}` 开头。发 /sessions 看看现有的会话。"

        lines = [f"`{token}` 匹配到 {len(matches)} 条会话，请改用序号或更长的 ID："]
        for s in matches:
            lines.append(f"- {s.session_id[:12]} · {_title_of(s)}")
        return None, "\n".join(lines)

    # ---------- /status ----------

    async def _cmd_status(self, msg: IncomingMessage) -> None:
        active = self._state.active
        lines: list[str] = []
        if active is None:
            lines.append("当前没有活跃会话。发 /sessions 看列表，再 /use <序号>。")
        else:
            lines.extend(
                [
                    f"活跃会话：{active.short_id} · {active.title}",
                    f"仓库：{active.workdir}",
                    f"切换于：{active.switched_at}",
                    f"最后活动：{active.last_active_at or '（本桥尚未驱动过）'}",
                ]
            )
        lines.append(f"运行中：{'是' if self._dispatcher.is_running() else '否'}")
        lines.append(f"排队中：{self._dispatcher.queue_size()} 条")
        lines.append(f"Claude 连接：{self._dispatcher.connection.state_text}")
        lines.append(f"权限模式：{self._cfg.runner.permission_mode}")
        await self._reply(msg, "\n".join(lines))

    # ---------- 工具 ----------

    async def _reply(self, msg: IncomingMessage, text: str) -> None:
        await self._api.reply_text(msg.message_id, text, in_thread=msg.in_thread)


def _title_of(info: SDKSessionInfo) -> str:
    title = (info.custom_title or info.summary or "").strip()
    if title:
        return title
    first = (info.first_prompt or "").strip().replace("\n", " ")
    return (first[:40] + "…") if len(first) > 40 else (first or "(无标题)")


def _fmt_ms(epoch_ms: int | None) -> str:
    if not epoch_ms:
        return "-"
    try:
        return datetime.fromtimestamp(epoch_ms / 1000).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"
