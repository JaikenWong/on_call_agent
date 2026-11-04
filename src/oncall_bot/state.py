"""全局活跃会话状态与原子持久化。

A 模式：任意时刻至多一条活跃会话。额外记住最后一张运行中卡片，
以便进程重启后把它刷成「状态未知」，而不是永远停在「处理中」。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_VERSION = 1


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ActiveSession:
    session_id: str
    workdir: str
    title: str
    switched_by: str
    switched_at: str
    last_active_at: str | None = None
    # /new 刚建好、还没跑过第一轮。此时会话文件尚未落盘：
    # options 要用 session_id= 而不是 resume=，而且不能做「会话必须存在」检查。
    pending_create: bool = False

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def repo_name(self) -> str:
        return Path(self.workdir).name or self.workdir


@dataclass
class CardRef:
    message_id: str
    chat_id: str


class StateStore:
    """单文件状态。所有读写走同一把 asyncio.Lock；写入用临时文件 + os.replace。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._active: ActiveSession | None = None
        self._last_card: CardRef | None = None

    # ---------- 读 ----------

    @property
    def active(self) -> ActiveSession | None:
        return self._active

    @property
    def last_card(self) -> CardRef | None:
        return self._last_card

    # ---------- 生命周期 ----------

    async def load(self) -> None:
        async with self._lock:
            if not self._path.exists():
                log.info("状态文件不存在，从空状态开始：%s", self._path)
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("状态文件无法读取，按空状态启动：%s（%s）", self._path, exc)
                return
            active = raw.get("active")
            if isinstance(active, dict):
                try:
                    self._active = ActiveSession(**active)
                except TypeError as exc:
                    log.warning("状态文件里的 active 字段不合法，已忽略：%s", exc)
            card = raw.get("last_card")
            if isinstance(card, dict):
                with contextlib.suppress(TypeError):
                    self._last_card = CardRef(**card)
            if self._active:
                log.info("恢复活跃会话 %s（%s）", self._active.short_id, self._active.workdir)

    # ---------- 写 ----------

    async def set_active(self, session: ActiveSession) -> None:
        async with self._lock:
            self._active = session
            self._write()

    async def clear_active(self) -> None:
        async with self._lock:
            self._active = None
            self._write()

    async def touch_active(self) -> None:
        async with self._lock:
            if self._active is not None:
                self._active.last_active_at = now_iso()
                self._write()

    async def finish_pending_create(self, title: str | None = None) -> None:
        """新建的会话跑完第一轮、文件已落盘之后调用。"""
        async with self._lock:
            if self._active is None or not self._active.pending_create:
                return
            self._active.pending_create = False
            if title and title.strip():
                self._active.title = title.strip()
            self._write()

    async def update_session_id(self, new_session_id: str) -> None:
        """SDK 返回的 session_id 与状态不一致时的兜底（实测正常情况不会发生）。"""
        async with self._lock:
            if self._active is not None:
                self._active.session_id = new_session_id
                self._write()

    async def set_last_card(self, ref: CardRef | None) -> None:
        async with self._lock:
            self._last_card = ref
            self._write()

    def _write(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "active": asdict(self._active) if self._active else None,
            "last_card": asdict(self._last_card) if self._last_card else None,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("状态写入失败：%s（%s）", self._path, exc)
            tmp.unlink(missing_ok=True)
