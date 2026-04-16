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

_PERMISSION_PATTERNS = [
    "permission denied",
    "not allowed",
    "not permitted",
    "disallowed tool",
    "permission mode",
    "not have permission",
    "tool is not available",
    "tool_use is not allowed",
    "unauthorized",
]


def _classify_error(error_text: object) -> str:
    """Classify an error message into a category.

    Accepts any type and coerces to string — some claude CLI error paths
    produce non-string values (None, dict) that would crash `.lower()`.
    """
    text = str(error_text) if error_text else ""
    lower = text.lower()
    for pattern in _PERMISSION_PATTERNS:
        if pattern in lower:
            return "permission"
    return "cli_error"


def _permission_hint(agent_name: str) -> str:
    """Generate actionable hint for permission errors."""
    return (
        f"\n\nHint: Agent '{agent_name}' was denied a tool or action. "
        "To fix, configure permissions in agents.yaml:\n"
        f"  agent-dispatch update {agent_name} --permission-mode bypassPermissions\n"
        f"  agent-dispatch update {agent_name} "
        "--allowed-tools Bash,Read,Edit,Write,Glob,Grep"
    )


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

    permission_mode = agent.permission_mode or settings.default_permission_mode
    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])

    # None = inherit from settings; [] = explicitly empty (no inheritance)
    allowed = (
        agent.allowed_tools if agent.allowed_tools is not None
        else settings.default_allowed_tools
    )
    for tool in allowed:
        cmd.extend(["--allowedTools", tool])

    disallowed = (
        agent.disallowed_tools if agent.disallowed_tools is not None
        else settings.default_disallowed_tools
    )
    for tool in disallowed:
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
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="recursion",
        )

    try:
        claude_path = _find_claude()
    except FileNotFoundError as e:
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="not_found",
        )

    if not agent.directory.is_dir():
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=f"Directory does not exist: {agent.directory}",
            error_type="not_found",
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
            error_type="timeout",
        )

    if proc.returncode != 0 and not proc.stdout.strip():
        error_text = proc.stderr.strip() or f"claude exited with code {proc.returncode}"
        error_type = _classify_error(error_text)
        if error_type == "permission":
            error_text += _permission_hint(agent_name)
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=error_text,
            error_type=error_type,
        )

    # Parse JSON output
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Fallback: treat stdout as plain text
        success = proc.returncode == 0
        text = proc.stdout.strip()
        if success:
            return DispatchResult(agent=agent_name, success=True, result=text)
        error_text = text or f"claude exited with code {proc.returncode} (non-JSON output)"
        error_type = _classify_error(error_text)
        if error_type == "permission":
            error_text += _permission_hint(agent_name)
        return DispatchResult(
            agent=agent_name,
            success=False,
            result=text,
            error=error_text,
            error_type=error_type,
        )

    is_error = data.get("is_error", False)
    if is_error:
        raw_result = data.get("result", "")
        error_text = str(raw_result) if raw_result else (
            f"Agent '{agent_name}' reported an error with no details "
            f"(exit code {proc.returncode})"
        )
        error_type = _classify_error(error_text)
        if error_type == "permission":
            error_text += _permission_hint(agent_name)
        return DispatchResult(
            agent=agent_name,
            success=False,
            result=str(raw_result) if raw_result else "",
            session_id=data.get("session_id"),
            cost_usd=data.get("total_cost_usd"),
            duration_ms=data.get("duration_ms"),
            num_turns=data.get("num_turns"),
            error=error_text,
            error_type=error_type,
        )

    return DispatchResult(
        agent=agent_name,
        success=True,
        result=data.get("result", ""),
        session_id=data.get("session_id"),
        cost_usd=data.get("total_cost_usd"),
        duration_ms=data.get("duration_ms"),
        num_turns=data.get("num_turns"),
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
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="recursion",
        )

    try:
        claude_path = _find_claude()
    except FileNotFoundError as e:
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="not_found",
        )

    if not agent.directory.is_dir():
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=f"Directory does not exist: {agent.directory}",
            error_type="not_found",
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
    except FileNotFoundError as e:
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="not_found",
        )
    except PermissionError as e:
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="permission",
        )
    except OSError as e:
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="cli_error",
        )

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
                logger.debug("Non-JSON line in stream: %s", line[:200])
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
            error_type="timeout",
        )

    if result_data:
        is_error = result_data.get("is_error", False)
        if is_error:
            raw_result = result_data.get("result", "")
            error_text = str(raw_result) if raw_result else (
                f"Agent '{agent_name}' reported an error with no details"
            )
            error_type = _classify_error(error_text)
            if error_type == "permission":
                error_text += _permission_hint(agent_name)
            return DispatchResult(
                agent=agent_name,
                success=False,
                result=str(raw_result) if raw_result else "",
                session_id=result_data.get("session_id"),
                cost_usd=result_data.get("total_cost_usd"),
                duration_ms=result_data.get("duration_ms"),
                num_turns=result_data.get("num_turns"),
                error=error_text,
                error_type=error_type,
            )
        return DispatchResult(
            agent=agent_name,
            success=True,
            result=result_data.get("result", ""),
            session_id=result_data.get("session_id"),
            cost_usd=result_data.get("total_cost_usd"),
            duration_ms=result_data.get("duration_ms"),
            num_turns=result_data.get("num_turns"),
        )

    # Fallback: no result line received
    stderr = proc.stderr.read() if proc.stderr else ""
    error_text = stderr.strip() or f"No result received (exit code {proc.returncode})"
    error_type = _classify_error(error_text)
    if error_type == "permission":
        error_text += _permission_hint(agent_name)
    return DispatchResult(
        agent=agent_name,
        success=False,
        result="",
        error=error_text,
        error_type=error_type,
    )
