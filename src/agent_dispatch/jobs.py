"""Async dispatch jobs: persistent job state on disk.

Each job is stored as a JSON file at `<jobs_dir>/<job_id>.json`.  Workers
update the file as the job transitions through pending -> running -> done
or failed.  Atomic writes via os.replace() so partial files never appear.
"""

from __future__ import annotations

import logging
import os
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
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.json"

    def _write(self, job: Job) -> None:
        path = self._path(job.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(job.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
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
        """Read a job by id. Returns None if not found or unreadable."""
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
        """Mark job as running. Returns updated job or None if not found."""
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None
            job.status = "running"
            job.started_at = time.time()
            self._write(job)
            return job

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
