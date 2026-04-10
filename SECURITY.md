# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it via [GitHub Security Advisories](https://github.com/ginkida/agent-dispatch/security/advisories/new).

**Do not** open a public issue for security vulnerabilities.

## Scope

`agent-dispatch` runs `claude -p` subprocesses in configured directories. Security-relevant areas:

- **Command injection** — task/context strings are passed as CLI arguments, not shell-evaluated
- **Directory traversal** — agent directories are resolved to absolute paths via `Path.resolve()`
- **Recursion** — `AGENT_DISPATCH_DEPTH` env var prevents infinite dispatch loops
- **Cost** — `max_budget_usd` limits spending per dispatch

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
