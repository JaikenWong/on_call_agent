"""飞书交互卡片 JSON 构造。全部是纯函数，便于单测。

config.update_multi 必须为 true，否则群内卡片发出后无法再被更新。
"""

from __future__ import annotations

TEMPLATE_RUNNING = "blue"
TEMPLATE_DONE = "green"
TEMPLATE_FAILED = "red"
TEMPLATE_INFO = "grey"

_PLACEHOLDER = "（暂无输出）"


def _shell(title: str, template: str, elements: list[dict]) -> dict:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def _markdown(text: str) -> dict:
    """正文用 markdown 组件，不用 div + lark_md。

    实测对比（见 scripts/check_card_markdown.py）：lark_md 不渲染代码围栏和列表，
    markdown 组件两者都支持，代码块还带行号和语法高亮。两者都不支持标题 / 行内代码 / 表格，
    这三样由 render.to_feishu_markdown() 做等价替换。
    """
    return {"tag": "markdown", "content": text or _PLACEHOLDER}


def _note(text: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _divider() -> dict:
    return {"tag": "hr"}


def running_card(repo_name: str, body: str, *, elapsed_seconds: float | None = None) -> dict:
    footer = "发送 /stop 可中断"
    if elapsed_seconds is not None:
        footer = f"已运行 {_fmt_duration(elapsed_seconds)} · {footer}"
    return _shell(
        f"处理中 · {repo_name}",
        TEMPLATE_RUNNING,
        [_markdown(body), _divider(), _note(footer)],
    )


def done_card(
    repo_name: str,
    body: str,
    *,
    duration_ms: int | None,
    num_turns: int | None,
    tokens: int | None,
    short_session_id: str,
    part: tuple[int, int] | None = None,
) -> dict:
    """完成卡片。回答被拆成多张时，part=(第几张, 共几张)，统计信息只放在最后一张上。"""
    bits: list[str] = []
    if duration_ms is not None:
        bits.append(f"耗时 {_fmt_duration(duration_ms / 1000)}")
    if num_turns is not None:
        bits.append(f"{num_turns} 轮")
    if tokens is not None:
        bits.append(f"{tokens} tokens")
    bits.append(f"会话 {short_session_id}")
    if part is not None:
        bits.append(f"共 {part[1]} 张")
    return _shell(
        f"完成 · {repo_name}{_part_suffix(part)}",
        TEMPLATE_DONE,
        [_markdown(body), _divider(), _note(" · ".join(bits))],
    )


def part_card(repo_name: str, body: str, *, index: int, total: int) -> dict:
    """回答被拆成多张时，除最后一张以外的那些。"""
    return _shell(
        f"完成 · {repo_name}{_part_suffix((index, total))}",
        TEMPLATE_DONE,
        [
            _markdown(body),
            _divider(),
            _note(f"回答较长，分 {total} 张卡片发出 · 接着看第 {index + 1} 张"),
        ],
    )


def _part_suffix(part: tuple[int, int] | None) -> str:
    return f"（{part[0]}/{part[1]}）" if part is not None else ""


def aborted_card(repo_name: str, body: str, reason: str, *, short_session_id: str) -> dict:
    return _shell(
        f"已中断 · {repo_name}",
        TEMPLATE_INFO,
        [_markdown(body), _divider(), _note(f"{reason} · 会话 {short_session_id} · 下一条消息会续上")],
    )


def failed_card(repo_name: str, reason: str, next_step: str = "") -> dict:
    elements: list[dict] = [_markdown(reason)]
    if next_step:
        elements.extend([_divider(), _note(next_step)])
    return _shell(f"失败 · {repo_name}", TEMPLATE_FAILED, elements)


def stale_card(note: str = "桥已重启，这一轮的最终状态未知。会话历史没有丢，可以直接发下一条消息。") -> dict:
    return _shell("状态未知", TEMPLATE_INFO, [_markdown(note)])


def _fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
