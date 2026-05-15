"""Tests for the JobStore persistence layer."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_dispatch.jobs import Job, JobStore
from agent_dispatch.models import DispatchResult


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


class TestJobStoreCreateGet:
    def test_create_persists_pending_job(self, store: JobStore):
        job = store.create("infra", "check pods")
        assert job.id
        assert len(job.id) == 32  # uuid4 hex
        assert job.agent == "infra"
        assert job.task == "check pods"
        assert job.status == "pending"
        assert job.created_at > 0
        assert job.started_at is None
        assert job.completed_at is None

        path = store.directory / f"{job.id}.json"
        assert path.exists()

    def test_get_returns_persisted_job(self, store: JobStore):
        job = store.create("db", "list tables", context="prod", caller="api", goal="audit")
        reread = store.get(job.id)
        assert reread is not None
        assert reread.id == job.id
        assert reread.context == "prod"
        assert reread.caller == "api"
        assert reread.goal == "audit"

    def test_get_missing_returns_none(self, store: JobStore):
        assert store.get("nonexistent") is None

    def test_get_handles_corrupt_file(self, store: JobStore):
        (store.directory / "broken.json").write_text("{not valid json")
        assert store.get("broken") is None


class TestJobStoreLifecycle:
    def test_mark_running_sets_timestamp(self, store: JobStore):
        job = store.create("infra", "task")
        before = time.time()
        updated = store.mark_running(job.id)
        assert updated is not None
        assert updated.status == "running"
        assert updated.started_at is not None
        assert updated.started_at >= before

    def test_finish_success(self, store: JobStore):
        job = store.create("infra", "task")
        result = DispatchResult(agent="infra", success=True, result="done", cost_usd=0.05)
        updated = store.finish(job.id, result)
        assert updated is not None
        assert updated.status == "done"
        assert updated.completed_at is not None
        assert updated.result is not None
        assert updated.result.success is True
        assert updated.result.cost_usd == 0.05
        assert updated.error is None

    def test_finish_failure_captures_error(self, store: JobStore):
        job = store.create("infra", "task")
        result = DispatchResult(
            agent="infra", success=False, result="", error="oops", error_type="cli_error"
        )
        updated = store.finish(job.id, result)
        assert updated is not None
        assert updated.status == "failed"
        assert updated.error == "oops"

    def test_fail_without_result(self, store: JobStore):
        job = store.create("infra", "task")
        updated = store.fail(job.id, "worker crashed")
        assert updated is not None
        assert updated.status == "failed"
        assert updated.error == "worker crashed"
        assert updated.result is None

    def test_mark_running_unknown_returns_none(self, store: JobStore):
        assert store.mark_running("nope") is None

    def test_finish_unknown_returns_none(self, store: JobStore):
        result = DispatchResult(agent="x", success=True, result="")
        assert store.finish("nope", result) is None


class TestJobStoreList:
    def test_list_empty(self, store: JobStore):
        assert store.list() == []

    def test_list_returns_all_sorted_recent_first(self, store: JobStore):
        a = store.create("infra", "first")
        time.sleep(0.005)
        b = store.create("db", "second")
        time.sleep(0.005)
        c = store.create("backend", "third")
        jobs = store.list()
        assert [j.id for j in jobs] == [c.id, b.id, a.id]

    def test_list_filters_by_status(self, store: JobStore):
        a = store.create("infra", "a")
        b = store.create("db", "b")
        store.create("backend", "c")
        store.mark_running(a.id)
        store.finish(b.id, DispatchResult(agent="db", success=True, result="done"))

        running = store.list(status="running")
        assert [j.id for j in running] == [a.id]
        done = store.list(status="done")
        assert [j.id for j in done] == [b.id]
        pending = store.list(status="pending")
        assert len(pending) == 1
        assert pending[0].agent == "backend"

    def test_list_skips_corrupt_files(self, store: JobStore):
        store.create("infra", "good")
        (store.directory / "broken.json").write_text("not json")
        jobs = store.list()
        assert len(jobs) == 1


class TestJobStoreGC:
    def test_gc_removes_terminal_jobs_past_threshold(self, store: JobStore):
        old = store.create("infra", "old")
        result = DispatchResult(agent="infra", success=True, result="done")
        finished = store.finish(old.id, result)
        assert finished is not None
        # Backdate completion to long ago
        finished.completed_at = time.time() - 86400 * 30  # 30 days
        store._write(finished)

        recent = store.create("db", "recent")
        store.finish(recent.id, DispatchResult(agent="db", success=True, result="done"))

        purged = store.gc(max_age_seconds=86400 * 7)  # 7 days
        assert purged == 1
        assert store.get(old.id) is None
        assert store.get(recent.id) is not None

    def test_gc_never_removes_pending_or_running(self, store: JobStore):
        pending = store.create("infra", "pending")
        # Backdate created_at far in the past
        pending.created_at = time.time() - 86400 * 365
        store._write(pending)

        running = store.create("db", "running")
        store.mark_running(running.id)
        updated = store.get(running.id)
        assert updated is not None
        updated.started_at = time.time() - 86400 * 365
        store._write(updated)

        purged = store.gc(max_age_seconds=86400)
        assert purged == 0
        assert store.get(pending.id) is not None
        assert store.get(running.id) is not None

    def test_gc_returns_zero_when_empty(self, store: JobStore):
        assert store.gc(max_age_seconds=1) == 0


class TestJobModel:
    def test_is_terminal(self):
        for s in ("done", "failed", "cancelled"):
            assert Job(id="x", agent="a", task="t", status=s).is_terminal()
        for s in ("pending", "running"):
            assert not Job(id="x", agent="a", task="t", status=s).is_terminal()
