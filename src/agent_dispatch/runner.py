"""Core dispatch logic: run claude -p in agent directories."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable

from .models import AgentConfig, DispatchResult, Settings

logger = logging.getLogger(__name__)

_DEPTH_ENV_VAR = "AGENT_DISPATCH_DEPTH"


def _current_depth() -> int:
    raw = os.environ.get(_DEPTH_ENV_VAR, "0")
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s value: %r, treating as 0", _DEPTH_ENV_VAR, raw)
        return 0


def _check_recursion(max_depth: int) -> None:
    depth = _current_depth()
    if depth >= max_depth:
        raise RecursionError(
            f"Dispatch depth {depth} >= max {max_depth}. "
            "An agent is trying to dispatch to another agent that dispatches back. "
            "Increase settings.max_dispatch_depth if this is intentional."
        )


def _find_claude() -> str:
    path = shutil.which("claude")
    if path is None:
        raise FileNotFoundError(
            "Claude CLI not found in PATH. "
            "Install: https://docs.anthropic.com/en/docs/claude-code"
        )
    return path


def _build_command(
    claude_path: str,
    task: str,
    agent: AgentConfig,
    settings: Settings,
    session_id: str | None = None,
) -> list[str]:
    cmd = [claude_path, "-p", task, "--output-format", "json"]

    if session_id:
        cmd.extend(["--resume", session_id])

    budget = agent.max_budget_usd or settings.default_max_budget_usd
    if budget:
        cmd.extend(["--max-budget-usd", str(budget)])

    if agent.model:
        cmd.extend(["--model", agent.model])

    if agent.permission_mode:
        cmd.extend(["--permission-mode", agent.permission_mode])

    for tool in agent.allowed_tools:
        cmd.extend(["--allowedTools", tool])

    for tool in agent.disallowed_tools:
        cmd.extend(["--disallowedTools", tool])

    return cmd


def _build_prompt(
    task: str,
    context: str | None = None,
    caller: str | None = None,
    goal: str | None = None,
) -> str:
    """Build a structured prompt for the dispatched agent.

    When *caller* or *goal* are provided the prompt uses a structured
    multi-section format so the dispatched agent understands who asked,
    why, and what broader objective it serves.  Without metadata the
    output is identical to the original simple format (backward compat).
    """
    if not caller and not goal:
        if context:
            return f"Context:\n{context}\n\nTask:\n{task}"
        return task

    parts: list[str] = []
    if goal:
        parts.append(f"## Goal\n{goal}")
    if caller:
        parts.append(f"## Dispatched by\n{caller}")
    if context:
        parts.append(f"## Context\n{context}")
    parts.append(f"## Task\n{task}")
    return "\n\n".join(parts)


def dispatch(
    agent_name: str,
    task: str,
    agent: AgentConfig,
    settings: Settings,
    context: str | None = None,
    session_id: str | None = None,
    *,
    caller: str | None = None,
    goal: str | None = None,
) -> DispatchResult:
    """Run a task via claude -p in the agent's directory."""
    try:
        _check_recursion(settings.max_dispatch_depth)
    except RecursionError as e:
        return DispatchResult(agent=agent_name, success=False, result="", error=str(e))

    try:
        claude_path = _find_claude()
    except FileNotFoundError as e:
        return DispatchResult(agent=agent_name, success=False, result="", error=str(e))

    if not agent.directory.is_dir():
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=f"Directory does not exist: {agent.directory}",
        )

    full_task = _build_prompt(task, context, caller, goal)

    cmd = _build_command(claude_path, full_task, agent, settings, session_id)
    timeout = agent.timeout or settings.default_timeout

    # Propagate depth for recursion protection
    env = os.environ.copy()
    env[_DEPTH_ENV_VAR] = str(_current_depth() + 1)

    logger.info("Dispatching to %s (timeout=%ds)", agent_name, timeout)
    logger.debug("Task: %s", task[:200])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(agent.directory),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=f"Agent '{agent_name}' timed out after {timeout}s. "
            "Increase timeout in agents.yaml if the task needs more time.",
        )

    if proc.returncode != 0 and not proc.stdout.strip():
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=proc.stderr.strip() or f"claude exited with code {proc.returncode}",
        )

    # Parse JSON output
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Fallback: treat stdout as plain text
        return DispatchResult(
            agent=agent_name,
            success=proc.returncode == 0,
            result=proc.stdout.strip(),
        )

    is_error = data.get("is_error", False)
    return DispatchResult(
        agent=agent_name,
        success=not is_error,
        result=data.get("result", ""),
        session_id=data.get("session_id"),
        cost_usd=data.get("total_cost_usd"),
        duration_ms=data.get("duration_ms"),
        num_turns=data.get("num_turns"),
        error=data.get("result") if is_error else None,
    )


def dispatch_stream(
    agent_name: str,
    task: str,
    agent: AgentConfig,
    settings: Settings,
    context: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    *,
    caller: str | None = None,
    goal: str | None = None,
) -> DispatchResult:
    """Run a task via claude -p with streaming output and progress callbacks.

    Uses --output-format stream-json to read intermediate results while the
    agent works.  Each assistant message and tool-use event is forwarded to
    *on_progress* so callers can surface live updates.
    """
    try:
        _check_recursion(settings.max_dispatch_depth)
    except RecursionError as e:
        return DispatchResult(agent=agent_name, success=False, result="", error=str(e))

    try:
        claude_path = _find_claude()
    except FileNotFoundError as e:
        return DispatchResult(agent=agent_name, success=False, result="", error=str(e))

    if not agent.directory.is_dir():
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=f"Directory does not exist: {agent.directory}",
        )

    full_task = _build_prompt(task, context, caller, goal)

    cmd = _build_command(claude_path, full_task, agent, settings)
    # Switch from json to stream-json
    fmt_idx = cmd.index("--output-format")
    cmd[fmt_idx + 1] = "stream-json"

    timeout = agent.timeout or settings.default_timeout
    env = os.environ.copy()
    env[_DEPTH_ENV_VAR] = str(_current_depth() + 1)

    logger.info("Dispatching (stream) to %s (timeout=%ds)", agent_name, timeout)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(agent.directory),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except OSError as e:
        return DispatchResult(agent=agent_name, success=False, result="", error=str(e))

    # Kill the process if it exceeds the timeout
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()

    result_data: dict | None = None
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            if msg_type == "result":
                result_data = data
            elif msg_type == "assistant" and on_progress:
                content = data.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text" and block.get("text"):
                            on_progress(block["text"][:500])
                        elif block.get("type") == "tool_use":
                            on_progress(f"Using tool: {block.get('name', '?')}")

        proc.wait()
    finally:
        timer.cancel()
        # Ensure the process is not left orphaned on any exit path
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if timed_out.is_set():
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=f"Agent '{agent_name}' timed out after {timeout}s. "
            "Increase timeout in agents.yaml if the task needs more time.",
        )

    if result_data:
        is_error = result_data.get("is_error", False)
        return DispatchResult(
            agent=agent_name,
            success=not is_error,
            result=result_data.get("result", ""),
            session_id=result_data.get("session_id"),
            cost_usd=result_data.get("total_cost_usd"),
            duration_ms=result_data.get("duration_ms"),
            num_turns=result_data.get("num_turns"),
            error=result_data.get("result") if is_error else None,
        )

    # Fallback: no result line received
    stderr = proc.stderr.read() if proc.stderr else ""
    return DispatchResult(
        agent=agent_name,
        success=False,
        result="",
        error=stderr.strip() or f"No result received (exit code {proc.returncode})",
    )
