# agent-dispatch

**Delegate tasks between Claude Code agents across projects.**

One agent doesn't need to know everything. Let your backend agent ask the infra agent to check container logs — without switching chats, copy-pasting, or wasting context.

## The Problem

You have multiple Claude Code projects, each with their own MCP servers and tools:

```
~/projects/infra/     → Portainer MCP (container logs, restarts)
~/projects/backend/   → Source code, tests, database
~/projects/frontend/  → React app, build tools
```

When your backend agent needs container logs, you manually copy-paste between chats. That's slow, error-prone, and wastes context window on both sides.

## The Solution

```
Backend agent: "The service is crashing, find and fix the bug"
  │
  ├─ dispatch("infra", "find recent errors in container logs",
  │           caller="backend", goal="debug production crash")
  │    └─→ runs claude -p in ~/projects/infra/ (has Portainer MCP)
  │    └─→ returns: "TypeError in scheduler.py:42"
  │
  └─ fixes scheduler.py:42
```

`agent-dispatch` is an MCP server that gives every Claude Code session a `dispatch()` tool. It runs `claude -p` in the target project's directory — so the dispatched agent inherits that project's MCP servers, CLAUDE.md, and tools. The calling agent just gets the result.

Works with OAuth, API key, and Claude subscription authentication.

## Quick Start

```bash
pip install agent-dispatch

# Initialize: creates config + registers MCP server
agent-dispatch init

# Add agents (description auto-generated from project files)
agent-dispatch add infra ~/projects/infra
agent-dispatch add backend ~/projects/backend

# Test it works
agent-dispatch test infra "What containers are running?"
```

Done. Every Claude Code session now has access to all dispatch tools.

## MCP Tools

### `list_agents()`

Lists all configured agents with descriptions and health status.

### `dispatch(agent, task, context?, caller?, goal?)`

Delegate a one-shot task. Results are cached by default — identical requests within the TTL return instantly.

| Parameter | Type | Description |
|-----------|------|-------------|
| `agent` | string | Agent name from config |
| `task` | string | What to do (be specific) |
| `context` | string | Optional: error messages, code snippets |
| `caller` | string | Who is dispatching — helps the agent understand the request |
| `goal` | string | The broader objective — the agent can make better trade-offs |

### `dispatch_session(agent, task, session_id?, context?, caller?, goal?)`

Multi-turn dispatch. First call starts a new session; pass back `session_id` to continue.

```
result = dispatch_session("infra", "List running containers")
# result.session_id = "abc-123"

result = dispatch_session("infra", "Restart the nginx one", session_id="abc-123")
# agent remembers previous context
```

### `dispatch_parallel(dispatches, aggregate?)`

Run multiple dispatch tasks concurrently. Much faster than sequential calls.

```json
dispatches = [
  {"agent": "infra", "task": "check pod logs for errors"},
  {"agent": "db", "task": "are all migrations applied?"},
  {"agent": "monitoring", "task": "any alerts in the last hour?"}
]
```

With aggregation — results are synthesized by a designated agent:

```
dispatch_parallel(dispatches, aggregate="backend")
→ returns { individual_results: [...], aggregated: { result: "Summary: ..." } }
```

### `dispatch_stream(agent, task, context?, caller?, goal?)`

Same as `dispatch()` but shows live progress via log messages. Use for long-running tasks where you want to monitor what the agent is doing.

### `dispatch_dialogue(requester, responder, topic, max_rounds?)`

Two agents collaborate through multi-turn dialogue. The `requester` poses a problem, the `responder` provides expertise. They alternate turns until one signals completion or `max_rounds` is reached.

```
dispatch_dialogue(
  requester="backend",
  responder="db",
  topic="staging is broken, check if migrations are the cause"
)
```

Returns full conversation, aggregated cost, and whether the dialogue resolved.

### `cache_stats()` / `cache_clear()`

View cache hit rate and size, or clear all cached results.

## Configuration

Config lives at `~/.config/agent-dispatch/agents.yaml`:

```yaml
agents:
  infra:
    directory: ~/projects/infra
    description: "Infrastructure agent. MCP servers: portainer."
    timeout: 300            # seconds (default: 300)
    # model: sonnet         # optional model override
    # max_budget_usd: 1.0   # optional cost limit per dispatch
    # permission_mode: auto # optional permission mode

settings:
  default_timeout: 300
  max_dispatch_depth: 3     # recursion protection (min: 1)
  max_concurrency: 5        # max parallel claude -p processes (min: 1)
  cache:
    enabled: true
    ttl: 300                # seconds; 0 effectively disables
```

### Auto-Description

When you run `agent-dispatch add` without `--description`, it reads the target project's files to generate one:

- `CLAUDE.md` — first meaningful paragraph
- `.mcp.json` — lists MCP server names
- `pyproject.toml` / `package.json` — project description
- `Dockerfile`, `Cargo.toml`, `go.mod` — stack indicators

## How It Works

```
┌─────────────────────┐
│ Claude Code Session  │
│ (backend project)    │
│                      │
│ calls dispatch(      │
│   "infra",           │
│   "find errors",     │
│   caller="backend",  │
│   goal="debug crash" │
│ )                    │
└──────────┬───────────┘
           │ MCP tool call
┌──────────▼───────────┐
│ agent-dispatch        │
│ MCP Server            │
│                       │
│ ┌─ cache check ──┐   │
│ │ hit? → return   │   │
│ └─────────────────┘   │
│ ┌─ semaphore ─────┐   │
│ │ limit parallel  │   │
│ └─────────────────┘   │
│ subprocess.run(       │
│   "claude -p ...",    │
│   cwd=~/projects/     │
│       infra/          │
│ )                     │
└──────────┬───────────┘
           │ inherits project context
┌──────────▼───────────┐
│ Claude Code Session   │
│ (infra project)       │
│                       │
│ ## Goal               │
│ debug crash           │
│ ## Dispatched by      │
│ backend               │
│ ## Task               │
│ find errors           │
│                       │
│ ✓ Portainer MCP       │
│ ✓ CLAUDE.md           │
│ ✓ project tools       │
│                       │
│ → finds errors        │
│ → returns result      │
└───────────────────────┘
```

## Safety

### Recursion Protection

If Agent A dispatches to Agent B, and B tries to dispatch back to A, `agent-dispatch` stops it. The `AGENT_DISPATCH_DEPTH` environment variable tracks nesting depth. Default limit: 3.

### Cost Control

Set `max_budget_usd` per agent or globally to limit spending per dispatch.

### Concurrency Control

`max_concurrency` (default: 5) limits how many `claude -p` processes run simultaneously. Prevents hitting OAuth/API rate limits during `dispatch_parallel`.

### Timeout

Each dispatch has a configurable timeout (default: 300s). Streaming dispatches clean up orphaned processes on timeout or interruption.

### Caching

Identical `(agent, task, context)` requests within the TTL window return cached results instantly. Only successful results are cached. Session dispatches and dialogues are never cached.

## CLI Reference

| Command | Description |
|---------|-------------|
| `agent-dispatch init` | Create config + register MCP server with Claude Code |
| `agent-dispatch add <name> <dir>` | Add an agent (auto-generates description) |
| `agent-dispatch remove <name>` | Remove an agent |
| `agent-dispatch list` | List agents with health status |
| `agent-dispatch test <name> [task]` | Test an agent with a dispatch |
| `agent-dispatch serve` | Start MCP server (used by Claude Code) |

## Requirements

- Python >= 3.10
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (OAuth, API key, or subscription)

## License

MIT
