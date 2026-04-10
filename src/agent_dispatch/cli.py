"""CLI: init, add, remove, list, test, serve."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import click

from .config import auto_describe, config_path, load_config, save_config
from .models import AgentConfig, DispatchConfig, validate_agent_name


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
def add(
    name: str,
    directory: str,
    description: str | None,
    timeout: int,
    model: str | None,
) -> None:
    """Add an agent. Auto-generates description from project files if omitted."""
    try:
        validate_agent_name(name)
    except ValueError as e:
        click.echo(f"Error: {e}")
        raise SystemExit(1)

    config = load_config()
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
    )
    save_config(config)
    click.echo(f"Added agent '{name}' -> {dir_path}")


@cli.command()
@click.argument("name")
def remove(name: str) -> None:
    """Remove an agent."""
    config = load_config()
    if name not in config.agents:
        click.echo(f"Agent '{name}' not found.")
        raise SystemExit(1)

    del config.agents[name]
    save_config(config)
    click.echo(f"Removed agent '{name}'.")


@cli.command("list")
def list_agents() -> None:
    """List configured agents with health status."""
    config = load_config()
    if not config.agents:
        click.echo("No agents configured. Run: agent-dispatch add <name> <directory>")
        return

    for name, agent in config.agents.items():
        healthy = agent.directory.is_dir()
        status = click.style("OK", fg="green") if healthy else click.style("NOT FOUND", fg="red")
        click.echo(f"  {name} [{status}]")
        click.echo(f"    dir:  {agent.directory}")
        click.echo(f"    desc: {agent.description}")
        click.echo()


@cli.command()
@click.argument("name")
@click.argument("task", default="What project is this? Describe in one sentence.")
def test(name: str, task: str) -> None:
    """Test an agent by dispatching a task."""
    config = load_config()
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
        raise SystemExit(1)


@cli.command()
def serve() -> None:
    """Start the MCP server (stdio transport)."""
    from .server import main

    main()
