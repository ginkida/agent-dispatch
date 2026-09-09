# AGENTS.md

Guidance for AI coding agents working on this repository.

> **Using agent-dispatch** (not developing it)? Read [README.md](README.md) — it has the full setup path with verify steps and the complete MCP tool reference. This file is for contributing to the codebase.

## What this project is

MCP server + CLI that lets Claude Code agents delegate tasks to agents in other project directories. One sync core, two surfaces:

| File | Role |
|------|------|
| `src/agent_dispatch/runner.py` | Sync subprocess wrapper around `claude -p` — the actual work |
| `src/agent_dispatch/server.py` | Async FastMCP interface (21 MCP tools), wraps runner in `asyncio.to_thread` + semaphore |
| `src/agent_dispatch/usage.py` | Append-only dispatch journal (cost/duration/outcome) + aggregation for `stats` and the `typical` block |
| `src/agent_dispatch/servers.py` | Registry of live `serve` processes and the version each one runs (`doctor` reads it) |
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
python3 -m pytest tests/ -v   # 714 tests, ~17s
```

Tests must **never** invoke the real `claude` CLI. Runner tests mock `shutil.which` + `subprocess.run`/`Popen`; server tests mock `_get_config` + `runner.dispatch`. The one exception is `TestStreamPipeHandling`, which spawns a short-lived *python* subprocess: a pipe deadlock lives in the OS pipe buffer, so a mocked `Popen` structurally cannot reproduce it.

## Non-obvious invariants (violating these breaks real behavior)

- `allowed_tools` / `disallowed_tools` are **tri-state**: `None` = inherit settings defaults, `[]` = explicitly no tools, `[...]` = exactly these. Check with `is not None`, never `or` — `[]` is falsy but semantically distinct.
- Error-type precedence on an `is_error` payload (`_build_error_result`): the CLI's own budget stop wins, then `denied_tools` non-empty ⇒ `error_type="permission"` regardless of the error text, then text classification.
- **Groups**: a group's `shared_context` is folded into the `context` *string* before the cache/runner calls (`_merge_group_context` in server.py) — runner.py and cache.py are untouched, the cache key disambiguates groups for free, and `group=""` is byte-identical to a plain dispatch. Membership is validated up front (`_validate_group_member`, separate from the pure merge so `dispatch_parallel`'s all-or-nothing pre-check holds). `DispatchConfig` validates only group *keys*, never member existence — a hard cross-ref check would brick config load when a shared gateway agent is removed; dangling refs are flagged (`unknown:true`) at read time instead.
- On failure, callers read `DispatchResult.error` + `error_type` — `result` holds the raw agent output even on errors.
- `--session-id` and `--resume` conflict — never pass both to `claude`.
- Valid permission modes: `default`, `plan`, `bypassPermissions` (`models.py: KNOWN_PERMISSION_MODES`).
- `JobStore.finish`/`fail` refuse already-terminal jobs (returns `None`) — this closes the race with force-cancel; never "fix" it by overwriting. `mark_running` likewise refuses any job that isn't `pending`, so a stale or duplicate worker can't resurrect a finished one.
- "Is this group member missing?" has exactly one implementation: `DispatchConfig.unknown_group_members()`. Any new surface that lists or validates membership calls it instead of re-deriving the check.
- Cancelling a *running* job requires the in-memory `_running_procs` registry (server.py) — the job is marked `cancelled` **before** the subprocess is killed. Don't persist PIDs to disk (PID reuse after restart could kill an unrelated process).
- `max_budget_usd` is enforced **by the claude CLI** (`_build_command` passes `--max-budget-usd`): a run stopped at the cap comes back `is_error` with no `result` text, and `_build_error_result` turns it into `error_type="budget"` + `budget_exceeded=True` + a resumable `session_id`. `_apply_budget` is the *secondary*, post-hoc signal for an overshoot that didn't stop the run; it never fails a dispatch.
- A CLI error payload can have no `result` field at all — the reason lives in `errors` / `subtype`. Read it via `_cli_error_details`, never assume `result` is populated on failure.
- **Both subprocess pipes must be drained concurrently.** `dispatch_stream` reads stdout in a loop while a daemon thread drains stderr; reading stderr only after the loop deadlocks any child that writes more than ~64 KiB to it (the child blocks in `write(2)`, never emits its result, and the dispatch dies at the timeout). `dispatch()` is immune only because `subprocess.run(capture_output=True)` uses `communicate()`.
- **A received `result` event outranks the timeout flag.** The agent finished and was billed; a lingering process is a cleanup problem, reported as a `hint`, not a failure.
- **The timeout must kill the process *tree*, not the child.** `dispatch_stream` spawns with `start_new_session=True` and `_kill_process_tree` sends SIGKILL to the group: a grandchild that inherited stdout keeps the read loop parked long past the deadline otherwise. `killpg` is guarded on a positive pid — `killpg(0)` would signal the dispatcher's own group.
- **Never `close()` a pipe another thread may still be reading.** `close()` waits on the reader's buffer lock with *no timeout*, so it would hang the dispatch forever — the bounded `join()` before it buys nothing. `dispatch_stream` skips the stderr close while the drain thread is alive and lets the daemon reader + Popen finalizer release the fd. This is not hypothetical: a stdio MCP server inherits the child's `stderr`, so the pipe often has no EOF even after `claude` exits cleanly.
- **No `await` inside `config_lock()`.** `ProcessLock`'s in-process guard is a `threading.RLock` — re-entrant per *thread* — and every MCP tool coroutine runs on the one event-loop thread. Suspending in the critical section lets a second coroutine re-enter the "held" lock and interleave its own load/mutate/save. Collect warnings as data, emit them after the `with` block (`test_no_await_inside_the_config_lock` enforces this by AST).
- Cross-process locks are acquired with a **bounded** wait, never a blocking `flock`: the server takes them on its event-loop thread, so a wedged holder would freeze every tool. After the deadline it proceeds unlocked and logs — a possible lost update beats a permanent freeze.
- `recover_stale` sweeps `pending` on a **much longer** threshold than `running`: the jobs directory is shared by every `agent-dispatch serve`, so an hours-old pending job may still be queued behind another live server's semaphore. For a *running* job, `started_at` alone does **not** prove abandonment — a dispatch may legitimately run to the 7200s timeout ceiling, so the file's own mtime is checked too (a live worker rewrites it on every progress flush). That check can only ever *skip* a recovery, never add one.
- Deleting job records is **opt-in** (`settings.job_retention_days`, default `0` = off) and only ever happens at server start, never inside a tool. They are the user's dispatch history and the deletion is irreversible, so an unreadable config is treated as "do nothing" rather than falling back to a default retention.
- `max_concurrency` must bound *subprocesses*, not coroutines. Cancelling a coroutine does not stop the thread behind `asyncio.to_thread`, so `async with sem:` gave the slot away while `claude` kept running and billing. Dispatches go through `_dispatch_guarded`, which releases from the future's done-callback and `shield`s the await. Never "simplify" it back to `async with`.
- Tool responses are serialized through `server._dumps`, never bare `json.dumps`: the stdlib default (`ensure_ascii=True`) turns every non-ASCII character into a `\uXXXX` escape, tripling the bytes and tokenizing badly, for no gain — the stdio transport emits raw UTF-8 via pydantic anyway. A real `list_groups()` carried 8520 escapes and weighed 59 KB instead of 25 KB.
- `agents.yaml` is parsed with libyaml's `CSafeLoader` when available (`config._YamlLoader`), falling back to the pure-Python **safe** loader — never `yaml.Loader`. The config is re-read on every tool call, so this parse is on the hot path of all 21 tools *and* blocks the event-loop thread: 9.80 ms → 0.76 ms on a real 38 KB config.
- Pydantic does **not** validate on assignment. `Field(ge=...)` guards only the *load* path; every mutation surface (CLI `add`/`update`, MCP `add_agent`/`update_agent`) needs its own boundary check, or the bound escapes as a raw `ValidationError`.
- Every state file (`agents.yaml`, job files) is written **temp file + `os.replace`**, never in place, and every load/mutate/save is wrapped in `config.ProcessLock` — the CLI and the MCP server are separate processes writing the same files, so a thread lock alone loses updates.
- Anything that changes an agent's config must call `_invalidate_agent_cache` — the cache key holds the agent *name*, not its directory or permissions.
- Only *clean* successes are cached: `cache.put` refuses failures, `denied_tools` results, `budget_exceeded` results, and results whose `outcome` is `partial`/`blocked`, so the documented "grant access / fix the cause, then re-dispatch" recovery is never short-circuited.
- **Coerce every number read off disk through `usage.as_float`.** `float("abc")` raises **ValueError** — not `JSONDecodeError`, not `OSError` — so it escaped the per-line handler in `usage.load` and the sort key in `servers.live_servers`, turning `agent-dispatch stats --days 7` and `doctor` into tracebacks over one hand-edited character. Same escape route as the `UnicodeDecodeError`-is-a-`ValueError` round. A journal or registry field is untrusted input: skip the record or sort it last, never raise from the command someone ran *because* something is already wrong.
- **`--json` output must be valid JSON on the empty path too.** `stats --json` printed prose when the journal had no records — i.e. on a fresh install, which is exactly when a script first pipes it somewhere.
- **The usage journal is written by a decorator, not at each return.** `runner._journaled` wraps `dispatch`/`dispatch_stream` because each has a dozen early returns; instrumenting them one by one guarantees the next new return path goes unrecorded. It records the outermost call only (`_use_session_flag` is False on the stream's old-CLI retry, which would otherwise double-count one dispatch). It also swallows exceptions itself even though `usage.record` already does: the result in hand is billed, and the guarantee has to hold at the boundary that owns the damage rather than on another module's promise (`test_a_broken_journal_never_breaks_a_paid_dispatch` removes record()'s guard to prove it).
- **A journal record is one `O_APPEND` write under `PIPE_BUF`**, never a locked read-modify-write: the CLI and every running server share one file, and this user has 14+ servers alive. Every field is capped so the line cannot approach 4096 bytes. Rotation is an atomic rename at ~2 MB, two generations kept.
- **Profiles are memoized on the journal's (path, size, mtime) and read a NARROWER tail than `stats`.** `list_agents`/`inspect_agent` run on the event-loop thread: a full 2 MB journal cost **24.8 ms** per discovery call at the 512 KB report window — 33x the config parse that 0.13.0 exists to have fixed. Now 3.3 ms cold, ~0 warm. Every profile consumer goes through `usage.profiles()`; never add a caller that re-parses per agent.
- **`usage_limit` is its own error type** (observed live: `You've hit your session limit · resets 6pm`). It is checked *before* the permission patterns and its hint says the limit is account-wide, so "try another agent" is not a workaround. Text-classified errors attach their advisory through the single `_classification_hint`, not per site.
- **Server liveness is an advisory lock, not a PID.** `servers.register` holds an exclusive `flock` on `<config dir>/servers/<pid>.json` for the process lifetime; a reader that *takes* that lock owns a dead entry and deletes it. `os.kill(pid, 0)` would believe a recycled PID and would never notice a SIGKILLed server. Never close that fd (re-registering closes the old one first).
- **The dispatch protocol goes in the system prompt, not the task.** `_build_system_prompt` (runner.py) renders the protocol (non-interactive, time/spend budget, lead with the outcome, trailing `STATUS:` line) plus the agent's `instructions`, and `_build_command` passes it as `--append-system-prompt` — on `--resume` too, since the CLI re-applies an appended prompt on every launch. `_build_prompt` (the `-p` text) is unchanged, so the cache key is unchanged. The text always starts with a `##` header line so it can never be read as a flag. `settings.dispatch_protocol: false` turns the protocol off (for CLIs that predate the flag; `doctor` probes `claude --help` for it); per-agent `instructions` still go through when set.
- **`outcome` is lifted from the LAST line of the result** (`_split_outcome`), before JSON parsing, by the shared `_build_success_result` (the success-side twin of `_build_error_result` — both `dispatch` and `dispatch_stream` go through it) and by the plain-text fallback tier. A bare marker line is removed from `result`; a marker with a trailing reason stays. `outcome` never flips `success`; `partial`/`blocked` add a `hint` with the `dispatch_session(...)` continuation, after the denial hint. In JSON mode the protocol omits the STATUS instruction (the JSON footer governs), but a STATUS line that arrives anyway is still stripped so `parsed_result` survives.
- Remediation text is a contract: a hint that names a flag must name one that exists (`test_printed_budget_hint_is_a_runnable_command` feeds the printed flags back into the CLI). Run the command you print.
- The config error sets are declared **once** and in two halves: `config.CONFIG_LOAD_ERRORS` (read) and `config.CONFIG_SAVE_ERRORS` (write — `yaml.dump`'s `RepresenterError` is a `yaml.YAMLError`, therefore neither `OSError` nor `ConfigLoadError`, and used to escape both the MCP guard and the CLI's `_save_or_exit`). Two halves, not one set, because the remediations differ: a failed write is atomic so the old config survives, while a failed read needs the YAML fixed.
- MCP tools that load config carry `@_config_guard` under `@mcp.tool()` so a broken `agents.yaml` — or a failed *write* — returns the `{"error": ...}` envelope instead of a raw traceback. The set of load errors lives in one place (`config.CONFIG_LOAD_ERRORS`) because three surfaces handle it: **`UnicodeDecodeError` is a `ValueError`, not an `OSError`**, and listing types per-site is exactly how a cp1251 config slipped past all three.

- Tests must not touch anything outside `tmp_path`. `test_server.py`'s autouse `_reset_globals` and `test_cli.py`'s `_isolated_config` redirect **both** `AGENT_DISPATCH_CONFIG` and `AGENT_DISPATCH_JOBS_DIR`: a mutation tool that bails out early (unknown agent) still takes `config_lock()` first, which would otherwise create a lock file beside the developer's real config.

- `mcp` is pinned **`>=1.2.0,<2`** deliberately: 2.0 removed `mcp.server.fastmcp`, which `server.py` imports, so an unbounded range gives every fresh install a dead `agent-dispatch serve`. Lifting the cap means porting to `mcp.server.mcpserver.MCPServer` — it is not a dependency bump.
- Verify packaging in a **clean venv**, never the dev machine: build the wheel, install it fresh, import the server. A stale pin in local site-packages hides exactly the failure a new user hits first.
- Run **`twine check dist/*`** before cutting the tag, with a *current* twine. The clean-venv check does not cover this: it proves the wheel installs, not that PyPI's uploader will accept its metadata. `python -m build` pulls the newest hatchling at build time, so the emitted `Metadata-Version` climbs on its own, and `pypa/gh-action-pypi-publish` is pinned to a SHA that bundles a fixed twine — v0.13.0's publish failed on exactly that mismatch (hatchling 1.32 → metadata 2.5; pinned twine 6.1.0 → "not a valid metadata version"). When it happens, bump the action pin rather than pinning the backend down.

## Deliberately not built

These were considered — some fully implemented — and cut on purpose: an agent router / auto-dispatch (`recommend_agent` / `dispatch_auto`, removed before 0.8.0 — a keyword scorer adds little over the calling LLM at a handful of agents, and auto-dispatch can spend money or mutate a repo on a guess); groups as an execution engine (they are a descriptive layer — no routing, no per-group settings); an agent-dispatch-side budget ledger across dispatches (the CLI's own `--max-budget-usd` covers a single run; anything cumulative would need state we deliberately don't keep). Please open an issue with the use case before adding any of them.

## Conventions

Python ≥ 3.10 · `from __future__ import annotations` everywhere · Pydantic v2 · Click (CLI) + FastMCP (server) · ruff, line length 100 · all MCP tools return JSON strings, errors as `{"error": "..."}`.

## When adding a feature, check every layer

`models.py` (data shape) → `config.py` (YAML round-trip + empty-collection pruning) → `runner.py` (dispatch mechanics) → `server.py` (MCP tool) → `cli.py` (CLI flag) → tests for each → `README.md` + `agents.example.yaml` (user docs).

## More detail

[README.md](README.md) documents every MCP tool with parameter tables, response shapes, and the error-recovery map — it doubles as the behavioral spec. The test suite (`tests/`, 714 tests) encodes the exact expected behavior of every layer: when in doubt, read the tests for the module you're touching (`test_runner.py`, `test_server.py`, `test_cli.py`, ...).
