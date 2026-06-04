"""Core dispatch logic: run claude -p in agent directories."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable

from .models import AgentConfig, DispatchResult, Settings

logger = logging.getLogger(__name__)

_DEPTH_ENV_VAR = "AGENT_DISPATCH_DEPTH"

_JSON_RESPONSE_FOOTER = (
    "\n\n## Response format\n"
    "Respond with a single valid JSON value (object, array, or scalar) and "
    "nothing else. Do not wrap the JSON in markdown code fences. Do not add "
    "any explanatory text before or after. If you cannot satisfy this, "
    'respond with {"error": "<reason>"}.'
)


def _parse_structured_response(text: str) -> object | None:
    """Attempt to parse *text* as JSON, tolerating common wrappers.

    Strips a leading/trailing ```json or ``` code fence if present, then
    tries json.loads. Returns the parsed value (dict/list/scalar) or None
    on parse failure. Used when response_format="json" was requested.
    """
    if not text:
        return None
    candidate = text.strip()
    # Strip markdown code fences if present
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines:
            # Drop leading fence line (```json / ```)
            lines = lines[1:]
            # Drop trailing fence if present
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None


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


# permission_denials comes from the dispatched subprocess, which runs untrusted
# project instructions — bound what we keep so a hostile/buggy agent can't
# inflate DispatchResult/job files/ref payloads (mirrors the progress caps).
_MAX_DENIED_TOOLS = 10
_MAX_TOOL_NAME_CHARS = 100


def _extract_denied_tools(data: dict) -> list[str] | None:
    """Pull tool names out of the claude CLI's `permission_denials` field.

    Recent claude CLI versions report which tool calls were auto-denied in
    non-interactive (`-p`) mode. This is the deterministic signal that a
    "successful" dispatch actually ran with its hands tied — the agent often
    replies "I need permission for X" instead of failing. Returns a
    deduplicated, order-preserving list of tool names (capped at
    _MAX_DENIED_TOOLS entries of _MAX_TOOL_NAME_CHARS chars each), or None
    when the field is absent/empty/unrecognized (older CLIs).
    """
    denials = data.get("permission_denials")
    if not isinstance(denials, list) or not denials:
        return None
    names: list[str] = []
    for entry in denials:
        if isinstance(entry, dict):
            name = str(entry.get("tool_name") or entry.get("tool") or "unknown")
        else:
            name = str(entry)
        name = name[:_MAX_TOOL_NAME_CHARS]
        if name not in names:
            names.append(name)
            if len(names) >= _MAX_DENIED_TOOLS:
                break
    return names or None


def _denial_hint(agent_name: str, denied_tools: list[str]) -> str:
    """Advisory hint for a dispatch that succeeded but had tools denied."""
    tools = ", ".join(denied_tools)
    return (
        f"{len(denied_tools)} tool call(s) were denied by permissions: {tools}. "
        "The result may be incomplete — the agent could not use these tools. "
        f"To grant access: update_agent(name='{agent_name}', "
        f"allowed_tools='{','.join(denied_tools)}') or "
        f"permission_mode='bypassPermissions' "
        f"(CLI: agent-dispatch update {agent_name} --allowed-tools ...)."
    )


def _session_flag_unsupported(stderr_text: str) -> bool:
    """True when the installed claude CLI predates --session-id support.

    Old CLIs reject the flag with an option-parsing error before doing any
    work. Detecting it lets dispatch retry once without the flag instead of
    failing 100% of dispatches with a cryptic "unknown option" error.
    """
    lower = (stderr_text or "").lower()
    if "--session-id" not in lower:
        return False
    return any(
        marker in lower
        for marker in ("unknown option", "unrecognized option", "unknown argument",
                       "unexpected argument")
    )


def _timeout_error(agent_name: str, timeout: int, session_uuid: str | None) -> str:
    """Actionable timeout message: how to retry, extend, or resume."""
    msg = (
        f"Agent '{agent_name}' timed out after {timeout}s. "
        "Options: pass timeout_seconds= for a longer one-off run, use "
        "dispatch_async for fire-and-forget, or raise the agent default "
        f"(agent-dispatch update {agent_name} --timeout {timeout * 2})."
    )
    if session_uuid:
        msg += (
            f" Partial work may be resumable: dispatch_session(agent='{agent_name}', "
            f"task='Continue where you left off', session_id='{session_uuid}')."
        )
    return msg


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


class ArgInjectionError(ValueError):
    """Raised when a structured field would smuggle an extra CLI flag.

    Values like ``session_id="--permission-mode"`` are passed in the argument
    position right after a flag (``--resume``).  The ``claude`` CLI parses a
    token starting with ``-`` as a *new* flag rather than the preceding flag's
    value, so an attacker controlling such a field could inject arbitrary
    options (e.g. flip ``--permission-mode bypassPermissions``).  Every
    structured value we place on the command line is checked against this.
    """


def _reject_flaglike(label: str, value: str) -> None:
    """Reject a value that would be parsed as a CLI flag (option smuggling).

    The dispatched task itself (the ``-p`` prompt) is exempt — it is intended
    free-form content. This guard is only for *structured* fields that should
    never look like flags: session_id, model, permission_mode, tool names.
    """
    if value.startswith("-"):
        raise ArgInjectionError(
            f"Refusing to dispatch: {label} {value!r} starts with '-' and "
            "would be interpreted as a command-line flag by the claude CLI."
        )


def _build_command(
    claude_path: str,
    task: str,
    agent: AgentConfig,
    settings: Settings,
    session_id: str | None = None,
    *,
    new_session_id: str | None = None,
) -> list[str]:
    cmd = [claude_path, "-p", task, "--output-format", "json"]

    if session_id:
        _reject_flaglike("session_id", session_id)
        cmd.extend(["--resume", session_id])
    elif new_session_id:
        # Pre-chosen UUID for a fresh session. Knowing the id up front means
        # a timed-out dispatch can still hand back a resumable session_id —
        # the partial transcript survives the kill.
        cmd.extend(["--session-id", new_session_id])

    budget = agent.max_budget_usd or settings.default_max_budget_usd
    if budget:
        cmd.extend(["--max-budget-usd", str(budget)])

    if agent.model:
        _reject_flaglike("model", agent.model)
        cmd.extend(["--model", agent.model])

    permission_mode = agent.permission_mode or settings.default_permission_mode
    if permission_mode:
        _reject_flaglike("permission_mode", permission_mode)
        cmd.extend(["--permission-mode", permission_mode])

    # None = inherit from settings; [] = explicitly empty (no inheritance)
    allowed = (
        agent.allowed_tools if agent.allowed_tools is not None
        else settings.default_allowed_tools
    )
    for tool in allowed:
        _reject_flaglike("allowed tool", tool)
        cmd.extend(["--allowedTools", tool])

    disallowed = (
        agent.disallowed_tools if agent.disallowed_tools is not None
        else settings.default_disallowed_tools
    )
    for tool in disallowed:
        _reject_flaglike("disallowed tool", tool)
        cmd.extend(["--disallowedTools", tool])

    return cmd


def _build_prompt(
    task: str,
    context: str | None = None,
    caller: str | None = None,
    goal: str | None = None,
    response_format: str | None = None,
) -> str:
    """Build a structured prompt for the dispatched agent.

    When *caller* or *goal* are provided the prompt uses a structured
    multi-section format so the dispatched agent understands who asked,
    why, and what broader objective it serves.  Without metadata the
    output is identical to the original simple format (backward compat).

    When *response_format* is ``"json"`` a footer is appended instructing
    the agent to return a single JSON value with no extra prose.
    """
    if not caller and not goal:
        if context:
            base = f"Context:\n{context}\n\nTask:\n{task}"
        else:
            base = task
        if response_format == "json":
            base = base + _JSON_RESPONSE_FOOTER
        return base

    parts: list[str] = []
    if goal:
        parts.append(f"## Goal\n{goal}")
    if caller:
        parts.append(f"## Dispatched by\n{caller}")
    if context:
        parts.append(f"## Context\n{context}")
    parts.append(f"## Task\n{task}")
    body = "\n\n".join(parts)
    if response_format == "json":
        body = body + _JSON_RESPONSE_FOOTER
    return body


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
    response_format: str | None = None,
) -> DispatchResult:
    """Run a task via claude -p in the agent's directory.

    Pass ``response_format="json"`` to ask the agent for a single JSON value;
    on success the parsed value lands in ``DispatchResult.parsed_result``.
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

    full_task = _build_prompt(task, context, caller, goal, response_format)

    # Pre-generate the session id for fresh sessions so a timeout can still
    # return something resumable. When resuming, the caller's id is the one.
    new_session = None if session_id else str(uuid.uuid4())
    session_uuid = session_id or new_session

    try:
        cmd = _build_command(
            claude_path, full_task, agent, settings, session_id,
            new_session_id=new_session,
        )
    except ArgInjectionError as e:
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="cli_error",
        )
    timeout = agent.timeout or settings.default_timeout

    # Propagate depth for recursion protection
    env = os.environ.copy()
    env[_DEPTH_ENV_VAR] = str(_current_depth() + 1)

    logger.info("Dispatching to %s (timeout=%ds)", agent_name, timeout)
    logger.debug("Task: %s", task[:200])

    for attempt in (0, 1):
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
                session_id=session_uuid,
                error=_timeout_error(agent_name, timeout, session_uuid),
                error_type="timeout",
            )
        # Self-heal on old claude CLIs that don't know --session-id: strip the
        # flag and retry once. Timed-out dispatches lose resumability, but
        # every dispatch working beats 100% failing with "unknown option".
        if (
            attempt == 0
            and new_session
            and proc.returncode != 0
            and not proc.stdout.strip()
            and _session_flag_unsupported(proc.stderr or "")
        ):
            logger.warning(
                "claude CLI does not support --session-id; retrying without it "
                "(timed-out dispatches will not be resumable — upgrade claude)"
            )
            idx = cmd.index("--session-id")
            del cmd[idx : idx + 2]
            new_session = None
            session_uuid = session_id
            continue
        break

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
            parsed = (
                _parse_structured_response(text)
                if response_format == "json" else None
            )
            return DispatchResult(
                agent=agent_name, success=True, result=text, parsed_result=parsed,
                session_id=session_uuid,
            )
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

    denied = _extract_denied_tools(data)

    is_error = data.get("is_error", False)
    if is_error:
        raw_result = data.get("result", "")
        error_text = str(raw_result) if raw_result else (
            f"Agent '{agent_name}' reported an error with no details "
            f"(exit code {proc.returncode})"
        )
        error_type = _classify_error(error_text)
        if denied and error_type != "permission":
            # Denied tools are a stronger signal than substring matching.
            error_type = "permission"
        if error_type == "permission":
            error_text += _permission_hint(agent_name)
        return DispatchResult(
            agent=agent_name,
            success=False,
            result=str(raw_result) if raw_result else "",
            session_id=data.get("session_id") or session_uuid,
            cost_usd=data.get("total_cost_usd"),
            duration_ms=data.get("duration_ms"),
            num_turns=data.get("num_turns"),
            error=error_text,
            error_type=error_type,
            denied_tools=denied,
        )

    result_text = data.get("result", "")
    parsed: object | None = None
    if response_format == "json":
        parsed = _parse_structured_response(result_text)
    return DispatchResult(
        agent=agent_name,
        success=True,
        result=result_text,
        session_id=data.get("session_id") or session_uuid,
        cost_usd=data.get("total_cost_usd"),
        duration_ms=data.get("duration_ms"),
        num_turns=data.get("num_turns"),
        parsed_result=parsed,
        denied_tools=denied,
        hint=_denial_hint(agent_name, denied) if denied else None,
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
    response_format: str | None = None,
    _use_session_flag: bool = True,
) -> DispatchResult:
    """Run a task via claude -p with streaming output and progress callbacks.

    Uses --output-format stream-json to read intermediate results while the
    agent works.  Each assistant message and tool-use event is forwarded to
    *on_progress* so callers can surface live updates.

    ``_use_session_flag`` is internal: set to False on the one-shot retry when
    the installed claude CLI rejects ``--session-id`` (old version).
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

    full_task = _build_prompt(task, context, caller, goal, response_format)

    # Pre-generate session id so a timed-out stream is resumable (see dispatch()).
    new_session = str(uuid.uuid4()) if _use_session_flag else None

    try:
        cmd = _build_command(
            claude_path, full_task, agent, settings, new_session_id=new_session,
        )
    except ArgInjectionError as e:
        return DispatchResult(
            agent=agent_name, success=False, result="", error=str(e),
            error_type="cli_error",
        )
    # Switch from json to stream-json. Current claude CLIs refuse
    # `--print --output-format stream-json` without --verbose (non-verbose
    # print mode only emits the final result, which defeats streaming).
    fmt_idx = cmd.index("--output-format")
    cmd[fmt_idx + 1] = "stream-json"
    cmd.append("--verbose")

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
            session_id=new_session,
            error=_timeout_error(agent_name, timeout, new_session),
            error_type="timeout",
        )

    if result_data:
        denied = _extract_denied_tools(result_data)
        is_error = result_data.get("is_error", False)
        if is_error:
            raw_result = result_data.get("result", "")
            error_text = str(raw_result) if raw_result else (
                f"Agent '{agent_name}' reported an error with no details"
            )
            error_type = _classify_error(error_text)
            if denied and error_type != "permission":
                error_type = "permission"
            if error_type == "permission":
                error_text += _permission_hint(agent_name)
            return DispatchResult(
                agent=agent_name,
                success=False,
                result=str(raw_result) if raw_result else "",
                session_id=result_data.get("session_id") or new_session,
                cost_usd=result_data.get("total_cost_usd"),
                duration_ms=result_data.get("duration_ms"),
                num_turns=result_data.get("num_turns"),
                error=error_text,
                error_type=error_type,
                denied_tools=denied,
            )
        result_text = result_data.get("result", "")
        parsed = (
            _parse_structured_response(result_text)
            if response_format == "json" else None
        )
        return DispatchResult(
            agent=agent_name,
            success=True,
            result=result_text,
            session_id=result_data.get("session_id") or new_session,
            cost_usd=result_data.get("total_cost_usd"),
            duration_ms=result_data.get("duration_ms"),
            num_turns=result_data.get("num_turns"),
            parsed_result=parsed,
            denied_tools=denied,
            hint=_denial_hint(agent_name, denied) if denied else None,
        )

    # Fallback: no result line received
    stderr = proc.stderr.read() if proc.stderr else ""
    if new_session and _session_flag_unsupported(stderr):
        # Old claude CLI rejected --session-id before doing any work —
        # retry once without the flag (bounded: the retry passes
        # _use_session_flag=False so it can never recurse again).
        logger.warning(
            "claude CLI does not support --session-id; retrying stream "
            "without it (timed-out dispatches will not be resumable)"
        )
        return dispatch_stream(
            agent_name, task, agent, settings, context, on_progress,
            caller=caller, goal=goal, response_format=response_format,
            _use_session_flag=False,
        )
    error_text = stderr.strip() or f"No result received (exit code {proc.returncode})"
    error_type = _classify_error(error_text)
    if error_type == "permission":
        error_text += _permission_hint(agent_name)
    return DispatchResult(
        agent=agent_name,
        success=False,
        result="",
        session_id=new_session,
        error=error_text,
        error_type=error_type,
    )
