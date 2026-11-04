"""SDK 消息 → 卡片正文，带节流更新。

节流用「脏标记 + 定时刷新」实现，不是每条消息都调一次 OpenAPI。
最后一次更新由 finalize() 保证发出。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import unicodedata
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from ..config import RenderConfig
from ..feishu import cards
from ..feishu.api import FeishuApi

log = logging.getLogger(__name__)

TOOL_ARG_MAX = 80
TRUNCATION_NOTE = "…（前文已截断）\n\n"

# 工具 → 最能说明意图的那个参数
_TOOL_KEY_ARG: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "BashOutput": ("bash_id",),
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("pattern",),
    "Glob": ("pattern",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "Task": ("description", "subagent_type"),
    "Skill": ("command",),
}


def summarize_tool_use(tool_name: str, tool_input: dict[str, Any]) -> str:
    keys = _TOOL_KEY_ARG.get(tool_name)
    detail = ""
    if keys:
        picked = [str(tool_input.get(k)) for k in keys if tool_input.get(k) is not None]
        detail = " ".join(picked)
    if not detail:
        if tool_name == "TodoWrite":
            todos = tool_input.get("todos")
            detail = f"{len(todos)} 项" if isinstance(todos, list) else ""
        else:
            try:
                detail = json.dumps(tool_input, ensure_ascii=False)
            except (TypeError, ValueError):
                detail = str(tool_input)
    detail = " ".join(detail.split())
    if len(detail) > TOOL_ARG_MAX:
        detail = detail[: TOOL_ARG_MAX - 1] + "…"
    # 用加粗而不是反引号：飞书卡片不渲染行内代码，反引号会裸露出来
    return f"**[工具] {tool_name}**{': ' + detail if detail else ''}"


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")

# 行内代码：单对反引号，不跨行。落单的反引号不动。
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# 实测飞书 markdown 组件支持的行内着色写法（grey 不在调色板里，red 读起来像报错，
# 加粗会和模型自己的加粗混在一起）
_INLINE_CODE_SUB = r"<font color='blue'>\1</font>"


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


_SEPARATOR_CELL = re.compile(r"^:?-{1,}:?$")


def _display_width(text: str) -> int:
    """中日韩字符在等宽字体里占两格，按这个算宽度才能对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def format_table(rows: list[str]) -> list[str]:
    """markdown 表格 → 对齐好的等宽文本块。

    飞书卡片不渲染表格，但渲染代码围栏。把表格列对齐后放进围栏，
    比原样丢一堆竖线进去可读得多，而且 `|---|---|` 这种分隔行要丢掉。
    """
    grid = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows]
    grid = [cells for cells in grid if not all(_SEPARATOR_CELL.match(c or "-") for c in cells)]
    if not grid:
        return []

    columns = max(len(cells) for cells in grid)
    grid = [cells + [""] * (columns - len(cells)) for cells in grid]
    widths = [max(_display_width(cells[i]) for cells in grid) for i in range(columns)]

    lines: list[str] = []
    for index, cells in enumerate(grid):
        lines.append("  ".join(_pad(cells[i], widths[i]) for i in range(columns)).rstrip())
        if index == 0 and len(grid) > 1:
            lines.append("  ".join("-" * widths[i] for i in range(columns)))
    return ["```text", *lines, "```"]


def to_feishu_markdown(text: str) -> str:
    """把标准 markdown 调整成飞书 markdown 组件能渲染的形式。

    飞书支持：加粗 / 斜体 / 删除线 / 链接 / 代码围栏（带高亮）/ 有序无序列表 / `---`
    飞书不支持，这里做替换：
      - 标题 `## x`     → `**x**`
      - 表格            → 解析成对齐的等宽文本，放进代码围栏
      - 行内代码 `x`    → `<font color='blue'>x</font>`
    这些替换都是按实测结果定的，见 scripts/check_card_markdown.py。
    """
    out: list[str] = []
    table: list[str] = []
    in_fence = False

    def flush_table() -> None:
        if not table:
            return
        out.extend(format_table(table))
        table.clear()

    for line in (text or "").split("\n"):
        if line.strip().startswith("```"):
            flush_table()
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue

        if _is_table_row(line):
            table.append(line.strip())
            continue
        flush_table()

        heading = _HEADING_RE.match(line)
        if heading:
            title = heading.group(2).strip()
            out.append(f"**{_inline_code(title)}**" if title else "")
            continue

        out.append(_inline_code(line))

    flush_table()
    if in_fence:
        # 正文被截断在代码块中间时补上收尾，否则后面所有内容都会被当成代码
        out.append("```")
    return "\n".join(out)


def _inline_code(text: str) -> str:
    return _INLINE_CODE_RE.sub(_INLINE_CODE_SUB, text)


def clip_tail(text: str, max_chars: int) -> str:
    """超长时保留尾部，开头标注已截断。"""
    if len(text) <= max_chars:
        return text
    keep = max_chars - len(TRUNCATION_NOTE)
    return TRUNCATION_NOTE + text[-keep:] if keep > 0 else text[-max_chars:]


def total_tokens(usage: dict[str, Any] | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    total = 0
    found = False
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
            found = True
    return total if found else None


class TurnRenderer:
    """一轮对应一个实例，持有一张卡片。"""

    def __init__(
        self,
        api: FeishuApi,
        card_message_id: str,
        repo_name: str,
        short_session_id: str,
        cfg: RenderConfig,
    ) -> None:
        self._api = api
        self._card_id = card_message_id
        self._repo = repo_name
        self._short_sid = short_session_id
        self._cfg = cfg
        self._lines: list[str] = []
        self._status = "正在处理…"
        self._dirty = False
        self._started = time.monotonic()
        self._flusher: asyncio.Task[None] | None = None
        self._closed = False
        self._final_result: str = ""

    # ---------- 输入 ----------

    def replace_status(self, text: str) -> None:
        """模型还没产出任何内容时卡片上显示的占位文字。"""
        self._status = text
        if not self._lines:
            self._dirty = True

    def add_note(self, text: str) -> None:
        """插入一条非模型产生的说明（如云文档抓取结果）。"""
        if text.strip():
            # 用 *...*：飞书渲染星号斜体，不渲染下划线斜体
            self._append(f"*{text.strip()}*")

    def feed(self, message: Any) -> None:
        """同步累积，不发请求。"""
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text = (block.text or "").strip()
                    if text:
                        self._append(text)
                elif isinstance(block, ToolUseBlock):
                    self._append(summarize_tool_use(block.name, block.input or {}))
        elif isinstance(message, SystemMessage):
            log.debug("SystemMessage subtype=%s", getattr(message, "subtype", None))
        elif isinstance(message, ResultMessage) and message.result:
            self._final_result = message.result

    def _append(self, line: str) -> None:
        self._lines.append(line)
        self._dirty = True

    # ---------- 输出 ----------

    @property
    def body(self) -> str:
        if not self._lines:
            return self._status
        # 先截断再转换：转换里有「补上未闭合的代码围栏」这一步，必须在截断之后做
        return to_feishu_markdown(clip_tail("\n\n".join(self._lines), self._cfg.max_card_chars))

    @property
    def final_result(self) -> str:
        return self._final_result

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    async def start(self) -> None:
        self._flusher = asyncio.create_task(self._flush_loop(), name="card-flusher")

    async def _flush_loop(self) -> None:
        interval = self._cfg.update_interval_ms / 1000
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                if self._dirty:
                    self._dirty = False
                    await self._api.patch_card(
                        self._card_id,
                        cards.running_card(self._repo, self.body, elapsed_seconds=self.elapsed),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("卡片刷新循环异常，停止刷新（不影响 Claude 执行）")

    async def finalize(self, card: dict) -> None:
        """停止节流循环并把最终卡片发出（必发）。"""
        self._closed = True
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._flusher
            self._flusher = None
        await self._api.patch_card(self._card_id, card)
