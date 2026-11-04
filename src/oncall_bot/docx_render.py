"""飞书新版文档 block 树 → markdown。

为什么不用 `raw_content`：它会丢图片位置，而且跳过某些容器块里的内容。
实测那篇 234 block 的文档里有 24 个 image block，但 raw_content 只给出 23 个
`image.png` 占位符（第 24 张被包在 view 容器里），按序号替换占位符必然错位。

block_type 的含义以实测为准（对照真实文档确认）：
  1 page        2 text        3..11 heading1..9   12 bullet    13 ordered
  14 code       15 quote      17 todo             19 callout   22 divider
  23 file       27 image      31 table            32 table_cell  33 view
未知类型不丢弃：能提出文字就按普通段落输出。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import unquote

log = logging.getLogger(__name__)

BLOCK_PAGE = 1
BLOCK_TEXT = 2
BLOCK_HEADING_MIN = 3
BLOCK_HEADING_MAX = 11
BLOCK_BULLET = 12
BLOCK_ORDERED = 13
BLOCK_CODE = 14
BLOCK_QUOTE = 15
BLOCK_TODO = 17
BLOCK_CALLOUT = 19
BLOCK_DIVIDER = 22
BLOCK_FILE = 23
BLOCK_IMAGE = 27
BLOCK_TABLE = 31
BLOCK_TABLE_CELL = 32
BLOCK_VIEW = 33

# block_type → 该块正文所在的字段名
_PAYLOAD_KEY = {
    BLOCK_PAGE: "page",
    BLOCK_TEXT: "text",
    BLOCK_BULLET: "bullet",
    BLOCK_ORDERED: "ordered",
    BLOCK_CODE: "code",
    BLOCK_QUOTE: "quote",
    BLOCK_TODO: "todo",
}
for _t in range(BLOCK_HEADING_MIN, BLOCK_HEADING_MAX + 1):
    _PAYLOAD_KEY[_t] = f"heading{_t - 2}"

# 纯容器：自身没有正文，只负责包住 children
_CONTAINER_TYPES = {BLOCK_CALLOUT, BLOCK_VIEW, BLOCK_TABLE, BLOCK_TABLE_CELL}


def document_title(blocks: list[dict]) -> str | None:
    for block in blocks:
        if block.get("block_type") == BLOCK_PAGE:
            return render_elements((block.get("page") or {}).get("elements")) or None
    return None


def count_images(blocks: list[dict]) -> int:
    return sum(1 for b in blocks if b.get("block_type") == BLOCK_IMAGE)


def image_tokens(blocks: list[dict]) -> list[str]:
    """按文档顺序返回图片 token。"""
    tokens: list[str] = []
    for block in _walk(blocks):
        if block.get("block_type") == BLOCK_IMAGE:
            token = (block.get("image") or {}).get("token")
            if token:
                tokens.append(token)
    return tokens


def render_elements(elements: list[dict] | None) -> str:
    """text.elements → 行内文本。"""
    out: list[str] = []
    for el in elements or []:
        if "text_run" in el:
            out.append(_render_text_run(el["text_run"]))
        elif "mention_doc" in el:
            doc = el["mention_doc"]
            title = doc.get("title") or "文档"
            url = unquote(doc.get("url") or "")
            out.append(f"[{title}]({url})" if url else f"[{title}]")
        elif "mention_user" in el:
            user = el["mention_user"]
            out.append(f"@{user.get('user_id') or '用户'}")
        elif "equation" in el:
            content = (el["equation"].get("content") or "").strip()
            out.append(f"${content}$" if content else "")
        elif "inline_file" in el:
            out.append(f"[附件：{el['inline_file'].get('file_token', '')}]")
        elif "reminder" in el:
            out.append("[提醒]")
        else:
            # 未知行内元素：不静默吞掉，留个可见痕迹方便排查
            out.append(f"[{'/'.join(el.keys())}]")
    return "".join(out)


def _render_text_run(text_run: dict) -> str:
    content = text_run.get("content") or ""
    style = text_run.get("text_element_style") or {}
    if style.get("inline_code"):
        content = f"`{content}`"
    else:
        if style.get("bold"):
            content = f"**{content}**"
        if style.get("italic"):
            content = f"*{content}*"
        if style.get("strikethrough"):
            content = f"~~{content}~~"
    link = (style.get("link") or {}).get("url")
    if link:
        content = f"[{content}]({unquote(link)})"
    return content


def _block_text(block: dict) -> str:
    key = _PAYLOAD_KEY.get(block.get("block_type"))
    if key:
        return render_elements((block.get(key) or {}).get("elements"))
    # 未知类型：任何带 elements 的字段都试一下
    for value in block.values():
        if isinstance(value, dict) and "elements" in value:
            return render_elements(value["elements"])
    return ""


def _walk(blocks: list[dict]):
    """按文档树顺序遍历。page.children 给出真正的顺序，容器块递归展开。"""
    by_id = {b.get("block_id"): b for b in blocks if b.get("block_id")}
    root = next((b for b in blocks if b.get("block_type") == BLOCK_PAGE), None)

    if root is None:
        # 没有 page 块时退回扁平顺序（接口本身也是按文档顺序返回的）
        yield from blocks
        return

    visited: set[str] = set()

    def visit(block_id: str, depth: int):
        if block_id in visited:
            return
        visited.add(block_id)
        block = by_id.get(block_id)
        if block is None:
            return
        block = dict(block)
        block["_depth"] = depth
        yield block
        for child_id in block.get("children") or []:
            yield from visit(child_id, depth + 1)

    for child_id in root.get("children") or []:
        yield from visit(child_id, 0)


def render(
    blocks: list[dict],
    image_ref: Callable[[int, str], str] | None = None,
) -> str:
    """把 block 树渲染成 markdown。

    image_ref(序号从 1 开始, token) 返回这张图在 markdown 里的表示；
    为 None 时图片渲染成占位说明。
    """
    lines: list[str] = []
    ordered_counters: dict[int, int] = {}
    image_index = 0

    for block in _walk(blocks):
        block_type = block.get("block_type")
        depth = int(block.get("_depth") or 0)
        indent = "  " * depth

        if block_type == BLOCK_ORDERED:
            # 飞书自己给了序号信息：sequence 为数字表示新列表起点，"auto" 表示延续。
            # 不能按「遇到非 ordered 块就重置」来算 —— 实测列表项之间会夹着文本块和图片块。
            sequence = ((block.get("ordered") or {}).get("style") or {}).get("sequence")
            if isinstance(sequence, str) and sequence.isdigit():
                ordered_counters[depth] = int(sequence)
                for deeper in [d for d in ordered_counters if d > depth]:
                    del ordered_counters[deeper]
            else:
                ordered_counters[depth] = ordered_counters.get(depth, 0) + 1

        if block_type == BLOCK_IMAGE:
            image_index += 1
            token = (block.get("image") or {}).get("token") or ""
            if image_ref is not None:
                lines.extend(["", image_ref(image_index, token), ""])
            else:
                lines.extend(["", f"（图 {image_index}：未下载）", ""])
            continue

        if block_type == BLOCK_DIVIDER:
            lines.extend(["", "---", ""])
            continue

        if block_type == BLOCK_FILE:
            info = block.get("file") or {}
            lines.append(f"{indent}[文档内附件：{info.get('name') or info.get('token') or '未命名'}]")
            continue

        if block_type == BLOCK_CODE:
            code = _block_text(block)
            lines.extend(["", "```", code, "```", ""])
            continue

        text = _block_text(block)
        if not text.strip():
            if block_type in _CONTAINER_TYPES:
                continue
            continue

        if BLOCK_HEADING_MIN <= (block_type or 0) <= BLOCK_HEADING_MAX:
            level = min(6, block_type - 2)
            lines.extend(["", f"{'#' * level} {text}", ""])
        elif block_type == BLOCK_BULLET:
            lines.append(f"{indent}- {text}")
        elif block_type == BLOCK_ORDERED:
            lines.append(f"{indent}{ordered_counters[depth]}. {text}")
        elif block_type == BLOCK_TODO:
            done = ((block.get("todo") or {}).get("style") or {}).get("done")
            lines.append(f"{indent}- [{'x' if done else ' '}] {text}")
        elif block_type == BLOCK_QUOTE:
            lines.append(f"{indent}> {text}")
        else:
            lines.append(f"{indent}{text}")

    return _squeeze_blank_lines(lines)


def _squeeze_blank_lines(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        if not line.strip():
            if out and not out[-1].strip():
                continue
            out.append("")
        else:
            out.append(line.rstrip())
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)
