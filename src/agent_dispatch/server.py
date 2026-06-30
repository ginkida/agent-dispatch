"""MCP server: exposes list_agents, dispatch, dispatch_session tools."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import queue
import sys
import threading
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import runner
from .cache import DispatchCache
from .config import (
    auto_describe,
    collect_mcp_servers,
    detect_dbs,
    detect_stacks,
    load_config,
    save_config,
)
from .jobs import JobStore, default_jobs_dir, is_valid_job_id
from .models import (
    AgentConfig,
    DispatchConfig,
    DispatchResult,
    Settings,
    check_permission_mode,
    validate_agent_name,
)

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "agent-dispatch",
    instructions=(
        "This server lets you delegate tasks to Claude Code agents in other project "
        "directories. Each agent has its own MCP servers, CLAUDE.md, and tools.\n\n"
        "WHEN TO DISPATCH: Use dispatch when a task needs tools, files, or context "
        "from another project — database queries, container logs, API calls, reading "
        "code you don't have access to. Don't dispatch for things you can do yourself.\n\n"
        "HOW TO USE:\n"
        "1. list_agents() — see who's available and what they can do\n"
        "2. inspect_agent(name) — cheap detailed lookup (MCP, stack, CLAUDE.md preview)\n"
        "3. dispatch(agent, task) — one-shot delegation (cached)\n"
        "4. dispatch_session(agent, task, session_id?) — multi-turn conversation\n"
        "5. dispatch_parallel(dispatches, aggregate?) — concurrent tasks\n"
        "6. dispatch_stream(agent, task) — live progress updates\n"
        "7. dispatch_dialogue(requester, responder, topic) — two agents collaborate\n"
        "8. Always pass caller= (your project name) and goal= (why you need this)\n\n"
        "GROUPS (coordinate a set of related projects):\n"
        "- list_groups() — see configured groups (code repos + gateway agents)\n"
        "- inspect_group(name) — the group's brief + members + shared facts\n"
        "- dispatch(agent, task, group=name) — agent must be a member; the "
        "group's shared_context (stack names, ids, conventions) is auto-prepended "
        "to your context. Works on dispatch and per-item in dispatch_parallel.\n\n"
        "TIMEOUTS: pass timeout_seconds= per call for known-long tasks (no config "
        "edit needed). If a dispatch times out anyway, the error includes a "
        "session_id — resume the partial work via dispatch_session(agent, "
        "'Continue where you left off', session_id=...) instead of restarting.\n\n"
        "PERMISSIONS: a result may include denied_tools + hint — the agent "
        "finished but some tool calls were blocked, so the answer may be "
        "incomplete. Grant access via update_agent(allowed_tools=...) or "
        "permission_mode='bypassPermissions', then re-dispatch.\n\n"
        "ASYNC DISPATCH (don't block on long tasks):\n"
        "- dispatch_async(agent, task) — fire-and-forget, returns job_id\n"
        "- dispatch_status(job_id) — check state + live progress tail, non-blocking\n"
        "- dispatch_wait(job_id, timeout?) — block until done (or timeout)\n"
        "- dispatch_cancel(job_id) — cancel a pending job, or kill a running one\n"
        "- dispatch_jobs(status?) — list recent async jobs\n\n"
        "SAVING CONTEXT for big results:\n"
        "- dispatch(..., return_ref=True) — returns just ref+summary, not full text\n"
        "- fetch_result(ref, max_chars?) — load the full text only when needed\n\n"
        "MANAGING AGENTS:\n"
        "- add_agent(name, directory) — register a project, auto-generates description\n"
        "- update_agent(name, ...) — change permissions, timeout, model, etc.\n"
        "- remove_agent(name) — unregister an agent\n"
        "- cache_stats() / cache_clear() — monitor and manage result cache"
    ),
)

_cache: DispatchCache | None = None
_semaphore: asyncio.Semaphore | None = None
_semaphore_limit: int = 0
_job_store: JobStore | None = None
_job_semaphore: threading.BoundedSemaphore | None = None
_job_semaphore_limit: int = 0
# Live Popen handles of running async jobs, keyed by job_id. In-memory on
# purpose: only the server that spawned a subprocess can kill it safely (a
# PID persisted to disk could be reused by an unrelated process after a
# restart). Registered by the worker via runner's on_proc callback.
_running_procs: dict[str, Any] = {}
_running_procs_lock = threading.Lock()

_RESOLVED_MARKER = "[RESOLVED]"
_JOB_GC_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
_DEFAULT_SUMMARY_CHARS = 500
_MAX_SUMMARY_CHARS = 100_000  # upper bound for return_ref summaries
_MAX_JOBS_LIMIT = 1000  # upper bound for dispatch_jobs pagination
_STALE_RUNNING_SECONDS = 3600  # recover jobs stuck 'running' longer than this
_MIN_TIMEOUT_OVERRIDE = 10  # seconds; floor for per-call timeout_seconds
_MAX_TIMEOUT_OVERRIDE = 7200  # seconds; ceiling for per-call timeout_seconds
_JOB_PROGRESS_MAX_LINES = 20  # rolling tail kept in the job file
_JOB_PROGRESS_WRITE_INTERVAL = 1.0  # seconds between progress file writes


def _get_config() -> DispatchConfig:
    """Load config fresh each call so new agents are picked up immediately."""
    return load_config()


def _get_cache(config: DispatchConfig) -> DispatchCache | None:
    """Return the global cache instance, creating it on first call."""
    global _cache  # noqa: PLW0603
    cache_cfg = config.settings.cache
    if not cache_cfg.enabled:
        return None
    if _cache is None or _cache._ttl != cache_cfg.ttl or _cache._max_size != cache_cfg.max_size:
        _cache = DispatchCache(ttl=cache_cfg.ttl, max_size=cache_cfg.max_size)
    return _cache


def _get_semaphore(config: DispatchConfig) -> asyncio.Semaphore:
    """Return concurrency-limiting semaphore, recreated if limit changes."""
    global _semaphore, _semaphore_limit  # noqa: PLW0603
    limit = config.settings.max_concurrency
    if _semaphore is None or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_limit = limit
    return _semaphore


def _apply_timeout(agent_config: AgentConfig, timeout_seconds: int) -> AgentConfig:
    """Return the agent config with a per-call timeout override applied.

    ``timeout_seconds <= 0`` means "use the agent's configured timeout" —
    the config is returned untouched. Positive values are clamped to
    [_MIN_TIMEOUT_OVERRIDE, _MAX_TIMEOUT_OVERRIDE] and applied via a copy so
    the shared config object is never mutated. Not part of the cache key:
    a result computed under one timeout is just as valid under another.
    """
    if timeout_seconds <= 0:
        return agent_config
    clamped = max(_MIN_TIMEOUT_OVERRIDE, min(int(timeout_seconds), _MAX_TIMEOUT_OVERRIDE))
    return agent_config.model_copy(update={"timeout": clamped})


def _ref_payload(
    job_id: str,
    result: DispatchResult,
    summary_chars: int = _DEFAULT_SUMMARY_CHARS,
) -> dict:
    """Build the compact reference response for return_ref=True dispatches.

    Caller gets just enough to know what happened; fetch_result(ref) reads
    the full text on demand. Keeps the calling agent's context small.
    """
    chars = max(0, min(int(summary_chars), _MAX_SUMMARY_CHARS))
    text = result.result or ""
    size = len(text)
    summary = text[:chars] if chars else ""
    payload: dict = {
        "ref": job_id,
        "agent": result.agent,
        "success": result.success,
        "size": size,
        "summary_chars": min(size, chars) if chars else 0,
        "summary": summary,
    }
    if result.session_id:
        payload["session_id"] = result.session_id
    if result.cost_usd is not None:
        payload["cost_usd"] = result.cost_usd
    if result.duration_ms is not None:
        payload["duration_ms"] = result.duration_ms
    if result.num_turns is not None:
        payload["num_turns"] = result.num_turns
    if result.error:
        payload["error"] = result.error
    if result.error_type:
        payload["error_type"] = result.error_type
    if result.denied_tools:
        payload["denied_tools"] = result.denied_tools
    if result.hint:
        payload["hint"] = result.hint
    if result.parsed_result is not None:
        # Parsed JSON is structured and typically small — include it inline so
        # the caller can use it without a fetch round-trip.
        payload["parsed_result"] = result.parsed_result
    return payload


def _validate_agent(config: DispatchConfig, name: str) -> str | None:
    """Return an error JSON string if the agent doesn't exist, else None."""
    if name not in config.agents:
        available = ", ".join(config.agents.keys()) or "(none configured)"
        return json.dumps({"error": f"Unknown agent: {name!r}. Available: {available}"})
    return None


def _validate_group(config: DispatchConfig, name: str) -> str | None:
    """Return an error JSON string if the group doesn't exist, else None."""
    if name not in config.groups:
        available = ", ".join(config.groups.keys()) or "(none configured)"
        return json.dumps({"error": f"Unknown group: {name!r}. Available: {available}"})
    return None


def _validate_group_member(config: DispatchConfig, group: str, agent: str) -> str | None:
    """Return an error JSON string if (group, agent) is not a valid membership.

    Validates the group exists and `agent` is one of its members. A str|None
    validator (not a value-returning helper) so it plugs into the established
    walrus idiom: ``if err := _validate_group_member(...): return err``. The
    actual shared_context merge is a separate pure function (_merge_group_context)
    — this split keeps dispatch_parallel's up-front-rejection contract intact.
    """
    if err := _validate_group(config, group):
        return err
    members = [m.agent for m in config.groups[group].members]
    if agent not in members:
        if not members:
            return json.dumps(
                {
                    "error": f"Group {group!r} has no members yet. Add some with "
                    "'agent-dispatch group add' or by editing agents.yaml."
                }
            )
        return json.dumps(
            {
                "error": f"Agent {agent!r} is not a member of group {group!r}. "
                f"Members: {', '.join(members)}"
            }
        )
    return None


def _merge_group_context(config: DispatchConfig, group: str, context: str | None) -> str | None:
    """Fold a group's member-facing shared_context into the per-call context.

    Returns the effective context string (or None when both are empty). The
    group's shared_context is prepended as a labeled block so the member knows
    these are group-wide facts. Pure value function — assumes membership was
    already validated by the caller. Folding into the context STRING (rather
    than a new prompt section or cache-key field) keeps runner.py and cache.py
    untouched and the cache key correct: a different shared_context yields a
    different effective context and therefore a different key, while group=""
    leaves the context byte-identical to today (full backward compatibility).
    """
    shared = config.groups[group].shared_context.strip() if group in config.groups else ""
    ctx = (context or "").strip()
    if shared and ctx:
        return f"Shared context (group {group!r}):\n{shared}\n\n{ctx}"
    if shared:
        return f"Shared context (group {group!r}):\n{shared}"
    return context or None


def _validate_ref(ref: str) -> str | None:
    """Return an error JSON string if *ref* is not a valid job id, else None.

    Job ids/refs are uuid4 hex (32 hex chars). Rejecting anything else at the
    tool boundary blocks path-traversal attempts (``../../secret``) before they
    reach the JobStore.
    """
    if not is_valid_job_id(ref):
        return json.dumps(
            {"error": f"Invalid ref/job_id format: {ref!r} (expected a 32-char hex id)"}
        )
    return None


def _parse_csv(value: str) -> list[str]:
    return [t.strip() for t in value.split(",") if t.strip()]


def _get_job_store() -> JobStore:
    """Return the global JobStore instance, creating it on first call."""
    global _job_store  # noqa: PLW0603
    if _job_store is None:
        _job_store = JobStore(default_jobs_dir())
    return _job_store


def _get_job_semaphore(config: DispatchConfig) -> threading.BoundedSemaphore:
    """Threading semaphore for async jobs, recreated if max_concurrency changes."""
    global _job_semaphore, _job_semaphore_limit  # noqa: PLW0603
    limit = config.settings.max_concurrency
    if _job_semaphore is None or _job_semaphore_limit != limit:
        _job_semaphore = threading.BoundedSemaphore(limit)
        _job_semaphore_limit = limit
    return _job_semaphore


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_agents(ctx: Context | None = None) -> str:
    """List all configured agents with descriptions and health status.

    Call this first to see which agents are available and what they can do.
    Use the agent name in dispatch() or dispatch_session() calls.
    """
    config = _get_config()
    if not config.agents:
        return json.dumps(
            {"error": "No agents configured. Run: agent-dispatch add <name> <directory>"},
            indent=2,
        )

    agents = []
    for name, agent in config.agents.items():
        # is_dir() can raise OSError (PermissionError, network FS hiccup, etc.).
        # Fall back to "UNREADABLE" so a single broken agent doesn't crash the
        # whole listing — per CLAUDE.md's documented response shape.
        mcp_servers: list[str] = []
        stacks: list[str] = []
        dbs: list[str] = []
        try:
            healthy: bool | str = agent.directory.is_dir()
            has_claude_md = (agent.directory / "CLAUDE.md").exists() if healthy else False
            has_mcp_config = (agent.directory / ".mcp.json").exists() if healthy else False
            if healthy:
                # Cheap I/O: scans a handful of well-known files in the agent dir.
                # Surface these so callers can pick the right agent without
                # dispatching a probe.
                try:
                    mcp_servers = collect_mcp_servers(agent.directory)
                    stacks = detect_stacks(agent.directory)
                    dbs = detect_dbs(agent.directory)
                except OSError:
                    pass
        except OSError:
            healthy = "UNREADABLE"
            has_claude_md = False
            has_mcp_config = False
        entry: dict = {
            "name": name,
            "directory": str(agent.directory),
            "description": agent.description,
            "healthy": healthy,
            "has_claude_md": has_claude_md,
            "has_mcp_config": has_mcp_config,
        }
        if mcp_servers:
            entry["mcp_servers"] = mcp_servers
        if stacks:
            entry["stacks"] = stacks
        if dbs:
            entry["dbs"] = dbs
        if agent.permission_mode:
            entry["permission_mode"] = agent.permission_mode
        # Include when explicitly set (even []) to distinguish from inheriting defaults
        if agent.allowed_tools is not None:
            entry["allowed_tools"] = agent.allowed_tools
        if agent.disallowed_tools is not None:
            entry["disallowed_tools"] = agent.disallowed_tools
        if agent.capabilities:
            entry["capabilities"] = agent.capabilities
        if agent.risky_capabilities:
            entry["risky_capabilities"] = agent.risky_capabilities
        agents.append(entry)
    if ctx:
        await ctx.info(f"Found {len(agents)} configured agents")
    return json.dumps(agents, indent=2)


def _read_preview(path, max_lines: int, max_chars: int) -> tuple[str, bool]:
    """Read a small preview from a file. Returns (text, truncated)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    preview = "\n".join(lines)
    if len(preview) > max_chars:
        preview = preview[:max_chars]
        truncated = True
    return preview, truncated


@mcp.tool()
async def inspect_agent(
    name: str,
    preview_lines: int = 40,
    ctx: Context | None = None,
) -> str:
    """Inspect an agent's project without dispatching a claude session.

    Reads the agent's directory cheaply (no subprocess) and returns:
    directory, description, config fields, detected MCP servers/stacks/DBs,
    plus short previews of CLAUDE.md and README.md when present.

    Use this BEFORE dispatch_async/dispatch to confirm an agent has the
    right tools and context for your task — much cheaper than a probe
    dispatch.

    Args:
        name: Agent name (from list_agents).
        preview_lines: Max lines of CLAUDE.md / README.md to include
            (default 40, capped at 200). Pass 0 to omit previews.
    """
    config = _get_config()
    if err := _validate_agent(config, name):
        return err

    agent = config.agents[name]
    info: dict = {
        "name": name,
        "directory": str(agent.directory),
        "description": agent.description,
        "timeout": agent.timeout,
    }
    if agent.model:
        info["model"] = agent.model
    if agent.max_budget_usd is not None:
        info["max_budget_usd"] = agent.max_budget_usd
    if agent.permission_mode:
        info["permission_mode"] = agent.permission_mode
    if agent.allowed_tools is not None:
        info["allowed_tools"] = agent.allowed_tools
    if agent.disallowed_tools is not None:
        info["disallowed_tools"] = agent.disallowed_tools
    if agent.capabilities:
        info["capabilities"] = agent.capabilities
    if agent.risky_capabilities:
        info["risky_capabilities"] = agent.risky_capabilities

    try:
        healthy = agent.directory.is_dir()
    except OSError:
        info["healthy"] = "UNREADABLE"
        return json.dumps(info, indent=2)

    info["healthy"] = healthy
    if not healthy:
        return json.dumps(info, indent=2)

    try:
        info["mcp_servers"] = collect_mcp_servers(agent.directory)
        info["stacks"] = detect_stacks(agent.directory)
        info["dbs"] = detect_dbs(agent.directory)
    except OSError as e:
        info["scan_error"] = str(e)

    info["has_claude_md"] = (agent.directory / "CLAUDE.md").exists()
    info["has_readme"] = (agent.directory / "README.md").exists()
    info["has_mcp_config"] = (agent.directory / ".mcp.json").exists()

    lines = max(0, min(int(preview_lines), 200))
    if lines > 0:
        char_cap = max(200, lines * 200)  # roughly 200 chars/line cap
        if info["has_claude_md"]:
            text, truncated = _read_preview(agent.directory / "CLAUDE.md", lines, char_cap)
            info["claude_md_preview"] = text
            if truncated:
                info["claude_md_truncated"] = True
        if info["has_readme"]:
            text, truncated = _read_preview(agent.directory / "README.md", lines, char_cap)
            info["readme_preview"] = text
            if truncated:
                info["readme_truncated"] = True

    if ctx:
        await ctx.info(f"Inspected {name} ({agent.directory})")

    return json.dumps(info, indent=2)


@mcp.tool()
async def list_groups(ctx: Context | None = None) -> str:
    """List configured agent groups — cross-project working sets.

    A group bundles related agents (code projects + capability gateways such as
    infra or analytics) so one orchestrating session can coordinate work across
    them. This is a cheap, descriptive readout: it never spawns a claude
    subprocess and does no routing — you pick members by reading their hints.

    Then: inspect_group(name) for the full brief + members, and
    dispatch(agent, task, group=name) to auto-attach the group's shared facts.
    """
    config = _get_config()
    if not config.groups:
        return json.dumps(
            {"error": "No groups configured. Create one: agent-dispatch group add <name>"},
            indent=2,
        )

    groups = []
    for name, grp in config.groups.items():
        members = []
        for m in grp.members:
            entry: dict = {"agent": m.agent}
            if m.use_for:
                entry["use_for"] = m.use_for
            # Flag dangling refs (agent removed) instead of crashing — never
            # touch config.agents[m.agent] when it's unknown.
            if m.agent not in config.agents:
                entry["unknown"] = True
            else:
                try:
                    entry["healthy"] = config.agents[m.agent].directory.is_dir()
                except OSError:
                    entry["healthy"] = "UNREADABLE"
            members.append(entry)
        groups.append(
            {
                "name": name,
                "description": grp.description,
                "shared_context_present": bool(grp.shared_context.strip()),
                "member_count": len(grp.members),
                "members": members,
            }
        )
    if ctx:
        await ctx.info(f"Found {len(groups)} configured groups")
    return json.dumps(groups, indent=2)


@mcp.tool()
async def inspect_group(name: str, ctx: Context | None = None) -> str:
    """Inspect one group: its orchestration brief, shared facts, and members.

    Returns the orchestrator-facing ``description`` (how to coordinate the
    group), the full member-facing ``shared_context`` (the facts auto-injected
    into dispatches made with group=), and the member list with each agent's
    ``use_for`` hint + health. For a deep dive on a specific member, call
    inspect_agent(member) — this tool deliberately stays a cheap membership +
    brief readout and does not duplicate inspect_agent's per-project scan.

    Args:
        name: Group name (from list_groups).
    """
    config = _get_config()
    if err := _validate_group(config, name):
        return err

    grp = config.groups[name]
    members = []
    for m in grp.members:
        entry: dict = {"agent": m.agent}
        if m.use_for:
            entry["use_for"] = m.use_for
        if m.agent not in config.agents:
            entry["unknown"] = True
        else:
            agent = config.agents[m.agent]
            entry["directory"] = str(agent.directory)
            if agent.description:
                entry["description"] = agent.description
            try:
                entry["healthy"] = agent.directory.is_dir()
            except OSError:
                entry["healthy"] = "UNREADABLE"
        members.append(entry)

    info = {
        "name": name,
        "description": grp.description,
        "shared_context": grp.shared_context,
        "member_count": len(grp.members),
        "members": members,
    }
    if ctx:
        await ctx.info(f"Inspected group {name} ({len(members)} members)")
    return json.dumps(info, indent=2)


@mcp.tool()
async def dispatch(
    agent: str,
    task: str,
    context: str = "",
    caller: str = "",
    goal: str = "",
    response_format: str = "",
    return_ref: bool = False,
    summary_chars: int = _DEFAULT_SUMMARY_CHARS,
    timeout_seconds: int = 0,
    group: str = "",
    ctx: Context | None = None,
) -> str:
    """Delegate a task to an agent in another project directory.

    The agent runs as a separate Claude Code session with its own MCP servers,
    CLAUDE.md, and project context. Results are cached by default.

    Args:
        agent: Name of the agent (from list_agents).
        task: The task to perform. Be specific and self-contained.
        context: Optional extra context — error messages, code snippets, etc.
        caller: Who is dispatching (your project/role) — helps the agent
            understand the request.
        goal: The broader objective this task serves — the agent can make
            better trade-offs when it knows *why*.
        response_format: ``"json"`` to ask the agent for a single JSON value.
            On success the parsed value is returned in ``parsed_result``.
            Leave empty for free-form text (default).
        return_ref: When True, persist the full result and return only a
            compact reference (ref id + summary preview + metadata). Use
            fetch_result(ref) to load the full text later. Saves caller
            context when the result is large.
        summary_chars: Max chars of the result text to include in the ref
            response (default 500; only relevant when return_ref=True).
        timeout_seconds: One-off timeout override for this call (0 = use the
            agent's configured timeout). Clamped to [10, 7200]. Use for tasks
            you know are long instead of editing the agent config. If a
            dispatch still times out, the error includes a session_id you can
            resume via dispatch_session.
        group: Optional group name (from list_groups). When set, the agent must
            be a member of the group, and the group's shared_context (member-
            facing facts) is auto-prepended to ``context``. Leave empty for a
            plain dispatch.
    """
    config = _get_config()
    if err := _validate_agent(config, agent):
        return err
    if group and (err := _validate_group_member(config, group, agent)):
        return err

    rf = response_format or None
    # Fold the group's shared facts into the context once, then reuse the same
    # string at every cache/runner/persist site below — store-under-merged /
    # fetch-under-raw would otherwise make every group dispatch a cache miss.
    effective_context = _merge_group_context(config, group, context) if group else (context or None)

    # Check cache. caller/goal/response_format are part of the key because
    # they change the prompt sent to Claude and therefore the response.
    cache = _get_cache(config)
    if cache:
        cached = cache.get(
            agent,
            task,
            effective_context,
            caller or None,
            goal or None,
            rf,
        )
        if cached:
            if ctx:
                await ctx.info(f"Cache hit for {agent} — returning cached result")
            cached_dict = json.loads(cached.model_dump_json(indent=2, exclude_none=True))
            cached_dict["cached"] = True
            return json.dumps(cached_dict, indent=2)

    agent_config = _apply_timeout(config.agents[agent], timeout_seconds)
    if ctx:
        await ctx.info(f"Dispatching to {agent}: {task[:80]}...")

    async with _get_semaphore(config):
        result = await asyncio.to_thread(
            runner.dispatch,
            agent,
            task,
            agent_config,
            config.settings,
            effective_context,
            caller=caller or None,
            goal=goal or None,
            response_format=rf,
        )

    # Populate cache
    if cache:
        cache.put(
            agent,
            task,
            result,
            effective_context,
            caller or None,
            goal or None,
            rf,
        )

    if return_ref:
        store = _get_job_store()
        job = store.create_completed(
            agent,
            task,
            result,
            context=effective_context,
            caller=caller or None,
            goal=goal or None,
        )
        return json.dumps(_ref_payload(job.id, result, summary_chars), indent=2)

    return result.model_dump_json(indent=2, exclude_none=True)


@mcp.tool()
async def dispatch_session(
    agent: str,
    task: str,
    session_id: str = "",
    context: str = "",
    caller: str = "",
    goal: str = "",
    response_format: str = "",
    timeout_seconds: int = 0,
    ctx: Context | None = None,
) -> str:
    """Multi-turn dispatch: continue a conversation with an agent.

    First call without session_id starts a new session. Use the returned
    session_id in subsequent calls to continue the conversation — the agent
    retains full context from previous turns.

    Also the recovery path for timeouts: when a dispatch times out, its error
    includes a session_id — pass it here with task="Continue where you left
    off" to salvage the partial work instead of restarting from scratch.

    Session dispatches are never cached because each turn builds on the prior.

    Args:
        agent: Name of the agent.
        task: The task or follow-up message.
        session_id: Session ID from a previous call (empty for new session).
        context: Optional extra context.
        caller: Who is dispatching.
        goal: The broader objective.
        timeout_seconds: One-off timeout override (0 = agent default;
            clamped to [10, 7200]).
    """
    config = _get_config()
    if err := _validate_agent(config, agent):
        return err

    agent_config = _apply_timeout(config.agents[agent], timeout_seconds)
    if ctx:
        turn = "new session" if not session_id else f"resuming {session_id[:12]}..."
        await ctx.info(f"Dispatching to {agent} ({turn}): {task[:80]}...")

    async with _get_semaphore(config):
        result = await asyncio.to_thread(
            runner.dispatch,
            agent,
            task,
            agent_config,
            config.settings,
            context or None,
            session_id or None,
            caller=caller or None,
            goal=goal or None,
            response_format=response_format or None,
        )
    return result.model_dump_json(indent=2, exclude_none=True)


@mcp.tool()
async def dispatch_parallel(
    dispatches: str,
    aggregate: str = "",
    ctx: Context | None = None,
) -> str:
    """Run multiple dispatch tasks in parallel and return all results at once.

    Much faster than sequential dispatch() calls when you need answers from
    several agents — all subprocesses run concurrently.

    Args:
        dispatches: JSON array of requests, each with "agent", "task", and
            optional "context", "caller", "goal", "response_format",
            "return_ref", "summary_chars", "timeout_seconds", "group" (the
            agent must be a member; its shared_context is auto-injected).
            Example:
            [
              {"agent": "infra", "task": "check pod logs for errors"},
              {"agent": "db", "task": "are all migrations applied?", "timeout_seconds": 900}
            ]
        aggregate: Optional agent name. When set, after all dispatches
            complete their results are sent to this agent for synthesis
            into a single coherent answer.
    """
    try:
        items = json.loads(dispatches)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in dispatches: {e}"})

    if not isinstance(items, list) or not items:
        return json.dumps({"error": "dispatches must be a non-empty JSON array"})

    config = _get_config()

    # Cap fan-out: each item spawns a claude subprocess. Without a bound a
    # caller could submit thousands of items and pile up coroutines/processes.
    max_items = max(100, config.settings.max_concurrency * 20)
    if len(items) > max_items:
        return json.dumps(
            {
                "error": f"Too many dispatches: {len(items)} > {max_items}. "
                "Split into multiple calls or raise settings.max_concurrency.",
            }
        )

    cache = _get_cache(config)

    # Validate structure and agents up front (including aggregator)
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return json.dumps(
                {"error": f"dispatches[{i}] must be an object, got {type(item).__name__}"}
            )
        if "agent" not in item or "task" not in item:
            return json.dumps({"error": f"dispatches[{i}] must have 'agent' and 'task' keys"})
        if err := _validate_agent(config, item["agent"]):
            return err
        # Group membership is validated up front too — keeps the documented
        # contract that one bad item rejects the whole call before any paid
        # subprocess runs. The actual shared_context merge happens in _run_one.
        if item.get("group") and (
            err := _validate_group_member(config, str(item["group"]), item["agent"])
        ):
            return err
        # Numeric fields are coerced inside _run_one — validate here so a bad
        # value rejects the whole call before any dispatch runs (per contract),
        # instead of surfacing as a cryptic per-item int() error.
        for field in ("timeout_seconds", "summary_chars"):
            if item.get(field) is not None:  # JSON null = "not set"
                try:
                    int(item[field])
                except (TypeError, ValueError):
                    return json.dumps(
                        {
                            "error": f"dispatches[{i}].{field} must be a number, "
                            f"got {item[field]!r}",
                        }
                    )
    if aggregate:
        if err := _validate_agent(config, aggregate):
            return err

    if ctx:
        names = ", ".join(item["agent"] for item in items)
        await ctx.info(f"Dispatching in parallel to: {names}")

    async def _run_one(item: dict[str, Any]) -> dict[str, Any]:
        name = item["agent"]
        task = item["task"]
        item_context = item.get("context") or None
        item_group = str(item["group"]) if item.get("group") else ""
        # Fold group shared_context once, reuse at every cache/runner/persist
        # site below (see dispatch()). Membership was validated up front.
        effective_context = (
            _merge_group_context(config, item_group, item_context) if item_group else item_context
        )
        item_caller = item.get("caller") or None
        item_goal = item.get("goal") or None
        item_rf = item.get("response_format") or None
        item_return_ref = bool(item.get("return_ref"))
        raw_summary = item.get("summary_chars")
        if raw_summary is None:  # absent or JSON null — use the default; 0 stays 0
            raw_summary = _DEFAULT_SUMMARY_CHARS
        item_summary_chars = max(0, min(int(raw_summary), _MAX_SUMMARY_CHARS))
        item_timeout = int(item.get("timeout_seconds") or 0)

        # Check cache (caller/goal/response_format are part of the key — see dispatch())
        if cache:
            cached = cache.get(
                name,
                task,
                effective_context,
                item_caller,
                item_goal,
                item_rf,
            )
            if cached:
                if item_return_ref:
                    store = _get_job_store()
                    job = store.create_completed(
                        name,
                        task,
                        cached,
                        context=effective_context,
                        caller=item_caller,
                        goal=item_goal,
                    )
                    payload = _ref_payload(job.id, cached, item_summary_chars)
                    payload["cached"] = True
                    return payload
                d = json.loads(cached.model_dump_json(exclude_none=True))
                d["cached"] = True
                return d

        agent_config = _apply_timeout(config.agents[name], item_timeout)
        async with _get_semaphore(config):
            result = await asyncio.to_thread(
                runner.dispatch,
                name,
                task,
                agent_config,
                config.settings,
                effective_context,
                caller=item_caller,
                goal=item_goal,
                response_format=item_rf,
            )

        if cache:
            cache.put(
                name,
                task,
                result,
                effective_context,
                item_caller,
                item_goal,
                item_rf,
            )

        if item_return_ref:
            store = _get_job_store()
            job = store.create_completed(
                name,
                task,
                result,
                context=effective_context,
                caller=item_caller,
                goal=item_goal,
            )
            return _ref_payload(job.id, result, item_summary_chars)

        return json.loads(result.model_dump_json(exclude_none=True))

    results = await asyncio.gather(*[_run_one(item) for item in items], return_exceptions=True)

    output = []
    for item, res in zip(items, results, strict=True):
        if isinstance(res, Exception):
            output.append(
                {
                    "agent": item["agent"],
                    "success": False,
                    "result": "",
                    "error": str(res),
                    "error_type": "cli_error",
                }
            )
        else:
            output.append(res)

    # ---- Aggregation ----
    if not aggregate:
        return json.dumps(output, indent=2)

    # Build a summary for the aggregator agent
    parts = []
    for item, res in zip(items, output, strict=True):
        status = "OK" if res.get("success") else "FAILED"
        body = res.get("result") or res.get("error", "")
        parts.append(f"## Agent: {item['agent']} [{status}]\n{body}")
    summary = "\n\n".join(parts)

    if ctx:
        await ctx.info(f"Aggregating results via {aggregate}...")

    agg_task = (
        "Synthesize the results below into a single coherent answer. Highlight key "
        "findings, note any conflicts between agents, and provide actionable conclusions."
    )
    agg_config = config.agents[aggregate]
    async with _get_semaphore(config):
        agg_result = await asyncio.to_thread(
            runner.dispatch,
            aggregate,
            agg_task,
            agg_config,
            config.settings,
            summary,
            caller="dispatch_parallel",
            goal="aggregate parallel dispatch results",
        )

    return json.dumps(
        {
            "individual_results": output,
            "aggregated": json.loads(agg_result.model_dump_json(exclude_none=True)),
        },
        indent=2,
    )


@mcp.tool()
async def dispatch_stream(
    agent: str,
    task: str,
    context: str = "",
    caller: str = "",
    goal: str = "",
    response_format: str = "",
    timeout_seconds: int = 0,
    ctx: Context | None = None,
) -> str:
    """Dispatch with streaming progress — see live updates as the agent works.

    Same as dispatch() but shows intermediate progress via log messages.
    Use this for long-running tasks where you want to monitor what the agent
    is doing while it works.

    Args:
        agent: Name of the agent.
        task: The task to perform.
        context: Optional extra context.
        caller: Who is dispatching.
        goal: The broader objective.
        response_format: ``"json"`` to request structured output. Empty = text.
        timeout_seconds: One-off timeout override (0 = agent default;
            clamped to [10, 7200]).
    """
    config = _get_config()
    if err := _validate_agent(config, agent):
        return err

    agent_config = _apply_timeout(config.agents[agent], timeout_seconds)
    if ctx:
        await ctx.info(f"Dispatching (stream) to {agent}: {task[:80]}...")

    progress_queue: queue.Queue[str] = queue.Queue()

    def on_progress(msg: str) -> None:
        progress_queue.put(msg)

    async with _get_semaphore(config):
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            None,
            lambda: runner.dispatch_stream(
                agent,
                task,
                agent_config,
                config.settings,
                context or None,
                on_progress,
                caller=caller or None,
                goal=goal or None,
                response_format=response_format or None,
            ),
        )

        # Forward progress messages while the subprocess runs
        while not future.done():
            await asyncio.sleep(0.1)
            while True:
                try:
                    msg = progress_queue.get_nowait()
                except queue.Empty:
                    break
                if ctx:
                    await ctx.info(f"[{agent}] {msg[:300]}")

        result = await asyncio.wrap_future(future)

        # Drain any remaining messages
        while True:
            try:
                msg = progress_queue.get_nowait()
            except queue.Empty:
                break
            if ctx:
                await ctx.info(f"[{agent}] {msg[:300]}")

    return result.model_dump_json(indent=2, exclude_none=True)


# ---------------------------------------------------------------------------
# Agent-to-agent dialogue
# ---------------------------------------------------------------------------

_DIALOGUE_INITIAL = (
    "You are starting a collaborative dialogue with agent '{other}'.\n"
    "Provide your analysis or ask questions. When you have a complete answer "
    "and no further questions, end your response with {marker}.\n\n"
    "Topic:\n{topic}"
)

_DIALOGUE_REPLY = (
    "Agent '{other}' responds:\n\n{message}\n\n"
    "Continue the discussion. If you have a complete answer and no further "
    "questions, end your response with {marker}."
)


@mcp.tool()
async def dispatch_dialogue(
    requester: str,
    responder: str,
    topic: str,
    max_rounds: int = 3,
    ctx: Context | None = None,
) -> str:
    """Two agents collaborate through multi-turn dialogue.

    *requester* poses a problem/question, *responder* provides expertise.
    They alternate turns until one signals completion with [RESOLVED] or
    max_rounds is reached.  Each agent maintains context via session IDs
    so the conversation builds naturally.

    Cost: up to 2 dispatches per round (one per agent).

    Args:
        requester: Agent with the problem/context.
        responder: Agent with the expertise/tools to help.
        topic: The problem or question to discuss.
        max_rounds: Maximum back-and-forth rounds (default 3, max 10).
    """
    config = _get_config()
    for name in (requester, responder):
        if err := _validate_agent(config, name):
            return err

    max_rounds = max(1, min(max_rounds, 10))
    conversation: list[dict] = []
    total_cost = 0.0
    total_duration = 0
    session_responder: str | None = None
    session_requester: str | None = None
    resolved = False
    final_answer = ""

    if ctx:
        await ctx.info(f"Starting dialogue: {requester} <-> {responder} (max {max_rounds} rounds)")

    for round_num in range(1, max_rounds + 1):
        # ---- Responder turn ----
        if round_num == 1:
            resp_task = _DIALOGUE_INITIAL.format(
                other=requester, topic=topic, marker=_RESOLVED_MARKER
            )
        else:
            resp_task = _DIALOGUE_REPLY.format(
                other=requester,
                message=conversation[-1]["message"],
                marker=_RESOLVED_MARKER,
            )

        resp_config = config.agents[responder]
        async with _get_semaphore(config):
            resp_result = await asyncio.to_thread(
                runner.dispatch,
                responder,
                resp_task,
                resp_config,
                config.settings,
                session_id=session_responder,
                caller=requester,
                goal=topic[:200],
            )
        session_responder = resp_result.session_id
        total_cost += resp_result.cost_usd or 0
        total_duration += resp_result.duration_ms or 0

        resp_entry: dict = {
            "agent": responder,
            "role": "responder",
            "round": round_num,
            "message": resp_result.result,
            "cost_usd": resp_result.cost_usd,
        }
        if not resp_result.success:
            resp_entry["error"] = resp_result.error
            resp_entry["error_type"] = resp_result.error_type
        conversation.append(resp_entry)

        if ctx:
            await ctx.info(f"[round {round_num}] {responder}: {resp_result.result[:120]}...")

        if _RESOLVED_MARKER in resp_result.result or not resp_result.success:
            resolved = _RESOLVED_MARKER in resp_result.result
            final_answer = resp_result.result.replace(_RESOLVED_MARKER, "").strip()
            if not resp_result.success and resp_result.error:
                final_answer = resp_result.error
            break

        # ---- Requester turn ----
        req_task = _DIALOGUE_REPLY.format(
            other=responder,
            message=resp_result.result,
            marker=_RESOLVED_MARKER,
        )

        req_config = config.agents[requester]
        async with _get_semaphore(config):
            req_result = await asyncio.to_thread(
                runner.dispatch,
                requester,
                req_task,
                req_config,
                config.settings,
                session_id=session_requester,
                caller=responder,
                goal=topic[:200],
            )
        session_requester = req_result.session_id
        total_cost += req_result.cost_usd or 0
        total_duration += req_result.duration_ms or 0

        req_entry: dict = {
            "agent": requester,
            "role": "requester",
            "round": round_num,
            "message": req_result.result,
            "cost_usd": req_result.cost_usd,
        }
        if not req_result.success:
            req_entry["error"] = req_result.error
            req_entry["error_type"] = req_result.error_type
        conversation.append(req_entry)

        if ctx:
            await ctx.info(f"[round {round_num}] {requester}: {req_result.result[:120]}...")

        if _RESOLVED_MARKER in req_result.result or not req_result.success:
            resolved = _RESOLVED_MARKER in req_result.result
            final_answer = req_result.result.replace(_RESOLVED_MARKER, "").strip()
            if not req_result.success and req_result.error:
                final_answer = req_result.error
            break

    if not final_answer and conversation:
        final_answer = conversation[-1]["message"]

    return json.dumps(
        {
            "resolved": resolved,
            "rounds": conversation[-1]["round"] if conversation else 0,
            "total_cost_usd": round(total_cost, 4),
            "total_duration_ms": total_duration,
            "final_answer": final_answer,
            "conversation": conversation,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Agent management
# ---------------------------------------------------------------------------


@mcp.tool()
async def add_agent(
    name: str,
    directory: str,
    description: str = "",
    timeout: int = 0,
    max_budget_usd: float = 0,
    permission_mode: str = "",
    allowed_tools: str = "",
    disallowed_tools: str = "",
    capabilities: str = "",
    risky_capabilities: str = "",
    ctx: Context | None = None,
) -> str:
    """Register a project directory as a dispatchable agent.

    The directory must exist. If no description is provided, one is
    auto-generated from the project's files (CLAUDE.md, MCP servers,
    package.json/pyproject.toml, stack indicators).

    Args:
        name: Agent name (letters, digits, hyphens, underscores; must start
            with letter or digit).
        directory: Absolute path to the project directory.
        description: What this agent can do. Leave empty for auto-generation.
        timeout: Timeout in seconds (0 uses global default of 300).
        max_budget_usd: Max cost in USD per dispatch (0 or omitted = no limit).
        permission_mode: Permission mode for the claude CLI
            (e.g. default, plan, bypassPermissions). Leave empty for default.
        allowed_tools: Comma-separated list of allowed tools
            (e.g. "Bash,Read,Edit"). Leave empty for no restrictions.
        disallowed_tools: Comma-separated list of disallowed tools.
            Leave empty for no restrictions.
        capabilities: Comma-separated task labels describing what the agent is
            good at (e.g. "docker_logs,deploy_debug"). Surfaced in list_agents /
            inspect_agent to help callers pick the right agent.
        risky_capabilities: Comma-separated high-risk capability labels
            (e.g. "restart_services"). Descriptive only; surfaced for visibility.
    """
    try:
        validate_agent_name(name)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    from pathlib import Path

    dir_path = Path(directory).expanduser().resolve()
    if not dir_path.is_dir():
        return json.dumps({"error": f"Directory does not exist: {dir_path}"})

    config = _get_config()
    if name in config.agents:
        return json.dumps({"error": f"Agent '{name}' already exists. Remove it first."})

    desc = description or auto_describe(dir_path)
    parsed_allowed = _parse_csv(allowed_tools) if allowed_tools else None
    parsed_disallowed = _parse_csv(disallowed_tools) if disallowed_tools else None
    parsed_capabilities = _parse_csv(capabilities)
    parsed_risky_capabilities = _parse_csv(risky_capabilities)

    if ctx and (warning := check_permission_mode(permission_mode or None)):
        await ctx.info(f"Warning: {warning}")

    config.agents[name] = AgentConfig(
        directory=dir_path,
        description=desc,
        timeout=timeout or 300,
        max_budget_usd=max_budget_usd or None,
        permission_mode=permission_mode or None,
        allowed_tools=parsed_allowed,
        disallowed_tools=parsed_disallowed,
        capabilities=parsed_capabilities,
        risky_capabilities=parsed_risky_capabilities,
    )
    save_config(config)

    if ctx:
        await ctx.info(f"Added agent '{name}' -> {dir_path}")

    result: dict = {
        "added": name,
        "directory": str(dir_path),
        "description": desc,
    }
    if permission_mode:
        result["permission_mode"] = permission_mode
    if parsed_allowed:
        result["allowed_tools"] = parsed_allowed
    if parsed_disallowed:
        result["disallowed_tools"] = parsed_disallowed
    if parsed_capabilities:
        result["capabilities"] = parsed_capabilities
    if parsed_risky_capabilities:
        result["risky_capabilities"] = parsed_risky_capabilities

    return json.dumps(result, indent=2)


@mcp.tool()
async def remove_agent(
    name: str,
    ctx: Context | None = None,
) -> str:
    """Remove an agent from the dispatch configuration.

    Args:
        name: Agent name to remove.
    """
    config = _get_config()
    if name not in config.agents:
        available = ", ".join(config.agents.keys()) or "(none)"
        return json.dumps({"error": f"Agent '{name}' not found. Available: {available}"})

    del config.agents[name]
    save_config(config)

    if ctx:
        await ctx.info(f"Removed agent '{name}'")

    return json.dumps({"removed": name})


@mcp.tool()
async def update_agent(
    name: str,
    description: str = "",
    timeout: int = 0,
    max_budget_usd: float = 0,
    model: str = "",
    permission_mode: str = "",
    allowed_tools: str = "",
    disallowed_tools: str = "",
    capabilities: str = "",
    risky_capabilities: str = "",
    ctx: Context | None = None,
) -> str:
    """Update an existing agent's configuration.

    Only fields with non-empty values are updated. To clear a string field,
    pass "none". To clear a list field (allowed_tools, disallowed_tools),
    pass "none".

    Args:
        name: Agent name to update.
        description: New description.
        timeout: New timeout in seconds (0 = don't change).
        max_budget_usd: New max cost in USD per dispatch (0 = don't change;
            pass a negative number to clear the limit).
        model: Model override. Pass "none" to clear.
        permission_mode: Permission mode. Pass "none" to clear.
        allowed_tools: Comma-separated allowed tools. Pass "none" to clear.
        disallowed_tools: Comma-separated disallowed tools. Pass "none" to clear.
        capabilities: Comma-separated capabilities. Pass "none" to clear.
        risky_capabilities: Comma-separated risky capabilities. Pass "none"
            to clear.
    """
    config = _get_config()
    if name not in config.agents:
        available = ", ".join(config.agents.keys()) or "(none)"
        return json.dumps({"error": f"Agent '{name}' not found. Available: {available}"})

    agent = config.agents[name]
    updated: list[str] = []

    if description:
        agent.description = description
        updated.append("description")
    if timeout:
        agent.timeout = timeout
        updated.append("timeout")
    if max_budget_usd:
        agent.max_budget_usd = None if max_budget_usd < 0 else max_budget_usd
        updated.append("max_budget_usd")
    if model:
        agent.model = None if model.lower() == "none" else model
        updated.append("model")
    if permission_mode:
        effective = None if permission_mode.lower() == "none" else permission_mode
        agent.permission_mode = effective
        if ctx and (warning := check_permission_mode(effective)):
            await ctx.info(f"Warning: {warning}")
        updated.append("permission_mode")
    if allowed_tools:
        if allowed_tools.lower() == "none":
            agent.allowed_tools = None
        else:
            agent.allowed_tools = [t.strip() for t in allowed_tools.split(",") if t.strip()]
        updated.append("allowed_tools")
    if disallowed_tools:
        if disallowed_tools.lower() == "none":
            agent.disallowed_tools = None
        else:
            agent.disallowed_tools = _parse_csv(disallowed_tools)
        updated.append("disallowed_tools")
    if capabilities:
        agent.capabilities = [] if capabilities.lower() == "none" else _parse_csv(capabilities)
        updated.append("capabilities")
    if risky_capabilities:
        agent.risky_capabilities = (
            [] if risky_capabilities.lower() == "none" else _parse_csv(risky_capabilities)
        )
        updated.append("risky_capabilities")

    if not updated:
        return json.dumps({"error": "Nothing to update. Pass at least one non-empty field."})

    save_config(config)

    if ctx:
        await ctx.info(f"Updated agent '{name}': {', '.join(updated)}")

    return json.dumps({"updated": name, "fields": updated}, indent=2)


# ---------------------------------------------------------------------------
# Async dispatch (job_id pattern)
# ---------------------------------------------------------------------------


def _run_job(
    job_id: str,
    agent: str,
    task: str,
    agent_config: AgentConfig,
    settings: Settings,
    context: str | None,
    caller: str | None,
    goal: str | None,
    response_format: str | None,
    sem: threading.BoundedSemaphore,
) -> None:
    """Worker thread body: runs the dispatch and persists the result.

    Uses the streaming runner so the job file accumulates a rolling tail of
    progress lines — dispatch_status shows what the agent is *doing*, not
    just "running". Writes are throttled to one per
    _JOB_PROGRESS_WRITE_INTERVAL to keep disk churn negligible.
    """
    store = _get_job_store()
    with sem:
        # mark_running refuses (returns None) if the job was cancelled while
        # queued — honor that and skip the dispatch entirely.
        if store.mark_running(job_id) is None:
            logger.info("Async job %s skipped (cancelled or missing)", job_id)
            return
        logger.info("Async job %s running (agent=%s)", job_id, agent)

        progress_lines: list[str] = []
        last_write = 0.0

        def on_progress(msg: str) -> None:
            # Called synchronously from the worker thread's stdout loop —
            # no locking needed around progress_lines.
            nonlocal last_write
            progress_lines.append(msg[:300])
            del progress_lines[:-_JOB_PROGRESS_MAX_LINES]
            now = time.monotonic()
            if now - last_write >= _JOB_PROGRESS_WRITE_INTERVAL:
                last_write = now
                store.update_progress(job_id, list(progress_lines))

        def on_proc(proc: Any) -> None:
            # Register the live subprocess so dispatch_cancel can kill it.
            # May fire twice (old-CLI retry respawns) — last write wins.
            with _running_procs_lock:
                _running_procs[job_id] = proc

        try:
            result = runner.dispatch_stream(
                agent,
                task,
                agent_config,
                settings,
                context,
                on_progress,
                caller=caller,
                goal=goal,
                response_format=response_format,
                on_proc=on_proc,
            )
            # Flush trailing progress lines the throttle skipped, so the
            # finished job's trace is complete.
            if progress_lines:
                store.update_progress(job_id, list(progress_lines))
            # finish() refuses terminal jobs — if dispatch_cancel force-killed
            # us, the job is already 'cancelled' and this is a no-op.
            store.finish(job_id, result)
            logger.info(
                "Async job %s finished: success=%s cost=%s",
                job_id,
                result.success,
                result.cost_usd,
            )
        except Exception as e:  # noqa: BLE001 — must not crash worker thread
            logger.exception("Async job %s crashed: %s", job_id, e)
            store.fail(job_id, f"Worker crashed: {e}")
        finally:
            with _running_procs_lock:
                _running_procs.pop(job_id, None)


@mcp.tool()
async def dispatch_async(
    agent: str,
    task: str,
    context: str = "",
    caller: str = "",
    goal: str = "",
    response_format: str = "",
    timeout_seconds: int = 0,
    ctx: Context | None = None,
) -> str:
    """Start a dispatch in the background and return immediately with a job_id.

    Use this for long-running tasks where you don't want to block your own
    tool slot. Poll dispatch_status(job_id) — it shows a rolling tail of the
    agent's progress while running — or block on dispatch_wait(job_id) to
    retrieve the result. Job state persists to disk so it survives across
    polling sessions.

    Args:
        agent: Name of the agent.
        task: The task to perform.
        context: Optional extra context.
        caller: Who is dispatching.
        goal: The broader objective.
        response_format: ``"json"`` to request a single JSON value
            (parsed into ``parsed_result`` on completion). Empty = free-form.
        timeout_seconds: One-off timeout override (0 = agent default;
            clamped to [10, 7200]). Prefer this over editing the agent
            config for known-long tasks.
    """
    config = _get_config()
    if err := _validate_agent(config, agent):
        return err

    store = _get_job_store()
    job = store.create(
        agent,
        task,
        context=context or None,
        caller=caller or None,
        goal=goal or None,
    )

    sem = _get_job_semaphore(config)
    agent_config = _apply_timeout(config.agents[agent], timeout_seconds)
    thread = threading.Thread(
        target=_run_job,
        args=(
            job.id,
            agent,
            task,
            agent_config,
            config.settings,
            context or None,
            caller or None,
            goal or None,
            response_format or None,
            sem,
        ),
        daemon=True,
        name=f"dispatch-{job.id[:8]}",
    )
    thread.start()

    if ctx:
        await ctx.info(f"Started async dispatch {job.id} to {agent}")

    return json.dumps(
        {"job_id": job.id, "status": "pending", "agent": agent},
        indent=2,
    )


@mcp.tool()
async def dispatch_status(
    job_id: str,
    ctx: Context | None = None,
) -> str:
    """Check the current state of an async job without blocking.

    Returns the job record including status (pending/running/done/failed),
    timestamps, a rolling tail of the agent's recent activity (``progress``)
    while running, and the DispatchResult once complete.

    Args:
        job_id: ID returned by dispatch_async.
    """
    if err := _validate_ref(job_id):
        return err
    store = _get_job_store()
    job = store.get(job_id)
    if job is None:
        return json.dumps({"error": f"Job not found: {job_id}"})
    return job.model_dump_json(indent=2, exclude_none=True)


@mcp.tool()
async def dispatch_wait(
    job_id: str,
    timeout_seconds: int = 60,
    ctx: Context | None = None,
) -> str:
    """Block until a job completes, or until timeout_seconds elapses.

    Returns the same shape as dispatch_status. If the timeout fires before
    the job finishes, the response includes "timed_out_waiting": true and
    the job continues running in the background — call again to keep waiting.

    Args:
        job_id: ID returned by dispatch_async.
        timeout_seconds: Max seconds to wait (default 60, capped at 3600).
    """
    if err := _validate_ref(job_id):
        return err
    timeout = max(1, min(int(timeout_seconds), 3600))
    store = _get_job_store()
    deadline = time.monotonic() + timeout

    while True:
        job = store.get(job_id)
        if job is None:
            return json.dumps({"error": f"Job not found: {job_id}"})
        if job.is_terminal():
            return job.model_dump_json(indent=2, exclude_none=True)
        if time.monotonic() >= deadline:
            d = json.loads(job.model_dump_json(exclude_none=True))
            d["timed_out_waiting"] = True
            if ctx:
                await ctx.info(
                    f"dispatch_wait timed out for {job_id} after {timeout}s "
                    f"(job still {job.status})"
                )
            return json.dumps(d, indent=2)
        await asyncio.sleep(0.25)


@mcp.tool()
async def dispatch_cancel(
    job_id: str,
    ctx: Context | None = None,
) -> str:
    """Cancel an async job — pending always, running when this server owns it.

    A *pending* job is simply marked cancelled. A *running* job can be
    cancelled too if its subprocess was spawned by this server instance: the
    job is marked cancelled first, then the claude subprocess is killed
    (partial work is lost; the job's progress tail is preserved). A running
    job started by a previous server run cannot be killed safely — poll
    dispatch_status until it finishes.

    Returns the job's new state plus an ``outcome`` field: ``cancelled``
    (was pending), ``cancelled_running`` (was running, subprocess killed),
    ``running`` (could not cancel — not owned by this server),
    ``already_terminal``, or ``not_found``.

    Args:
        job_id: ID returned by dispatch_async.
    """
    if err := _validate_ref(job_id):
        return err
    store = _get_job_store()
    job, outcome = store.cancel(job_id)
    if outcome == "running":
        # We can kill the subprocess only if this server spawned it.
        with _running_procs_lock:
            proc = _running_procs.get(job_id)
        if proc is not None:
            # Mark cancelled BEFORE killing: finish()/fail() refuse terminal
            # jobs, so the worker's trailing write cannot undo this. If the
            # job finished in the meantime, cancel returns already_terminal
            # and we leave the (already exiting) process alone.
            job, outcome = store.cancel(job_id, force=True)
            if outcome == "cancelled_running":
                try:
                    proc.kill()
                except OSError as e:  # already gone — job stays cancelled
                    logger.debug("Kill of job %s subprocess failed: %s", job_id, e)
    if outcome == "not_found":
        return json.dumps({"error": f"Job not found: {job_id}"})
    if ctx:
        await ctx.info(f"Cancel {job_id}: {outcome}")
    payload: dict = {"job_id": job_id, "outcome": outcome}
    if job is not None:
        payload["status"] = job.status
    if outcome == "cancelled_running":
        payload["message"] = "Job was running; its subprocess has been killed."
    elif outcome == "running":
        payload["message"] = (
            "Job is running but was not started by this server instance, so "
            "its subprocess cannot be killed safely. Poll dispatch_status "
            "until it finishes."
        )
    return json.dumps(payload, indent=2)


@mcp.tool()
async def dispatch_jobs(
    status: str = "",
    limit: int = 50,
    ctx: Context | None = None,
) -> str:
    """List recent async jobs as summaries (most recent first).

    Each entry includes: id, agent, status, task (truncated), created_at,
    completed_at, success (if terminal). Full record available via
    dispatch_status(job_id).

    Args:
        status: Optional filter — "pending", "running", "done", "failed",
            "cancelled". Empty = all.
        limit: Max entries returned (default 50).
    """
    store = _get_job_store()
    valid_statuses = {"pending", "running", "done", "failed", "cancelled"}
    filt = status.strip().lower() or None
    if filt and filt not in valid_statuses:
        return json.dumps(
            {
                "error": f"Invalid status: {status!r}. "
                f"Use one of: {', '.join(sorted(valid_statuses))} or empty.",
            }
        )
    jobs = store.list(status=filt)  # type: ignore[arg-type]
    jobs = jobs[: max(1, min(int(limit), _MAX_JOBS_LIMIT))]
    summaries: list[dict] = []
    for j in jobs:
        entry: dict = {
            "id": j.id,
            "agent": j.agent,
            "status": j.status,
            "task": j.task[:120],
            "created_at": j.created_at,
        }
        if j.started_at is not None:
            entry["started_at"] = j.started_at
        if j.completed_at is not None:
            entry["completed_at"] = j.completed_at
        if j.result is not None:
            entry["success"] = j.result.success
            if j.result.cost_usd is not None:
                entry["cost_usd"] = j.result.cost_usd
        if j.status == "running" and j.progress:
            entry["last_progress"] = j.progress[-1]
        if j.error:
            entry["error_type"] = (
                j.result.error_type if j.result and j.result.error_type else "cli_error"
            )
        summaries.append(entry)
    return json.dumps(summaries, indent=2)


@mcp.tool()
async def fetch_result(
    ref: str,
    max_chars: int = 0,
    ctx: Context | None = None,
) -> str:
    """Fetch the full DispatchResult behind a ref returned by ``return_ref=True``.

    Also works with job_ids returned by ``dispatch_async`` (the underlying
    storage is the same).

    Args:
        ref: The ref/job_id whose result you want.
        max_chars: Truncate ``result`` to this many characters (0 = no limit).
            Useful when probing a very large result before deciding to load
            it fully.
    """
    if err := _validate_ref(ref):
        return err
    store = _get_job_store()
    job = store.get(ref)
    if job is None:
        return json.dumps({"error": f"Ref not found: {ref}"})
    if job.result is None:
        # Not yet completed (e.g. async job still running) or failed before
        # producing a DispatchResult. Return the job record so the caller
        # can see the status.
        return job.model_dump_json(indent=2, exclude_none=True)

    payload = json.loads(job.result.model_dump_json(exclude_none=True))
    text = payload.get("result") or ""
    cap = max(0, int(max_chars))
    if cap and len(text) > cap:
        payload["result"] = text[:cap]
        payload["truncated"] = True
        payload["full_size"] = len(text)
    return json.dumps(payload, indent=2)


@mcp.tool()
async def dispatch_gc(
    max_age_days: float = 7,
    ctx: Context | None = None,
) -> str:
    """Delete terminal jobs (done/failed/cancelled) older than max_age_days.

    Pending and running jobs are never deleted. Returns the count purged.

    Args:
        max_age_days: Age threshold in days (default 7).
    """
    if max_age_days <= 0:
        return json.dumps({"error": "max_age_days must be > 0"})
    max_age_seconds = float(max_age_days) * 86400
    if not math.isfinite(max_age_seconds):
        return json.dumps({"error": "max_age_days is too large (non-finite)"})
    store = _get_job_store()
    purged = store.gc(max_age_seconds=max_age_seconds)
    if ctx:
        await ctx.info(f"Purged {purged} terminal jobs older than {max_age_days}d")
    return json.dumps({"purged": purged, "max_age_days": max_age_days})


# ---------------------------------------------------------------------------
# Cache tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def cache_stats(ctx: Context | None = None) -> str:
    """Show dispatch cache statistics: size, hit rate, TTL."""
    config = _get_config()
    cache = _get_cache(config)
    if cache is None:
        return json.dumps({"enabled": False, "message": "Cache is disabled in settings"})
    cache.evict_expired()
    return json.dumps(cache.stats(), indent=2)


@mcp.tool()
async def cache_clear(ctx: Context | None = None) -> str:
    """Clear all cached dispatch results."""
    config = _get_config()
    cache = _get_cache(config)
    if cache is None:
        return json.dumps({"enabled": False, "message": "Cache is disabled in settings"})
    count = cache.clear()
    if ctx:
        await ctx.info(f"Cleared {count} cached entries")
    return json.dumps({"cleared": count})


def main() -> None:
    """Entry point for the MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Recover jobs abandoned in 'running' by a previous crashed/killed server
    # so callers don't poll them forever.
    try:
        recovered = _get_job_store().recover_stale(_STALE_RUNNING_SECONDS)
        if recovered:
            logger.info("Recovered %d stale 'running' job(s) from a prior run", recovered)
    except OSError as e:
        logger.warning("Stale-job recovery skipped: %s", e)
    mcp.run(transport="stdio")
