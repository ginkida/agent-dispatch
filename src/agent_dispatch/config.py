"""Configuration loading and saving."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import yaml

from .models import DispatchConfig

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "agent-dispatch"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "agents.yaml"


def config_path() -> Path:
    """Return config path, respecting AGENT_DISPATCH_CONFIG env var."""
    return Path(os.environ.get("AGENT_DISPATCH_CONFIG", str(DEFAULT_CONFIG_PATH)))


_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.02


def _acquire_lock(lock_path: Path) -> int | None:
    """Take an exclusive advisory lock, or return None if that's not possible."""
    if fcntl is None:  # pragma: no cover - Windows
        return None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:  # pragma: no cover - unwritable dir; caller still proceeds
        logger.debug("Could not open lock file %s: %s", lock_path, e)
        return None
    # Bounded, non-blocking retry rather than a plain blocking LOCK_EX: the MCP
    # server takes this lock on its event-loop thread, so one wedged holder
    # (a stopped CLI process, a stale NFS lock) would freeze every tool in the
    # server indefinitely. After the deadline we proceed unlocked — a possible
    # lost update is a far smaller failure than a permanent freeze — and say so.
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                logger.warning(
                    "Could not acquire %s within %ss — proceeding without it; "
                    "a concurrent edit could be lost.",
                    lock_path,
                    _LOCK_TIMEOUT_SECONDS,
                )
                os.close(fd)
                return None
            time.sleep(_LOCK_POLL_SECONDS)


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
    except OSError as e:  # pragma: no cover - best effort
        logger.debug("Could not unlock fd %s: %s", fd, e)
    finally:
        os.close(fd)


class ProcessLock:
    """Re-entrant lock that serializes a read-modify-write across threads *and* processes.

    ``agents.yaml`` and the job files are edited by two independent programs —
    the MCP server and the ``agent-dispatch`` CLI — and every mutation is a
    load / mutate / save-whole-file cycle. A ``threading.RLock`` only orders
    writers inside one process, so without this the later writer silently
    discards the earlier one's change (add two agents concurrently and one
    disappears). Uses ``flock`` on *path*; where flock is unavailable (Windows,
    exotic filesystems) it degrades to thread-only locking rather than failing
    the operation, exactly like ``_chmod_quiet``.

    Re-entrant on purpose: ``JobStore.recover_stale`` holds the lock while
    calling ``fail()``, and a second ``flock`` on a fresh descriptor in the same
    process would block forever.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._thread_lock = threading.RLock()
        self._fd: int | None = None
        self._depth = 0

    def __enter__(self) -> ProcessLock:
        self._thread_lock.acquire()
        if self._depth == 0:
            self._fd = _acquire_lock(self._path)
        self._depth += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            _release_lock(self._fd)
            self._fd = None
        self._thread_lock.release()


_config_locks: dict[str, ProcessLock] = {}
_config_locks_guard = threading.Lock()


def config_lock() -> ProcessLock:
    """Lock guarding the active config file — wrap load+mutate+save in it.

    One instance per config path: two ``ProcessLock`` objects for the same file
    inside one process would each take their own descriptor and deadlock if
    nested, so the instance is memoized.
    """
    key = str(config_path())
    with _config_locks_guard:
        lock = _config_locks.get(key)
        if lock is None:
            if len(_config_locks) > 32:  # tests churn through temp config paths
                _config_locks.clear()
            lock = ProcessLock(Path(f"{key}.lock"))
            _config_locks[key] = lock
        return lock


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """One-shot advisory lock on ``<path>.lock`` (non-re-entrant helper)."""
    fd = _acquire_lock(Path(f"{path}.lock"))
    try:
        yield
    finally:
        _release_lock(fd)


def load_config(path: Path | None = None) -> DispatchConfig:
    """Load config from YAML file. Returns empty config if file missing."""
    p = path or config_path()
    if not p.exists():
        return DispatchConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if raw is None:
        return DispatchConfig()
    return DispatchConfig.model_validate(raw)


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort chmod. Silently ignores platforms/filesystems without it."""
    try:
        os.chmod(path, mode)
    except OSError as e:  # pragma: no cover - platform dependent
        logger.debug("chmod %s to %o failed: %s", path, mode, e)


def save_config(config: DispatchConfig, path: Path | None = None) -> None:
    """Save config to YAML file (owner-only perms — it records project paths)."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod_quiet(p.parent, 0o700)
    # Follow a symlinked config to its target. os.replace() below swaps the
    # *link itself*, so writing through it (as the old write_text did) is what
    # users pointing agents.yaml at a dotfiles repo expect; the temp file also
    # has to sit next to the real file to keep the rename same-filesystem.
    p = Path(os.path.realpath(p))
    data = config.model_dump(mode="json", exclude_none=True)
    # capabilities/risky_capabilities default to [] (not None), so exclude_none
    # won't drop them — prune empties so agents that never declare capabilities
    # stay clean in YAML instead of growing two empty-list keys on every save.
    for agent_data in data.get("agents", {}).values():
        for key in ("capabilities", "risky_capabilities"):
            if not agent_data.get(key):
                agent_data.pop(key, None)
    # `groups` also defaults to {} (not None), so exclude_none keeps it — prune
    # the whole block when empty, and drop empty per-group member lists. Empty
    # LISTS only, mirroring capabilities; empty strings (description /
    # shared_context / use_for) are kept, like AgentConfig.description.
    for group_data in data.get("groups", {}).values():
        if not group_data.get("members"):
            group_data.pop("members", None)
    if not data.get("groups"):
        data.pop("groups", None)
    rendered = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    # Atomic replace, like JobStore._write. Writing in place would truncate the
    # live file first: an interrupted write (ENOSPC, quota, SIGKILL) would leave
    # a half-written agents.yaml that no longer parses — losing every agent and
    # group at once. The temp name is unique per write so a concurrent writer
    # (CLI vs MCP server) can never publish an interleaved file.
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(rendered, encoding="utf-8")
        _chmod_quiet(tmp, 0o600)  # owner-only before it becomes visible
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            logger.debug("Failed to clean up temp config %s", tmp)
        raise


def _collect_mcp_servers(directory: Path) -> list[str]:
    """Collect MCP server names from all known config locations."""
    servers: list[str] = []
    for path in (
        directory / ".mcp.json",
        directory / ".claude" / "settings.local.json",
    ):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("top-level JSON value is not an object")
                configured = data.get("mcpServers", {})
                if not isinstance(configured, dict):
                    raise ValueError("mcpServers is not an object")
                servers.extend(str(name) for name in configured)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                logger.debug("Failed to parse MCP config: %s", path)
    return list(dict.fromkeys(servers))  # deduplicate, preserve order


# Public alias — callers outside config.py should use this name.
collect_mcp_servers = _collect_mcp_servers


def detect_stacks(directory: Path) -> list[str]:
    """Detect language/runtime stacks present in a project directory.

    Returns a deduplicated list of indicators like ["Python", "Docker"].
    Used by auto_describe() and by the MCP list_agents tool to surface
    capabilities cheaply (no claude subprocess needed).
    """
    indicators: list[str] = []
    if (directory / "Dockerfile").exists():
        indicators.append("Docker")
    if (directory / "docker-compose.yaml").exists() or (directory / "docker-compose.yml").exists():
        indicators.append("Docker Compose")
    if (directory / "Cargo.toml").exists():
        indicators.append("Rust")
    if (directory / "go.mod").exists():
        indicators.append("Go")
    if (directory / "requirements.txt").exists() or (directory / "pyproject.toml").exists():
        indicators.append("Python")
    if (directory / "package.json").exists():
        indicators.append("Node.js")
    return indicators


def detect_dbs(directory: Path) -> list[str]:
    """Detect database-related artifacts: Prisma, Alembic, generic migrations dir."""
    indicators: list[str] = []
    if (directory / "prisma").is_dir() or (directory / "schema.prisma").exists():
        indicators.append("Prisma")
    if (directory / "alembic").is_dir() or (directory / "alembic.ini").exists():
        indicators.append("Alembic")
    if (directory / "migrations").is_dir():
        indicators.append("migrations")
    return indicators


def auto_describe(directory: Path) -> str:
    """Generate agent description by reading project files.

    Produces a string like:
      MCP server for cross-project agent delegation | MCP: portainer, postgres | Python, Docker
    """
    parts: list[str] = []

    # CLAUDE.md — first meaningful lines (up to 2 sentences)
    claude_md = directory / "CLAUDE.md"
    if claude_md.exists():
        sentences: list[str] = []
        try:
            lines = claude_md.read_text(encoding="utf-8").strip().splitlines()[:40]
        except (OSError, UnicodeDecodeError):
            logger.debug("Failed to read CLAUDE.md: %s", claude_md)
            lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith("--"):
                sentences.append(stripped)
                if len(sentences) >= 2:
                    break
        if sentences:
            parts.append(" ".join(sentences))

    # README.md — fallback if no CLAUDE.md description
    if not parts:
        readme = directory / "README.md"
        if readme.exists():
            try:
                lines = readme.read_text(encoding="utf-8").strip().splitlines()[:20]
            except (OSError, UnicodeDecodeError):
                logger.debug("Failed to read README.md: %s", readme)
                lines = []
            for line in lines:
                stripped = line.strip()
                if (
                    stripped
                    and not stripped.startswith("#")
                    and not stripped.startswith("[")
                    and not stripped.startswith("!")
                    and len(stripped) > 20
                ):
                    parts.append(stripped)
                    break

    # pyproject.toml — project description
    pyproject = directory / "pyproject.toml"
    if pyproject.exists():
        try:
            lines = pyproject.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            logger.debug("Failed to read pyproject.toml: %s", pyproject)
            lines = []
        for line in lines:
            if line.strip().startswith("description"):
                _, separator, value = line.partition("=")
                if not separator:
                    continue
                desc = value.strip().strip('"').strip("'")
                if desc:
                    parts.append(desc)
                break

    # package.json — project description
    pkg_json = directory / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            if isinstance(pkg, dict):
                desc = pkg.get("description")
                if isinstance(desc, str) and desc.strip():
                    parts.append(desc)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.debug("Failed to parse package.json: %s", pkg_json)

    # MCP servers — critical for understanding what tools this agent has
    servers = _collect_mcp_servers(directory)
    if servers:
        parts.append(f"MCP: {', '.join(servers)}")

    # Stack indicators (Python/Node/Rust/Go/Docker)
    stacks = detect_stacks(directory)
    if stacks:
        parts.append(f"Stack: {', '.join(stacks)}")

    # Database indicators (Prisma/Alembic/migrations)
    dbs = detect_dbs(directory)
    if dbs:
        parts.append(f"DB: {', '.join(dbs)}")

    return " | ".join(parts) if parts else f"Agent in {directory.name}"
