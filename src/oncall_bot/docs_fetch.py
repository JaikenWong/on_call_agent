"""消息里出现飞书云文档链接时，抓成本地 md（含图片），再把路径交给 Claude。

为什么落地成文件而不是塞进 prompt：文档可能几万字，塞进 prompt 会挤掉会话上下文。
写成文件后由 Claude 自己决定读多少。文件放在 inbox 目录（不污染目标仓库），
通过 ClaudeAgentOptions.add_dirs 授权读取。

正文走 block 树渲染而不是 raw_content，因为 raw_content 会丢图片位置
（实测 24 个 image block 只给 23 个占位符，有一张被包在 view 容器里直接跳过）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import docx_render
from .config import DocsConfig
from .feishu.api import RESOURCE_DENIED_STATUS, SCOPE_DENIED_CODE, FeishuApi, FeishuApiError

log = logging.getLogger(__name__)

# https://<tenant>.feishu.cn/docx/<token> 等
_URL_RE = re.compile(
    r"https?://[\w.-]+\.(?:feishu\.cn|feishu-pre\.net|larksuite\.com|larkoffice\.com)"
    r"/(?P<kind>docx|wiki|docs|sheets|base|file)/(?P<token>[A-Za-z0-9]{10,})",
    re.IGNORECASE,
)

SUPPORTED_KINDS = {"docx", "wiki"}

_KIND_LABEL = {
    "docs": "旧版文档",
    "sheets": "电子表格",
    "base": "多维表格",
    "file": "云空间文件",
}

PERMISSION_HINT = (
    "如果是权限问题：机器人是独立身份，仅把文档分享到群里它读不到。"
    "请在文档右上角「分享」里把这个机器人加为协作者（可阅读），或把文档设为组织内可阅读，然后重发链接。"
)

IMAGE_SCOPE_HINT = (
    "文档内的图片没能下载：应用缺少云空间读取权限。"
    "请在飞书开放平台给应用加上 drive:drive:readonly（或 docs:document.media:download），"
    "发布新版本并等审核通过后重试。正文文字不受影响。"
)

IMAGE_FORBIDDEN_HINT = (
    "文档内的图片没能下载：飞书返回 403，应用权限是够的，是这篇文档本身不允许下载素材。"
    "常见原因是文档「更多设置」里「可复制、打印、下载」限制为「仅可编辑的人」，"
    "而机器人只有可阅读权限。改法二选一：把机器人的协作者权限提升到「可编辑」，"
    "或把该设置的范围放宽到「可阅读的人」。正文文字不受影响。"
)

# 失败提示最多列这么多张图，再多就只给总数，免得灌满卡片和 prompt
_MAX_LISTED_FAILURES = 5

# 从文件头几个字节判断真实格式，不信任扩展名
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


@dataclass(frozen=True)
class DocLink:
    url: str
    kind: str
    token: str


@dataclass
class FetchedDoc:
    url: str
    title: str
    path: Path
    chars: int
    truncated: bool
    images: int = 0
    image_notes: list[str] = field(default_factory=list)


@dataclass
class FetchFailure:
    url: str
    reason: str


@dataclass
class FetchOutcome:
    docs: list[FetchedDoc] = field(default_factory=list)
    failures: list[FetchFailure] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.docs and not self.failures


def find_links(text: str, limit: int) -> list[DocLink]:
    """按出现顺序提取链接，同一 token 只取一次。"""
    seen: set[str] = set()
    links: list[DocLink] = []
    for m in _URL_RE.finditer(text or ""):
        token = m.group("token")
        if token in seen:
            continue
        seen.add(token)
        links.append(DocLink(url=m.group(0), kind=m.group("kind").lower(), token=token))
        if len(links) >= limit:
            break
    return links


class DocFetcher:
    def __init__(self, api: FeishuApi, cfg: DocsConfig) -> None:
        self._api = api
        self._cfg = cfg

    async def fetch(self, text: str, subdir: str) -> FetchOutcome:
        outcome = FetchOutcome()
        if not self._cfg.enabled:
            return outcome

        links = find_links(text, self._cfg.max_docs_per_message)
        if not links:
            return outcome

        target_dir = self._cfg.inbox_dir / (subdir or "default")
        target_dir.mkdir(parents=True, exist_ok=True)

        for link in links:
            try:
                doc = await self._fetch_one(link, target_dir)
            except FeishuApiError as exc:
                log.warning("抓取云文档失败 %s：%s", link.url, exc)
                outcome.failures.append(
                    FetchFailure(link.url, f"读取失败（code={exc.code}: {exc.msg}）。{PERMISSION_HINT}")
                )
            except OSError as exc:
                log.warning("写入云文档失败 %s：%s", link.url, exc)
                outcome.failures.append(FetchFailure(link.url, f"写入本地文件失败：{exc}"))
            except Exception:
                log.exception("抓取云文档出现未预期异常：%s", link.url)
                outcome.failures.append(FetchFailure(link.url, "内部错误，详情见本机日志。"))
            else:
                if doc is None:
                    label = _KIND_LABEL.get(link.kind, link.kind)
                    outcome.failures.append(
                        FetchFailure(
                            link.url,
                            f"暂不支持{label}。可以导出成文件，或改贴新版文档（/docx/）的链接。",
                        )
                    )
                else:
                    outcome.docs.append(doc)
        return outcome

    async def _fetch_one(self, link: DocLink, target_dir: Path) -> FetchedDoc | None:
        kind, token = link.kind, link.token
        wiki_title: str | None = None

        if kind == "wiki":
            obj_type, obj_token, wiki_title = await self._api.wiki_node(token)
            if obj_type != "docx":
                log.info("知识库节点 %s 指向 %s，暂不支持", token, obj_type)
                return None
            kind, token = "docx", obj_token

        if kind != "docx":
            return None

        blocks: list[dict] | None = None
        try:
            blocks = await self._api.docx_blocks(token)
        except FeishuApiError as exc:
            # block 接口不可用时退回 raw_content：正文还在，只是图片位置会丢
            log.warning("blocks 接口失败（%s），退回 raw_content：%s", exc, token)

        image_notes: list[str] = []
        image_count = 0

        if blocks:
            title = (docx_render.document_title(blocks) or wiki_title or "").strip()
            if not title:
                title = (await self._api.docx_title(token) or "").strip()
            title = title or f"未命名文档-{token[:8]}"
            assets_dir = target_dir / f"{_slug(title)}-{token[:8]}.assets"
            image_map, image_notes = await self._download_images(blocks, assets_dir)
            image_count = len(image_map)
            body = docx_render.render(blocks, image_ref=_make_image_ref(image_map))
        else:
            title = (wiki_title or await self._api.docx_title(token) or "").strip()
            title = title or f"未命名文档-{token[:8]}"
            body = await self._api.docx_raw_content(token)
            image_notes.append("本篇用的是纯文本接口，文档内图片未抓取。")

        truncated = len(body) > self._cfg.max_chars_per_doc
        if truncated:
            body = body[: self._cfg.max_chars_per_doc]

        path = target_dir / f"{_slug(title)}-{token[:8]}.md"
        path.write_text(
            _header(link.url, title, image_count, image_notes, truncated) + body, encoding="utf-8"
        )
        log.info(
            "云文档已抓取：%s → %s（%d 字，%d 张图）", link.url, path, len(body), image_count
        )
        return FetchedDoc(
            url=link.url,
            title=title,
            path=path,
            chars=len(body),
            truncated=truncated,
            images=image_count,
            image_notes=image_notes,
        )

    async def _download_images(
        self, blocks: list[dict], assets_dir: Path
    ) -> tuple[dict[int, Path], list[str]]:
        tokens = docx_render.image_tokens(blocks)
        if not tokens:
            return {}, []
        if not self._cfg.fetch_images:
            return {}, [f"文档内有 {len(tokens)} 张图，但配置里关闭了图片抓取。"]

        notes: list[str] = []
        limited = tokens[: self._cfg.max_images_per_doc]
        if len(limited) < len(tokens):
            notes.append(f"文档内有 {len(tokens)} 张图，只下载了前 {len(limited)} 张（max_images_per_doc）。")

        assets_dir.mkdir(parents=True, exist_ok=True)
        saved: dict[int, Path] = {}
        failures: list[str] = []

        for index, token in enumerate(limited, start=1):
            try:
                data = await self._api.download_media(token)
            except FeishuApiError as exc:
                # 这两种拒绝对每张图都一样，没必要再试 N 次，也不该刷 N 条提示
                if exc.code == SCOPE_DENIED_CODE:
                    notes.append(IMAGE_SCOPE_HINT)
                    log.warning("图片下载缺少应用权限，放弃本篇剩余图片：%s", exc)
                    break
                if exc.http_status == RESOURCE_DENIED_STATUS:
                    notes.append(IMAGE_FORBIDDEN_HINT)
                    log.warning("图片下载被文档权限拒绝（403），放弃本篇剩余图片：%s", exc)
                    break
                failures.append(f"图 {index}（code={exc.code}）")
                continue
            except Exception:
                log.exception("图 %d 下载出现未预期异常", index)
                failures.append(f"图 {index}（内部错误）")
                continue

            if not data:
                failures.append(f"图 {index}（内容为空）")
                continue
            if len(data) > self._cfg.max_image_bytes:
                failures.append(f"图 {index}（{len(data) // 1024} KB，超过上限）")
                continue

            path = assets_dir / f"img-{index:02d}{_image_ext(data)}"
            path.write_bytes(data)
            saved[index] = path

        if failures:
            listed = "、".join(failures[:_MAX_LISTED_FAILURES])
            more = f" 等 {len(failures)} 张" if len(failures) > _MAX_LISTED_FAILURES else ""
            notes.append(f"以下图片下载失败：{listed}{more}。")

        if saved:
            log.info("下载图片 %d/%d → %s", len(saved), len(limited), assets_dir)
        return saved, notes


def _make_image_ref(image_map: dict[int, Path]):
    def ref(index: int, token: str) -> str:
        path = image_map.get(index)
        if path is None:
            return f"（图 {index} 未能下载，原因见本文件开头的说明）"
        # 绝对路径 + 正斜杠：既是合法 markdown，也能直接喂给 Read 工具
        return f"![图 {index}]({path.as_posix()})"

    return ref


def _header(
    url: str, title: str, image_count: int, image_notes: list[str], truncated: bool
) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 来源：{url}",
        f"- 抓取时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "- 由飞书机器人自动抓取" + ("（正文已截断）" if truncated else ""),
    ]
    if image_count:
        lines.append(
            f"- 文档内 {image_count} 张图已下载到同目录的 `.assets` 文件夹，"
            "正文中的图片链接是本机绝对路径，需要看图时直接用 Read 工具读那个文件。"
        )
    for note in image_notes:
        lines.append(f"- 注意：{note}")
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def build_prompt(user_text: str, outcome: FetchOutcome) -> str:
    """把抓取结果附在用户原文后面。没有链接时原样返回。"""
    if outcome.empty:
        return user_text

    parts = [user_text, "", "---", ""]
    if outcome.docs:
        parts.append("消息里的飞书云文档已抓取到本机，请先读这些文件再处理上面的需求：")
        for i, doc in enumerate(outcome.docs, start=1):
            suffix = "，正文已截断" if doc.truncated else ""
            image_bit = f"，含 {doc.images} 张图" if doc.images else ""
            parts.append(f"{i}. 《{doc.title}》（{doc.chars} 字{image_bit}{suffix}）")
            parts.append(f"   本地路径：{doc.path}")
            parts.append(f"   原始链接：{doc.url}")
            for note in doc.image_notes:
                parts.append(f"   注意：{note}")
        if any(d.images for d in outcome.docs):
            parts.append(
                "   文档里的图片是本机绝对路径，需要看图时用 Read 工具读对应文件。"
            )
        parts.append("")
    if outcome.failures:
        parts.append("以下链接我没能读取，请在回复里提醒用户：")
        for f in outcome.failures:
            parts.append(f"- {f.url}")
            parts.append(f"  {f.reason}")
    return "\n".join(parts)


def summarize_outcome(outcome: FetchOutcome) -> str:
    """给飞书卡片用的一行摘要。"""
    bits: list[str] = []
    if outcome.docs:
        names = "、".join(f"《{d.title}》" for d in outcome.docs)
        total_images = sum(d.images for d in outcome.docs)
        bits.append(f"已读取云文档：{names}" + (f"（含 {total_images} 张图）" if total_images else ""))
    if outcome.failures:
        bits.append(f"{len(outcome.failures)} 个链接读取失败（见最终回复）")
    return " · ".join(bits)


def _image_ext(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _slug(title: str) -> str:
    cleaned = _INVALID_FILENAME.sub("_", title).strip(" .")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned[:60] or "doc").rstrip("_")
