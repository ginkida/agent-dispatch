# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-06-30

Coordinate a group of related projects from one session.

### Added
- **Project groups.** A new `groups` mapping in the config bundles related
  agents — code repos plus capability gateways like infra (Portainer) or
  analytics (browser / Yandex Metrica) — into a cross-project working set.
  Each group has an orchestrator-facing `description` (how to coordinate, never
  sent to members) and a member-facing `shared_context` of facts (stack names,
  ids, conventions). Members reference agents by name; membership is
  many-to-many (a shared gateway can belong to several groups). A group is a
  *descriptive layer*, not an execution engine — there is no router; the
  orchestrating LLM coordinates with the existing dispatch tools.
- **`list_groups()` / `inspect_group(name)` MCP tools** — cheap, no-subprocess
  readouts of groups, their briefs, and members (dangling member refs are
  flagged, never crash). For a deep dive on a member, use `inspect_agent`.
- **`group=` on `dispatch` and per-item in `dispatch_parallel`** — when set,
  the agent must be a member of the group and the group's `shared_context` is
  auto-prepended to the call's `context`. Folded into the context string, so
  the cache key disambiguates groups automatically and `group=""` is byte-for-
  byte identical to a plain dispatch. Parallel validates membership up front
  (one bad item rejects the whole call before any subprocess runs).
- **`agent-dispatch group` CLI** — `add` / `list` / `inspect` / `update` /
  `remove` for managing groups, mirroring the agent commands.

### Changed
- `save_config` prunes an empty `groups` block and empty member lists so
  group-less configs stay clean in YAML (same idiom as capabilities).

## [0.8.0] - 2026-06-17

Let agents declare what they are good at, so callers can pick the right one.

### Added
- **Declared capabilities.** `AgentConfig` gains `capabilities` and
  `risky_capabilities` — short snake_case labels describing what an agent is
  for (e.g. `docker_logs`, `restart_services`). They are descriptive metadata
  only (never passed to the `claude` CLI): settable via `add_agent` /
  `update_agent` (MCP) and `add` / `update` (CLI, `--capabilities` /
  `--risky-capabilities`, `none` clears), and surfaced in `list_agents` /
  `inspect_agent` so the calling agent can choose a target at a glance.
  `risky_capabilities` flags higher-risk abilities for extra scrutiny.

### Changed
- `save_config` no longer writes empty `capabilities` / `risky_capabilities`
  keys for agents that don't declare them, keeping `agents.yaml` clean.

### Note
- A keyword-scoring router (`recommend_agent` / `dispatch_auto` MCP tools and
  `recommend` / `auto` CLI commands) was prototyped during this cycle and then
  removed before release: a deterministic keyword scorer adds little over the
  calling LLM's own judgment when there are only a handful of agents, and the
  capability labels above cover the "what is this agent for" need without the
  extra surface area or the risk of auto-dispatching to a wrong guess.

## [0.7.0] - 2026-06-10

Job control release: running jobs become cancellable, the budget field stops
being decorative, and async jobs get a CLI.

### Added
- **Cancel running jobs.** `dispatch_cancel(job_id)` now kills a *running*
  job's `claude` subprocess when the job was started by the same server
  instance (in-memory process registry — no PID files, no risk of killing an
  unrelated process after a restart). The job is marked `cancelled` *before*
  the kill, and `JobStore.finish`/`fail` now refuse already-terminal jobs, so
  the worker's trailing write can't resurrect it. New outcome:
  `cancelled_running`. Jobs from a previous server run still report
  `running` (cannot be killed safely).
- **Budget visibility (post-hoc).** `max_budget_usd` was stored and displayed
  but never checked. A dispatch whose `cost_usd` exceeds the agent's
  `max_budget_usd` (or `settings.default_max_budget_usd`) now returns
  `budget_exceeded: true` plus a `hint`. The dispatch is *not* failed — the
  `claude` CLI has no spend cap, so by the time the cost is known the money is
  spent; the flag makes runaway agents visible instead of silent.
- **CLI for async jobs.** New commands: `agent-dispatch jobs [--status
  --limit]` (list), `agent-dispatch job <id>` (detail with progress tail and
  result preview), `agent-dispatch cancel <id>` (pending jobs; running jobs
  belong to the MCP server process), `agent-dispatch gc [--days]` (purge old
  terminal jobs).
- **PyPI discoverability:** expanded package keywords (5 → 12).

### Changed
- `runner.dispatch_stream` accepts an `on_proc` callback (receives the Popen
  handle right after spawn) — used by the async worker to register the
  process for cancellation.
- `JobStore.cancel` accepts `force=True` to cancel running jobs (callers must
  kill the subprocess themselves); `finish`/`fail` return `None` for terminal
  jobs instead of overwriting them.

## [0.6.0] - 2026-06-04

Reliability release: timeouts stop being fatal, permission-blocked "successes"
become visible, async jobs show live progress.

### Fixed
- **`dispatch_stream` was broken on current claude CLIs** — they reject
  `--print --output-format stream-json` without `--verbose` ("requires
  --verbose"), so every stream dispatch (and CLI `test --stream`) failed
  immediately. The runner now passes `--verbose`. Caught by live verification
  against the real CLI before this release; without it the async-worker
  switch to streaming (below) would have broken all `dispatch_async` jobs.

### Added
- **Per-call timeout override.** `dispatch`, `dispatch_session`,
  `dispatch_stream`, and `dispatch_async` accept `timeout_seconds` (0 = agent
  default, clamped to 10–7200); `dispatch_parallel` accepts it per item. Use
  it for known-long tasks instead of editing the agent config. CLI:
  `agent-dispatch test <name> --timeout N`.
- **Resumable timeouts.** Fresh dispatches pre-assign a session UUID via
  `--session-id`, so a timed-out dispatch still returns a `session_id` — the
  partial transcript survives the kill. The timeout error now spells out the
  recovery options: resume via `dispatch_session(..., session_id=...)`, retry
  with `timeout_seconds`, or go async.
- **Denied-tools visibility.** The claude CLI's `permission_denials` output is
  parsed into `DispatchResult.denied_tools`. A dispatch that "succeeds" while
  tools were blocked (the agent answers "I need permission for X") now carries
  `denied_tools` + a `hint` that the result may be incomplete and how to grant
  access. On `is_error` results, non-empty denials force
  `error_type="permission"` even when the error text has no permission
  keywords. CLI `test` prints the hint as a yellow note.
- **Async job progress.** Async workers now run with streaming: the job file
  keeps a rolling tail (last 20 lines, throttled to ~1 write/sec) of assistant
  text and tool-use events. `dispatch_status` returns it as `progress` while
  running (kept afterwards as a post-mortem trace); `dispatch_jobs` shows
  `last_progress` for running jobs. New `JobStore.update_progress` (refuses
  terminal jobs, so a trailing write can't resurrect a finished job).

### Changed
- Timeout error messages are actionable (mention `timeout_seconds`,
  `dispatch_async`, `agent-dispatch update --timeout`, and the resumable
  session) instead of just "increase timeout in agents.yaml".
- Plain-text fallback successes now carry the generated `session_id`; the
  stream "no result line" fallback does too (a crash mid-stream stays
  resumable).
- **Old-CLI self-healing**: if the installed claude CLI predates
  `--session-id`, dispatch detects the "unknown option" rejection and retries
  once without the flag (logged warning; timed-out dispatches lose
  resumability) instead of failing every dispatch.
- `dispatch_parallel` validates per-item `timeout_seconds` / `summary_chars`
  numerically **up front** — a bad value rejects the whole call before any
  dispatch runs, consistent with the structural validation contract.
- `denied_tools` parsing is bounded (10 entries, 100 chars per name) — the
  field comes from the dispatched subprocess's output, which is untrusted;
  unbounded lists could inflate job files and `return_ref` payloads.

## [0.5.0] - 2026-06-01

Security-hardening release. A multi-agent audit of the codebase surfaced
several issues; the confirmed ones are fixed here, plus job cancellation,
cache bounding, and stale-job recovery.

### Security
- **Path traversal in async jobs (fixed).** `dispatch_status`, `dispatch_wait`,
  and `fetch_result` accept a caller-supplied `job_id`/`ref` that flowed
  straight into `JobStore`'s file-path construction. A crafted value such as
  `../../secret` could read any Job-shaped `.json` file outside the jobs
  directory. Job ids are now validated against `^[0-9a-f]{32}$` at the tool
  boundary (`_validate_ref`), in `JobStore.get`, and in `JobStore._path`
  (defense in depth). Malformed ids are rejected without touching the
  filesystem. New helper `jobs.is_valid_job_id`.
- **Argument/flag injection via structured CLI fields (fixed).** A
  `session_id` (caller-controlled in `dispatch_session`) — or a misconfigured
  `model`, `permission_mode`, or tool name — that started with `-` was placed
  in the argument position after a flag (e.g. `--resume <session_id>`) and the
  `claude` CLI parsed it as a *new* flag, allowing options like
  `--permission-mode bypassPermissions` to be smuggled in. `_build_command`
  now rejects any such value via `_reject_flaglike` (raising
  `runner.ArgInjectionError`); `dispatch`/`dispatch_stream` surface it as a
  clean failed result, never spawning a subprocess.
- **Tightened file permissions.** Job files are written `0o600` and the jobs
  directory is created `0o700` (they hold full task/context/result payloads
  that may contain secrets). `save_config` now writes `agents.yaml` `0o600`
  and its parent directory `0o700`. All `chmod`s are best-effort (skipped on
  platforms without POSIX modes).

### Added
- `dispatch_cancel(job_id)` MCP tool — cancel a *pending* async job before it
  starts. Running jobs are left to finish (their subprocess can't be safely
  interrupted); the tool reports an `outcome` of `cancelled`, `running`,
  `already_terminal`, or `not_found`. Makes the previously-unreachable
  `cancelled` job status real. Backed by `JobStore.cancel`, and the
  cancel/start race is closed by `mark_running` refusing a cancelled job.
- Cache size bound — `CacheSettings.max_size` (default 1000) caps the
  in-memory dispatch cache, evicting the oldest entry first (FIFO by insertion
  time; read access does not refresh, since the timestamp also drives TTL),
  preventing unbounded memory growth from many unique requests. `cache_stats`
  now reports `max_size` and `evictions`.
- Stale-job recovery — on startup the server marks jobs abandoned in
  `running` (older than 1h, e.g. from a crashed prior run) as `failed` so
  callers don't poll them forever (`JobStore.recover_stale`).

### Changed
- Input bounds hardened across MCP tools: `dispatch_jobs(limit)` clamped to
  `[1, 1000]`; `dispatch_gc(max_age_days)` rejects non-finite values;
  `summary_chars` (in `dispatch` and per-item `dispatch_parallel`) clamped to
  `[0, 100000]`; `dispatch_parallel` rejects more than
  `max(100, max_concurrency * 20)` items to bound subprocess fan-out.
- Async job worker now logs lifecycle transitions (running / finished) with
  the job id for easier production debugging.
- Type hints filled in (`_ref_payload`, `_run_job`, `_run_one`).
- Lint surface expanded — ruff now enforces bugbear (`B`), bandit security
  (`S`), import order (`I`), and pyupgrade (`UP`) in addition to the defaults,
  with documented ignores for the trusted `claude` subprocess calls.
- `SECURITY.md` rewritten: accurate supported-versions table and an expanded
  threat model (bypassPermissions, on-disk job files, env inheritance,
  best-effort recursion depth, argument-injection mitigation).

## [0.4.0] - 2026-05-15

### Added
- Result references — `dispatch(..., return_ref=True)` and per-item in
  `dispatch_parallel` now return a compact `{ref, agent, success, size,
  summary, summary_chars, cost_usd, ...}` payload instead of the full
  result text. The full DispatchResult is persisted to disk (reusing the
  async JobStore) and can be loaded on demand via the new
  `fetch_result(ref, max_chars=0)` MCP tool. Saves caller context when
  the result is large; the JSON parsed_result (small by nature) is still
  inlined alongside the ref. fetch_result also works on any
  `dispatch_async` job_id — the storage is shared.
- `JobStore.create_completed(...)` — persists an already-finished
  DispatchResult as a Job in terminal state. Used by ref mode; future
  iterations can use it for result archival.
- Structured JSON response support — `dispatch`, `dispatch_session`,
  `dispatch_async`, `dispatch_stream`, and per-item in `dispatch_parallel`
  now accept `response_format="json"`. When set, the runner appends a clear
  "respond with a single JSON value, no prose, no fences" footer to the
  prompt and attempts to parse the agent's response (tolerating ```json
  fences). The parsed value lands in a new `DispatchResult.parsed_result`
  field — `None` when not requested or unparseable (soft mode: parse
  failure does NOT mark the dispatch as failed). Cache key now includes
  `response_format` so JSON and text requests for the same task don't
  collide.
- `list_agents` MCP tool now surfaces `mcp_servers`, `stacks`, and `dbs`
  per agent (when present) — the same structured data `auto_describe`
  already collects from `.mcp.json`, `Dockerfile`, `pyproject.toml`,
  `package.json`, `Cargo.toml`, `go.mod`, `prisma/`, `alembic.ini`, etc.
  Calling agents no longer need to dispatch a probe just to learn what
  tools the target has.
- New `inspect_agent(name, preview_lines=40)` MCP tool — cheap detailed
  lookup without a `claude` subprocess. Returns the agent's full config
  fields (timeout, model, budget, permission_mode, tool lists), detected
  MCP/stacks/DBs, plus short previews of `CLAUDE.md` and `README.md` so
  the caller can confirm capabilities before spending a real dispatch.
- `config.collect_mcp_servers()`, `config.detect_stacks()`, and
  `config.detect_dbs()` are now public helpers (the previous private
  `_collect_mcp_servers` remains as an alias for compatibility).
- Async dispatch with a `job_id` pattern — five new MCP tools let calling
  agents fire-and-forget long-running tasks without blocking their own tool
  slot:
  - `dispatch_async(agent, task, ...)` — start a dispatch in the background,
    returns `{job_id, status: "pending", agent}` immediately.
  - `dispatch_status(job_id)` — read the current state of a job without
    blocking (pending / running / done / failed) including the
    `DispatchResult` once complete.
  - `dispatch_wait(job_id, timeout_seconds=60)` — block until terminal or
    until the timeout fires (capped at 3600s). Returns the same shape as
    `dispatch_status` plus `timed_out_waiting: true` on timeout — the job
    keeps running and the caller can poll/wait again.
  - `dispatch_jobs(status?, limit=50)` — list recent jobs as summaries,
    optionally filtered by status (most recent first).
  - `dispatch_gc(max_age_days=7)` — purge terminal jobs older than the
    threshold. Pending and running jobs are never touched.
- Job state persists to disk as one JSON file per job under
  `~/.config/agent-dispatch/jobs/` (override via `AGENT_DISPATCH_JOBS_DIR`).
  Atomic writes via `os.replace()` so partial files never appear, and jobs
  survive across server restarts (existing terminal jobs remain queryable,
  in-flight jobs are abandoned on restart — to be addressed in a future
  iteration with PID tracking).

## [0.3.0] - 2026-05-08

### Added
- `agent-dispatch doctor` CLI command — diagnoses installation issues:
  checks `claude` CLI on PATH, `agent-dispatch` on PATH, config validity,
  MCP registration with Claude Code, and per-agent directory health.
  Exits non-zero if any blocking issue is found.
- `agent-dispatch describe <name>` CLI command — show one agent's full
  configuration: directory, description, timeout, model, budget, permission
  mode, tri-state tool fields (`(inherit defaults)` vs `(none — explicit
  override)` vs explicit list), and which project files would be inherited.
- `--stream` flag for `agent-dispatch test` — surfaces live progress
  (assistant text + tool use) while the agent works, useful for long
  tasks where you'd otherwise see nothing until completion.

### Fixed
- `list_agents` MCP tool no longer crashes the entire response when one
  agent's directory is unreadable (`PermissionError`, network FS hiccup,
  etc.). The bad agent now reports `healthy: "UNREADABLE"` and the rest
  of the listing succeeds — matching the documented response shape.
- Dispatch cache key now includes `caller` and `goal`. Previously two
  requests with the same `(agent, task, context)` but different framing
  (e.g. `caller="frontend"` vs `caller="backend"`) would collide and the
  second request would receive the cached response from the first — even
  though the structured prompt sent to Claude is materially different.

## [0.2.2] - 2026-04-17

### Fixed
- `agent-dispatch list` now distinguishes `allowed_tools: None` (inherit
  from settings defaults) from `allowed_tools: []` (explicitly no tools).
  Previously both were rendered identically.

## [0.2.1] - 2026-04-17

### Fixed
- 13 bugs across the runner, server, CLI, config, and models:
  - Runner: defensive coercion in `_classify_error` for non-string inputs;
    fallback messages when `is_error=True` produces empty `result`;
    correct error_type classification on plain-text stdout fallbacks;
    orphan subprocess cleanup on stream exit paths.
  - Server: up-front validation in `dispatch_parallel` (rejects bad items
    before any dispatch runs); `dispatch_dialogue` surfaces per-turn errors;
    `cache_stats` evicts expired entries before reporting.
  - CLI: friendly error messages on malformed YAML / invalid schema;
    `list` handles `OSError` from unreachable directories;
    sentinel patterns for `update` to clear fields (`"none"` / `""`).
  - Config: deduplication when collecting MCP servers from multiple paths.
  - Models: tighter validation bounds (`ge=0`, `ge=1`).

## [0.2.0] - 2026-04-16

### Added
- Error classification — `DispatchResult.error_type` now reports
  `permission`, `timeout`, `recursion`, `not_found`, or `cli_error`.
  Permission errors include an actionable hint with suggested fixes.
- Permission management — agents and global settings support
  `permission_mode`, `allowed_tools`, and `disallowed_tools`. Tool lists
  use tri-state semantics: `None` inherits from defaults, `[]` overrides
  to "no tools", a list specifies the allowed/disallowed set.
- `update_agent` MCP tool — modify an existing agent's configuration
  without remove + re-add. CLI parity via `agent-dispatch update`.
- CLI tests for `init` and `test` commands.

## [0.1.0] - 2026-04-10

### Added
- Initial release.
- 11 MCP tools: `list_agents`, `add_agent`, `remove_agent`, `dispatch`,
  `dispatch_session`, `dispatch_parallel` (with optional aggregation),
  `dispatch_stream`, `dispatch_dialogue`, `cache_stats`, `cache_clear`.
- CLI: `init`, `add`, `remove`, `list`, `test`, `serve`.
- Recursion protection via `AGENT_DISPATCH_DEPTH` env var.
- In-memory TTL cache (thread-safe).
- Concurrency control via `asyncio.Semaphore` (default: 5 parallel
  `claude -p` processes).
- Auto-description from `CLAUDE.md`, `README.md`, `pyproject.toml`,
  `package.json`, `.mcp.json`, and stack/DB indicators.
- PyPI publishing via Trusted Publisher (OIDC).
- CI matrix on Python 3.10, 3.11, 3.12, 3.13.
- Dependabot for `pip` + `github-actions`, GitHub Actions pinned to
  commit SHAs for supply-chain integrity.

[Unreleased]: https://github.com/ginkida/agent-dispatch/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/ginkida/agent-dispatch/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/ginkida/agent-dispatch/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/ginkida/agent-dispatch/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/ginkida/agent-dispatch/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/ginkida/agent-dispatch/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ginkida/agent-dispatch/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ginkida/agent-dispatch/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ginkida/agent-dispatch/releases/tag/v0.1.0
