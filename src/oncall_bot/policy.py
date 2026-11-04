"""准入策略：message_id 去重、群内 @ 校验、open_id 白名单。"""

from __future__ import annotations

import time
from collections import OrderedDict


class Deduper:
    """message_id 的 TTL 去重缓存。飞书在未及时确认时会重推同一条消息。"""

    def __init__(self, ttl_seconds: float = 600.0, maxsize: int = 10_000) -> None:
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._seen: OrderedDict[str, float] = OrderedDict()

    def check_and_add(self, message_id: str) -> bool:
        """首次见到返回 True；重复返回 False。"""
        now = time.monotonic()
        self._evict(now)
        if message_id in self._seen:
            return False
        self._seen[message_id] = now
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return True

    def _evict(self, now: float) -> None:
        while self._seen:
            key, ts = next(iter(self._seen.items()))
            if now - ts <= self._ttl:
                break
            self._seen.popitem(last=False)

    def __len__(self) -> int:
        return len(self._seen)
