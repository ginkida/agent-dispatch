"""Core dispatch logic: run claude -p in agent directories."""

from __future__ import annotations

import functools
import json
import logging
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable

from . import usage
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


# The dispatched agent's self-reported verdict, asked for by the dispatch
# protocol below. Tolerates markdown decoration ("**STATUS: done**") and a
# trailing reason ("STATUS: partial — DB unreachable").
_OUTCOME_RE = re.compile(
    r"^\s*[*_`]*\s*STATUS\s*:\s*[*_`]*\s*(done|partial|blocked)\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_OUTCOME_DECOR_RE = re.compile(r"^[\s*_`.!]*$")


def _split_outcome(text: str) -> tuple[str, str | None]:
    """Extract the trailing ``STATUS: done|partial|blocked`` line, if any.

    Returns ``(text, outcome)``. The marker line is removed from the text when
    it carries nothing but the marker; a line with a trailing reason
    ("STATUS: partial — migrations pending") is kept in the text, because the
    reason is the useful part, and only the verdict is lifted out. Only the
    LAST non-empty line counts, so an agent quoting the protocol mid-answer
    does not trip it.
    """
    if not text:
        return text, None
    stripped = text.rstrip()
    nl = stripped.rfind("\n")
    last = stripped[nl + 1 :]
    m = _OUTCOME_RE.match(last)
    if not m:
        return text, None
    outcome = m.group(1).lower()
    if not _OUTCOME_DECOR_RE.match(m.group("rest")):
        return text, outcome
    body = stripped[:nl].rstrip() if nl >= 0 else ""
    return body, outcome


def _outcome_hint(agent_name: str, outcome: str | None, session_id: str | None) -> str | None:
    """Advisory for a self-reported unfinished result: what to do next."""
    if outcome not in ("partial", "blocked"):
        return None
    resume = (
        f"dispatch_session(agent='{agent_name}', task='Continue where you left off', "
        f"session_id='{session_id}')"
        if session_id
        else f"dispatch_session(agent='{agent_name}', ...)"
    )
    if outcome == "partial":
        return (
            "The agent reports its work is PARTIAL — read the result for what is "
            f"missing, then continue in the same session via {resume} or "
            "re-dispatch with what it needed."
        )
    return (
        "The agent reports it is BLOCKED — the result names what it needs (context, "
        f"access, a tool, a running service). Supply it and re-dispatch, or resume via {resume}."
    )


_PROTOCOL_HEADER = "## Dispatch protocol"


def _build_system_prompt(
    agent_name: str,
    agent: AgentConfig,
    settings: Settings,
    timeout: int,
    *,
    caller: str | None = None,
    response_format: str | None = None,
) -> str | None:
    """Text for ``--append-system-prompt``: the dispatch protocol + standing orders.

    The protocol tells the dispatched agent what `claude -p` never does on its
    own: that it is non-interactive (a clarifying question ends the run and is
    billed for nothing), what its time and spend budget is, to lead with the
    outcome (``return_ref`` summaries are the HEAD of the text) and to end
    with a ``STATUS:`` line that :func:`_split_outcome` lifts into
    ``DispatchResult.outcome``. In JSON mode the last two bullets are dropped —
    the JSON footer in the task prompt governs the shape of the reply.

    Per-agent ``instructions`` follow under their own header. Returns None
    when there is nothing to send (protocol off, no instructions), so the flag
    is not passed at all. The text always starts with a header line, never
    with ``-``, so it can never be parsed as a flag.
    """
    parts: list[str] = []
    if settings.dispatch_protocol:
        by = f' by "{caller}" (another agent)' if caller else " by another agent"
        budget = _effective_budget(agent, settings)
        budget_note = f", spend cap ${budget:g}" if budget else ""
        bullets = [
            f'You are the agent "{agent_name}", dispatched{by} and running '
            "non-interactively: no human is watching this session.",
            "- Nobody can answer a question or approve an action. Never end with a "
            "clarifying question — state your assumption and proceed; when two "
            "readings differ materially, take the safer one and say which you took.",
            "- If a tool is denied or something is missing, report it and still "
            "finish everything that is possible.",
            f"- Time budget: about {timeout} seconds for the whole run{budget_note}. "
            "Scope the work to fit; a complete partial answer beats an unfinished "
            "perfect one.",
        ]
        if response_format != "json":
            bullets += [
                "- Reply for a machine reader: lead with the outcome or answer, then "
                "the evidence and what you changed; list what you could not do and "
                "why. No preamble, no restating the task.",
                "- End your reply with one final line: `STATUS: done` (task fully "
                "completed), `STATUS: partial` (some of it done — say what is "
                "missing), or `STATUS: blocked` (no progress possible — say what "
                "you need).",
            ]
        parts.append(_PROTOCOL_HEADER + "\n" + "\n".join(bullets))
    if agent.instructions.strip():
        parts.append(f'## Standing instructions for "{agent_name}"\n{agent.instructions.strip()}')
    return "\n\n".join(parts) if parts else None


def _encodable(value: object) -> object:
    """Replace lone surrogates in every string of a parsed JSON value.

    ``json.loads`` turns a ``\\udXXX`` escape without its pair into a str that
    UTF-8 cannot encode. The CLI's output is pure ASCII on the wire, so decoding
    with ``errors="replace"`` never sees it — it is *manufactured* by the parse,
    most often when an agent hand-escapes an emoji in a response_format="json"
    answer. Left in place it makes ``model_dump_json`` raise out of the MCP
    tools, and ``JobStore`` fail to persist a paid result. One total pass at the
    parse boundary keeps every downstream serializer safe.
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # Via UTF-16: each lone surrogate becomes exactly one U+FFFD.
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        return value
    if isinstance(value, list):
        return [_encodable(item) for item in value]
    if isinstance(value, dict):
        return {_encodable(k): _encodable(v) for k, v in value.items()}
    return value


def _loads(text: str) -> object:
    """``json.loads`` for untrusted CLI/agent text; see :func:`_encodable`."""
    return _encodable(json.loads(text))


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
        return _loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None


# The account's own rate/usage limit, observed live 2026-09-09: three concurrent
# dispatches came back with `You've hit your session limit · resets 6pm
# (Asia/Almaty)`. It used to classify as a generic `cli_error`, which is the
# least useful thing to tell a caller — the distinguishing facts are that the
# limit is on the ACCOUNT (so every agent shares it, and a "try another agent"
# retry fails identically) and that it clears at a stated time, so retrying
# before then is pure waste.
_USAGE_LIMIT_PATTERNS = [
    "session limit",
    "usage limit",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota exceeded",
]
_RESET_RE = re.compile(r"resets?\s+(?:at\s+)?([^\n.]{1,40})", re.IGNORECASE)


def _usage_limit_hint(error_text: str) -> str:
    """Actionable guidance for an account-wide usage limit."""
    m = _RESET_RE.search(error_text or "")
    when = f" It resets {m.group(1).strip()}." if m else ""
    return (
        f"\n\nHint: this is the Claude ACCOUNT's usage limit, not this agent's.{when} "
        "Every agent shares it, so dispatching a different agent (or retrying now) "
        "fails the same way — wait for the reset, or reduce concurrent dispatches "
        "(settings.max_concurrency). Nothing was billed for this call."
    )


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
    # Checked before permissions: an account limit is a specific, actionable
    # state, and "limit" text carries none of the permission vocabulary anyway.
    for pattern in _USAGE_LIMIT_PATTERNS:
        if pattern in lower:
            return "usage_limit"
    for pattern in _PERMISSION_PATTERNS:
        if pattern in lower:
            return "permission"
    return "cli_error"


def _classification_hint(agent_name: str, error_type: str, error_text: str) -> str:
    """The advisory that belongs to a text-classified error type ("" when none).

    Declared once because four call sites classify an error and then attach its
    hint; when `usage_limit` was added, three of the four would otherwise have
    kept silently attaching nothing.
    """
    if error_type == "permission":
        return _permission_hint(agent_name)
    if error_type == "usage_limit":
        return _usage_limit_hint(error_text)
    return ""


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


def _effective_budget(agent: AgentConfig, settings: Settings) -> float | None:
    """The spend cap for this dispatch: per-agent, else the settings default."""
    return agent.max_budget_usd or settings.default_max_budget_usd


# The claude CLI enforces `--max-budget-usd` itself: when the cap is reached it
# ends the session and emits a result payload with `is_error: true`, NO `result`
# field, `subtype: "error_max_budget_usd"`, `terminal_reason: "budget_exhausted"`
# and the human-readable reason in `errors`. Verified against claude 2.1.220.
_BUDGET_ERROR_SUBTYPE = "error_max_budget_usd"
_BUDGET_TERMINAL_REASON = "budget_exhausted"

# `errors` also comes from the untrusted subprocess — cap it like denied_tools.
_MAX_ERROR_DETAILS = 5
_MAX_ERROR_DETAIL_CHARS = 300


def _is_budget_error(data: dict) -> bool:
    """True when the CLI ended the session because the spend cap was reached."""
    return (
        data.get("subtype") == _BUDGET_ERROR_SUBTYPE
        or data.get("terminal_reason") == _BUDGET_TERMINAL_REASON
    )


def _cli_error_details(data: dict) -> str:
    """Best available failure text for an error payload that has no `result`.

    CLI-level failures (budget exhausted, max turns, execution errors) carry no
    `result` text — the reason lives in `errors` and `subtype`. Without this the
    caller only ever saw "reported an error with no details".
    """
    errors = data.get("errors")
    if isinstance(errors, list):
        details = [
            text[:_MAX_ERROR_DETAIL_CHARS]
            for text in (str(item).strip() for item in errors[:_MAX_ERROR_DETAILS])
            if text
        ]
        if details:
            return "; ".join(details)
    subtype = data.get("subtype")
    if isinstance(subtype, str) and subtype.strip():
        return f"claude CLI reported '{subtype.strip()[:_MAX_ERROR_DETAIL_CHARS]}'"
    return ""


def _budget_error_hint(agent_name: str, budget: float | None, session_uuid: str | None) -> str:
    """Actionable hint for a dispatch the CLI stopped at the spend cap."""
    cap = f"${budget:g}" if budget else "the configured cap"
    msg = (
        f"\n\nHint: the dispatch was stopped by the spend cap {cap} "
        "(passed to claude as --max-budget-usd), so this answer is incomplete. "
        f"Raise it (agent-dispatch update {agent_name} --max-budget-usd <amount>), "
        "use a cheaper model, or split the task into smaller dispatches."
    )
    if session_uuid:
        msg += (
            f" Partial work may be resumable: dispatch_session(agent='{agent_name}', "
            f"task='Continue where you left off', session_id='{session_uuid}')."
        )
    return msg


def _session_id_from(data: dict, fallback: str | None) -> str | None:
    """The CLI-reported session id when it is usable, else our generated one.

    `data` is the subprocess's JSON: a number or object here raised a
    ValidationError out of the runner (a billed answer lost), and a blank one
    silently dropped resumability. The pre-generated uuid names the same run.
    """
    reported = data.get("session_id")
    if isinstance(reported, str) and reported.strip():
        return reported
    return fallback


def _build_error_result(
    agent_name: str,
    data: dict,
    denied: list[str] | None,
    agent: AgentConfig,
    settings: Settings,
    *,
    session_fallback: str | None,
    exit_code: int | None = None,
) -> DispatchResult:
    """Build the DispatchResult for a CLI payload with ``is_error: true``.

    Shared by ``dispatch`` and ``dispatch_stream`` — the two paths differ only
    in which session id they fall back to and whether an exit code is known.
    Error-type precedence: budget (the CLI's own terminal reason) > denied tools
    (a deterministic signal) > text classification.
    """
    raw_result = data.get("result", "")
    result_text = str(raw_result) if raw_result else ""
    detail = _cli_error_details(data)
    if result_text:
        error_text = result_text
    elif detail:
        error_text = f"Agent '{agent_name}' failed: {detail}"
    else:
        suffix = f" (exit code {exit_code})" if exit_code is not None else ""
        error_text = f"Agent '{agent_name}' reported an error with no details{suffix}"

    session_id = _session_id_from(data, session_fallback)
    budget_error = _is_budget_error(data)
    if budget_error:
        error_type = "budget"
        error_text += _budget_error_hint(agent_name, _effective_budget(agent, settings), session_id)
    elif denied:
        # Denied tools are a stronger signal than substring matching.
        error_type = "permission"
        error_text += _permission_hint(agent_name)
    else:
        error_type = _classify_error(error_text)
        error_text += _classification_hint(agent_name, error_type, error_text)

    return _apply_budget(
        DispatchResult(
            agent=agent_name,
            success=False,
            result=result_text,
            session_id=session_id,
            cost_usd=data.get("total_cost_usd"),
            duration_ms=data.get("duration_ms"),
            num_turns=data.get("num_turns"),
            error=error_text,
            error_type=error_type,
            denied_tools=denied,
            # The cap was hit by definition — flag it even if the reported cost
            # lands a hair under the budget after rounding.
            budget_exceeded=True if budget_error else None,
        ),
        agent,
        settings,
    )


def _journaled(fn: Callable) -> Callable:
    """Record every DispatchResult this function returns in the usage journal.

    Wrapping is what makes the record complete: `dispatch` and `dispatch_stream`
    each have a dozen early returns (recursion, missing CLI, spawn failure,
    timeout, budget stop), and instrumenting them individually would guarantee
    that the next new return path is the one that goes unrecorded. The journal
    is best-effort by construction (`usage.record` swallows its own errors), so
    this can never turn a completed dispatch into a raised exception.

    The stream's old-CLI retry re-enters the same function with
    `_use_session_flag=False`; only the outermost call records, or one dispatch
    would appear twice.
    """

    @functools.wraps(fn)
    def wrapper(agent_name, task, agent, settings, *args, **kwargs):  # noqa: ANN001, ANN002
        started = time.monotonic()
        result = fn(agent_name, task, agent, settings, *args, **kwargs)
        if getattr(settings, "usage_log", True) and kwargs.get("_use_session_flag", True):
            try:
                wall_ms = int((time.monotonic() - started) * 1000)
                usage.record(
                    agent_name,
                    ok=result.success,
                    cost_usd=result.cost_usd,
                    # The CLI's own duration when it reported one; wall time
                    # otherwise, so a timeout still shows up as "this agent took
                    # its whole budget" rather than as a hole in the data.
                    duration_ms=result.duration_ms if result.duration_ms is not None else wall_ms,
                    num_turns=result.num_turns,
                    outcome=result.outcome,
                    error_type=result.error_type,
                    caller=kwargs.get("caller"),
                )
            except Exception as e:  # noqa: BLE001 - see below
                # `usage.record` already swallows everything, so this arm is
                # belt and braces — deliberately. The result in hand has been
                # billed; letting a bookkeeping failure raise from here would
                # destroy a paid-for answer, and the guarantee has to hold at
                # the boundary that owns the damage, not on the other module's
                # promise. A test removes record()'s own guard to prove it.
                logger.debug("Could not journal dispatch for %s: %s", agent_name, e)
        return result

    return wrapper


def _build_success_result(
    agent_name: str,
    data: dict,
    denied: list[str] | None,
    agent: AgentConfig,
    settings: Settings,
    *,
    session_fallback: str | None,
    response_format: str | None,
    extra_hint: str | None = None,
) -> DispatchResult:
    """Build the DispatchResult for a non-error CLI payload.

    Shared by ``dispatch`` and ``dispatch_stream`` (they differ only in the
    session-id fallback and whether the stream's kill-after-result note
    applies), like ``_build_error_result`` for the failure side. Order matters:
    the STATUS line is lifted out *before* JSON parsing, so an agent that adds
    one in JSON mode anyway does not break ``parsed_result``.
    """
    # Coerce like _build_error_result does: a malformed/again-changed CLI payload
    # can carry a null, number or object here, which would raise a ValidationError
    # out of the runner instead of coming back as a DispatchResult.
    raw_result = data.get("result", "")
    result_text = str(raw_result) if raw_result else ""
    result_text, outcome = _split_outcome(result_text)
    parsed = _parse_structured_response(result_text) if response_format == "json" else None
    session_id = _session_id_from(data, session_fallback)
    hints = [
        _denial_hint(agent_name, denied) if denied else None,
        extra_hint,
        _outcome_hint(agent_name, outcome, session_id),
    ]
    hint = " ".join(h for h in hints if h) or None
    return _apply_budget(
        DispatchResult(
            agent=agent_name,
            success=True,
            result=result_text,
            session_id=session_id,
            cost_usd=data.get("total_cost_usd"),
            duration_ms=data.get("duration_ms"),
            num_turns=data.get("num_turns"),
            parsed_result=parsed,
            denied_tools=denied,
            hint=hint,
            outcome=outcome,
        ),
        agent,
        settings,
    )


def _apply_budget(result: DispatchResult, agent: AgentConfig, settings: Settings) -> DispatchResult:
    """Flag a result whose cost exceeded the configured budget (post-hoc).

    The CLI's own ``--max-budget-usd`` stops a session once the cap is reached
    (that path returns ``error_type="budget"``), but the last turn can still
    overshoot, and the cap only covers a single dispatch. This adds the
    post-hoc signal: ``budget_exceeded=True`` plus a hint, without failing the
    result — the money is already spent. Applied to error results too.
    """
    budget = _effective_budget(agent, settings)
    if result.error_type == "budget":
        # Already flagged with a far more actionable message — don't double up.
        return result
    if not budget or not result.cost_usd or result.cost_usd <= budget:
        return result
    result.budget_exceeded = True
    note = (
        f"Cost ${result.cost_usd:.4f} exceeded the configured budget "
        f"${budget:.2f} for this agent. Consider a cheaper model, a tighter "
        f"task, or raising max_budget_usd."
    )
    result.hint = f"{result.hint} {note}" if result.hint else note
    return result


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
        for marker in (
            "unknown option",
            "unrecognized option",
            "unknown argument",
            "unexpected argument",
        )
    )


# Streaming reads stdout line by line while the child also writes stderr. Both
# are pipes, so stderr must be drained concurrently: once its ~64 KiB buffer
# fills the child blocks in write(2), never emits its stdout result line, and
# the parent blocks reading stdout until the timeout kills it — a successful run
# reported as a timeout. Keep the HEAD of stderr (the first error is the useful
# one, and _session_flag_unsupported looks for an option-parsing error that the
# CLI prints first) but keep reading past the cap so the child never blocks.
_MAX_STDERR_CAPTURE = 64 * 1024
_STDERR_CHUNK = 8192
_STDERR_JOIN_SECONDS = 5  # wait for stderr we still need (no result arrived)
_STDERR_GRACE_SECONDS = 0.2  # the agent already answered — stderr is unused

_KILLED_AFTER_RESULT = (
    "The agent produced its final result but the process had not exited by the "
    "timeout and was killed. The result is complete — only cleanup was cut short."
)


def _drain_stream(stream: object, into: list[str]) -> None:
    """Read *stream* to EOF, keeping at most _MAX_STDERR_CAPTURE chars in *into*."""
    read = getattr(stream, "read", None)
    if read is None:
        return
    kept = 0
    try:
        while True:
            chunk = read(_STDERR_CHUNK)
            if not chunk:
                break
            if kept < _MAX_STDERR_CAPTURE:
                into.append(chunk[: _MAX_STDERR_CAPTURE - kept])
                kept += len(chunk)
    except (OSError, ValueError):  # pipe closed under us (kill / orphan cleanup)
        logger.debug("stderr drain ended early")


# dispatch_stream reads stdout through a queue so it can stop waiting for EOF.
_STREAM_EOF = object()
_STREAM_POLL_SECONDS = 0.1
# After a kill, how long a pipe held by an out-of-group descendant may keep us.
_STREAM_ORPHAN_GRACE_SECONDS = 1.0


def _pump_lines(stream: object, into: queue.Queue, abandoned: threading.Event) -> None:
    """Forward *stream*'s lines into *into*, then _STREAM_EOF (always, even on error).

    Once *abandoned* is set nobody reads the queue any more, so lines a
    detached writer keeps producing are dropped instead of piling up.
    """
    try:
        for line in stream:  # type: ignore[attr-defined]
            if not abandoned.is_set():
                into.put(line)
    except (OSError, ValueError):  # pipe closed under us (kill / orphan cleanup)
        logger.debug("stdout reader ended early")
    finally:
        into.put(_STREAM_EOF)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the dispatched process *and everything it spawned*.

    Killing only the direct child is not enough to enforce a timeout: a
    grandchild that inherited the child's stdout/stderr keeps those pipes from
    reaching EOF, so the reader stays blocked for the grandchild's whole
    lifetime — the dispatch overruns its deadline (holding a semaphore slot) no
    matter what the timer does. The child is spawned with ``start_new_session``,
    so it leads its own process group and its pid *is* the group id.
    """
    # Mark first: dispatch_stream reads this to tell "we killed it" (timer or
    # dispatch_cancel) from a leader that merely exited, without reaping it.
    try:
        proc._dispatch_killed = True  # type: ignore[attr-defined]
    except (AttributeError, TypeError):  # pragma: no cover - exotic stand-ins
        pass
    pid = getattr(proc, "pid", None)
    killpg = getattr(os, "killpg", None)
    # pid must be a real, positive id: killpg(0) would signal *our own* process
    # group and take the dispatcher down with the agent. And the leader must
    # not have been reaped yet: once returncode is set its PID is free for
    # reuse, possibly by an unrelated group leader. Read the attribute — poll()
    # would itself reap. Callers therefore reap only after this decision.
    if (
        killpg is not None
        and isinstance(pid, int)
        and pid > 0
        and getattr(proc, "returncode", None) is None
    ):
        try:
            killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError) as e:
            # Already reaped, or no new session (Windows / start_new_session
            # unsupported) — fall through to the plain kill.
            logger.debug("killpg(%s) failed: %s", pid, e)
    try:
        proc.kill()
    except OSError as e:  # pragma: no cover - already gone
        logger.debug("kill failed: %s", e)


def _was_killed(proc: object) -> bool:
    """True once _kill_process_tree has been called on *proc*."""
    return bool(getattr(proc, "_dispatch_killed", False))


def _wait_leader_exit(proc: subprocess.Popen, timeout: float | None) -> bool:
    """Wait up to *timeout* seconds (None = forever) for the leader to exit.

    Does NOT reap it where the platform allows: a reaped leader's PID can be
    recycled into an unrelated process-group leader, so every killpg after
    that point is unsafe (see _kill_process_tree). ``waitid(WNOWAIT)`` on
    Linux, a kqueue NOTE_EXIT on macOS/BSD (which has no waitid). Elsewhere —
    Windows, where there is no killpg to protect — or for stand-ins without a
    pid, fall back to the reaping wait/poll.
    """
    if getattr(proc, "returncode", None) is not None:
        return True
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 0:
        waitid = getattr(os, "waitid", None)
        if waitid is not None and hasattr(os, "WNOWAIT"):
            deadline = None if timeout is None else time.monotonic() + timeout
            delay = 0.005
            while True:
                try:
                    if waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                        return True
                except ChildProcessError:  # reaped by someone else — certainly gone
                    return True
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    delay = min(delay, remaining)
                time.sleep(delay)
                delay = min(delay * 2, 0.05)
        kqueue = getattr(select, "kqueue", None)
        try:
            kq = kqueue() if kqueue is not None else None
        except OSError as e:  # out of descriptors — the reaping fallback still works
            logger.debug("kqueue unavailable: %s", e)
            kq = None
        if kq is not None:
            try:
                event = select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                # An exited-but-unreaped leader comes back at once as an
                # EV_ERROR/ESRCH event rather than NOTE_EXIT — either way a
                # returned event means "gone".
                return bool(kq.control([event], 1, timeout))
            except ProcessLookupError:
                return True
            except OSError as e:  # pragma: no cover - fall back to the reaping wait
                logger.debug("kevent on %s failed: %s", pid, e)
            finally:
                kq.close()
    if timeout is None:
        proc.wait()
        return True
    if timeout <= 0:
        return proc.poll() is not None
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _close_quiet(stream: object) -> None:
    """Close a subprocess pipe if it has a close(); ignore anything it raises."""
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        close()
    except (OSError, ValueError):  # pragma: no cover - best effort
        logger.debug("Failed to close subprocess stream")


def _run_captured(
    cmd: list[str], *, cwd: str, env: dict[str, str], timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Capture both pipes with a deadline covering the child and its descendants.

    Unlike subprocess.run, retain the process handle until the whole group has
    been killed on timeout. Readers own their pipes: a detached descendant may
    keep one open even after killpg, so cleanup joins are bounded and the caller
    never closes a pipe while its reader is alive. Binary read1 captures output
    immediately, including a flushed JSON result without a trailing newline.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    readers: list[threading.Thread] = []
    collecting = threading.Event()
    collecting.set()
    timed_out = False
    finished = False

    def capture(pipe, chunks: list[bytes], limit: int | None) -> None:
        kept = 0
        try:
            while chunk := pipe.read1(_STDERR_CHUNK):
                if not collecting.is_set():
                    continue
                if limit is None:
                    chunks.append(chunk)
                elif kept < limit:
                    chunks.append(chunk[:limit - kept])
                    kept += len(chunk)
        except (OSError, ValueError):
            logger.debug("Captured pipe drain ended early")
        finally:
            _close_quiet(pipe)

    try:
        for pipe, chunks, limit in (
            (proc.stdout, stdout, None), (proc.stderr, stderr, _MAX_STDERR_CAPTURE),
        ):
            reader = threading.Thread(
                target=capture, args=(pipe, chunks, limit), daemon=True,
                name="dispatch-capture",
            )
            reader.start()
            readers.append(reader)
        # Completion is stdout EOF + leader exit — NOT stderr EOF. A descendant
        # that inherited only fd 2 (a stdio MCP server started by claude, say)
        # keeps stderr open after claude exits cleanly; waiting for it burned
        # the whole timeout and then reported a finished run as a timeout (or
        # turned a real stderr-only CLI error into error_type="timeout").
        stdout_reader, stderr_reader = readers
        stdout_reader.join(timeout=max(0, deadline - time.monotonic()))
        if stdout_reader.is_alive():
            raise subprocess.TimeoutExpired(cmd, timeout)
        # Wait for the leader WITHOUT reaping it: an unreaped leader keeps its
        # PID — and so the process-group id — reserved, which is what makes the
        # killpg below (and in the timeout path) safe against PID reuse.
        if not _wait_leader_exit(proc, max(0, deadline - time.monotonic())):
            raise subprocess.TimeoutExpired(cmd, timeout)
        # Whatever the leader wrote to stderr is already in the pipe; this join
        # only decides how long descendants may keep it open. Barely at all once
        # stdout carried an answer; up to _STDERR_JOIN_SECONDS when stderr is
        # the only explanation we will get.
        remaining = max(0, deadline - time.monotonic())
        stderr_reader.join(
            timeout=_STDERR_GRACE_SECONDS if stdout else min(_STDERR_JOIN_SECONDS, remaining)
        )
        if stderr_reader.is_alive():
            # A holder inside the group dies here; one that detached cannot be
            # reached, so the second join is bounded too and the pipe is left
            # to its daemon reader (closing it would block on the reader's lock).
            _kill_process_tree(proc)
            stderr_reader.join(timeout=_STDERR_GRACE_SECONDS)
        proc.wait()  # the leader has exited — this only reaps it
        finished = True
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if not finished:
            _kill_process_tree(proc)
            # Kill is asynchronous. Bound reaping and both reader joins by ONE
            # cleanup deadline, even if a descendant escaped the process group.
            cleanup_deadline = time.monotonic() + 1.0
            try:
                proc.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logger.warning("Dispatched process did not exit after kill")
            for reader in readers:
                reader.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        # Pipes without a started reader (e.g. thread creation failed) still
        # belong to this thread and can safely be closed here.
        for pipe in (proc.stdout, proc.stderr)[len(readers):]:
            _close_quiet(pipe)
        collecting.clear()

    out = b"".join(stdout).decode("utf-8", errors="replace")
    err = b"".join(stderr).decode("utf-8", errors="replace")
    # A detached writer may keep its reader alive. Retain neither this result
    # nor future output in that daemon after the dispatch has returned.
    stdout.clear()
    stderr.clear()
    if timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _timeout_error(agent_name: str, timeout: int, session_uuid: str | None) -> str:
    """Actionable timeout message: how to retry, extend, or resume.

    When the usage journal has enough history, "use a longer timeout" becomes a
    number: `usage.suggested_timeout` pads this agent's real p90 by 50%. Doubling
    the current value was a guess that is wrong in both directions — too small
    for an agent whose runs cluster at 10 minutes, wastefully large for one that
    normally answers in twenty seconds and just hit a bad run.
    """
    suggested = usage.suggested_timeout(agent_name)
    if suggested and suggested <= timeout:
        suggested = None  # already generous; the run failed for another reason
    retry_value = suggested or timeout * 2
    msg = (
        f"Agent '{agent_name}' timed out after {timeout}s. "
        "Options: pass timeout_seconds= for a longer one-off run, use "
        "dispatch_async for fire-and-forget, or raise the agent default "
        f"(agent-dispatch update {agent_name} --timeout {retry_value})."
    )
    if suggested:
        msg += (
            f" Recent runs of this agent suggest timeout_seconds={suggested} "
            "(50% over its measured p90)."
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
            "Claude CLI not found in PATH. Install: https://docs.anthropic.com/en/docs/claude-code"
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
    system_prompt: str | None = None,
) -> list[str]:
    cmd = [claude_path, "-p", task, "--output-format", "json"]

    if system_prompt:
        # Protocol + standing instructions (see _build_system_prompt). Passed
        # on --resume too: with --append-system-prompt the CLI does not snapshot
        # the system prompt, so the text applies fresh to every launch.
        cmd.extend(["--append-system-prompt", system_prompt])

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
        agent.allowed_tools if agent.allowed_tools is not None else settings.default_allowed_tools
    )
    for tool in allowed:
        _reject_flaglike("allowed tool", tool)
        cmd.extend(["--allowedTools", tool])

    disallowed = (
        agent.disallowed_tools
        if agent.disallowed_tools is not None
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


@_journaled
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
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
            error_type="recursion",
        )

    try:
        claude_path = _find_claude()
    except FileNotFoundError as e:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
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

    timeout = agent.timeout or settings.default_timeout
    try:
        cmd = _build_command(
            claude_path,
            full_task,
            agent,
            settings,
            session_id,
            new_session_id=new_session,
            system_prompt=_build_system_prompt(
                agent_name, agent, settings, timeout, caller=caller, response_format=response_format
            ),
        )
    except ArgInjectionError as e:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
            error_type="cli_error",
        )

    # Propagate depth for recursion protection
    env = os.environ.copy()
    env[_DEPTH_ENV_VAR] = str(_current_depth() + 1)

    logger.info("Dispatching to %s (timeout=%ds)", agent_name, timeout)
    logger.debug("Task: %s", task[:200])

    cleanup_timed_out = False
    for attempt in (0, 1):
        try:
            proc = _run_captured(
                cmd,
                cwd=str(agent.directory),
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.output or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            try:
                completed = _loads(output)
            except (ValueError, TypeError):
                completed = None
            # Partial text or unrelated JSON does not prove completion. Only a
            # full CLI result event outranks the deadline, as in streaming.
            if not isinstance(completed, dict) or completed.get("type") != "result":
                return DispatchResult(
                    agent=agent_name,
                    success=False,
                    result="",
                    session_id=session_uuid,
                    error=_timeout_error(agent_name, timeout, session_uuid),
                    error_type="timeout",
                )
            cleanup_timed_out = True
            proc = subprocess.CompletedProcess(cmd, -signal.SIGKILL, output, "")
            break
        # Spawn failures, classified exactly as dispatch_stream does — the
        # is_dir() check above does not prove the child can chdir into it, and
        # the directory can vanish between the check and the spawn.
        except FileNotFoundError as e:
            return DispatchResult(
                agent=agent_name, success=False, result="", error=str(e), error_type="not_found"
            )
        except PermissionError as e:
            return DispatchResult(
                agent=agent_name,
                success=False,
                result="",
                error=f"{e}{_permission_hint(agent_name)}",
                error_type="permission",
            )
        except OSError as e:
            return DispatchResult(
                agent=agent_name, success=False, result="", error=str(e), error_type="cli_error"
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
            # From index 3 — cmd[2] is the prompt (see dispatch_stream).
            idx = cmd.index("--session-id", 3)
            del cmd[idx : idx + 2]
            new_session = None
            session_uuid = session_id
            continue
        break

    if proc.returncode != 0 and not proc.stdout.strip():
        error_text = proc.stderr.strip() or f"claude exited with code {proc.returncode}"
        error_type = _classify_error(error_text)
        error_text += _classification_hint(agent_name, error_type, error_text)
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=error_text,
            error_type=error_type,
        )

    # Parse JSON output
    try:
        data = _loads(proc.stdout)
        if not isinstance(data, dict):
            # Valid JSON but not the CLI's result object (a bare string/array).
            # Every field access below assumes a mapping — fall through to the
            # plain-text tier instead of raising out of the runner.
            raise json.JSONDecodeError("payload is not an object", proc.stdout, 0)
    except json.JSONDecodeError:
        # Fallback: treat stdout as plain text
        text = proc.stdout.strip()
        # rc=0 with no output at all is a swallowed failure, not an empty
        # answer — reporting success would also cache "" for the whole TTL.
        success = proc.returncode == 0 and bool(text)
        if success:
            text, outcome = _split_outcome(text)
            parsed = _parse_structured_response(text) if response_format == "json" else None
            return DispatchResult(
                agent=agent_name,
                success=True,
                result=text,
                parsed_result=parsed,
                session_id=session_uuid,
                hint=_outcome_hint(agent_name, outcome, session_uuid),
                outcome=outcome,
            )
        error_text = (
            text
            or proc.stderr.strip()
            or f"claude exited with code {proc.returncode} and produced no output"
        )
        error_type = _classify_error(error_text)
        error_text += _classification_hint(agent_name, error_type, error_text)
        return DispatchResult(
            agent=agent_name,
            success=False,
            result=text,
            # Keep the run resumable: the session exists even when the CLI
            # returned nothing parseable.
            session_id=session_uuid,
            error=error_text,
            error_type=error_type,
        )

    denied = _extract_denied_tools(data)

    is_error = data.get("is_error", False)
    if is_error:
        return _build_error_result(
            agent_name,
            data,
            denied,
            agent,
            settings,
            session_fallback=session_uuid,
            exit_code=proc.returncode,
        )

    return _build_success_result(
        agent_name,
        data,
        denied,
        agent,
        settings,
        session_fallback=session_uuid,
        response_format=response_format,
        extra_hint=_KILLED_AFTER_RESULT if cleanup_timed_out else None,
    )


@_journaled
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
    on_proc: Callable[[subprocess.Popen], None] | None = None,
    _use_session_flag: bool = True,
) -> DispatchResult:
    """Run a task via claude -p with streaming output and progress callbacks.

    Uses --output-format stream-json to read intermediate results while the
    agent works.  Each assistant message and tool-use event is forwarded to
    *on_progress* so callers can surface live updates.

    *on_proc* is called with the Popen handle right after spawn — callers use
    it to register the process for external cancellation (it may be called a
    second time if the old-CLI retry respawns).

    ``_use_session_flag`` is internal: set to False on the one-shot retry when
    the installed claude CLI rejects ``--session-id`` (old version).
    """
    try:
        _check_recursion(settings.max_dispatch_depth)
    except RecursionError as e:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
            error_type="recursion",
        )

    try:
        claude_path = _find_claude()
    except FileNotFoundError as e:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
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
    timeout = agent.timeout or settings.default_timeout

    try:
        cmd = _build_command(
            claude_path,
            full_task,
            agent,
            settings,
            new_session_id=new_session,
            system_prompt=_build_system_prompt(
                agent_name, agent, settings, timeout, caller=caller, response_format=response_format
            ),
        )
    except ArgInjectionError as e:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
            error_type="cli_error",
        )
    # Switch from json to stream-json. Current claude CLIs refuse
    # `--print --output-format stream-json` without --verbose (non-verbose
    # print mode only emits the final result, which defeats streaming).
    # Search from index 3: cmd[2] is the caller-supplied prompt, and a task whose
    # text is exactly "--output-format" would otherwise match it and rewrite the
    # prompt instead of the flag.
    fmt_idx = cmd.index("--output-format", 3)
    cmd[fmt_idx + 1] = "stream-json"
    cmd.append("--verbose")

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
            encoding="utf-8",
            errors="replace",  # see dispatch(): strict decoding raises mid-stream
            env=env,
            # Own process group, so the timeout can take the whole tree down
            # (see _kill_process_tree). Without it a backgrounded grandchild
            # holds the inherited stdout pipe open and the read loop below
            # outlives the deadline by the grandchild's entire lifetime.
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
            error_type="not_found",
        )
    except PermissionError as e:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
            error_type="permission",
        )
    except OSError as e:
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            error=str(e),
            error_type="cli_error",
        )

    if on_proc is not None:
        try:
            on_proc(proc)
        except BaseException:
            # Registration can fail (for example while reading job storage).
            # No reader or timer exists yet to clean up the spawned process.
            _kill_process_tree(proc)
            proc.wait()
            _close_quiet(proc.stdout)
            _close_quiet(proc.stderr)
            raise

    # Drain stderr concurrently — see _MAX_STDERR_CAPTURE for why this cannot
    # wait until the stdout loop is done.
    stderr_chunks: list[str] = []
    stderr_reader = threading.Thread(
        target=_drain_stream,
        args=(proc.stderr, stderr_chunks),
        daemon=True,
        name="dispatch-stderr",
    )

    # Read stdout on its own thread too, so the loop below can stop waiting for
    # EOF. killpg reaches only the process group: a descendant that detached
    # (setsid / start_new_session) and inherited fd 1 keeps the pipe open
    # after the timer or dispatch_cancel killed everything else, and a bare
    # `for line in proc.stdout` would then block forever — holding the
    # caller's concurrency slot. _run_captured bounds the same case.
    lines: queue.Queue = queue.Queue()
    abandoned = threading.Event()
    stdout_reader = threading.Thread(
        target=_pump_lines,
        args=(proc.stdout, lines, abandoned),
        daemon=True,
        name="dispatch-stdout",
    )

    # Kill the process if it exceeds the timeout
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        _kill_process_tree(proc)

    timer = threading.Timer(timeout, _kill)
    try:
        stderr_reader.start()
        stdout_reader.start()
        timer.start()
    except BaseException:
        # "can't start new thread" leaves a live claude with no timer and no
        # reader: it would run (and bill) unsupervised after we raise. The
        # leader is unreaped here, so the group kill is still PID-safe.
        abandoned.set()
        timer.cancel()
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=_STDERR_JOIN_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("Dispatched process did not exit after kill")
        for reader, pipe in ((stderr_reader, proc.stderr), (stdout_reader, proc.stdout)):
            if reader.ident is not None:
                reader.join(timeout=_STDERR_GRACE_SECONDS)
            if not reader.is_alive():
                _close_quiet(pipe)
        raise

    def _emit_progress(text: str) -> None:
        # Progress is best-effort. An async job's callback writes the job file,
        # so ENOSPC/EACCES there used to escape this loop, kill the process tree
        # mid-task and drop the paid result together with its resumable session.
        try:
            on_progress(text)
        except Exception:  # noqa: BLE001 - any callback failure is non-fatal here
            logger.warning("on_progress callback failed; continuing", exc_info=True)

    result_data: dict | None = None
    stdout_eof = False
    killed_gone_at: float | None = None
    try:
        while True:
            try:
                line = lines.get(timeout=_STREAM_POLL_SECONDS)
            except queue.Empty:
                line = None
            if line is _STREAM_EOF:
                stdout_eof = True
                break
            # Only after a kill (timer or dispatch_cancel) whose leader is gone
            # may we stop short of EOF: whoever still holds stdout then is out
            # of our reach. A leader that exited on its own keeps us waiting —
            # an in-group grandchild may still hold the pipe, and the timer's
            # killpg is what ends it.
            if (timed_out.is_set() or _was_killed(proc)) and _wait_leader_exit(proc, 0):
                now = time.monotonic()
                if killed_gone_at is None:
                    killed_gone_at = now
                elif now - killed_gone_at >= _STREAM_ORPHAN_GRACE_SECONDS:
                    logger.warning(
                        "stdout of %s still open after its process group was killed "
                        "(a detached descendant likely inherited it); abandoning the pipe",
                        agent_name,
                    )
                    break
            if line is None:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                data = _loads(line)
            except json.JSONDecodeError:
                logger.debug("Non-JSON line in stream: %s", line[:200])
                continue
            if not isinstance(data, dict):
                continue

            msg_type = data.get("type")
            if msg_type == "result":
                result_data = data
            elif msg_type == "assistant" and on_progress:
                message = data.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if (
                            block.get("type") == "text"
                            and isinstance(block.get("text"), str)
                            and block["text"]
                        ):
                            _emit_progress(block["text"][:500])
                        elif block.get("type") == "tool_use":
                            _emit_progress(f"Using tool: {block.get('name', '?')}")

        # Wait for the leader to exit, with the timer still armed (a leader that
        # closed stdout but lingers is killed by it) and WITHOUT reaping: the
        # tree-kill decision below needs the group id still reserved.
        _wait_leader_exit(proc, None)
    finally:
        timer.cancel()
        # A timer already mid-_kill must finish before we reap below, or its
        # killpg could race the PID being freed.
        join_timer = getattr(timer, "join", None)
        if join_timer is not None:
            join_timer(timeout=1)
        abandoned.set()
        # Ensure nothing is left orphaned on any exit path: a live leader, or
        # anyone in its group still holding stdout. Decide BEFORE reaping.
        if not stdout_eof or not _wait_leader_exit(proc, 0):
            _kill_process_tree(proc)
        # Usually the child is gone and the drain thread hits EOF at once. Not
        # always: EOF needs *every* holder of the write end to close it, and a
        # stdio MCP server (or any grandchild) inherits fd 2 and can outlive
        # `claude`. Only the no-result, not-timed-out path below reads this
        # text, so wait for it just long enough to be useful and never longer.
        need_stderr = result_data is None and not timed_out.is_set() and stdout_eof
        stderr_reader.join(timeout=_STDERR_JOIN_SECONDS if need_stderr else _STDERR_GRACE_SECONDS)
        if stderr_reader.is_alive():
            # Same as _run_captured: an in-group holder is an orphan now — the
            # leader is still unreaped, so the group id is still ours to kill.
            _kill_process_tree(proc)
            stderr_reader.join(timeout=_STDERR_GRACE_SECONDS)
        proc.wait()
        stderr = "".join(stderr_chunks)
        # NEVER close a pipe another thread is still reading: close() waits on
        # the BufferedReader lock that the blocked read() holds, and that wait
        # is untimed — it would hang dispatch_stream forever (and with it the
        # caller's semaphore slot), which is exactly what the bounded joins
        # exist to prevent. The daemon threads and Popen's finalizer release
        # the fds once the last writer goes away.
        if not stdout_reader.is_alive():
            _close_quiet(proc.stdout)
        if stderr_reader.is_alive():
            logger.log(
                logging.DEBUG if result_data else logging.WARNING,
                "stderr of %s still open (a grandchild likely inherited it); "
                "leaving the pipe to the daemon reader",
                agent_name,
            )
        else:
            _close_quiet(proc.stderr)

    # A received `result` event beats the clock: the agent finished (and was
    # billed) — only its exit lagged past the deadline. Reporting a timeout here
    # would throw away a complete, paid-for answer.
    if result_data:
        denied = _extract_denied_tools(result_data)
        is_error = result_data.get("is_error", False)
        if is_error:
            return _build_error_result(
                agent_name,
                result_data,
                denied,
                agent,
                settings,
                session_fallback=new_session,
            )
        return _build_success_result(
            agent_name,
            result_data,
            denied,
            agent,
            settings,
            session_fallback=new_session,
            response_format=response_format,
            extra_hint=_KILLED_AFTER_RESULT if timed_out.is_set() else None,
        )

    if timed_out.is_set():
        return DispatchResult(
            agent=agent_name,
            success=False,
            result="",
            session_id=new_session,
            error=_timeout_error(agent_name, timeout, new_session),
            error_type="timeout",
        )

    # Fallback: no result line received
    if new_session and _session_flag_unsupported(stderr):
        # Old claude CLI rejected --session-id before doing any work —
        # retry once without the flag (bounded: the retry passes
        # _use_session_flag=False so it can never recurse again).
        logger.warning(
            "claude CLI does not support --session-id; retrying stream "
            "without it (timed-out dispatches will not be resumable)"
        )
        return dispatch_stream(
            agent_name,
            task,
            agent,
            settings,
            context,
            on_progress,
            caller=caller,
            goal=goal,
            response_format=response_format,
            on_proc=on_proc,
            _use_session_flag=False,
        )
    error_text = stderr.strip() or f"No result received (exit code {proc.returncode})"
    error_type = _classify_error(error_text)
    error_text += _classification_hint(agent_name, error_type, error_text)
    return DispatchResult(
        agent=agent_name,
        success=False,
        result="",
        session_id=new_session,
        error=error_text,
        error_type=error_type,
    )
