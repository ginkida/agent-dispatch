"""Registry of live `agent-dispatch serve` processes and the code they run.

Why: every Claude Code session spawns its own stdio MCP server, and a Python
process holds its modules in memory for life. Upgrading the package therefore
changes nothing for any session already open — on this machine 18 servers were
found still executing a previous release, some of them days old, with no way to
tell which ones. `doctor` can now say it.

Liveness is decided by an advisory lock, not by the PID. Each server writes
`<config dir>/servers/<pid>.json` and holds an exclusive `flock` on it for its
whole lifetime; a reader that *succeeds* in taking that lock is holding a file
whose owner is gone, and deletes it. Checking `os.kill(pid, 0)` instead would
mistake a recycled PID for a live server, and would keep believing a server that
was SIGKILLed (no chance to clean up) is still there. Where `fcntl` is missing
(Windows) the PID check is the fallback, with that caveat.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Kept alive for the process lifetime: closing this fd would drop the flock and
# make a live server look dead to `doctor`.
_registration_fd: int | None = None


def servers_dir() -> Path:
    """Registry directory: $AGENT_DISPATCH_SERVERS_DIR or <config dir>/servers."""
    override = os.environ.get("AGENT_DISPATCH_SERVERS_DIR")
    if override:
        return Path(override)
    from .config import config_path

    return config_path().parent / "servers"


def register(version: str, *, directory: Path | None = None) -> bool:
    """Announce this process as a live server. Best-effort; never raises.

    Returns True when the entry was written and locked.
    """
    global _registration_fd
    try:
        if _registration_fd is not None:
            # Re-registering (a test, or a server that restarts in-process)
            # must not leak the old descriptor: the lock lives on the fd, so a
            # leaked one would keep a deleted entry looking alive forever.
            os.close(_registration_fd)
            _registration_fd = None
        d = directory or servers_dir()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = d / f"{os.getpid()}.json"
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "version": version,
                "started_at": time.time(),
                "config": str(_config_path_str()),
            }
        ).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:  # pragma: no cover - a PID cannot collide with itself
                os.close(fd)
                return False
        os.write(fd, payload)
        # Deliberately NOT closed — the lock lives as long as the fd does.
        _registration_fd = fd
        return True
    except Exception as e:  # noqa: BLE001 - a diagnostics file must not stop the server
        logger.debug("Could not register server process: %s", e)
        return False


def _config_path_str() -> str:
    from .config import config_path

    return str(config_path())


def _is_stale(path: Path) -> bool:
    """True when nobody holds this entry's lock any more (owner is gone)."""
    if fcntl is None:  # pragma: no cover - Windows
        try:
            pid = int(json.loads(path.read_text())["pid"])
            os.kill(pid, 0)
            return False
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return True
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False  # someone holds it: a live server
    else:
        return True
    finally:
        os.close(fd)


def live_servers(*, directory: Path | None = None, prune: bool = True) -> list[dict]:
    """Live server entries, newest first. Stale files are deleted when *prune*.

    Never raises: a broken registry is a diagnostics problem, not an outage.
    """
    out: list[dict] = []
    try:
        d = directory or servers_dir()
        entries = sorted(d.glob("*.json"))
    except OSError:
        return []
    for path in entries:
        try:
            if _is_stale(path):
                if prune:
                    path.unlink(missing_ok=True)
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("pid"):
                out.append(data)
        except (OSError, ValueError, json.JSONDecodeError):
            # An unreadable or half-written entry tells us nothing; a locked one
            # is live but unparseable, so leave the file alone and skip it.
            continue
    out.sort(key=lambda e: float(e.get("started_at") or 0), reverse=True)
    return out


def version_drift(current: str, *, directory: Path | None = None) -> dict[str, int]:
    """Live servers whose version differs from *current*, counted by version."""
    drift: dict[str, int] = {}
    for entry in live_servers(directory=directory):
        version = str(entry.get("version") or "unknown")
        if version != current:
            drift[version] = drift.get(version, 0) + 1
    return dict(sorted(drift.items()))
