"""Append-only dispatch journal: one line per dispatch, plus aggregation.

Why this exists: a dispatch costs real money and real minutes, and until now
nothing recorded either. `cost_usd` and `duration_ms` came back on the
DispatchResult and were then thrown away unless the caller happened to print
them — so "which agent burns money", "how long does this agent actually take"
and "how often do agents come back `partial`" had no answer, and the only
timing knowledge in the system was hand-written prose in agent descriptions.

Design constraints, in order:

1. **It must never break a dispatch.** Every public function swallows its own
   errors: a full disk, a read-only config dir or a corrupt line degrades the
   journal, never the work. Callers do not check a return value.
2. **It must survive concurrent writers.** The CLI and every `agent-dispatch
   serve` process (this user routinely has 14+ alive) append to one file with
   no lock between them. Each record is written with a *single* `os.write` to
   a fd opened `O_APPEND`, and is capped at `_MAX_LINE_BYTES` — well under the
   4096-byte `PIPE_BUF` boundary below which POSIX append writes do not
   interleave. A lock would be the wrong tool: this write sits on the MCP
   server's event-loop thread.
3. **It must stay bounded.** JSONL grows forever otherwise. `_rotate_if_large`
   renames the journal to `.1` (atomic, so a reader never sees a half file)
   once it passes the size cap, keeping at most two generations. Reads only
   ever pull the tail (`_TAIL_BYTES`), so aggregation cost does not grow with
   history either.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# One record must fit in a single atomic append. PIPE_BUF is 4096 on Linux and
# macOS; half of that leaves room for the JSON envelope of a long agent name or
# caller without ever approaching the boundary.
_MAX_LINE_BYTES = 2048
# Aggregation reads only the tail: median/p90 over the last few thousand
# dispatches is what "how long does this agent take" means, and it keeps
# `inspect_agent` from reading megabytes on every call.
_TAIL_BYTES = 512 * 1024
# Rotate at ~10k records. Two generations max, so the journal is bounded at
# roughly 4 MB total no matter how long the install lives.
_MAX_JOURNAL_BYTES = 2_000_000

# Discovery (`list_agents` / `inspect_agent`) reads a much shorter window than
# the `stats` report: it runs on the MCP server's event-loop thread, where it
# blocks every other tool, and a median/p90 over the last ~1000 dispatches is
# both enough and more current than one over the whole file. Measured on a
# full 2 MB journal: 24.8 ms at 512 KB, 6 ms here, ~0 on the memoized repeat.
_PROFILE_TAIL_BYTES = 128 * 1024

_CALLER_MAX_CHARS = 60

# Plausibility ceilings for journal numbers. A dispatch is capped at 7200 s by
# `timeout_seconds`, and no single run costs a million dollars; these are far
# above any real value and far below where a sum of ~10k records could leave
# the finite range, so the report is finite by construction.
_MAX_PLAUSIBLE_MS = 1e9
_MAX_PLAUSIBLE_COST_USD = 1e6

# Memo of the last profile computation, keyed by the journal's identity and
# fingerprint (size + mtime). The file only changes when a dispatch finishes,
# and an append always changes its size — so a stale hit is not reachable,
# while a discovery pass that calls list_agents and then inspect_agent pays
# the parse once. One slot, not a dict: there is one journal.
_profile_memo: tuple[tuple, dict[str, dict]] | None = None


def _fingerprint(path: Path) -> tuple:
    """Identity of the journal as it is right now (path, size, mtime)."""
    try:
        st = path.stat()
        return (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return (str(path), -1, -1)


def journal_path() -> Path:
    """Journal path: $AGENT_DISPATCH_USAGE_LOG override or <config dir>/usage.jsonl."""
    override = os.environ.get("AGENT_DISPATCH_USAGE_LOG")
    if override:
        return Path(override)
    from .config import config_path

    return config_path().parent / "usage.jsonl"


def _rotate_if_large(path: Path, max_bytes: int) -> None:
    """Move the journal aside once it passes *max_bytes*. Best-effort."""
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    try:
        # os.replace is atomic: a concurrent reader sees either the old file or
        # the new empty one, never a truncated mix. Two processes rotating at
        # once is harmless — the second rename simply wins.
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError as e:  # pragma: no cover - platform/permission dependent
        logger.debug("Could not rotate usage journal %s: %s", path, e)


def record(
    agent: str,
    *,
    ok: bool,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    num_turns: int | None = None,
    outcome: str | None = None,
    error_type: str | None = None,
    cached: bool = False,
    caller: str | None = None,
    path: Path | None = None,
) -> None:
    """Append one dispatch record. Never raises, never blocks on a lock."""
    try:
        entry: dict[str, object] = {
            "t": int(time.time()),
            "agent": str(agent)[:100],
            "ok": bool(ok),
        }
        if cost_usd is not None:
            cost = round(float(cost_usd), 6)
            if math.isfinite(cost):
                entry["cost"] = cost
        if duration_ms is not None:
            entry["ms"] = int(duration_ms)
        if num_turns is not None:
            entry["turns"] = int(num_turns)
        if outcome:
            entry["outcome"] = str(outcome)[:20]
        if error_type:
            entry["err"] = str(error_type)[:40]
        if cached:
            entry["cached"] = True
        if caller:
            entry["caller"] = str(caller)[:_CALLER_MAX_CHARS]
        line = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > _MAX_LINE_BYTES:  # pragma: no cover - fields are capped above
            # Atomicity beats completeness: drop the optional text fields rather
            # than emit a write that another process could interleave with.
            entry.pop("caller", None)
            line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
            if len(line) > _MAX_LINE_BYTES:
                return
        p = path or journal_path()
        _rotate_if_large(p, _MAX_JOURNAL_BYTES)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # O_APPEND + one write() is the whole concurrency story (see module docstring).
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001 - a journal must never break a dispatch
        logger.debug("Could not record usage for %s: %s", agent, e)


def as_float(value: object, default: float = 0.0) -> float:
    """Coerce a field read off disk to a finite float, or return the default.

    Declared once and shared with `servers.py` and the CLI because all three
    read numbers out of files a user can edit and a torn write can mangle.
    `float("abc")` raises **ValueError**, which is not caught by a
    `json.JSONDecodeError` handler and is not an `OSError` either — the same
    escape route that has produced two rounds of bugs in this codebase. A
    record with a broken timestamp must be skipped or sorted last, never
    turned into a traceback from the command you ran *because* something is
    wrong.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _read_tail(path: Path, max_bytes: int) -> list[str]:
    """Last *max_bytes* of a file as whole lines (a partial first line is dropped)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                partial = True
            else:
                partial = False
            raw = fh.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if partial and lines:
        lines = lines[1:]  # the seek landed mid-record
    return lines


def load(
    *,
    days: int = 0,
    agent: str | None = None,
    path: Path | None = None,
    max_bytes: int = _TAIL_BYTES,
) -> list[dict]:
    """Read journal records, newest generation last. Corrupt lines are skipped.

    ``days=0`` means "everything in the tail". Reads the rotated ``.1``
    generation too, so a window that predates a rotation is still complete.
    """
    p = path or journal_path()
    lines = _read_tail(p.with_name(p.name + ".1"), max_bytes) + _read_tail(p, max_bytes)
    cutoff = time.time() - days * 86400 if days > 0 else None
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # a torn write or hand-edit must not break the report
        if not isinstance(entry, dict) or "agent" not in entry:
            continue
        if cutoff is not None and as_float(entry.get("t")) < cutoff:
            continue
        if agent is not None and entry.get("agent") != agent:
            continue
        out.append(entry)
    return out


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a NON-empty, unsorted list."""
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def _stats(entries: list[dict]) -> dict:
    """Aggregate one agent's (or everything's) records into a summary dict."""
    live = [e for e in entries if not e.get("cached")]

    def values(field: str, ceiling: float) -> list[float]:
        # Finite is not enough: two corrupted lines of 1e308 are each finite
        # and sum to inf, which `stats --json` prints as `Infinity` (not JSON)
        # and the text report as `$inf`. Anything outside [0, ceiling) is a
        # mangled record, not a measurement, so it is dropped here, at the read.
        numbers = (as_float(e.get(field), default=math.nan) for e in live)
        return [number for number in numbers if 0 <= number < ceiling]

    durations = values("ms", _MAX_PLAUSIBLE_MS)
    costs = values("cost", _MAX_PLAUSIBLE_COST_USD)
    outcomes: dict[str, int] = {}
    errors: dict[str, int] = {}
    ok = 0
    for e in live:
        if e.get("ok"):
            ok += 1
        if isinstance(e.get("outcome"), str):
            outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
        if isinstance(e.get("err"), str):
            errors[e["err"]] = errors.get(e["err"], 0) + 1
    summary: dict = {
        "dispatches": len(live),
        "cached_hits": len(entries) - len(live),
        "ok": ok,
        "failed": len(live) - ok,
        "cost_usd": round(math.fsum(costs), 4) if costs else 0.0,
    }
    if durations:
        summary["median_ms"] = int(_percentile(durations, 50))
        summary["p90_ms"] = int(_percentile(durations, 90))
        summary["max_ms"] = int(max(durations))
    if costs:
        summary["median_cost_usd"] = round(_percentile(costs, 50), 4)
        summary["max_cost_usd"] = round(max(costs), 4)
    if outcomes:
        summary["outcomes"] = dict(sorted(outcomes.items()))
    if errors:
        summary["errors"] = dict(sorted(errors.items()))
    return summary


def summarize(entries: list[dict]) -> dict:
    """Totals plus a per-agent breakdown, agents sorted by spend then volume."""
    by_agent: dict[str, list[dict]] = {}
    for e in entries:
        by_agent.setdefault(str(e.get("agent")), []).append(e)
    agents = {name: _stats(rows) for name, rows in by_agent.items()}
    ordered = dict(
        sorted(
            agents.items(),
            key=lambda kv: (-kv[1]["cost_usd"], -kv[1]["dispatches"], kv[0]),
        )
    )
    return {"total": _stats(entries), "agents": ordered}


def _profile(stats: dict, min_samples: int) -> dict | None:
    """The compact "what this agent usually costs" block, or None on thin data.

    Below *min_samples* live dispatches there is no typical anything — two data
    points would invite a caller to size a timeout off noise, which is worse
    than telling it nothing.
    """
    if stats["dispatches"] < min_samples:
        return None
    profile: dict = {"dispatches": stats["dispatches"]}
    if "median_ms" in stats:
        profile["median_seconds"] = round(stats["median_ms"] / 1000, 1)
        profile["p90_seconds"] = round(stats["p90_ms"] / 1000, 1)
    if "median_cost_usd" in stats:
        profile["median_cost_usd"] = stats["median_cost_usd"]
    if stats["failed"]:
        profile["failed"] = stats["failed"]
    if "outcomes" in stats:
        profile["outcomes"] = stats["outcomes"]
    return profile


def agent_profile(
    agent: str,
    *,
    days: int = 0,
    path: Path | None = None,
    min_samples: int = 3,
) -> dict | None:
    """One agent's timing/cost profile, or None when there is too little data."""
    return profiles(days=days, path=path, min_samples=min_samples).get(agent)


def profiles(
    *,
    days: int = 0,
    path: Path | None = None,
    min_samples: int = 3,
) -> dict[str, dict]:
    """Every agent's profile from a SINGLE journal read, memoized per journal state.

    Every profile consumer goes through here — `list_agents` enriches all agents
    at once and `inspect_agent`/`suggested_timeout` ask for one — so the parse
    happens once per journal change, not once per agent and not once per call.
    """
    global _profile_memo
    p = path or journal_path()
    key = (_fingerprint(p), days, min_samples)
    memo = _profile_memo
    if memo is not None and memo[0] == key:
        return memo[1]
    try:
        entries = load(days=days, path=p, max_bytes=_PROFILE_TAIL_BYTES)
    except Exception as e:  # noqa: BLE001 - discovery must not fail on a bad journal
        logger.debug("Could not read usage journal: %s", e)
        return {}
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(str(entry.get("agent")), []).append(entry)
    out = {}
    for name, rows in grouped.items():
        profile = _profile(_stats(rows), min_samples)
        if profile:
            out[name] = profile
    _profile_memo = (key, out)
    return out


def suggested_timeout(agent: str, *, path: Path | None = None) -> int | None:
    """A timeout that would have covered p90 of this agent's real runs, or None.

    Used to turn "increase the timeout" into a number. Rounded up to 30s and
    padded 50% over p90, because the point is to stop the *next* run timing out.
    """
    profile = agent_profile(agent, path=path)
    if not profile or "p90_seconds" not in profile:
        return None
    padded = profile["p90_seconds"] * 1.5
    return max(30, int((padded + 29) // 30 * 30))
