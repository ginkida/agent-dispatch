"""Async dispatch jobs: persistent job state on disk.

Each job is stored as a JSON file at `<jobs_dir>/<job_id>.json`.  Workers
update the file as the job transitions through pending -> running -> done
or failed.  Atomic writes via os.replace() so partial files never appear.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .models import DispatchResult

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "done", "failed", "cancelled"]
_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})

# Job IDs are uuid4().hex — 32 lowercase hex chars. Anything else (notably
# values containing "/" or "..") is rejected so a caller-supplied ref/job_id
# can never escape the jobs directory via path traversal.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_job_id(job_id: str) -> bool:
    """Return True if *job_id* is a well-formed uuid4 hex string."""
    return isinstance(job_id, str) and bool(_JOB_ID_RE.match(job_id))


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort chmod. Silently ignores platforms/filesystems without it."""
    try:
        os.chmod(path, mode)
    except OSError as e:  # pragma: no cover - platform dependent
        logger.debug("chmod %s to %o failed: %s", path, mode, e)


class Job(BaseModel):
    """Persistent record of an async dispatch."""

    id: str
    agent: str
    task: str
    context: str | None = None
    caller: str | None = None
    goal: str | None = None
    status: JobStatus = "pending"
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result: DispatchResult | None = None
    error: str | None = None

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


class JobStore:
    """Thread-safe persistent store of dispatch jobs."""

    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser()
        # Owner-only (0o700): job files hold full task/context/result payloads
        # that may contain secrets — keep them off other local users' radar.
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod_quiet(self.directory, 0o700)
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        # Defense-in-depth: validate before building any path so a crafted
        # job_id ("../../etc/foo") can never resolve outside self.directory.
        if not is_valid_job_id(job_id):
            raise ValueError(f"Invalid job_id: {job_id!r}")
        return self.directory / f"{job_id}.json"

    def _write(self, job: Job) -> None:
        path = self._path(job.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(job.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        _chmod_quiet(tmp, 0o600)  # owner-only before it becomes visible
        os.replace(tmp, path)

    def create(
        self,
        agent: str,
        task: str,
        *,
        context: str | None = None,
        caller: str | None = None,
        goal: str | None = None,
    ) -> Job:
        """Create a new pending job, persist it, return it."""
        job = Job(
            id=uuid.uuid4().hex,
            agent=agent,
            task=task,
            context=context,
            caller=caller,
            goal=goal,
        )
        with self._lock:
            self._write(job)
        return job

    def create_completed(
        self,
        agent: str,
        task: str,
        result: DispatchResult,
        *,
        context: str | None = None,
        caller: str | None = None,
        goal: str | None = None,
    ) -> Job:
        """Persist a synchronous dispatch result as an already-finished job.

        Used by ``return_ref`` mode so callers can fetch the full result later
        via ``fetch_result(ref)`` without keeping the text in their context.
        """
        now = time.time()
        job = Job(
            id=uuid.uuid4().hex,
            agent=agent,
            task=task,
            context=context,
            caller=caller,
            goal=goal,
            status="done" if result.success else "failed",
            started_at=now,
            completed_at=now,
            result=result,
            error=result.error if not result.success else None,
        )
        with self._lock:
            self._write(job)
        return job

    def get(self, job_id: str) -> Job | None:
        """Read a job by id. Returns None if not found, invalid, or unreadable."""
        if not is_valid_job_id(job_id):
            # Malformed/hostile id (e.g. path traversal attempt) — treat as
            # "not found" without touching the filesystem.
            logger.debug("Rejecting malformed job_id: %r", job_id)
            return None
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            return Job.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("Failed to read job %s: %s", job_id, e)
            return None

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """List all jobs, optionally filtered by status. Sorted by created_at desc."""
        jobs: list[Job] = []
        for path in self.directory.glob("*.json"):
            try:
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                logger.debug("Skipping unreadable job file %s: %s", path, e)
                continue
            if status is None or job.status == status:
                jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def mark_running(self, job_id: str) -> Job | None:
        """Mark a pending job as running.

        Returns the updated job, or None if the job is missing OR has already
        been cancelled. Refusing to run a cancelled job closes the race with
        ``cancel()``: both take ``self._lock``, so whichever wins, the worker
        either sees ``cancelled`` (and skips) or sets ``running`` first (and
        cancel then refuses).
        """
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None
            if job.status == "cancelled":
                return None
            job.status = "running"
            job.started_at = time.time()
            self._write(job)
            return job

    def cancel(self, job_id: str) -> tuple[Job | None, str]:
        """Attempt to cancel a job.

        Only *pending* jobs can be cancelled — a running job's subprocess is
        already in flight and is left to finish. Returns ``(job, outcome)``
        where outcome is one of: ``cancelled`` (was pending, now cancelled),
        ``running`` (in flight, untouched), ``already_terminal`` (done/failed/
        already cancelled), or ``not_found``.
        """
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None, "not_found"
            if job.is_terminal():
                return job, "already_terminal"
            if job.status == "running":
                return job, "running"
            # pending -> cancelled
            job.status = "cancelled"
            job.completed_at = time.time()
            job.error = "Cancelled before execution"
            self._write(job)
            return job, "cancelled"

    def recover_stale(self, stale_threshold_seconds: float = 3600) -> int:
        """Mark jobs stuck in 'running' beyond the threshold as failed.

        Async workers are daemon threads — if the server dies mid-dispatch the
        job file is left in ``running`` forever. Call this on startup to flip
        such orphans to ``failed`` so callers don't poll them indefinitely.
        Returns the count recovered.
        """
        now = time.time()
        recovered = 0
        with self._lock:
            for job in self.list(status="running"):
                age = now - (job.started_at or job.created_at)
                if age > stale_threshold_seconds:
                    # Count only jobs we actually transitioned (fail() returns
                    # None for a missing/malformed id, e.g. a planted file).
                    if self.fail(
                        job.id,
                        f"Abandoned in 'running' for {age:.0f}s — likely a "
                        "server restart. Re-dispatch if still needed.",
                    ) is not None:
                        recovered += 1
        return recovered

    def finish(
        self,
        job_id: str,
        result: DispatchResult,
    ) -> Job | None:
        """Mark job as done/failed based on result.success. Persist the result."""
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None
            job.status = "done" if result.success else "failed"
            job.completed_at = time.time()
            job.result = result
            if not result.success:
                job.error = result.error
            self._write(job)
            return job

    def fail(self, job_id: str, error: str) -> Job | None:
        """Mark job as failed with an error message (no DispatchResult)."""
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None
            job.status = "failed"
            job.completed_at = time.time()
            job.error = error
            self._write(job)
            return job

    def gc(self, max_age_seconds: float) -> int:
        """Delete terminal jobs whose completed_at is older than max_age_seconds.

        Pending/running jobs are never deleted (they may still be active).
        Returns the count of deleted jobs.
        """
        cutoff = time.time() - max_age_seconds
        deleted = 0
        with self._lock:
            for path in self.directory.glob("*.json"):
                try:
                    job = Job.model_validate_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not job.is_terminal():
                    continue
                # Use completed_at if set, else created_at as fallback
                ts = job.completed_at or job.created_at
                if ts < cutoff:
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError as e:
                        logger.warning("Failed to gc job %s: %s", job.id, e)
        return deleted
