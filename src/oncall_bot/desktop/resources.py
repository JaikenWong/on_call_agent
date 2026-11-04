"""Bounded Feishu image fetches. The SDK's standard download buffers the whole file."""

import asyncio
import json
import shutil
from urllib.parse import quote

import httpx
from lark_oapi.core.token.manager import TokenManager

from ..attachments import _image_ext
from ..feishu.api import FeishuApi

IMAGE_LIMIT = 20 * 1024**2
CACHE_LIMIT = 1024**3


class DesktopApi(FeishuApi):
    async def download_message_resource(self, message_id, file_key, resource_type="image"):
        token = await asyncio.to_thread(TokenManager.get_self_tenant_token, self._client.config)
        url = (
            "https://open.feishu.cn/open-apis/im/v1/messages/"
            + quote(message_id, safe="")
            + "/resources/"
            + quote(file_key, safe="")
        )
        async with (
            httpx.AsyncClient(timeout=15) as client,
            client.stream(
                "GET", url, headers={"Authorization": "Bearer " + token}, params={"type": resource_type}
            ) as response,
        ):
            response.raise_for_status()
            if int(response.headers.get("content-length", 0)) > IMAGE_LIMIT:
                raise ValueError("图片超过 20 MiB")
            body = bytearray()
            async for block in response.aiter_bytes(64 * 1024):
                if len(body) + len(block) > IMAGE_LIMIT:
                    raise ValueError("图片超过 20 MiB")
                body.extend(block)
            return bytes(body)


class ImageCache:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "staging"
        self.pending = {}
        self.slots = asyncio.Semaphore(2)

    def start(self, api, row):
        iid = row["input_id"]
        if iid not in self.pending:
            task = asyncio.create_task(self._load(api, row))
            self.pending[iid] = task

            def done(t):
                self.pending.pop(iid, None)
                if not t.cancelled():
                    t.exception()  # Prefetch may finish before any text claims this image.

            task.add_done_callback(done)
        return self.pending[iid]

    async def _load(self, api, row):
        async with self.slots:
            folder = self.root / str(row["input_id"])
            ready = folder / "ready.json"
            if ready.is_file():
                return [folder / name for name in json.loads(ready.read_text())]
            folder.mkdir(parents=True, exist_ok=True)
            async with asyncio.timeout(30):
                paths = []
                for index, key in enumerate(json.loads(row["keys"])):
                    data = await api.download_message_resource(row["mid"], key)
                    ext = _image_ext(data)
                    if not data or ext not in (".png", ".jpg", ".gif", ".webp"):
                        raise ValueError("图片格式不支持")
                    usage = sum(
                        f.stat().st_size
                        for base in (self.root, self.store.root / "tasks")
                        for f in base.rglob("*")
                        if f.is_file()
                    )
                    if usage + len(data) > CACHE_LIMIT:
                        raise ValueError("缓存额度不足")
                    # Recheck after the network await: release/revoke can retire this input.
                    if not self.store.db.execute(
                        "SELECT 1 FROM images WHERE input_id=?", (row["input_id"],)
                    ).fetchone():
                        raise ValueError("暂存图片已过期")
                    path = folder / f"{row['input_id']}-{index}{ext}"
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(data)
                    tmp.replace(path)
                    paths.append(path)
                tmp = folder / "manifest.tmp"
                tmp.write_text(json.dumps([p.name for p in paths]))
                tmp.replace(ready)
                return paths

    async def collect(self, api, tid, target):
        inputs = {}
        rows = self.store.db.execute(
            "SELECT images.*, inputs.mid FROM images JOIN inputs ON input_id=inputs.id WHERE task_id=?",
            (tid,),
        ).fetchall()
        if rows:
            target.mkdir(parents=True, exist_ok=True)
            lists = await asyncio.gather(*(asyncio.shield(self.start(api, r)) for r in rows))
            for paths in lists:
                for path in paths:
                    dest = target / path.name
                    # Move the staged file only after its single immutable owner is known.
                    if path.exists():
                        path.replace(dest)
                    if not dest.is_file():
                        raise ValueError("附件缺失")
                    inputs[dest.name] = str(dest)
        return inputs

    def prefetch(self, api, mid, route):
        row = self.store.db.execute(
            "SELECT images.*, inputs.mid FROM inputs JOIN images ON inputs.id=input_id "
            "WHERE inputs.mid=? AND app=? AND tenant=? AND route=?",
            (mid, route[0], route[1], json.dumps(route)),
        ).fetchone()
        if row and len(self.pending) < 18:
            self.start(api, row)

    def prune(self):
        for directory in self.root.glob("*"):
            if not directory.name.isdecimal() or int(directory.name) in self.pending:
                continue
            if not self.store.db.execute(
                "SELECT 1 FROM images WHERE input_id=?", (int(directory.name),)
            ).fetchone():
                shutil.rmtree(directory)

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
