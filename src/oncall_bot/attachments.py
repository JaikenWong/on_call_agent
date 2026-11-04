"""飞书消息里的图片：下载到本地，攒着，附在下一条文字需求上交给 Claude。

为什么要「攒着」而不是收到图就跑一轮：飞书里发图片是一条独立消息，不能带文字。
人的习惯是先发图、再打字说要干什么。如果收到图就跑一轮，那一轮没有任何指令，
纯属浪费；而且紧接着的文字消息又看不到图。所以图片只下载 + 回执，
攒在 PendingAttachments 里，下一条文字消息带着它们一起进 prompt。

富文本（post）消息可以图文混排，那种是一条消息里既有文字又有图，直接当一轮跑。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import AttachmentsConfig
from .feishu.api import RESOURCE_DENIED_STATUS, SCOPE_DENIED_CODE, FeishuApi, FeishuApiError

log = logging.getLogger(__name__)

SCOPE_HINT = (
    "下载图片需要应用权限 `im:resource`（读取消息中的资源文件）。"
    "请在飞书开放平台加上它，发布新版本并等审核通过后重试。"
)

FORBIDDEN_HINT = "飞书拒绝了这张图片的下载（403）。可能是消息太旧或资源已被清理。"

# 按文件头判断真实格式，不信任消息里声明的类型
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


@dataclass
class SavedImage:
    path: Path
    size: int
    added_at: float = field(default_factory=time.monotonic)


@dataclass
class PendingAttachments:
    """等着被下一条文字消息领走的图片。A 模式只有一条活跃会话，所以全局一份。"""

    ttl_seconds: int
    max_items: int
    _items: list[SavedImage] = field(default_factory=list)

    def add(self, image: SavedImage) -> None:
        self._evict_expired()
        self._items.append(image)
        # 超出上限时丢掉最旧的
        while len(self._items) > self.max_items:
            dropped = self._items.pop(0)
            log.info("待处理图片超过上限，丢弃最旧的一张：%s", dropped.path.name)

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
            log.info("清空待处理图片 %d 张：%s", count, reason)
        return count

    def peek_count(self) -> int:
        self._evict_expired()
        return len(self._items)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        kept = [item for item in self._items if now - item.added_at <= self.ttl_seconds]
        if len(kept) != len(self._items):
            log.info("丢弃 %d 张过期图片", len(self._items) - len(kept))
        self._items = kept


class AttachmentStore:
    def __init__(self, api: FeishuApi, cfg: AttachmentsConfig) -> None:
        self._api = api
        self._cfg = cfg

    async def download_images(
        self, message_id: str, image_keys: list[str], subdir: str
    ) -> tuple[list[SavedImage], list[str]]:
        """返回 (已保存, 失败原因列表)。失败原因已折叠，不会每张一条。"""
        if not self._cfg.enabled or not image_keys:
            return [], []

        target = self._cfg.dir / (subdir or "unbound")
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        saved: list[SavedImage] = []
        failures: list[str] = []

        for index, key in enumerate(image_keys, start=1):
            try:
                data = await self._api.download_message_resource(message_id, key, "image")
            except FeishuApiError as exc:
                if exc.code == SCOPE_DENIED_CODE:
                    failures.append(SCOPE_HINT)
                    log.warning("下载消息图片缺少权限，放弃剩余图片：%s", exc)
                    break
                if exc.http_status == RESOURCE_DENIED_STATUS:
                    failures.append(FORBIDDEN_HINT)
                    log.warning("下载消息图片被拒绝（403），放弃剩余图片：%s", exc)
                    break
                failures.append(f"第 {index} 张下载失败（code={exc.code}）")
                continue
            except Exception:
                log.exception("下载第 %d 张图片时出现未预期异常", index)
                failures.append(f"第 {index} 张下载失败（内部错误）")
                continue

            if not data:
                failures.append(f"第 {index} 张下载到空内容")
                continue
            if len(data) > self._cfg.max_bytes:
                failures.append(f"第 {index} 张有 {len(data) // 1024} KB，超过上限")
                continue

            path = target / f"{stamp}-{index:02d}{_image_ext(data)}"
            try:
                path.write_bytes(data)
            except OSError as exc:
                failures.append(f"第 {index} 张写入失败：{exc}")
                continue
            saved.append(SavedImage(path=path, size=len(data)))

        if saved:
            log.info("消息图片已保存 %d 张 → %s", len(saved), target)
        return saved, failures


def append_attachments(prompt: str, paths: list[Path]) -> str:
    """把图片路径附在需求后面。没有图片时原样返回。"""
    if not paths:
        return prompt
    lines = [prompt, "", "---", "", "用户在飞书里发了以下图片，已下载到本机，请用 Read 工具查看："]
    for index, path in enumerate(paths, start=1):
        lines.append(f"{index}. {path}")
    lines.append("")
    lines.append("图片内容是需求的一部分，看过之后再动手。")
    return "\n".join(lines)


def ack_text(saved: list[SavedImage], failures: list[str], pending_total: int) -> str:
    """纯图片消息的回执。不跑一轮，只告诉用户收到了。"""
    lines: list[str] = []
    if saved:
        total_kb = sum(s.size for s in saved) // 1024
        lines.append(f"收到 {len(saved)} 张图片（共 {total_kb} KB），已存到本机。")
        if pending_total > len(saved):
            lines.append(f"当前一共攒了 {pending_total} 张等着处理。")
        lines.append("把需求发给我，我会带着这些图一起看。")
    if failures:
        lines.append("")
        lines.append("有图片没能下载：")
        lines.extend(f"- {reason}" for reason in failures)
    return "\n".join(lines) if lines else "图片没能处理，详情见本机日志。"


def _image_ext(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"
