# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it via [GitHub Security Advisories](https://github.com/ginkida/agent-dispatch/security/advisories/new).

**Do not** open a public issue for security vulnerabilities.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.5.x   | Yes       |
| 0.4.x   | Yes       |
| ≤ 0.3.x | No        |

## Threat Model

`agent-dispatch` runs `claude -p` subprocesses in configured directories on
behalf of a calling Claude Code agent. The MCP caller and the agent
configurations are part of the same trust domain as the user running the
server — this is a developer tool, not a multi-tenant service. With that in
mind, the security-relevant areas are:

### Subprocess execution
- Tasks/context strings are passed as **argument-list** elements to
  `subprocess.run`/`Popen` (never `shell=True`), so there is no shell
  injection.
- **Argument injection is guarded.** Structured fields placed next to a CLI
  flag (`session_id` → `--resume`, `model` → `--model`, `permission_mode`,
  and tool names) are rejected if they start with `-`, which the `claude`
  CLI would otherwise parse as a *separate* flag. See
  `runner._reject_flaglike` / `runner.ArgInjectionError`.

### Permission escalation (`bypassPermissions`)
- Setting `permission_mode: bypassPermissions` (or a permissive
  `default_permission_mode`) disables Claude Code's permission prompts for
  that agent — it can use any tool without confirmation. Only enable it for
  agents whose project directories you trust. Prefer `allowed_tools` /
  `disallowed_tools` for least privilege.
- A dispatched agent running with broad permissions can, in principle, start
  its own `claude`/dispatch chain. Recursion depth (`AGENT_DISPATCH_DEPTH`,
  bounded by `max_dispatch_depth`) is **best-effort**: it crosses the process
  boundary via an environment variable, so a deliberately hostile agent that
  clears its environment can reset the counter. It protects against accidental
  A→B→A loops, not against an adversarial agent.

### On-disk state
- Async/`return_ref` job records persist to
  `~/.config/agent-dispatch/jobs/<job_id>.json` (override with
  `AGENT_DISPATCH_JOBS_DIR`). They contain the full task, context, and result,
  which may include sensitive output. Files are written `0o600` and the
  directory `0o700` (owner-only). Call `dispatch_gc()` periodically to purge
  old results.
- `agents.yaml` is written `0o600`. It records project paths and permission
  settings.
- `job_id`s are unauthenticated 32-char hex UUIDs — anyone who can call the
  MCP tools and knows a `job_id` can read its result. Don't relay `job_id`s
  over untrusted channels. Caller-supplied `job_id`/`ref` values are validated
  (`^[0-9a-f]{32}$`) before any filesystem access, blocking path traversal.

### Environment & directories
- The dispatched subprocess inherits the **full parent environment**
  (`os.environ.copy()`) — necessary for `claude` to find its credentials.
  Keep secrets you don't want dispatched agents to see out of the shell that
  launches the server.
- Agent directories are resolved to absolute paths via `Path.resolve()` and
  must exist at registration time.

### Cost
- `max_budget_usd` (per agent or as a default) caps spend per dispatch.

## Reproducibility & CI

Third-party GitHub Actions are pinned to commit SHAs; workflows run with
least-privilege `permissions`. Releases publish to PyPI via OIDC Trusted
Publishing (no long-lived tokens).
