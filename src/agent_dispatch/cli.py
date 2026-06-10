"""CLI: init, add, remove, list, update, test, describe, doctor, jobs, job, cancel, gc, serve."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

from .config import auto_describe, config_path, load_config, save_config
from .jobs import JobStore, default_jobs_dir, is_valid_job_id
from .models import AgentConfig, DispatchConfig, check_permission_mode, validate_agent_name


def _load_or_exit() -> DispatchConfig:
    """Load config, exiting with a friendly error on malformed YAML or schema."""
    try:
        return load_config()
    except ValidationError as e:
        click.echo(
            click.style(f"Error: config at {config_path()} has an invalid schema:", fg="red")
        )
        click.echo(str(e))
        raise SystemExit(1) from None
    except yaml.YAMLError as e:
        click.echo(click.style(f"Error: config at {config_path()} is not valid YAML:", fg="red"))
        click.echo(str(e))
        raise SystemExit(1) from None


@click.group()
@click.version_option(package_name="agent-dispatch")
def cli() -> None:
    """Delegate tasks between Claude Code agents across projects."""


@cli.command()
def init() -> None:
    """Create config file and register MCP server with Claude Code."""
    cp = config_path()

    # Create config with example
    if cp.exists():
        click.echo(f"Config already exists: {cp}")
    else:
        cp.parent.mkdir(parents=True, exist_ok=True)
        example = DispatchConfig()
        save_config(example, cp)
        click.echo(f"Created config: {cp}")

    # Register MCP server with Claude Code
    if shutil.which("claude") is None:
        click.echo("Warning: claude CLI not found. Register MCP server manually.")
        return

    agent_dispatch_cmd = shutil.which("agent-dispatch")
    if agent_dispatch_cmd is None:
        click.echo(
            "Warning: agent-dispatch not found in PATH. "
            "Run 'pip install -e .' first, then re-run 'agent-dispatch init'."
        )
        return

    mcp_config = json.dumps(
        {
            "type": "stdio",
            "command": agent_dispatch_cmd,
            "args": ["serve"],
        }
    )

    result = subprocess.run(
        ["claude", "mcp", "add-json", "agent-dispatch", mcp_config, "--scope", "user"],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        click.echo("Registered MCP server with Claude Code (user scope).")
        click.echo("\nNext steps:")
        click.echo("  agent-dispatch add <name> <directory>  # add your first agent")
        click.echo("  agent-dispatch list                    # verify agents")
        click.echo("  agent-dispatch test <name>             # test it works")
    else:
        click.echo(f"Failed to register MCP server: {result.stderr.strip()}")
        click.echo("You can register manually in ~/.claude/settings.json:")
        click.echo(f'  "mcpServers": {{ "agent-dispatch": {mcp_config} }}')


@cli.command()
@click.argument("name")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option(
    "-d",
    "--description",
    default=None,
    help="Agent description. Auto-generated if omitted.",
)
@click.option("--timeout", default=300, help="Timeout in seconds (default: 300).")
@click.option("--model", default=None, help="Model override for this agent.")
@click.option("--max-budget", default=None, type=float, help="Max cost in USD per dispatch.")
@click.option(
    "--permission-mode",
    default=None,
    help="Permission mode for claude CLI (e.g. default, plan, bypassPermissions).",
)
@click.option(
    "--allowed-tools",
    default=None,
    help="Comma-separated list of allowed tools (e.g. Bash,Read,Edit).",
)
@click.option(
    "--disallowed-tools",
    default=None,
    help="Comma-separated list of disallowed tools.",
)
def add(
    name: str,
    directory: str,
    description: str | None,
    timeout: int,
    model: str | None,
    max_budget: float | None,
    permission_mode: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
) -> None:
    """Add an agent. Auto-generates description from project files if omitted."""
    try:
        validate_agent_name(name)
    except ValueError as e:
        click.echo(f"Error: {e}")
        raise SystemExit(1) from None

    config = _load_or_exit()
    dir_path = Path(directory).resolve()

    if name in config.agents:
        click.echo(f"Agent '{name}' already exists. Use 'agent-dispatch remove {name}' first.")
        raise SystemExit(1)

    if description is None:
        description = auto_describe(dir_path)
        click.echo(f"Auto-generated description: {description}")

    config.agents[name] = AgentConfig(
        directory=dir_path,
        description=description,
        timeout=timeout,
        model=model,
        max_budget_usd=max_budget,
        permission_mode=permission_mode,
        allowed_tools=[t.strip() for t in allowed_tools.split(",") if t.strip()]
        if allowed_tools
        else None,
        disallowed_tools=[t.strip() for t in disallowed_tools.split(",") if t.strip()]
        if disallowed_tools
        else None,
    )
    if warning := check_permission_mode(permission_mode):
        click.echo(click.style(f"Warning: {warning}", fg="yellow"))

    save_config(config)
    click.echo(f"Added agent '{name}' -> {dir_path}")


@cli.command()
@click.argument("name")
def remove(name: str) -> None:
    """Remove an agent."""
    config = _load_or_exit()
    if name not in config.agents:
        click.echo(f"Agent '{name}' not found.")
        raise SystemExit(1)

    del config.agents[name]
    save_config(config)
    click.echo(f"Removed agent '{name}'.")


@cli.command("list")
def list_agents() -> None:
    """List configured agents with health status."""
    config = _load_or_exit()
    if not config.agents:
        click.echo("No agents configured. Run: agent-dispatch add <name> <directory>")
        return

    for name, agent in config.agents.items():
        try:
            healthy = agent.directory.is_dir()
            status_label = "OK" if healthy else "NOT FOUND"
            status_color = "green" if healthy else "red"
        except OSError:
            status_label = "UNREADABLE"
            status_color = "red"
        status = click.style(status_label, fg=status_color)
        click.echo(f"  {name} [{status}]")
        click.echo(f"    dir:  {agent.directory}")
        click.echo(f"    desc: {agent.description}")
        extras: list[str] = []
        if agent.timeout != 300:
            extras.append(f"timeout={agent.timeout}s")
        if agent.model:
            extras.append(f"model={agent.model}")
        if agent.max_budget_usd:
            extras.append(f"budget=${agent.max_budget_usd}")
        if extras:
            click.echo(f"    config: {', '.join(extras)}")
        if agent.permission_mode:
            click.echo(f"    permission_mode: {agent.permission_mode}")
        if agent.allowed_tools is not None:
            rendered = ", ".join(agent.allowed_tools) if agent.allowed_tools else "(none)"
            click.echo(f"    allowed_tools: {rendered}")
        if agent.disallowed_tools is not None:
            rendered = ", ".join(agent.disallowed_tools) if agent.disallowed_tools else "(none)"
            click.echo(f"    disallowed_tools: {rendered}")
        click.echo()


@cli.command()
@click.argument("name")
@click.option("-d", "--description", default=None, help="New description.")
@click.option("--timeout", default=None, type=int, help="Timeout in seconds.")
@click.option("--model", default=None, help="Model override.")
@click.option("--max-budget", default=None, type=float, help="Max cost in USD. Use 0 to clear.")
@click.option(
    "--permission-mode",
    default=None,
    help="Permission mode (default, plan, bypassPermissions). Use 'none' to clear.",
)
@click.option(
    "--allowed-tools",
    default=None,
    help="Comma-separated allowed tools. Use 'none' to clear.",
)
@click.option(
    "--disallowed-tools",
    default=None,
    help="Comma-separated disallowed tools. Use 'none' to clear.",
)
@click.pass_context
def update(
    ctx: click.Context,
    name: str,
    description: str | None,
    timeout: int | None,
    model: str | None,
    max_budget: float | None,
    permission_mode: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
) -> None:
    """Update an existing agent's configuration."""
    config = _load_or_exit()
    if name not in config.agents:
        click.echo(f"Agent '{name}' not found. Run 'agent-dispatch list' to see agents.")
        raise SystemExit(1)

    agent = config.agents[name]
    updated: list[str] = []

    if description is not None:
        agent.description = description
        updated.append("description")
    if timeout is not None:
        agent.timeout = timeout
        updated.append("timeout")
    if model is not None:
        agent.model = None if model.strip().lower() in ("none", "") else model
        updated.append("model")
    if max_budget is not None:
        agent.max_budget_usd = None if max_budget == 0 else max_budget
        updated.append("max_budget_usd")
    if permission_mode is not None:
        stripped = permission_mode.strip()
        effective = None if stripped.lower() in ("none", "") else stripped
        agent.permission_mode = effective
        if warning := check_permission_mode(effective):
            click.echo(click.style(f"Warning: {warning}", fg="yellow"))
        updated.append("permission_mode")
    if allowed_tools is not None:
        if allowed_tools.strip().lower() in ("none", ""):
            agent.allowed_tools = None
        else:
            agent.allowed_tools = [t.strip() for t in allowed_tools.split(",") if t.strip()]
        updated.append("allowed_tools")
    if disallowed_tools is not None:
        if disallowed_tools.strip().lower() in ("none", ""):
            agent.disallowed_tools = None
        else:
            agent.disallowed_tools = [t.strip() for t in disallowed_tools.split(",") if t.strip()]
        updated.append("disallowed_tools")

    if not updated:
        click.echo("Nothing to update. Pass at least one option (see --help).")
        raise SystemExit(1)

    save_config(config)
    click.echo(f"Updated agent '{name}': {', '.join(updated)}")


@cli.command()
@click.argument("name")
@click.argument("task", default="What project is this? Describe in one sentence.")
@click.option(
    "--stream",
    "stream",
    is_flag=True,
    help="Show live progress (assistant text + tool use) while the agent works.",
)
@click.option(
    "--timeout",
    "timeout",
    default=None,
    type=int,
    help="One-off timeout override in seconds (does not change the agent config).",
)
def test(name: str, task: str, stream: bool, timeout: int | None) -> None:
    """Test an agent by dispatching a task."""
    config = _load_or_exit()
    if name not in config.agents:
        click.echo(f"Agent '{name}' not found. Run 'agent-dispatch list' to see agents.")
        raise SystemExit(1)

    agent = config.agents[name]
    if timeout is not None and timeout > 0:
        agent = agent.model_copy(update={"timeout": timeout})
    click.echo(f"Dispatching to '{name}' ({agent.directory})...")
    click.echo(f"Task: {task}")
    click.echo("---")

    if stream:
        from .runner import dispatch_stream

        def _on_progress(msg: str) -> None:
            click.echo(click.style(f"  -> {msg}", fg="cyan"), err=True)

        result = dispatch_stream(
            name,
            task,
            agent,
            config.settings,
            on_progress=_on_progress,
        )
    else:
        from .runner import dispatch

        result = dispatch(name, task, agent, config.settings)

    if result.success:
        click.echo(result.result)
        if result.hint:
            click.echo()
            click.echo(click.style(f"Note: {result.hint}", fg="yellow"))
        if result.cost_usd is not None:
            click.echo(f"\n--- Cost: ${result.cost_usd:.4f} | Turns: {result.num_turns}")
    else:
        click.echo(click.style(f"Error: {result.error}", fg="red"))
        if result.error_type == "permission":
            click.echo()
            click.echo(click.style("Diagnosis: permission error", fg="yellow"))
            click.echo("The agent was denied a tool or action. To fix:")
            click.echo(f"  agent-dispatch update {name} --permission-mode bypassPermissions")
            click.echo(f"  agent-dispatch update {name} --allowed-tools Bash,Read,Edit,Write")
        elif result.error_type == "timeout":
            click.echo()
            click.echo(click.style("Diagnosis: timeout", fg="yellow"))
            click.echo(f"  agent-dispatch test {name} --timeout 600    # one-off")
            click.echo(f"  agent-dispatch update {name} --timeout 600  # permanent")
        raise SystemExit(1)


@cli.command()
@click.argument("name")
def describe(name: str) -> None:
    """Show full configuration for a single agent."""
    config = _load_or_exit()
    if name not in config.agents:
        click.echo(f"Agent '{name}' not found. Run 'agent-dispatch list' to see agents.")
        raise SystemExit(1)

    agent = config.agents[name]
    try:
        if agent.directory.is_dir():
            status_label, status_color = "OK", "green"
        else:
            status_label, status_color = "NOT FOUND", "red"
    except OSError:
        status_label, status_color = "UNREADABLE", "red"
    status = click.style(status_label, fg=status_color)

    def _render_tools(tools: list[str] | None) -> str:
        if tools is None:
            return click.style("(inherit defaults)", fg="cyan")
        if not tools:
            return click.style("(none — explicit override)", fg="yellow")
        return ", ".join(tools)

    click.echo(f"{click.style(name, bold=True)} [{status}]")
    click.echo(f"  directory:        {agent.directory}")
    click.echo(f"  description:      {agent.description}")
    click.echo(f"  timeout:          {agent.timeout}s")
    if agent.model:
        click.echo(f"  model:            {agent.model}")
    if agent.max_budget_usd is not None:
        click.echo(f"  max_budget_usd:   ${agent.max_budget_usd}")
    if agent.permission_mode:
        click.echo(f"  permission_mode:  {agent.permission_mode}")
    click.echo(f"  allowed_tools:    {_render_tools(agent.allowed_tools)}")
    click.echo(f"  disallowed_tools: {_render_tools(agent.disallowed_tools)}")

    # Surface project files used by auto_describe so the user can verify
    # what context the dispatched agent will actually inherit.
    try:
        files: list[str] = []
        for fname in ("CLAUDE.md", ".mcp.json", "README.md", "pyproject.toml", "package.json"):
            if (agent.directory / fname).exists():
                files.append(fname)
        if files:
            click.echo(f"  project files:    {', '.join(files)}")
    except OSError:
        pass


@cli.command()
def doctor() -> None:
    """Diagnose the agent-dispatch setup and surface common issues."""
    counters = {"issues": 0, "warnings": 0}

    def section(title: str) -> None:
        click.echo(f"\n{click.style(title, bold=True)}")

    def ok(msg: str) -> None:
        click.echo(f"  [{click.style('OK', fg='green')}] {msg}")

    def warn(msg: str) -> None:
        counters["warnings"] += 1
        click.echo(f"  [{click.style('WARN', fg='yellow')}] {msg}")

    def fail(msg: str) -> None:
        counters["issues"] += 1
        click.echo(f"  [{click.style('FAIL', fg='red')}] {msg}")

    section("Environment")
    claude_path = shutil.which("claude")
    if claude_path:
        ok(f"claude CLI: {claude_path}")
    else:
        fail("claude CLI not found on PATH")
        click.echo("    Install: https://docs.anthropic.com/en/docs/claude-code")

    ad_path = shutil.which("agent-dispatch")
    if ad_path:
        ok(f"agent-dispatch CLI: {ad_path}")
    else:
        warn("agent-dispatch not on PATH (MCP server still works via absolute path)")

    section("Config")
    cp = config_path()
    config: DispatchConfig | None = None
    if not cp.exists():
        warn(f"Config not found: {cp}")
        click.echo("    Run: agent-dispatch init")
    else:
        try:
            config = load_config()
            n = len(config.agents)
            suffix = "agent" if n == 1 else "agents"
            ok(f"Config: {cp} ({n} {suffix})")
        except ValidationError as e:
            fail(f"Config schema invalid: {cp}")
            click.echo(f"    {e}")
        except yaml.YAMLError as e:
            fail(f"Config not valid YAML: {cp}")
            click.echo(f"    {e}")

    section("MCP registration")
    if claude_path is None:
        warn("Skipped (claude CLI missing)")
    else:
        try:
            result = subprocess.run(
                [claude_path, "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Match the server name at the start of any line — `claude mcp list`
            # prints `<name>: <command> - <status>`, and we want to avoid false
            # positives from "agent-dispatch" appearing in command paths.
            entry_re = re.compile(r"^agent-dispatch[:\s]", re.MULTILINE)
            if result.returncode == 0 and entry_re.search(result.stdout):
                ok("agent-dispatch is registered with Claude Code")
            else:
                warn("agent-dispatch is not registered with Claude Code")
                click.echo("    Run: agent-dispatch init")
        except subprocess.TimeoutExpired:
            warn("Could not check MCP registration: claude mcp list timed out")
        except (FileNotFoundError, PermissionError, OSError) as e:
            warn(f"Could not check MCP registration: {e}")

    section("Agents")
    if config is None:
        warn("Skipped (config could not be loaded)")
    elif not config.agents:
        warn("No agents configured. Add one: agent-dispatch add <name> <directory>")
    else:
        for name, agent in config.agents.items():
            try:
                if agent.directory.is_dir():
                    extras: list[str] = []
                    if (agent.directory / "CLAUDE.md").exists():
                        extras.append("CLAUDE.md")
                    if (agent.directory / ".mcp.json").exists():
                        extras.append(".mcp.json")
                    suffix = f" [{', '.join(extras)}]" if extras else ""
                    ok(f"{name}: {agent.directory}{suffix}")
                else:
                    fail(f"{name}: directory missing - {agent.directory}")
            except OSError as e:
                fail(f"{name}: directory unreadable - {e}")

    section("Summary")
    issues = counters["issues"]
    warnings = counters["warnings"]
    if issues == 0 and warnings == 0:
        click.echo(click.style("All checks passed.", fg="green"))
    else:
        parts: list[str] = []
        if issues:
            parts.append(
                click.style(
                    f"{issues} issue{'s' if issues != 1 else ''}",
                    fg="red",
                )
            )
        if warnings:
            parts.append(
                click.style(
                    f"{warnings} warning{'s' if warnings != 1 else ''}",
                    fg="yellow",
                )
            )
        click.echo(", ".join(parts))
        if issues > 0:
            raise SystemExit(1)


_STATUS_COLORS = {
    "pending": "cyan",
    "running": "yellow",
    "done": "green",
    "failed": "red",
    "cancelled": "magenta",
}


def _job_store() -> JobStore:
    return JobStore(default_jobs_dir())


def _age(ts: float) -> str:
    """Compact human age like '3m' / '2h' / '5d' for a unix timestamp."""
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _styled_status(status: str) -> str:
    return click.style(status, fg=_STATUS_COLORS.get(status, "white"))


@cli.command("jobs")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "running", "done", "failed", "cancelled"]),
    help="Filter by job status.",
)
@click.option("--limit", default=20, type=int, help="Max jobs shown (default: 20).")
def jobs_list(status: str | None, limit: int) -> None:
    """List async dispatch jobs (most recent first)."""
    jobs = _job_store().list(status=status)[: max(1, limit)]  # type: ignore[arg-type]
    if not jobs:
        click.echo("No jobs found." if status is None else f"No {status} jobs found.")
        return
    for j in jobs:
        task = j.task[:60].replace("\n", " ")
        line = (
            f"{j.id}  {_styled_status(j.status):<18}  "
            f"{click.style(j.agent, bold=True):<20}  {_age(j.created_at):>4}  {task}"
        )
        click.echo(line)


@cli.command("job")
@click.argument("job_id")
def job_show(job_id: str) -> None:
    """Show one async job in detail (status, progress tail, result preview)."""
    if not is_valid_job_id(job_id):
        click.echo(click.style(f"Invalid job id: {job_id!r} (expected 32 hex chars)", fg="red"))
        raise SystemExit(1)
    job = _job_store().get(job_id)
    if job is None:
        click.echo(click.style(f"Job not found: {job_id}", fg="red"))
        raise SystemExit(1)

    click.echo(f"{click.style(job.id, bold=True)} [{_styled_status(job.status)}]")
    click.echo(f"  agent:      {job.agent}")
    click.echo(f"  task:       {job.task[:200]}")
    click.echo(f"  created:    {_age(job.created_at)} ago")
    if job.started_at:
        click.echo(f"  started:    {_age(job.started_at)} ago")
    if job.completed_at:
        click.echo(f"  completed:  {_age(job.completed_at)} ago")
    if job.error:
        click.echo(f"  error:      {click.style(job.error[:300], fg='red')}")
    if job.progress:
        click.echo("  progress:")
        for line in job.progress[-10:]:
            click.echo(f"    {line}")
    if job.result is not None:
        click.echo(f"  success:    {job.result.success}")
        if job.result.cost_usd is not None:
            click.echo(f"  cost_usd:   ${job.result.cost_usd:.4f}")
        if job.result.budget_exceeded:
            click.echo(click.style("  budget:     EXCEEDED", fg="yellow"))
        if job.result.result:
            preview = job.result.result[:2000]
            truncated = len(job.result.result) > 2000
            click.echo("  result:")
            for line in preview.splitlines():
                click.echo(f"    {line}")
            if truncated:
                click.echo(
                    click.style(
                        f"    … truncated ({len(job.result.result)} chars total)",
                        fg="cyan",
                    )
                )


@cli.command("cancel")
@click.argument("job_id")
def job_cancel(job_id: str) -> None:
    """Cancel a pending async job.

    Running jobs cannot be cancelled from the CLI — their subprocess belongs
    to the MCP server process. Use the dispatch_cancel MCP tool instead.
    """
    if not is_valid_job_id(job_id):
        click.echo(click.style(f"Invalid job id: {job_id!r} (expected 32 hex chars)", fg="red"))
        raise SystemExit(1)
    job, outcome = _job_store().cancel(job_id)
    if outcome == "not_found":
        click.echo(click.style(f"Job not found: {job_id}", fg="red"))
        raise SystemExit(1)
    if outcome == "cancelled":
        click.echo(f"Cancelled: {job_id}")
    elif outcome == "running":
        click.echo(
            click.style(
                "Job is running; its subprocess belongs to the MCP server process. "
                "Cancel it via the dispatch_cancel MCP tool (same server), or wait.",
                fg="yellow",
            )
        )
        raise SystemExit(1)
    else:  # already_terminal
        status = job.status if job else "terminal"
        click.echo(f"Job already {status}; nothing to cancel.")


@cli.command("gc")
@click.option("--days", default=7, type=int, help="Delete terminal jobs older than N days.")
def jobs_gc(days: int) -> None:
    """Purge old terminal async jobs (done/failed/cancelled)."""
    deleted = _job_store().gc(max(0, days) * 86400)
    click.echo(f"Deleted {deleted} job(s) older than {days} day(s).")


@cli.command()
def serve() -> None:
    """Start the MCP server (stdio transport)."""
    from .server import main

    main()
