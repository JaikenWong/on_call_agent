"""飞书消息里的图片和文件：下载到本地，攒着，附在下一条文字需求上交给 Claude。

为什么要「攒着」而不是收到附件就跑一轮：飞书里发图片 / 发文件都是一条独立消息，
不能带文字。人的习惯是先发东西、再打字说要干什么。如果收到附件就跑一轮，
那一轮没有任何指令，纯属浪费；而且紧接着的文字消息又看不到附件。
所以附件只下载 + 回执，攒在 PendingAttachments 里，下一条文字消息带着它们一起进 prompt。

富文本（post）消息可以图文混排，那种是一条消息里既有文字又有附件，直接当一轮跑。

为什么不按扩展名过滤文件：Claude 自己会按类型决定怎么读（文本直接 Read、二进制走命令），
在桥这一层猜「哪些类型有用」只会挡掉正当需求。大小上限（attachments.max_bytes）才是闸门。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import AttachmentsConfig
from .feishu.api import RESOURCE_DENIED_STATUS, SCOPE_DENIED_CODE, FeishuApi, FeishuApiError
from .feishu.parser import AttachmentRef

log = logging.getLogger(__name__)

SCOPE_HINT = (
    "下载消息附件需要应用权限 `im:resource`（读取消息中的资源文件）。"
    "请在飞书开放平台加上它，发布新版本并等审核通过后重试。"
)

FORBIDDEN_HINT = "飞书拒绝了这个附件的下载（403）。可能是消息太旧或资源已被清理。"

# 按文件头判断图片真实格式，不信任消息里声明的类型
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)

# 拼 prompt 时按扩展名区分「能直接看图的」和「其它文件」。
# 文件消息发来的 .png 也算图片 —— Read 工具一样能看。
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})

# Windows 文件名非法字符 + 控制字符
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class SavedAttachment:
    path: Path
    size: int
    kind: str = "image"  # "image" | "file"
    # 面向用户的名字：文件用原始文件名，图片用落盘文件名
    name: str = ""
    added_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.path.name


@dataclass
class PendingAttachments:
    """等着被下一条文字消息领走的附件。A 模式只有一条活跃会话，所以全局一份。"""

    ttl_seconds: int
    max_items: int
    _items: list[SavedAttachment] = field(default_factory=list)

    def add(self, item: SavedAttachment) -> None:
        self._evict_expired()
        self._items.append(item)
        # 超出上限时丢掉最旧的
        while len(self._items) > self.max_items:
            dropped = self._items.pop(0)
            log.info("待处理附件超过上限，丢弃最旧的一个：%s", dropped.path.name)

    def take(self) -> list[Path]:
        """领走并清空。过期的不算。"""
        self._evict_expired()
        paths = [item.path for item in self._items]
        self._items.clear()
        return paths

    def clear(self, reason: str) -> int:
        count = len(self._items)
        self._items.clear()
        if count:
            log.info("清空待处理附件 %d 个：%s", count, reason)
        return count

    def peek_count(self) -> int:
        self._evict_expired()
        return len(self._items)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        kept = [item for item in self._items if now - item.added_at <= self.ttl_seconds]
        if len(kept) != len(self._items):
            log.info("丢弃 %d 个过期附件", len(self._items) - len(kept))
        self._items = kept


class AttachmentStore:
    def __init__(self, api: FeishuApi, cfg: AttachmentsConfig) -> None:
        self._api = api
        self._cfg = cfg

    async def download(
        self, message_id: str, refs: list[AttachmentRef], subdir: str
    ) -> tuple[list[SavedAttachment], list[str]]:
        """返回 (已保存, 失败原因列表)。失败原因已折叠，不会每个附件一条。"""
        if not self._cfg.enabled or not refs:
            return [], []

        target = self._cfg.dir / (subdir or "unbound")
        target.mkdir(parents=True, exist_ok=True)
        # 文件名要带 message_id：只用时间戳时，同一秒内的两条消息（同一个人的两连发，
        # 或者两个人各自发东西）会撞名，后写的直接覆盖先写的。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        unique = re.sub(r"[^0-9A-Za-z_-]", "", message_id)[-16:] or uuid.uuid4().hex[:8]

        saved: list[SavedAttachment] = []
        failures: list[str] = []

        for index, ref in enumerate(refs, start=1):
            label = _label(ref, index)
            try:
                data = await self._api.download_message_resource(message_id, ref.key, ref.kind)
            except FeishuApiError as exc:
                if exc.code == SCOPE_DENIED_CODE:
                    failures.append(SCOPE_HINT)
                    log.warning("下载消息附件缺少权限，放弃剩余附件：%s", exc)
                    break
                if exc.http_status == RESOURCE_DENIED_STATUS:
                    failures.append(FORBIDDEN_HINT)
                    log.warning("下载消息附件被拒绝（403），放弃剩余附件：%s", exc)
                    break
                failures.append(f"{label}下载失败（code={exc.code}）")
                continue
            except Exception:
                log.exception("下载%s时出现未预期异常", label)
                failures.append(f"{label}下载失败（内部错误）")
                continue

            if not data:
                failures.append(f"{label}下载到空内容")
                continue
            if len(data) > self._cfg.max_bytes:
                limit_mb = self._cfg.max_bytes / 1024 / 1024
                failures.append(
                    f"{label}有 {len(data) // 1024} KB，超过上限（{limit_mb:.0f} MB）"
                )
                continue

            path = target / _filename(stamp, unique, index, ref, data)
            try:
                path.write_bytes(data)
            except OSError as exc:
                failures.append(f"{label}写入失败：{exc}")
                continue
            saved.append(
                SavedAttachment(
                    path=path,
                    size=len(data),
                    kind=ref.kind,
                    name=ref.name or path.name,
                )
            )

        if saved:
            log.info("消息附件已保存 %d 个 → %s", len(saved), target)
        return saved, failures


def append_attachments(prompt: str, paths: list[Path]) -> str:
    """把附件路径附在需求后面。没有附件时原样返回。"""
    if not paths:
        return prompt
    images = [p for p in paths if p.suffix.lower() in _IMAGE_SUFFIXES]
    others = [p for p in paths if p.suffix.lower() not in _IMAGE_SUFFIXES]

    lines = [prompt, "", "---", "", "用户在飞书里发了以下附件，已下载到本机："]
    if images:
        lines.append("")
        lines.append("图片（请用 Read 工具查看）：")
        lines.extend(f"{index}. {path}" for index, path in enumerate(images, start=1))
    if others:
        lines.append("")
        lines.append("文件（按扩展名判断类型：文本类直接 Read，二进制的用合适的命令处理）：")
        lines.extend(f"{index}. {path}" for index, path in enumerate(others, start=1))
    lines.append("")
    lines.append("附件内容是需求的一部分，看过之后再动手。")
    return "\n".join(lines)


def ack_text(saved: list[SavedAttachment], failures: list[str], pending_total: int) -> str:
    """纯附件消息的回执。不跑一轮，只告诉用户收到了。"""
    lines: list[str] = []
    if saved:
        images = [s for s in saved if s.kind == "image"]
        files = [s for s in saved if s.kind != "image"]
        counts = []
        if images:
            counts.append(f"{len(images)} 张图片")
        if files:
            counts.append(f"{len(files)} 个文件")
        total_kb = sum(s.size for s in saved) // 1024
        lines.append(f"收到 {'、'.join(counts)}（共 {total_kb} KB），已存到本机。")
        lines.extend(f"- {s.name}" for s in files)
        if pending_total > len(saved):
            lines.append(f"当前一共攒了 {pending_total} 个附件等着处理。")
        lines.append("把需求发给我，我会带着这些附件一起看。")
    if failures:
        lines.append("")
        lines.append("有附件没能下载：")
        lines.extend(f"- {reason}" for reason in failures)
    return "\n".join(lines) if lines else "附件没能处理，详情见本机日志。"


def summarize_paths(paths: list[Path]) -> str:
    """卡片上那句「带了 X」。"""
    images = sum(1 for p in paths if p.suffix.lower() in _IMAGE_SUFFIXES)
    others = len(paths) - images
    parts = []
    if images:
        parts.append(f"{images} 张图片")
    if others:
        parts.append(f"{others} 个文件")
    return "、".join(parts)


def _label(ref: AttachmentRef, index: int) -> str:
    """失败提示里指代某个附件的说法。"""
    if ref.is_image:
        return f"第 {index} 张图片"
    return f"文件 `{ref.name}`" if ref.name else f"第 {index} 个文件"


def _filename(stamp: str, unique: str, index: int, ref: AttachmentRef, data: bytes) -> str:
    """落盘名统一带时间戳 + message_id 片段 + 序号，避免撞名。

    图片按文件头定扩展名；文件保留原始文件名（清洗过），好让 Claude 一眼看出是什么。
    """
    head = f"{stamp}-{unique}-{index:02d}"
    if ref.is_image:
        return f"{head}{_image_ext(data)}"
    return f"{head}-{_safe_name(ref.name)}"


def _safe_name(name: str) -> str:
    """清洗飞书给的文件名：去掉路径分隔符和非法字符，并限制长度。"""
    cleaned = _UNSAFE_NAME.sub("_", (name or "").strip()).strip(". ")
    if not cleaned:
        return "attachment"
    stem, dot, suffix = cleaned.rpartition(".")
    if dot and 0 < len(suffix) <= 10:
        return f"{stem[:80] or 'attachment'}.{suffix}"
    return cleaned[:90]


def _image_ext(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"
