# agent-dispatch

[![PyPI](https://img.shields.io/pypi/v/agent-dispatch)](https://pypi.org/project/agent-dispatch/)
[![CI](https://github.com/ginkida/agent-dispatch/actions/workflows/ci.yml/badge.svg)](https://github.com/ginkida/agent-dispatch/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/agent-dispatch)](https://pypi.org/project/agent-dispatch/)
[![License](https://img.shields.io/github/license/ginkida/agent-dispatch)](LICENSE)

**MCP server that lets Claude Code agents delegate tasks to agents in other project directories.**

<p align="center">
  <img src="assets/mascot.png" alt="agent-dispatch mascot" width="600">
</p>

Each agent runs as a separate `claude -p` session in its own project directory — inheriting that project's MCP servers, CLAUDE.md, and tools. The calling agent just gets the result back.

Works with OAuth, API key, and Claude subscription authentication.

## Quick Start

```bash
pip install agent-dispatch

# Initialize: creates config + registers MCP server with Claude Code
agent-dispatch init

# Add agents (description auto-generated from project files)
agent-dispatch add infra ~/projects/infra
agent-dispatch add backend ~/projects/backend

# Test it works
agent-dispatch test infra

# If agents hit permission errors, grant tool access:
agent-dispatch update infra --permission-mode bypassPermissions

# If something doesn't work, run the diagnostic:
agent-dispatch doctor
```

Done. Every Claude Code session now has access to all dispatch tools.

## When to Dispatch

**Do dispatch** when a task needs tools, files, or context from another project:
- Check container logs via infra agent's Portainer MCP
- Query a database via db agent's postgres MCP
- Read code or run tests in another repository

**Don't dispatch** when you can do it yourself — dispatching spawns a full Claude session.

## MCP Tools Reference

### `list_agents`

Lists all configured agents. **Call this first** to see what's available.

```json
// Response (permission fields shown only when configured)
[
  {
    "name": "infra",
    "directory": "/home/user/projects/infra",
    "description": "Infrastructure agent. MCP: portainer. Stack: Python, Docker",
    "healthy": true,
    "has_claude_md": true,
    "has_mcp_config": true,
    "permission_mode": "bypassPermissions",
    "allowed_tools": ["Bash", "Read", "Grep"]
  }
]
```

### `dispatch`

One-shot task delegation. Results are cached — identical requests within TTL return instantly.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent` | string | yes | Agent name from `list_agents` |
| `task` | string | yes | What to do — be specific, the agent has no context from your conversation |
| `context` | string | no | Extra context: error messages, code snippets, stack traces |
| `caller` | string | no | Your project/role — helps the agent understand who's asking |
| `goal` | string | no | Broader objective — helps the agent make better trade-offs |

```json
// Response (success)
{
  "agent": "infra",
  "success": true,
  "result": "Found 3 errors in container logs: TypeError in scheduler.py:42...",
  "session_id": "sess-abc-123",
  "cost_usd": 0.02,
  "duration_ms": 5000,
  "num_turns": 2
}

// Response (failure — error_type helps you handle programmatically)
{
  "agent": "infra",
  "success": false,
  "result": "",
  "error": "Tool_use is not allowed in this permission mode\n\nHint: ...",
  "error_type": "permission"
}
```

**`error_type` values:** `permission` (tool/action denied), `timeout`, `recursion` (dispatch depth exceeded), `not_found` (missing directory or CLI), `cli_error` (other failures). Permission errors include an actionable hint.

**Always pass `caller` and `goal`** — the dispatched agent sees a structured prompt:

```markdown
## Goal
debug production crash

## Dispatched by
backend

## Context
Error: TypeError at scheduler.py:42

## Task
Check container logs for recent errors related to the scheduler service
```

### `dispatch_session`

Multi-turn: continue a conversation with an agent. First call starts a session, pass `session_id` back to continue. Never cached.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent` | string | yes | Agent name |
| `task` | string | yes | Task or follow-up message |
| `session_id` | string | no | From previous response — empty for new session |
| `context` | string | no | Extra context |
| `caller` | string | no | Who is dispatching |
| `goal` | string | no | Broader objective |

```
Turn 1: dispatch_session("infra", "List running containers")
         → session_id: "sess-abc"

Turn 2: dispatch_session("infra", "Restart the nginx one", session_id="sess-abc")
         → agent remembers previous context
```

### `dispatch_parallel`

Run multiple tasks concurrently. Much faster than sequential `dispatch` calls.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `dispatches` | string (JSON) | yes | JSON array of `{"agent", "task", "context?", "caller?", "goal?"}` |
| `aggregate` | string | no | Agent name to synthesize all results into one answer |

**Important:** `dispatches` is a JSON string, not a list.

```json
// Input
[
  {"agent": "infra", "task": "check pod logs for errors", "caller": "backend", "goal": "debug crash"},
  {"agent": "db", "task": "are all migrations applied?", "caller": "backend", "goal": "debug crash"}
]
```

```json
// Response (without aggregate)
[
  {"agent": "infra", "success": true, "result": "No errors in pod logs", ...},
  {"agent": "db", "success": true, "result": "All migrations applied", ...}
]
```

```json
// Response (with aggregate="backend")
{
  "individual_results": [
    {"agent": "infra", "success": true, "result": "No errors in pod logs", ...},
    {"agent": "db", "success": true, "result": "All migrations applied", ...}
  ],
  "aggregated": {
    "agent": "backend",
    "success": true,
    "result": "Summary: all systems nominal. No pod errors, all migrations applied."
  }
}
```

### `dispatch_stream`

Same as `dispatch` but shows live progress while the agent works. Use for long-running tasks. Not cached.

Parameters are identical to `dispatch`.

### `dispatch_dialogue`

Two agents collaborate through multi-turn conversation. Never cached.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `requester` | string | yes | Agent with the problem/context |
| `responder` | string | yes | Agent with the expertise/tools |
| `topic` | string | yes | Problem or question to discuss |
| `max_rounds` | int | no | Max back-and-forth rounds (default: 3, max: 10) |

Each round costs up to 2 dispatches. Agents signal completion with `[RESOLVED]`.

```json
// Response
{
  "resolved": true,
  "rounds": 2,
  "total_cost_usd": 0.04,
  "total_duration_ms": 12000,
  "final_answer": "Staging had 1 pending migration. Applied successfully.",
  "conversation": [
    {"agent": "db", "role": "responder", "round": 1, "message": "Which environment?", "cost_usd": 0.01},
    {"agent": "backend", "role": "requester", "round": 1, "message": "Staging", "cost_usd": 0.01},
    {"agent": "db", "role": "responder", "round": 2, "message": "Applied. [RESOLVED]", "cost_usd": 0.01}
  ]
}
```

### `add_agent`

Register a new project directory as an agent. Description is auto-generated from project files if omitted.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | yes | Agent name (letters, digits, hyphens, underscores) |
| `directory` | string | yes | Absolute path to project directory |
| `description` | string | no | What this agent can do — auto-generated if empty |
| `timeout` | int | no | Timeout in seconds (0 = use global default) |
| `permission_mode` | string | no | Permission mode (e.g. `default`, `plan`, `bypassPermissions`) |
| `allowed_tools` | string | no | Comma-separated allowed tools (e.g. `"Bash,Read,Edit"`) |
| `disallowed_tools` | string | no | Comma-separated disallowed tools |

### `update_agent`

Update an existing agent's configuration. Only non-empty fields are changed. Pass `"none"` to clear a field.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | yes | Agent name to update |
| `description` | string | no | New description |
| `timeout` | int | no | New timeout (0 = don't change) |
| `model` | string | no | Model override. `"none"` to clear |
| `permission_mode` | string | no | Permission mode. `"none"` to clear |
| `allowed_tools` | string | no | Comma-separated. `"none"` to clear |
| `disallowed_tools` | string | no | Comma-separated. `"none"` to clear |

### `remove_agent`

Remove an agent from config.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | yes | Agent name to remove |

### `cache_stats` / `cache_clear`

View cache hit rate and size, or clear all cached results.

### Async dispatch — `dispatch_async`, `dispatch_status`, `dispatch_wait`, `dispatch_jobs`, `dispatch_gc`

When a dispatched task is going to take a while, you don't want to block your own tool slot for minutes. Async dispatch returns a `job_id` immediately and lets you check back when you're ready.

```
// 1. fire and forget
dispatch_async(agent="infra", task="audit every container log for OOM kills today")
  -> {"job_id": "8f3a...e1", "status": "pending", "agent": "infra"}

// 2. do other work, then check progress (non-blocking)
dispatch_status(job_id="8f3a...e1")
  -> {"id": "8f3a...e1", "status": "running", "started_at": 1730000123.4, ...}

// 3. or block until done (with a timeout cap)
dispatch_wait(job_id="8f3a...e1", timeout_seconds=120)
  -> {"id": "8f3a...e1", "status": "done", "result": {"agent": "infra", "success": true, ...}}

// If the timeout fires, the job keeps running:
  -> {"id": "...", "status": "running", "timed_out_waiting": true}
```

`dispatch_jobs(status?)` lists recent jobs as summaries (filter by `pending` / `running` / `done` / `failed` / `cancelled`). `dispatch_gc(max_age_days=7)` purges terminal jobs older than the threshold — pending and running jobs are never deleted.

Job state persists to disk at `~/.config/agent-dispatch/jobs/` (override with `AGENT_DISPATCH_JOBS_DIR`). One JSON file per job, atomic writes — safe to read or `ls` while jobs are in flight.

| When to use async | When to use `dispatch` |
|-------------------|------------------------|
| Long task (minutes) — you want to keep working | Short task — you need the answer right now |
| Several long tasks you'll collect later | Several short tasks → `dispatch_parallel` |
| Don't care about caching (each call is a fresh job) | Cached by default — identical requests are free |

### Error Responses

All tools return errors as:

```json
{"error": "Unknown agent: 'foo'. Available: infra, db, monitoring"}
```

## Which Tool to Use

| Scenario | Tool |
|----------|------|
| Quick one-off question to another project | `dispatch` |
| Multi-step workflow with follow-ups | `dispatch_session` |
| Need answers from several agents at once | `dispatch_parallel` |
| Long task, want to see progress | `dispatch_stream` |
| Two agents need to collaborate | `dispatch_dialogue` |
| Need a combined summary from multiple agents | `dispatch_parallel` with `aggregate` |
| Long task — don't block your tool slot | `dispatch_async` + `dispatch_wait` |
| Check progress without blocking | `dispatch_status` |

## Configuration

Config at `~/.config/agent-dispatch/agents.yaml` (override: `AGENT_DISPATCH_CONFIG` env var):

```yaml
agents:
  infra:
    directory: ~/projects/infra
    description: "Infrastructure agent. MCP: portainer."
    timeout: 300            # seconds, default: 300
    # model: sonnet         # optional model override
    # max_budget_usd: 1.0   # cost limit per dispatch
    # permission_mode: auto # permission mode for the agent
    # allowed_tools:        # restrict which tools the agent can use
    #   - Read
    #   - Grep
    # disallowed_tools:     # block specific tools
    #   - Write

settings:
  default_timeout: 300
  # default_permission_mode: bypassPermissions  # inherited by all agents
  # default_allowed_tools:                      # inherited when agent has none
  #   - Bash
  #   - Read
  #   - Edit
  max_dispatch_depth: 3     # recursion protection
  max_concurrency: 5        # max parallel claude -p processes
  cache:
    enabled: true
    ttl: 300                # seconds
```

Config is reloaded on every tool call — add agents without restarting.

### Auto-Description

`agent-dispatch add` without `--description` generates one from:

- `CLAUDE.md` — first meaningful paragraph (priority)
- `README.md` — first substantial line (fallback)
- `pyproject.toml` / `package.json` — project description
- `.mcp.json` — lists MCP server names
- Stack indicators — Docker, Rust, Go, Python, Node.js
- DB indicators — Prisma, Alembic, migrations

## How It Works

```
Your Claude Code session
  │
  ├─ dispatch("infra", "find errors", caller="backend", goal="debug crash")
  │
  ▼
agent-dispatch MCP server
  ├─ cache check → hit? return cached result
  ├─ semaphore → limit concurrent processes
  └─ subprocess.run("claude -p ...", cwd=~/projects/infra/)
       │
       ▼
     New Claude Code session in ~/projects/infra/
       ├─ Inherits: CLAUDE.md, .mcp.json, project tools
       ├─ Receives structured prompt with goal/caller/context/task
       └─ Returns result → cached for future identical requests
```

## Safety

- **Recursion protection** — `AGENT_DISPATCH_DEPTH` env var tracks nesting. Default limit: 3.
- **Cost control** — `max_budget_usd` per agent or globally.
- **Concurrency** — `max_concurrency` (default: 5) limits parallel `claude -p` processes.
- **Timeout** — per-agent or global (default: 300s). Orphaned processes are cleaned up.
- **Caching** — identical `(agent, task, context)` requests return cached results. Only successes are cached. Sessions and dialogues are never cached.

## CLI

| Command | Description |
|---------|-------------|
| `agent-dispatch init` | Create config + register MCP server with Claude Code |
| `agent-dispatch add <name> <dir>` | Add an agent (auto-generates description) |
| `agent-dispatch update <name>` | Update agent config (permissions, timeout, model, etc.) |
| `agent-dispatch remove <name>` | Remove an agent |
| `agent-dispatch list` | List agents with health status and permissions |
| `agent-dispatch describe <name>` | Show full configuration for one agent (tri-state tools, project files) |
| `agent-dispatch test <name> [task] [--stream]` | Test an agent with a dispatch (`--stream` for live progress) |
| `agent-dispatch doctor` | Diagnose installation: claude CLI, MCP registration, agent health |
| `agent-dispatch serve` | Start MCP server (stdio, used by Claude Code) |

## Requirements

- Python >= 3.10
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

## License

MIT
