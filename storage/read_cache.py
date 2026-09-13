"""Bounded JSON read cache. The caller serializes access; never stores writes."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Entry:
    expires_at: float
    payload: bytes


class ReadCache:
    """LRU with fixed TTL and a serialized-payload budget, not an RSS limit.

    Values are serialized on insertion and decoded on retrieval so callers
    cannot mutate another request's cached statistics. Oversized values are
    returned normally by the caller, but are never retained here.
    """

    def __init__(
        self, *, ttl_seconds: float = 30.0, max_entries: int = 128,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.ttl_seconds = max(0.0, ttl_seconds)
        self.max_entries = max(0, max_entries)
        self.max_bytes = max(0, max_bytes)
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._bytes = 0

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def _remove(self, key: Hashable) -> None:
        entry = self._entries.pop(key)
        self._bytes -= len(entry.payload)

    def get(self, key: Hashable) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires_at:
            self._remove(key)
            return None
        self._entries.move_to_end(key)
        return json.loads(entry.payload)

    def put(self, key: Hashable, value: Any) -> None:
        if key in self._entries:
            self._remove(key)
        if not self.ttl_seconds or not self.max_entries or not self.max_bytes:
            return
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > self.max_bytes:
            return
        now = time.monotonic()
        for expired in [k for k, entry in self._entries.items() if entry.expires_at <= now]:
            self._remove(expired)
        while self._entries and (
            len(self._entries) >= self.max_entries or self._bytes + len(payload) > self.max_bytes
        ):
            self._remove(next(iter(self._entries)))
        self._entries[key] = _Entry(now + self.ttl_seconds, payload)
        self._bytes += len(payload)
