"""CLI: init, add, remove, list, test, serve."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

from .config import auto_describe, config_path, load_config, save_config
from .models import AgentConfig, DispatchConfig, check_permission_mode, validate_agent_name


def _load_or_exit() -> DispatchConfig:
    """Load config, exiting with a friendly error on malformed YAML or schema."""
    try:
        return load_config()
    except ValidationError as e:
        click.echo(click.style(
            f"Error: config at {config_path()} has an invalid schema:", fg="red"
        ))
        click.echo(str(e))
        raise SystemExit(1)
    except yaml.YAMLError as e:
        click.echo(click.style(
            f"Error: config at {config_path()} is not valid YAML:", fg="red"
        ))
        click.echo(str(e))
        raise SystemExit(1)


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

    mcp_config = json.dumps({
        "type": "stdio",
        "command": agent_dispatch_cmd,
        "args": ["serve"],
    })

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
@click.option("-d", "--description", default=None, help="Agent description. Auto-generated if omitted.")
@click.option("--timeout", default=300, help="Timeout in seconds (default: 300).")
@click.option("--model", default=None, help="Model override for this agent.")
@click.option("--max-budget", default=None, type=float, help="Max cost in USD per dispatch.")
@click.option(
    "--permission-mode", default=None,
    help="Permission mode for claude CLI (e.g. default, plan, bypassPermissions).",
)
@click.option(
    "--allowed-tools", default=None,
    help="Comma-separated list of allowed tools (e.g. Bash,Read,Edit).",
)
@click.option(
    "--disallowed-tools", default=None,
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
        raise SystemExit(1)

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
        if allowed_tools else None,
        disallowed_tools=[t.strip() for t in disallowed_tools.split(",") if t.strip()]
        if disallowed_tools else None,
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
        if agent.allowed_tools:
            click.echo(f"    allowed_tools: {', '.join(agent.allowed_tools)}")
        if agent.disallowed_tools:
            click.echo(f"    disallowed_tools: {', '.join(agent.disallowed_tools)}")
        click.echo()


@cli.command()
@click.argument("name")
@click.option("-d", "--description", default=None, help="New description.")
@click.option("--timeout", default=None, type=int, help="Timeout in seconds.")
@click.option("--model", default=None, help="Model override.")
@click.option("--max-budget", default=None, type=float, help="Max cost in USD. Use 0 to clear.")
@click.option(
    "--permission-mode", default=None,
    help="Permission mode (default, plan, bypassPermissions). Use 'none' to clear.",
)
@click.option(
    "--allowed-tools", default=None,
    help="Comma-separated allowed tools. Use 'none' to clear.",
)
@click.option(
    "--disallowed-tools", default=None,
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
            agent.disallowed_tools = [
                t.strip() for t in disallowed_tools.split(",") if t.strip()
            ]
        updated.append("disallowed_tools")

    if not updated:
        click.echo("Nothing to update. Pass at least one option (see --help).")
        raise SystemExit(1)

    save_config(config)
    click.echo(f"Updated agent '{name}': {', '.join(updated)}")


@cli.command()
@click.argument("name")
@click.argument("task", default="What project is this? Describe in one sentence.")
def test(name: str, task: str) -> None:
    """Test an agent by dispatching a task."""
    config = _load_or_exit()
    if name not in config.agents:
        click.echo(f"Agent '{name}' not found. Run 'agent-dispatch list' to see agents.")
        raise SystemExit(1)

    agent = config.agents[name]
    click.echo(f"Dispatching to '{name}' ({agent.directory})...")
    click.echo(f"Task: {task}")
    click.echo("---")

    from .runner import dispatch

    result = dispatch(name, task, agent, config.settings)

    if result.success:
        click.echo(result.result)
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
            click.echo(f"  agent-dispatch update {name} --timeout 600")
        raise SystemExit(1)


@cli.command()
def serve() -> None:
    """Start the MCP server (stdio transport)."""
    from .server import main

    main()
