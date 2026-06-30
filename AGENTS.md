# AGENTS.md

Guidance for AI coding agents working on this repository.

> **Using agent-dispatch** (not developing it)? Read [README.md](README.md) — it has the full setup path with verify steps and the complete MCP tool reference. This file is for contributing to the codebase.

## What this project is

MCP server + CLI that lets Claude Code agents delegate tasks to agents in other project directories. One sync core, two surfaces:

| File | Role |
|------|------|
| `src/agent_dispatch/runner.py` | Sync subprocess wrapper around `claude -p` — the actual work |
| `src/agent_dispatch/server.py` | Async FastMCP interface (21 MCP tools), wraps runner in `asyncio.to_thread` + semaphore |
| `src/agent_dispatch/cli.py` | Click CLI: `init`, `add`, `update`, `remove`, `list`, `describe`, `test`, `doctor`, `jobs`, `job`, `cancel`, `gc`, `group` (add/list/inspect/update/remove), `serve` |
| `src/agent_dispatch/models.py` | Pydantic v2 models (`AgentConfig`, `DispatchGroup`/`GroupMember`, `Settings`, `DispatchResult`) |
| `src/agent_dispatch/config.py` | YAML config load/save + project auto-description |
| `src/agent_dispatch/cache.py` | Thread-safe in-memory TTL cache |
| `src/agent_dispatch/jobs.py` | Persistent per-job JSON files for async dispatch |

## Dev setup

```bash
pip install -e ".[dev]"
```

## Gates — both must pass before a change is done (CI rejects otherwise)

```bash
ruff check src/ tests/
python3 -m pytest tests/ -v   # 466 tests, ~2s — all subprocess calls are mocked
```

Tests must **never** invoke the real `claude` CLI. Runner tests mock `shutil.which` + `subprocess.run`/`Popen`; server tests mock `_get_config` + `runner.dispatch`.

## Non-obvious invariants (violating these breaks real behavior)

- `allowed_tools` / `disallowed_tools` are **tri-state**: `None` = inherit settings defaults, `[]` = explicitly no tools, `[...]` = exactly these. Check with `is not None`, never `or` — `[]` is falsy but semantically distinct.
- `denied_tools` non-empty + `is_error` ⇒ `error_type="permission"`, regardless of what the error text matches.
- **Groups**: a group's `shared_context` is folded into the `context` *string* before the cache/runner calls (`_merge_group_context` in server.py) — runner.py and cache.py are untouched, the cache key disambiguates groups for free, and `group=""` is byte-identical to a plain dispatch. Membership is validated up front (`_validate_group_member`, separate from the pure merge so `dispatch_parallel`'s all-or-nothing pre-check holds). `DispatchConfig` validates only group *keys*, never member existence — a hard cross-ref check would brick config load when a shared gateway agent is removed; dangling refs are flagged (`unknown:true`) at read time instead.
- On failure, callers read `DispatchResult.error` + `error_type` — `result` holds the raw agent output even on errors.
- `--session-id` and `--resume` conflict — never pass both to `claude`.
- Valid permission modes: `default`, `plan`, `bypassPermissions` (`models.py: KNOWN_PERMISSION_MODES`).
- `JobStore.finish`/`fail` refuse already-terminal jobs (returns `None`) — this closes the race with force-cancel; never "fix" it by overwriting.
- Cancelling a *running* job requires the in-memory `_running_procs` registry (server.py) — the job is marked `cancelled` **before** the subprocess is killed. Don't persist PIDs to disk (PID reuse after restart could kill an unrelated process).
- `max_budget_usd` is **post-hoc**: `_apply_budget` (runner.py) sets `budget_exceeded` + `hint` after the cost is known; it never fails the dispatch.

## Conventions

Python ≥ 3.10 · `from __future__ import annotations` everywhere · Pydantic v2 · Click (CLI) + FastMCP (server) · ruff, line length 100 · all MCP tools return JSON strings, errors as `{"error": "..."}`.

## When adding a feature, check every layer

`models.py` (data shape) → `config.py` (YAML round-trip + empty-collection pruning) → `runner.py` (dispatch mechanics) → `server.py` (MCP tool) → `cli.py` (CLI flag) → tests for each → `README.md` + `agents.example.yaml` (user docs).

## More detail

[README.md](README.md) documents every MCP tool with parameter tables, response shapes, and the error-recovery map — it doubles as the behavioral spec. The test suite (`tests/`, 466 tests) encodes the exact expected behavior of every layer: when in doubt, read the tests for the module you're touching (`test_runner.py`, `test_server.py`, `test_cli.py`, ...).
