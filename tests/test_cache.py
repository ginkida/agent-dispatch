"""Tests for the dispatch cache."""

from __future__ import annotations

from agent_dispatch.cache import DispatchCache
from agent_dispatch.models import DispatchResult


def _ok_result(agent: str = "test", text: str = "done") -> DispatchResult:
    return DispatchResult(agent=agent, success=True, result=text)


def _fail_result(agent: str = "test") -> DispatchResult:
    return DispatchResult(agent=agent, success=False, result="", error="boom")


class TestCacheBasics:
    def test_miss_on_empty(self):
        cache = DispatchCache(ttl=60)
        assert cache.get("a", "task") is None

    def test_put_and_get(self):
        cache = DispatchCache(ttl=60)
        result = _ok_result()
        cache.put("a", "task", result)
        cached = cache.get("a", "task")
        assert cached is not None
        assert cached.result == "done"

    def test_different_tasks_are_separate(self):
        cache = DispatchCache(ttl=60)
        cache.put("a", "task1", _ok_result(text="one"))
        cache.put("a", "task2", _ok_result(text="two"))
        assert cache.get("a", "task1").result == "one"
        assert cache.get("a", "task2").result == "two"

    def test_different_agents_are_separate(self):
        cache = DispatchCache(ttl=60)
        cache.put("a", "task", _ok_result(text="from-a"))
        cache.put("b", "task", _ok_result(text="from-b"))
        assert cache.get("a", "task").result == "from-a"
        assert cache.get("b", "task").result == "from-b"

    def test_context_affects_key(self):
        cache = DispatchCache(ttl=60)
        cache.put("a", "task", _ok_result(text="no-ctx"))
        cache.put("a", "task", _ok_result(text="with-ctx"), context="extra info")
        assert cache.get("a", "task").result == "no-ctx"
        assert cache.get("a", "task", context="extra info").result == "with-ctx"

    def test_failures_not_cached(self):
        cache = DispatchCache(ttl=60)
        cache.put("a", "task", _fail_result())
        assert cache.get("a", "task") is None


class TestCacheTTL:
    def test_expired_entry_returns_none(self):
        cache = DispatchCache(ttl=1)
        cache.put("a", "task", _ok_result())
        # Pretend time has passed
        key = cache._make_key("a", "task", None)
        ts, result = cache._store[key]
        cache._store[key] = (ts - 2, result)  # 2 seconds in the past
        assert cache.get("a", "task") is None

    def test_evict_expired(self):
        cache = DispatchCache(ttl=1)
        cache.put("a", "task1", _ok_result())
        cache.put("a", "task2", _ok_result())
        # Expire one entry
        key1 = cache._make_key("a", "task1", None)
        ts, result = cache._store[key1]
        cache._store[key1] = (ts - 2, result)
        evicted = cache.evict_expired()
        assert evicted == 1
        assert cache.get("a", "task2") is not None


class TestCacheClear:
    def test_clear_returns_count(self):
        cache = DispatchCache(ttl=60)
        cache.put("a", "t1", _ok_result())
        cache.put("a", "t2", _ok_result())
        assert cache.clear() == 2

    def test_clear_resets_stats(self):
        cache = DispatchCache(ttl=60)
        cache.put("a", "t1", _ok_result())
        cache.get("a", "t1")  # hit
        cache.get("a", "t2")  # miss
        cache.clear()
        stats = cache.stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["size"] == 0


class TestCacheStats:
    def test_initial_stats(self):
        cache = DispatchCache(ttl=120)
        stats = cache.stats()
        assert stats == {"size": 0, "hits": 0, "misses": 0, "hit_rate": 0.0, "ttl": 120}

    def test_stats_after_operations(self):
        cache = DispatchCache(ttl=60)
        cache.put("a", "task", _ok_result())
        cache.get("a", "task")   # hit
        cache.get("a", "task")   # hit
        cache.get("a", "other")  # miss
        stats = cache.stats()
        assert stats["size"] == 1
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["hit_rate"] == round(2 / 3, 3)
