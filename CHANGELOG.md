# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ginkida/agent-dispatch/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ginkida/agent-dispatch/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/ginkida/agent-dispatch/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ginkida/agent-dispatch/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ginkida/agent-dispatch/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ginkida/agent-dispatch/releases/tag/v0.1.0
