"""TTL-based in-memory cache for dispatch results."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

from .models import DispatchResult

logger = logging.getLogger(__name__)


class DispatchCache:
    """Thread-safe TTL cache for dispatch results.

    Keyed on (agent, task, context) — identical requests within the TTL
    window return the cached result without spawning a new subprocess.
    """

    def __init__(self, ttl: int = 300) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, DispatchResult]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _make_key(agent: str, task: str, context: str | None) -> str:
        canonical = json.dumps(
            {"agent": agent, "task": task, "context": context or ""},
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, agent: str, task: str, context: str | None = None) -> DispatchResult | None:
        key = self._make_key(agent, task, context)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            ts, result = entry
            if time.monotonic() - ts > self._ttl:
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return result

    def put(
        self,
        agent: str,
        task: str,
        result: DispatchResult,
        context: str | None = None,
    ) -> None:
        if not result.success:
            return  # don't cache failures
        key = self._make_key(agent, task, context)
        with self._lock:
            self._store[key] = (time.monotonic(), result)

    def clear(self) -> int:
        with self._lock:
            count = len(self._store)
            self._store.clear()
            self._hits = 0
            self._misses = 0
            return count

    def evict_expired(self) -> int:
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
            for k in expired:
                del self._store[k]
            return len(expired)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
                "ttl": self._ttl,
            }
