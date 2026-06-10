"""Tests for the JobStore persistence layer."""

from __future__ import annotations

import stat
import sys
import time
from pathlib import Path

import pytest

from agent_dispatch.jobs import Job, JobStore, is_valid_job_id
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


class TestJobProgress:
    def test_update_progress_on_running_job(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        before = time.time()
        updated = store.update_progress(job.id, ["Using tool: Bash", "Reading logs..."])
        assert updated is not None
        assert updated.progress == ["Using tool: Bash", "Reading logs..."]
        assert updated.progress_updated_at is not None
        assert updated.progress_updated_at >= before
        # Persisted, not just in-memory
        reread = store.get(job.id)
        assert reread.progress == ["Using tool: Bash", "Reading logs..."]

    def test_update_progress_replaces_tail(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        store.update_progress(job.id, ["a", "b"])
        store.update_progress(job.id, ["b", "c"])
        assert store.get(job.id).progress == ["b", "c"]

    def test_update_progress_refuses_terminal_job(self, store: JobStore):
        """A worker's trailing write must not resurrect a finished job."""
        job = store.create("infra", "task")
        store.finish(job.id, DispatchResult(agent="infra", success=True, result="done"))
        assert store.update_progress(job.id, ["late line"]) is None
        assert store.get(job.id).progress is None

    def test_update_progress_unknown_returns_none(self, store: JobStore):
        assert store.update_progress("nope", ["x"]) is None

    def test_progress_survives_finish(self, store: JobStore):
        """finish() re-reads from disk, so the last progress tail is kept."""
        job = store.create("infra", "task")
        store.mark_running(job.id)
        store.update_progress(job.id, ["step 1", "step 2"])
        store.finish(job.id, DispatchResult(agent="infra", success=True, result="done"))
        reread = store.get(job.id)
        assert reread.status == "done"
        assert reread.progress == ["step 1", "step 2"]


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


class TestJobIdValidation:
    def test_valid_uuid_hex_accepted(self):
        assert is_valid_job_id("a" * 32)
        assert is_valid_job_id("0123456789abcdef0123456789abcdef")

    def test_real_create_id_is_valid(self, store: JobStore):
        job = store.create("infra", "t")
        assert is_valid_job_id(job.id)
        assert len(job.id) == 32

    def test_invalid_ids_rejected(self):
        for bad in (
            "",
            "short",
            "../secret",
            "../../etc/passwd",
            "ABCDEF0123456789abcdef0123456789",  # uppercase
            "g" * 32,  # non-hex
            "a" * 31,  # too short
            "a" * 33,  # too long
            "0123456789abcdef0123456789abcde/",
        ):
            assert not is_valid_job_id(bad), bad

    def test_get_rejects_traversal_outside_dir(self, store: JobStore, tmp_path: Path):
        """A crafted ref must not read a Job-shaped file outside the jobs dir."""
        # Plant a valid Job JSON one level above the jobs directory.
        secret = store.directory.parent / "secret_target.json"
        planted = Job(id="a" * 32, agent="leak", task="SENSITIVE")
        secret.write_text(planted.model_dump_json(), encoding="utf-8")

        # The traversal ref that, naively joined, would resolve to `secret`.
        assert store.get("../secret_target") is None
        assert store.get("../../etc/passwd") is None

    def test_path_raises_on_invalid_id(self, store: JobStore):
        with pytest.raises(ValueError, match="Invalid job_id"):
            store._path("../escape")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
class TestJobFilePermissions:
    def test_job_file_is_owner_only(self, store: JobStore):
        job = store.create("infra", "task with maybe SECRET")
        path = store.directory / f"{job.id}.json"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_jobs_dir_is_owner_only(self, store: JobStore):
        mode = stat.S_IMODE(store.directory.stat().st_mode)
        assert mode == 0o700, oct(mode)
        assert not (store.directory.stat().st_mode & stat.S_IROTH)


class TestJobCancel:
    def test_cancel_pending_marks_cancelled(self, store: JobStore):
        job = store.create("infra", "task")
        result, outcome = store.cancel(job.id)
        assert outcome == "cancelled"
        assert result is not None
        assert result.status == "cancelled"
        assert result.completed_at is not None
        # Persisted
        assert store.get(job.id).status == "cancelled"

    def test_cancel_running_is_refused(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        result, outcome = store.cancel(job.id)
        assert outcome == "running"
        assert result is not None
        assert result.status == "running"  # untouched

    def test_cancel_terminal_is_noop(self, store: JobStore):
        job = store.create("infra", "task")
        store.finish(job.id, DispatchResult(agent="infra", success=True, result="done"))
        result, outcome = store.cancel(job.id)
        assert outcome == "already_terminal"
        assert result.status == "done"

    def test_cancel_missing_returns_not_found(self, store: JobStore):
        result, outcome = store.cancel("f" * 32)
        assert result is None
        assert outcome == "not_found"

    def test_mark_running_refuses_cancelled_job(self, store: JobStore):
        """The cancel/run race is closed: a cancelled job never starts."""
        job = store.create("infra", "task")
        store.cancel(job.id)
        assert store.mark_running(job.id) is None
        assert store.get(job.id).status == "cancelled"


class TestRecoverStale:
    def test_recovers_old_running_job(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        # Backdate started_at well past the threshold.
        updated = store.get(job.id)
        updated.started_at = time.time() - 7200
        store._write(updated)

        recovered = store.recover_stale(stale_threshold_seconds=3600)
        assert recovered == 1
        after = store.get(job.id)
        assert after.status == "failed"
        assert "Abandoned" in (after.error or "")

    def test_leaves_recent_running_job(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        recovered = store.recover_stale(stale_threshold_seconds=3600)
        assert recovered == 0
        assert store.get(job.id).status == "running"

    def test_ignores_terminal_jobs(self, store: JobStore):
        job = store.create("infra", "task")
        store.finish(job.id, DispatchResult(agent="infra", success=True, result="done"))
        assert store.recover_stale(stale_threshold_seconds=0) == 0

    def test_planted_malformed_id_file_not_counted(self, store: JobStore):
        """A hand-planted running file with a bad id must not crash or be counted."""
        planted = Job(id="../evil", agent="x", task="t", status="running",
                      started_at=time.time() - 7200)
        (store.directory / "planted.json").write_text(
            planted.model_dump_json(), encoding="utf-8"
        )
        # Does not raise; the malformed id can't be transitioned, so it's not counted.
        assert store.recover_stale(stale_threshold_seconds=3600) == 0


class TestCreateCompleted:
    def test_create_completed_success(self, store: JobStore):
        result = DispatchResult(
            agent="infra", success=True, result="ok-text", cost_usd=0.03,
        )
        job = store.create_completed("infra", "task", result, caller="api")
        assert job.status == "done"
        assert job.completed_at is not None
        assert job.started_at is not None
        assert job.result is not None
        assert job.result.cost_usd == 0.03
        assert job.caller == "api"
        # Persisted
        reread = store.get(job.id)
        assert reread is not None
        assert reread.status == "done"

    def test_create_completed_failure(self, store: JobStore):
        result = DispatchResult(
            agent="infra", success=False, result="",
            error="boom", error_type="cli_error",
        )
        job = store.create_completed("infra", "task", result)
        assert job.status == "failed"
        assert job.error == "boom"


class TestForceCancel:
    def test_force_cancels_running_job(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        result, outcome = store.cancel(job.id, force=True)
        assert outcome == "cancelled_running"
        assert result.status == "cancelled"
        assert result.completed_at is not None
        assert "killed" in result.error
        assert store.get(job.id).status == "cancelled"

    def test_force_on_pending_is_plain_cancel(self, store: JobStore):
        job = store.create("infra", "task")
        result, outcome = store.cancel(job.id, force=True)
        assert outcome == "cancelled"
        assert result.status == "cancelled"

    def test_force_on_terminal_is_noop(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        store.finish(job.id, DispatchResult(agent="infra", success=True, result="done"))
        result, outcome = store.cancel(job.id, force=True)
        assert outcome == "already_terminal"
        assert result.status == "done"


class TestTerminalProtection:
    """finish/fail must not overwrite a terminal job (cancel race)."""

    def test_finish_refuses_cancelled_job(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        store.cancel(job.id, force=True)
        out = store.finish(
            job.id, DispatchResult(agent="infra", success=True, result="late"),
        )
        assert out is None
        reread = store.get(job.id)
        assert reread.status == "cancelled"
        assert reread.result is None  # the late result was discarded

    def test_fail_refuses_cancelled_job(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        store.cancel(job.id, force=True)
        assert store.fail(job.id, "late crash") is None
        reread = store.get(job.id)
        assert reread.status == "cancelled"
        assert "killed" in reread.error  # cancel's error survives

    def test_finish_refuses_done_job(self, store: JobStore):
        job = store.create("infra", "task")
        store.mark_running(job.id)
        store.finish(job.id, DispatchResult(agent="infra", success=True, result="first"))
        out = store.finish(
            job.id, DispatchResult(agent="infra", success=False, result="second"),
        )
        assert out is None
        assert store.get(job.id).result.result == "first"

    def test_fail_still_works_on_running(self, store: JobStore):
        # recover_stale depends on fail() accepting running jobs
        job = store.create("infra", "task")
        store.mark_running(job.id)
        assert store.fail(job.id, "stale") is not None
        assert store.get(job.id).status == "failed"


class TestDefaultJobsDir:
    def test_env_override(self, monkeypatch, tmp_path: Path):
        from agent_dispatch.jobs import default_jobs_dir
        monkeypatch.setenv("AGENT_DISPATCH_JOBS_DIR", str(tmp_path / "custom"))
        assert default_jobs_dir() == tmp_path / "custom"

    def test_defaults_next_to_config(self, monkeypatch, tmp_path: Path):
        from agent_dispatch.jobs import default_jobs_dir
        monkeypatch.delenv("AGENT_DISPATCH_JOBS_DIR", raising=False)
        monkeypatch.setenv("AGENT_DISPATCH_CONFIG", str(tmp_path / "cfg" / "agents.yaml"))
        assert default_jobs_dir() == tmp_path / "cfg" / "jobs"
