"""MCP server: exposes list_agents, dispatch, dispatch_session tools."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys

from mcp.server.fastmcp import Context, FastMCP

from . import runner
from .cache import DispatchCache
from .config import auto_describe, load_config, save_config
from .models import AgentConfig, DispatchConfig, validate_agent_name

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
        "2. dispatch(agent, task) — delegate a specific task\n"
        "3. Always pass caller= (your project name) and goal= (why you need this)\n"
        "4. Be specific in the task — the agent doesn't see your conversation\n\n"
        "MANAGING AGENTS: Use add_agent() to register a new project directory. "
        "The description is auto-generated from the project's files (CLAUDE.md, "
        "MCP servers, package files). You can also provide a custom description."
    ),
)

_cache: DispatchCache | None = None
_semaphore: asyncio.Semaphore | None = None
_semaphore_limit: int = 0

_RESOLVED_MARKER = "[RESOLVED]"


def _get_config() -> DispatchConfig:
    """Load config fresh each call so new agents are picked up immediately."""
    return load_config()


def _get_cache(config: DispatchConfig) -> DispatchCache | None:
    """Return the global cache instance, creating it on first call."""
    global _cache  # noqa: PLW0603
    if not config.settings.cache.enabled:
        return None
    if _cache is None or _cache._ttl != config.settings.cache.ttl:
        _cache = DispatchCache(ttl=config.settings.cache.ttl)
    return _cache


def _get_semaphore(config: DispatchConfig) -> asyncio.Semaphore:
    """Return concurrency-limiting semaphore, recreated if limit changes."""
    global _semaphore, _semaphore_limit  # noqa: PLW0603
    limit = config.settings.max_concurrency
    if _semaphore is None or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_limit = limit
    return _semaphore


def _validate_agent(config: DispatchConfig, name: str) -> str | None:
    """Return an error JSON string if the agent doesn't exist, else None."""
    if name not in config.agents:
        available = ", ".join(config.agents.keys()) or "(none configured)"
        return json.dumps({"error": f"Unknown agent: {name!r}. Available: {available}"})
    return None


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_agents(ctx: Context) -> str:
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
        healthy = agent.directory.is_dir()
        agents.append(
            {
                "name": name,
                "directory": str(agent.directory),
                "description": agent.description,
                "healthy": healthy,
                "has_claude_md": (agent.directory / "CLAUDE.md").exists() if healthy else False,
                "has_mcp_config": (agent.directory / ".mcp.json").exists() if healthy else False,
            }
        )
    await ctx.info(f"Found {len(agents)} configured agents")
    return json.dumps(agents, indent=2)


@mcp.tool()
async def dispatch(
    agent: str,
    task: str,
    context: str = "",
    caller: str = "",
    goal: str = "",
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
    """
    config = _get_config()
    if err := _validate_agent(config, agent):
        return err

    # Check cache
    cache = _get_cache(config)
    if cache:
        cached = cache.get(agent, task, context or None)
        if cached:
            if ctx:
                await ctx.info(f"Cache hit for {agent} — returning cached result")
            cached_dict = json.loads(cached.model_dump_json(indent=2, exclude_none=True))
            cached_dict["cached"] = True
            return json.dumps(cached_dict, indent=2)

    agent_config = config.agents[agent]
    if ctx:
        await ctx.info(f"Dispatching to {agent}: {task[:80]}...")

    async with _get_semaphore(config):
        result = await asyncio.to_thread(
            runner.dispatch,
            agent,
            task,
            agent_config,
            config.settings,
            context or None,
            caller=caller or None,
            goal=goal or None,
        )

    # Populate cache
    if cache:
        cache.put(agent, task, result, context or None)

    return result.model_dump_json(indent=2, exclude_none=True)


@mcp.tool()
async def dispatch_session(
    agent: str,
    task: str,
    session_id: str = "",
    context: str = "",
    caller: str = "",
    goal: str = "",
    ctx: Context | None = None,
) -> str:
    """Multi-turn dispatch: continue a conversation with an agent.

    First call without session_id starts a new session. Use the returned
    session_id in subsequent calls to continue the conversation — the agent
    retains full context from previous turns.

    Session dispatches are never cached because each turn builds on the prior.

    Args:
        agent: Name of the agent.
        task: The task or follow-up message.
        session_id: Session ID from a previous call (empty for new session).
        context: Optional extra context.
        caller: Who is dispatching.
        goal: The broader objective.
    """
    config = _get_config()
    if err := _validate_agent(config, agent):
        return err

    agent_config = config.agents[agent]
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
            optional "context", "caller", "goal".  Example:
            [
              {"agent": "infra", "task": "check pod logs for errors"},
              {"agent": "db", "task": "are all migrations applied?"}
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
    cache = _get_cache(config)

    # Validate structure and agents up front (including aggregator)
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return json.dumps({"error": f"dispatches[{i}] must be an object, got {type(item).__name__}"})
        if "agent" not in item or "task" not in item:
            return json.dumps({"error": f"dispatches[{i}] must have 'agent' and 'task' keys"})
        if err := _validate_agent(config, item["agent"]):
            return err
    if aggregate:
        if err := _validate_agent(config, aggregate):
            return err

    if ctx:
        names = ", ".join(item["agent"] for item in items)
        await ctx.info(f"Dispatching in parallel to: {names}")

    async def _run_one(item: dict) -> dict:
        name = item["agent"]
        task = item["task"]
        item_context = item.get("context") or None
        item_caller = item.get("caller") or None
        item_goal = item.get("goal") or None

        # Check cache
        if cache:
            cached = cache.get(name, task, item_context)
            if cached:
                d = json.loads(cached.model_dump_json(exclude_none=True))
                d["cached"] = True
                return d

        agent_config = config.agents[name]
        async with _get_semaphore(config):
            result = await asyncio.to_thread(
                runner.dispatch,
                name,
                task,
                agent_config,
                config.settings,
                item_context,
                caller=item_caller,
                goal=item_goal,
            )

        if cache:
            cache.put(name, task, result, item_context)

        return json.loads(result.model_dump_json(exclude_none=True))

    results = await asyncio.gather(*[_run_one(item) for item in items], return_exceptions=True)

    output = []
    for item, res in zip(items, results):
        if isinstance(res, Exception):
            output.append({
                "agent": item["agent"],
                "success": False,
                "result": "",
                "error": str(res),
            })
        else:
            output.append(res)

    # ---- Aggregation ----
    if not aggregate:
        return json.dumps(output, indent=2)

    # Build a summary for the aggregator agent
    parts = []
    for item, res in zip(items, output):
        status = "OK" if res.get("success") else "FAILED"
        parts.append(f"## Agent: {item['agent']} [{status}]\n{res.get('result') or res.get('error', '')}")
    summary = "\n\n".join(parts)

    if ctx:
        await ctx.info(f"Aggregating results via {aggregate}...")

    agg_task = "Synthesize the results below into a single coherent answer. Highlight key findings, note any conflicts between agents, and provide actionable conclusions."
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
    """
    config = _get_config()
    if err := _validate_agent(config, agent):
        return err

    agent_config = config.agents[agent]
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
            ),
        )

        # Forward progress messages while the subprocess runs
        while not future.done():
            await asyncio.sleep(0.1)
            while not progress_queue.empty():
                msg = progress_queue.get_nowait()
                if ctx:
                    await ctx.info(f"[{agent}] {msg[:300]}")

        result = await asyncio.wrap_future(future)

        # Drain any remaining messages
        while not progress_queue.empty():
            msg = progress_queue.get_nowait()
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
        await ctx.info(
            f"Starting dialogue: {requester} <-> {responder} (max {max_rounds} rounds)"
        )

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

        conversation.append({
            "agent": responder,
            "role": "responder",
            "round": round_num,
            "message": resp_result.result,
            "cost_usd": resp_result.cost_usd,
        })

        if ctx:
            await ctx.info(
                f"[round {round_num}] {responder}: {resp_result.result[:120]}..."
            )

        if _RESOLVED_MARKER in resp_result.result or not resp_result.success:
            resolved = _RESOLVED_MARKER in resp_result.result
            final_answer = resp_result.result.replace(_RESOLVED_MARKER, "").strip()
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

        conversation.append({
            "agent": requester,
            "role": "requester",
            "round": round_num,
            "message": req_result.result,
            "cost_usd": req_result.cost_usd,
        })

        if ctx:
            await ctx.info(
                f"[round {round_num}] {requester}: {req_result.result[:120]}..."
            )

        if _RESOLVED_MARKER in req_result.result or not req_result.success:
            resolved = _RESOLVED_MARKER in req_result.result
            final_answer = req_result.result.replace(_RESOLVED_MARKER, "").strip()
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

    config.agents[name] = AgentConfig(directory=dir_path, description=desc)
    save_config(config)

    if ctx:
        await ctx.info(f"Added agent '{name}' -> {dir_path}")

    return json.dumps(
        {
            "added": name,
            "directory": str(dir_path),
            "description": desc,
        },
        indent=2,
    )


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
    mcp.run(transport="stdio")
