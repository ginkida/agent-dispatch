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

    Keyed on (agent, task, context, caller, goal, response_format) — identical
    requests within the TTL window return the cached result without spawning a
    new subprocess. ``caller``/``goal``/``response_format`` all affect the
    prompt sent to the agent, so they must be part of the key: otherwise two
    requests with different framing would collide and return the wrong response.
    """

    def __init__(self, ttl: int = 300, max_size: int = 1000) -> None:
        self._ttl = ttl
        self._max_size = max_size
        # value = (stored_at, agent_name, result). The agent name is kept
        # alongside the hashed key so invalidate_agent() can drop exactly one
        # agent's entries when its config changes.
        self._store: dict[str, tuple[float, str, DispatchResult]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @staticmethod
    def _make_key(
        agent: str,
        task: str,
        context: str | None,
        caller: str | None = None,
        goal: str | None = None,
        response_format: str | None = None,
    ) -> str:
        canonical = json.dumps(
            {
                "agent": agent,
                "task": task,
                "context": context or "",
                "caller": caller or "",
                "goal": goal or "",
                "response_format": response_format or "",
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(
        self,
        agent: str,
        task: str,
        context: str | None = None,
        caller: str | None = None,
        goal: str | None = None,
        response_format: str | None = None,
    ) -> DispatchResult | None:
        key = self._make_key(agent, task, context, caller, goal, response_format)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            ts, _agent, result = entry
            if time.monotonic() - ts > self._ttl:
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            # Hand back a copy: the stored result is shared by every caller of
            # this key, and a caller that mutates what it got (adding a flag,
            # truncating text) would silently corrupt the entry for everyone.
            return result.model_copy(deep=True)

    def put(
        self,
        agent: str,
        task: str,
        result: DispatchResult,
        context: str | None = None,
        caller: str | None = None,
        goal: str | None = None,
        response_format: str | None = None,
    ) -> None:
        if not result.success:
            return  # don't cache failures
        if result.denied_tools or result.budget_exceeded:
            # Successful but degraded: the agent answered with tools blocked, or
            # the run cost more than its cap. The documented recovery is "grant
            # access / raise the budget, then re-dispatch" — caching this would
            # serve the same crippled answer back for the whole TTL and make that
            # recovery a no-op (the permission config is not part of the key).
            return
        key = self._make_key(agent, task, context, caller, goal, response_format)
        with self._lock:
            # Bound memory: when at capacity and inserting a new key, evict the
            # oldest entry by insertion time (FIFO). We intentionally do NOT
            # refresh timestamps on read — the timestamp also drives TTL expiry,
            # so touching it on access would turn TTL into idle-time. Refreshing
            # an existing key never triggers eviction.
            if key not in self._store and len(self._store) >= self._max_size:
                oldest = min(self._store, key=lambda k: self._store[k][0])
                del self._store[oldest]
                self._evictions += 1
            self._store[key] = (time.monotonic(), agent, result)

    def invalidate_agent(self, agent: str) -> int:
        """Drop every cached result for *agent*. Returns the number removed.

        Called whenever an agent's config changes: the cache key is
        (agent, task, context, caller, goal, response_format), so it cannot tell
        that the name now points at a different directory, permission set or
        model. Without this, ``remove_agent`` + ``add_agent`` under the same name
        keeps serving the previous project's answers for the rest of the TTL.
        """
        with self._lock:
            stale = [k for k, (_ts, name, _r) in self._store.items() if name == agent]
            for k in stale:
                del self._store[k]
            return len(stale)

    def clear(self) -> int:
        with self._lock:
            count = len(self._store)
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0
            return count

    def evict_expired(self) -> int:
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (ts, _n, _r) in self._store.items() if now - ts > self._ttl]
            for k in expired:
                del self._store[k]
            return len(expired)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._store),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
                "ttl": self._ttl,
            }
