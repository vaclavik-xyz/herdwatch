from __future__ import annotations

import time
from typing import Any, Callable, Hashable


class TTLCache:
    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.time) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._store: dict[Hashable, tuple[float, Any]] = {}

    def get_or(self, key: Hashable, fn: Callable[[], Any]) -> Any:
        now = self._clock()
        hit = self._store.get(key)
        if hit is not None and (now - hit[0]) < self._ttl:
            return hit[1]
        value = fn()
        self._store[key] = (now, value)
        return value
