"""飞书 OpenAPI 调用封装。全部用 a 前缀的异步方法。

所有调用失败都只记日志 + 重试一次；绝不让飞书侧的失败打断 Claude 的执行。
"""

from __future__ import annotations

import asyncio
import json
import logging

import lark_oapi as lark
from lark_oapi import AccessTokenType, BaseRequest, HttpMethod
from lark_oapi.api.docx.v1 import GetDocumentRequest, RawContentDocumentRequest
from lark_oapi.api.drive.v1 import DownloadMediaRequest
from lark_oapi.api.im.v1 import (
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 4000


class FeishuApiError(Exception):
    """OpenAPI 调用失败。保留 code/msg/HTTP 状态，以便区分「应用缺权限」和「这个资源不让你碰」。"""

    def __init__(
        self,
        code: int | None,
        msg: str | None,
        log_id: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"code={code} msg={msg} http={http_status} log_id={log_id}")
        self.code = code
        self.msg = msg or ""
        self.log_id = log_id
        self.http_status = http_status


class FeishuApi:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    # ---------- 机器人自身信息 ----------

    async def get_bot_open_id(self) -> str:
        req = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({AccessTokenType.TENANT})
            .build()
        )
        resp = await self._client.arequest(req)
        if not resp.success():
            raise RuntimeError(f"取机器人信息失败：code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
        payload = json.loads(resp.raw.content)
        open_id = (payload.get("bot") or {}).get("open_id")
        if not open_id:
            raise RuntimeError(f"取机器人信息成功但没有 open_id：{payload}")
        return open_id

    # ---------- 发送 ----------

    async def reply_text(self, message_id: str, text: str, *, in_thread: bool = False) -> str | None:
        return await self._reply(message_id, "text", {"text": text}, in_thread=in_thread)

    async def reply_long_text(self, message_id: str, text: str, *, in_thread: bool = False) -> None:
        """超长文本分段补发，每段不超过 MAX_TEXT_CHARS。"""
        for chunk in _split_chunks(text, MAX_TEXT_CHARS):
            await self.reply_text(message_id, chunk, in_thread=in_thread)

    async def reply_card(self, message_id: str, card: dict, *, in_thread: bool = False) -> str | None:
        return await self._reply(message_id, "interactive", card, in_thread=in_thread)

    async def patch_card(self, card_message_id: str, card: dict) -> bool:
        body = PatchMessageRequestBody.builder().content(_dumps(card)).build()
        req = PatchMessageRequest.builder().message_id(card_message_id).request_body(body).build()
        for attempt in (1, 2):
            try:
                resp = await self._client.im.v1.message.apatch(req)
            except Exception:
                log.warning("更新卡片抛异常（第 %d 次）", attempt, exc_info=True)
            else:
                if resp.success():
                    return True
                log.warning(
                    "更新卡片失败（第 %d 次）：code=%s msg=%s log_id=%s",
                    attempt,
                    resp.code,
                    resp.msg,
                    resp.get_log_id(),
                )
            if attempt == 1:
                await asyncio.sleep(0.5)
        return False

    # ---------- 云文档 ----------

    async def docx_raw_content(self, document_id: str) -> str:
        """新版文档（docx）的纯文本内容。失败抛 FeishuApiError。"""
        req = RawContentDocumentRequest.builder().document_id(document_id).lang(0).build()
        resp = await self._client.docx.v1.document.araw_content(req)
        if not resp.success():
            raise FeishuApiError(resp.code, resp.msg, resp.get_log_id())
        return getattr(resp.data, "content", "") or ""

    async def docx_title(self, document_id: str) -> str | None:
        """尽力获取文档标题；失败只记日志，不影响正文抓取。"""
        req = GetDocumentRequest.builder().document_id(document_id).build()
        try:
            resp = await self._client.docx.v1.document.aget(req)
        except Exception:
            log.debug("取文档标题异常：%s", document_id, exc_info=True)
            return None
        if not resp.success():
            log.debug("取文档标题失败：code=%s msg=%s", resp.code, resp.msg)
            return None
        document = getattr(resp.data, "document", None)
        return getattr(document, "title", None) if document else None

    async def docx_blocks(self, document_id: str, *, page_size: int = 500) -> list[dict]:
        """文档的全部 block（扁平列表，含 children 字段可还原文档树）。

        用原生请求而不是 typed API：需要的是完整 JSON，typed 模型反而要逐字段转。
        """
        blocks: list[dict] = []
        page_token = ""
        while True:
            queries = [("page_size", str(page_size)), ("document_revision_id", "-1")]
            if page_token:
                queries.append(("page_token", page_token))
            req = (
                BaseRequest.builder()
                .http_method(HttpMethod.GET)
                .uri(f"/open-apis/docx/v1/documents/{document_id}/blocks")
                .queries(queries)
                .token_types({AccessTokenType.TENANT})
                .build()
            )
            resp = await self._client.arequest(req)
            if not resp.success():
                raise FeishuApiError(resp.code, resp.msg, resp.get_log_id())
            data = (json.loads(resp.raw.content) or {}).get("data") or {}
            blocks.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
            if not page_token:
                break
        return blocks

    async def download_media(self, file_token: str) -> bytes:
        """下载云空间素材（文档内嵌图片等）。

        注意：二进制下载接口在成功时 resp.code 是 None，`resp.success()` 会返回 False，
        所以必须看 raw.status_code，不能用 success() 判断。
        """
        req = DownloadMediaRequest.builder().file_token(file_token).build()
        resp = await self._client.drive.v1.media.adownload(req)
        return _binary_payload(resp, "drive media")

    async def download_message_resource(
        self, message_id: str, file_key: str, resource_type: str = "image"
    ) -> bytes:
        """下载消息里的图片 / 文件。需要权限 `im:resource`。

        和素材下载一样是二进制接口：成功时 resp.code 是 None、success() 返回 False，
        必须看 raw.status_code。
        """
        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        resp = await self._client.im.v1.message_resource.aget(req)
        return _binary_payload(resp, f"message_resource {resource_type}")

    async def wiki_node(self, token: str) -> tuple[str, str, str | None]:
        """知识库节点 → (obj_type, obj_token, title)。失败抛 FeishuApiError。

        1.7.5 的 wiki.v2.space_node 没有 get 方法，这里走原生请求。
        """
        req = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri("/open-apis/wiki/v2/spaces/get_node")
            .queries([("token", token), ("obj_type", "wiki")])
            .token_types({AccessTokenType.TENANT})
            .build()
        )
        resp = await self._client.arequest(req)
        if not resp.success():
            raise FeishuApiError(resp.code, resp.msg, resp.get_log_id())
        payload = json.loads(resp.raw.content)
        node = ((payload.get("data") or {}).get("node")) or {}
        obj_type = node.get("obj_type") or ""
        obj_token = node.get("obj_token") or ""
        if not obj_type or not obj_token:
            raise FeishuApiError(resp.code, f"节点信息不完整：{node}", resp.get_log_id())
        return obj_type, obj_token, node.get("title")

    # ---------- 内部 ----------

    async def _reply(
        self, message_id: str, msg_type: str, content: dict, *, in_thread: bool
    ) -> str | None:
        body = (
            ReplyMessageRequestBody.builder()
            .content(_dumps(content))
            .msg_type(msg_type)
            .reply_in_thread(in_thread)
            .build()
        )
        req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        for attempt in (1, 2):
            try:
                resp = await self._client.im.v1.message.areply(req)
            except Exception:
                log.warning("回复消息抛异常（第 %d 次，type=%s）", attempt, msg_type, exc_info=True)
            else:
                if resp.success():
                    return getattr(resp.data, "message_id", None)
                log.warning(
                    "回复消息失败（第 %d 次，type=%s）：code=%s msg=%s log_id=%s",
                    attempt,
                    msg_type,
                    resp.code,
                    resp.msg,
                    resp.get_log_id(),
                )
            if attempt == 1:
                await asyncio.sleep(0.5)
        return None


def _dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


# 应用没开通所需 scope：HTTP 400 + 这个 code，响应体里会列出可用的 scope 清单
SCOPE_DENIED_CODE = 99991672
# 应用 scope 没问题，但这个具体资源不让访问（如文档安全设置禁止下载素材）：
# HTTP 403 且响应体为空，拿不到任何 code/msg
RESOURCE_DENIED_STATUS = 403


def _binary_payload(resp: object, what: str) -> bytes:
    """二进制下载接口的统一取值。

    这类接口成功时 `resp.code` 是 None、`success()` 返回 False，所以不能用 success() 判断；
    资源级拒绝会返回 403 且响应体为空，连 code/msg 都没有。
    """
    raw = getattr(resp, "raw", None)
    status = getattr(raw, "status_code", None)
    body = getattr(raw, "content", b"") or b""
    log_id = resp.get_log_id() if hasattr(resp, "get_log_id") else None

    if status != 200:
        code, msg = _parse_error_body(body)
        raise FeishuApiError(code, msg or f"{what} 下载失败", log_id, http_status=status)

    content_type = ((getattr(raw, "headers", None) or {}).get("content-type") or "").lower()
    if "application/json" in content_type:
        code, msg = _parse_error_body(body)
        raise FeishuApiError(code, msg or f"{what} 返回了 JSON 而不是二进制", log_id, http_status=status)

    file_obj = getattr(resp, "file", None)
    if file_obj is not None and hasattr(file_obj, "read"):
        data = file_obj.read()
        if data:
            return data
    return body


def _parse_error_body(body: bytes) -> tuple[int | None, str]:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, body[:200].decode("utf-8", errors="replace")
    return payload.get("code"), payload.get("msg") or ""


def _split_chunks(text: str, size: int) -> list[str]:
    """按行切块，尽量不把一行劈开。"""
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    buf: list[str] = []
    length = 0
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if buf:
                chunks.append("".join(buf))
                buf, length = [], 0
            chunks.append(line[:size])
            line = line[size:]
        if length + len(line) > size and buf:
            chunks.append("".join(buf))
            buf, length = [], 0
        buf.append(line)
        length += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks
