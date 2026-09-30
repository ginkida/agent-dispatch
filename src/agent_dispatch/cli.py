"""CLI: init, add, remove, list, update, test, describe, doctor, jobs, job, cancel, gc, serve."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import NoReturn

import click
import yaml
from pydantic import ValidationError

from .config import (
    CONFIG_SAVE_ERRORS,
    auto_describe,
    config_lock,
    config_path,
    load_config,
    save_config,
)
from .jobs import JobStore, default_jobs_dir, is_valid_job_id
from .models import (
    AgentConfig,
    DispatchConfig,
    DispatchGroup,
    GroupMember,
    check_permission_mode,
    validate_agent_name,
)


def _parse_csv(value: str | None) -> list[str] | None:
    return [t.strip() for t in value.split(",") if t.strip()] if value else None


def _save_or_exit(config: DispatchConfig) -> None:
    """Persist the config, turning an I/O failure into a message, not a traceback.

    A full disk or a read-only volume makes `save_config` raise OSError. The
    write is atomic, so the previous config survives — say so, since that is the
    one thing the user needs to know.
    """
    try:
        save_config(config)
    except OSError as e:
        click.echo(
            click.style(
                f"Error: could not write {config_path()}: {e}\n"
                "Nothing was changed — the previous config is intact "
                "(the write is atomic). Check free space and permissions.",
                fg="red",
            )
        )
        raise SystemExit(1) from None
    except CONFIG_SAVE_ERRORS as e:
        # A value YAML cannot represent — not an OSError, so the arm above never
        # saw it and it escaped as a traceback. See config.CONFIG_SAVE_ERRORS.
        click.echo(
            click.style(
                f"Error: could not serialize the config to YAML: {e}\n"
                "Nothing was changed — the previous config is intact. "
                "This is a bug: please report the field that failed.",
                fg="red",
            )
        )
        raise SystemExit(1) from None


def _check_budget_or_exit(max_budget: float | None) -> None:
    """Reject an invalid spend cap before constructing or mutating an agent.

    Without this the pydantic ValidationError escapes Click as a raw traceback.
    """
    if max_budget is not None and not math.isfinite(max_budget):
        click.echo("Error: --max-budget must be a finite number.")
        raise SystemExit(1)
    if max_budget is not None and max_budget < 0:
        click.echo(
            click.style(
                f"Error: --max-budget must be >= 0 (got {max_budget}); use 0 to clear the limit.",
                fg="red",
            )
        )
        raise SystemExit(1)


def _check_timeout_or_exit(timeout: int | None) -> None:
    """Reject a non-positive timeout before it reaches agents.yaml.

    A negative timeout is passed straight to ``subprocess.run(timeout=...)``,
    which raises TimeoutExpired instantly — every dispatch to that agent then
    fails with a nonsensical "timed out after -5s" until the YAML is hand-edited.
    """
    if timeout is not None and timeout < 1:
        click.echo(
            click.style(
                f"Error: --timeout must be a positive number of seconds (got {timeout}).",
                fg="red",
            )
        )
        raise SystemExit(1)


def _claude_supports_flag(claude_path: str, flag: str) -> bool | None:
    """Whether `claude --help` lists *flag*; None when the probe itself failed."""
    try:
        proc = subprocess.run(
            [claude_path, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return flag in (proc.stdout or "") + (proc.stderr or "")


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
    except UnicodeDecodeError as e:
        click.echo(
            click.style(
                f"Error: config at {config_path()} is not valid UTF-8 (re-save it as UTF-8):",
                fg="red",
            )
        )
        click.echo(str(e))
        raise SystemExit(1) from None
    except OSError as e:
        click.echo(click.style(f"Error: config at {config_path()} could not be read:", fg="red"))
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
@click.option(
    "--max-budget",
    "--max-budget-usd",
    "max_budget",
    default=None,
    type=float,
    help="Max cost in USD per dispatch.",
)
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
@click.option(
    "--capabilities",
    default=None,
    help="Comma-separated task capabilities (e.g. docker_logs,deploy_debug).",
)
@click.option(
    "--risky-capabilities",
    default=None,
    help="Comma-separated risky capabilities (e.g. restart_services).",
)
@click.option(
    "--instructions",
    default=None,
    help="Standing orders appended to the agent's system prompt on every dispatch.",
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
    capabilities: str | None,
    risky_capabilities: str | None,
    instructions: str | None,
) -> None:
    """Add an agent. Auto-generates description from project files if omitted."""
    try:
        validate_agent_name(name)
    except ValueError as e:
        click.echo(f"Error: {e}")
        raise SystemExit(1) from None

    _check_timeout_or_exit(timeout)
    _check_budget_or_exit(max_budget)
    dir_path = Path(directory).resolve()

    if description is None:
        description = auto_describe(dir_path)
        click.echo(f"Auto-generated description: {description}")

    # Load + mutate + save under the lock — the MCP server rewrites the same
    # file, and an unlocked read-modify-write silently drops one of two
    # concurrent additions.
    with config_lock():
        config = _load_or_exit()
        if name in config.agents:
            click.echo(f"Agent '{name}' already exists. Use 'agent-dispatch remove {name}' first.")
            raise SystemExit(1)

        config.agents[name] = AgentConfig(
            directory=dir_path,
            description=description,
            timeout=timeout,
            model=model,
            max_budget_usd=max_budget,
            permission_mode=permission_mode,
            allowed_tools=_parse_csv(allowed_tools),
            disallowed_tools=_parse_csv(disallowed_tools),
            capabilities=_parse_csv(capabilities) or [],
            risky_capabilities=_parse_csv(risky_capabilities) or [],
            instructions=(instructions or "").strip(),
        )
        if warning := check_permission_mode(permission_mode):
            click.echo(click.style(f"Warning: {warning}", fg="yellow"))

        _save_or_exit(config)
    click.echo(f"Added agent '{name}' -> {dir_path}")


@cli.command()
@click.argument("name")
def remove(name: str) -> None:
    """Remove an agent."""
    with config_lock():
        config = _load_or_exit()
        if name not in config.agents:
            click.echo(f"Agent '{name}' not found.")
            raise SystemExit(1)

        del config.agents[name]
        _save_or_exit(config)
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
        if agent.capabilities:
            click.echo(f"    capabilities: {', '.join(agent.capabilities)}")
        if agent.risky_capabilities:
            click.echo(f"    risky_capabilities: {', '.join(agent.risky_capabilities)}")
        click.echo()


@cli.command()
@click.argument("name")
@click.option("-d", "--description", default=None, help="New description.")
@click.option("--timeout", default=None, type=int, help="Timeout in seconds.")
@click.option("--model", default=None, help="Model override.")
@click.option(
    # --max-budget-usd is an alias, not decoration: it is the flag the budget
    # hints in runner.py and `test` tell the user to run.
    "--max-budget",
    "--max-budget-usd",
    "max_budget",
    default=None,
    type=float,
    help="Max cost in USD. Use 0 to clear.",
)
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
@click.option(
    "--capabilities",
    default=None,
    help="Comma-separated capabilities. Use 'none' to clear.",
)
@click.option(
    "--risky-capabilities",
    default=None,
    help="Comma-separated risky capabilities. Use 'none' to clear.",
)
@click.option(
    "--instructions",
    default=None,
    help="Standing orders for every dispatch (replaces the text). Use 'none' to clear.",
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
    capabilities: str | None,
    risky_capabilities: str | None,
    instructions: str | None,
) -> None:
    """Update an existing agent's configuration."""
    _check_timeout_or_exit(timeout)
    _check_budget_or_exit(max_budget)

    with config_lock():
        config = _load_or_exit()
        if name not in config.agents:
            click.echo(f"Agent '{name}' not found. Run 'agent-dispatch list' to see agents.")
            raise SystemExit(1)

        agent = config.agents[name]
        updated = _apply_cli_updates(
            agent,
            description=description,
            timeout=timeout,
            model=model,
            max_budget=max_budget,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            capabilities=capabilities,
            risky_capabilities=risky_capabilities,
            instructions=instructions,
        )

        if not updated:
            click.echo("Nothing to update. Pass at least one option (see --help).")
            raise SystemExit(1)

        _save_or_exit(config)
    click.echo(f"Updated agent '{name}': {', '.join(updated)}")


def _apply_cli_updates(
    agent: AgentConfig,
    *,
    description: str | None,
    timeout: int | None,
    model: str | None,
    max_budget: float | None,
    permission_mode: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
    capabilities: str | None,
    risky_capabilities: str | None,
    instructions: str | None = None,
) -> list[str]:
    """Apply `update`'s explicitly-passed options in place; return the fields touched."""
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
    if capabilities is not None:
        if capabilities.strip().lower() in ("none", ""):
            agent.capabilities = []
        else:
            agent.capabilities = _parse_csv(capabilities) or []
        updated.append("capabilities")
    if risky_capabilities is not None:
        if risky_capabilities.strip().lower() in ("none", ""):
            agent.risky_capabilities = []
        else:
            agent.risky_capabilities = _parse_csv(risky_capabilities) or []
        updated.append("risky_capabilities")
    if instructions is not None:
        stripped = instructions.strip()
        agent.instructions = "" if stripped.lower() == "none" else stripped
        updated.append("instructions")

    return updated


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
            tail = f"\n--- Cost: ${result.cost_usd:.4f} | Turns: {result.num_turns}"
            if result.outcome:
                color = "green" if result.outcome == "done" else "yellow"
                tail += f" | Outcome: {click.style(result.outcome, fg=color)}"
            click.echo(tail)
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
        elif result.error_type == "budget":
            click.echo()
            click.echo(click.style("Diagnosis: spend cap reached", fg="yellow"))
            click.echo("The claude CLI stopped the session at --max-budget-usd.")
            click.echo(f"  agent-dispatch update {name} --max-budget-usd 2.0  # raise the cap")
            click.echo(f"  agent-dispatch update {name} --model haiku         # cheaper model")
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
    if agent.capabilities:
        click.echo(f"  capabilities:     {', '.join(agent.capabilities)}")
    if agent.risky_capabilities:
        click.echo(f"  risky_caps:       {', '.join(agent.risky_capabilities)}")
    if agent.instructions:
        first, *rest = agent.instructions.splitlines()
        click.echo(f"  instructions:     {first}")
        for line in rest:
            click.echo(f"                    {line}")
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


@cli.group("group")
def group_cmd() -> None:
    """Manage agent groups (cross-project working sets)."""


@group_cmd.command("add")
@click.argument("name")
@click.option(
    "-d",
    "--description",
    default="",
    help="Orchestrator-facing brief: how to coordinate the group (not sent to members).",
)
@click.option(
    "--shared-context",
    default="",
    help="Member-facing facts (stack names, ids) auto-injected into group dispatches.",
)
@click.option(
    "--member",
    "members",
    multiple=True,
    help="Agent name to include (repeatable). Must already exist.",
)
def group_add(name: str, description: str, shared_context: str, members: tuple[str, ...]) -> None:
    """Create a group referencing existing agents."""
    try:
        validate_agent_name(name)
    except ValueError as e:
        click.echo(f"Error: {e}")
        raise SystemExit(1) from None

    with config_lock():
        config = _load_or_exit()
        if name in config.groups:
            click.echo(
                f"Group '{name}' already exists. Use 'agent-dispatch group remove {name}' first."
            )
            raise SystemExit(1)

        member_objs: list[GroupMember] = []
        for agent_name in members:
            if agent_name not in config.agents:
                click.echo(
                    click.style(
                        f"Error: agent '{agent_name}' not found. "
                        "Add it first ('agent-dispatch add') or check the name.",
                        fg="red",
                    )
                )
                raise SystemExit(1)
            member_objs.append(GroupMember(agent=agent_name))

        config.groups[name] = DispatchGroup(
            description=description,
            shared_context=shared_context,
            members=member_objs,
        )
        _save_or_exit(config)
    click.echo(f"Added group '{name}' ({len(member_objs)} member(s)).")
    if not member_objs:
        click.echo(
            click.style(
                "Warning: group has no members yet. Recreate with --member, "
                "or add agents + use_for hints by editing agents.yaml.",
                fg="yellow",
            )
        )


@group_cmd.command("list")
def group_list() -> None:
    """List configured groups."""
    config = _load_or_exit()
    if not config.groups:
        click.echo("No groups configured. Run: agent-dispatch group add <name> --member <agent>")
        return

    for name, grp in config.groups.items():
        click.echo(f"  {click.style(name, bold=True)} ({len(grp.members)} member(s))")
        if grp.description:
            click.echo(f"    desc: {grp.description}")
        unknown_members = set(config.unknown_group_members(grp))
        rendered: list[str] = []
        for m in grp.members:
            if m.agent in unknown_members:
                rendered.append(click.style(f"{m.agent}(unknown)", fg="red"))
            else:
                rendered.append(m.agent)
        if rendered:
            click.echo(f"    members: {', '.join(rendered)}")
        click.echo(f"    shared context: {'yes' if grp.shared_context.strip() else 'no'}")
        click.echo()


@group_cmd.command("inspect")
@click.argument("name")
def group_inspect(name: str) -> None:
    """Show a group's brief, shared context, and members."""
    config = _load_or_exit()
    if name not in config.groups:
        click.echo(f"Group '{name}' not found. Run 'agent-dispatch group list' to see groups.")
        raise SystemExit(1)

    grp = config.groups[name]
    click.echo(click.style(name, bold=True))
    if grp.description:
        click.echo(f"  description:    {grp.description}")
    if grp.shared_context:
        click.echo("  shared_context:")
        for line in grp.shared_context.splitlines():
            click.echo(f"    {line}")
    click.echo(f"  members ({len(grp.members)}):")
    unknown_members = set(config.unknown_group_members(grp))
    for m in grp.members:
        marker = click.style(" (unknown)", fg="red") if m.agent in unknown_members else ""
        hint = f" — {m.use_for}" if m.use_for else ""
        click.echo(f"    - {m.agent}{marker}{hint}")
    if not grp.members:
        click.echo(
            click.style(
                "    (no members — recreate with --member or edit agents.yaml)", fg="yellow"
            )
        )


@group_cmd.command("update")
@click.argument("name")
@click.option("-d", "--description", default=None, help="New description (pass '' to clear).")
@click.option("--shared-context", default=None, help="New shared context (pass '' to clear).")
def group_update(name: str, description: str | None, shared_context: str | None) -> None:
    """Update a group's description / shared_context.

    These are plain text fields: a new value is stored literally (pass an empty
    string to clear). Edit members by recreating the group or editing
    agents.yaml.
    """
    with config_lock():
        config = _load_or_exit()
        if name not in config.groups:
            click.echo(f"Group '{name}' not found. Run 'agent-dispatch group list' to see groups.")
            raise SystemExit(1)

        grp = config.groups[name]
        updated: list[str] = []
        if description is not None:
            grp.description = description
            updated.append("description")
        if shared_context is not None:
            grp.shared_context = shared_context
            updated.append("shared_context")

        if not updated:
            click.echo("Nothing to update. Pass --description and/or --shared-context.")
            raise SystemExit(1)

        _save_or_exit(config)
    click.echo(f"Updated group '{name}': {', '.join(updated)}")


@group_cmd.command("remove")
@click.argument("name")
def group_remove(name: str) -> None:
    """Remove a group (does not touch the underlying agents)."""
    with config_lock():
        config = _load_or_exit()
        if name not in config.groups:
            click.echo(f"Group '{name}' not found.")
            raise SystemExit(1)

        del config.groups[name]
        _save_or_exit(config)
    click.echo(f"Removed group '{name}'.")


_PERMISSION_HINT = (
    "    Your user needs read access to the file and search (x) access to every "
    "parent directory."
)


def _doctor_load_config(
    cp: Path,
) -> tuple[DispatchConfig | None, tuple[str, str, list[str]]]:
    """Load the config for doctor: (config or None, (status, message, details))."""
    try:
        # Outside load_config on purpose: a missing file is a WARN here, not an
        # empty config. Before Python 3.14, Path.exists() re-raises EACCES from
        # an unsearchable parent directory instead of answering False — and it
        # sat outside every handler, so doctor tracebacked with no report.
        exists = cp.exists()
    except OSError as e:
        return None, ("fail", f"Config could not be read: {cp}", [f"    {e}", _PERMISSION_HINT])
    if not exists:
        return None, ("warn", f"Config not found: {cp}", ["    Run: agent-dispatch init"])
    try:
        config = load_config(cp)
    except ValidationError as e:
        return None, ("fail", f"Config schema invalid: {cp}", [f"    {e}"])
    except yaml.YAMLError as e:
        return None, ("fail", f"Config not valid YAML: {cp}", [f"    {e}"])
    except UnicodeDecodeError as e:
        # A ValueError, not an OSError — it slipped past both handlers above
        # and crashed the one command meant to diagnose a broken config.
        return None, (
            "fail",
            f"Config is not valid UTF-8: {cp}",
            [f"    {e}", "    Re-save the file as UTF-8."],
        )
    except OSError as e:
        details = [f"    {e}"]
        if isinstance(e, PermissionError):
            details.append(_PERMISSION_HINT)
        return None, ("fail", f"Config could not be read: {cp}", details)
    n = len(config.agents)
    return config, ("ok", f"Config: {cp} ({n} {'agent' if n == 1 else 'agents'})", [])


def _protocol_flag_verdict(
    config: DispatchConfig | None, cp: Path
) -> tuple[str, str, list[str]]:
    """Severity of a claude CLI without --append-system-prompt, given the config.

    The runner sends that flag only when there is something to append: the
    dispatch protocol (settings.dispatch_protocol) or an agent's instructions.
    Warning unconditionally meant the printed remedy could never clear the
    warning, so `doctor --strict` could not pass on a correctly configured
    old CLI.
    """
    head = "claude CLI predates --append-system-prompt"
    if config is None:
        return "warn", f"{head}: dispatches that need it will fail", [
            "    Config could not be loaded, so whether dispatches send it is unknown.",
            "    Upgrade claude to use the dispatch protocol.",
        ]
    protocol = config.settings.dispatch_protocol
    instructed = [name for name, a in config.agents.items() if a.instructions.strip()]
    if not protocol and not instructed:
        return "ok", f"{head}; unused (dispatch_protocol is off, no agent has instructions)", []
    if protocol:
        msg = f"{head}: every dispatch will fail"
        details = ["    settings.dispatch_protocol is on (the default)."]
    else:
        msg = f"{head}: dispatches to {', '.join(instructed)} will fail"
        details = []
    if instructed:
        details.append(f"    Agents with instructions: {', '.join(instructed)}.")
    details.append("    Upgrade claude (recommended), or stop sending the flag:")
    if protocol:
        # No CLI command sets settings; say exactly which key to edit.
        details.append(f"      set `dispatch_protocol: false` under `settings:` in {cp}")
    for name in instructed:
        details.append(f"      agent-dispatch update {name} --instructions none")
    return "warn", msg, details


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output a structured diagnostic report.")
@click.option("--strict", is_flag=True, help="Exit with code 1 for warnings as well as failures.")
def doctor(as_json: bool, strict: bool) -> None:
    """Diagnose the agent-dispatch setup and surface common issues."""
    counters = {"issues": 0, "warnings": 0}
    checks: list[dict] = []
    current_section = ""

    def section(title: str) -> None:
        nonlocal current_section
        current_section = title
        if not as_json:
            click.echo(f"\n{click.style(title, bold=True)}")

    def record(status: str, msg: str) -> None:
        checks.append({"section": current_section, "status": status, "message": msg, "details": []})
        if not as_json:
            colors = {"ok": "green", "warn": "yellow", "fail": "red"}
            label = click.style(status.upper(), fg=colors[status])
            click.echo(f"  [{label}] {msg}")

    def detail(msg: str) -> None:
        checks[-1]["details"].append(msg.strip())
        if not as_json:
            click.echo(msg)

    def ok(msg: str) -> None:
        record("ok", msg)

    def warn(msg: str) -> None:
        counters["warnings"] += 1
        record("warn", msg)

    def fail(msg: str) -> None:
        counters["issues"] += 1
        record("fail", msg)

    def emit(status: str, msg: str, details: list[str]) -> None:
        {"ok": ok, "warn": warn, "fail": fail}[status](msg)
        for line in details:
            detail(line)

    # Loaded up front, reported under "Config" below. The Environment section
    # needs it too: whether an old claude CLI actually breaks anything depends
    # on settings.dispatch_protocol and on which agents carry instructions.
    cp = config_path()
    config, config_check = _doctor_load_config(cp)

    section("Environment")
    claude_path = shutil.which("claude")
    if claude_path:
        ok(f"claude CLI: {claude_path}")
        supported = _claude_supports_flag(claude_path, "--append-system-prompt")
        if supported is True:
            ok("claude CLI supports --append-system-prompt (dispatch protocol)")
        elif supported is False:
            emit(*_protocol_flag_verdict(config, cp))
        else:
            warn("Could not run `claude --help` to check --append-system-prompt support")
    else:
        fail("claude CLI not found on PATH")
        detail("    Install: https://docs.anthropic.com/en/docs/claude-code")

    ad_path = shutil.which("agent-dispatch")
    if ad_path:
        ok(f"agent-dispatch CLI: {ad_path}")
    else:
        warn("agent-dispatch not on PATH (MCP server still works via absolute path)")

    section("Running servers")
    from . import __version__, servers
    from . import usage as usage_mod

    live = servers.live_servers()
    if not live:
        ok("No agent-dispatch server is registered as running")
    else:
        drift = servers.version_drift(__version__, entries=live)
        stale_count = sum(drift.values())
        ok(f"{len(live)} server process(es) registered, {len(live) - stale_count} on {__version__}")
    # Printed in BOTH branches on purpose. A server only registers if it was
    # started on a version that has this registry, so the count is a lower
    # bound — on the machine this was written, 19 servers were running and
    # exactly 1 of them appeared here. Reporting "1 server running" without
    # this line would be a diagnostic that quietly under-counts.
    detail("    Lower bound: only servers started on this version or later register here.")
    if live:
        if drift:
            versions = ", ".join(f"{n}x {v}" for v, n in drift.items())
            warn(f"{stale_count} server(s) still executing older code: {versions}")
            detail("    A server keeps its modules in memory for life, so an upgrade")
            detail("    reaches an open Claude Code session only when that session")
            detail("    restarts. Restart them to pick up the installed version.")
            for entry in live:
                version = str(entry.get("version") or "unknown")
                if version != __version__:
                    started = usage_mod.as_float(entry.get("started_at"))
                    age = f", up {_age(started)}" if started > 0 else ""
                    detail(f"      pid {entry.get('pid')}: {version}{age}")

    section("Config")
    emit(*config_check)

    section("MCP registration")
    if claude_path is None:
        warn("Skipped (claude CLI missing)")
    else:
        try:
            result = subprocess.run(
                [claude_path, "mcp", "list"],
                capture_output=True,
                text=True,
                # Strict decoding raised UnicodeDecodeError — a ValueError, so
                # it walked past every arm below and doctor (and --json) died
                # with a traceback on one non-UTF-8 byte in a server's name.
                encoding="utf-8",
                errors="replace",
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
                detail("    Run: agent-dispatch init")
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

    section("Groups")
    if config is None:
        warn("Skipped (config could not be loaded)")
    elif not config.groups:
        ok("No groups configured")
    else:
        for name, group in config.groups.items():
            unknown = config.unknown_group_members(group)
            if unknown:
                missing = ", ".join(unknown)
                fail(f"{name}: unknown member(s): {missing}")
                detail(
                    f"    Fix by recreating: agent-dispatch group remove {name} && "
                    f"agent-dispatch group add {name} --member ... (or edit agents.yaml)"
                )
            elif not group.members:
                warn(f"{name}: no members configured")
                detail(
                    f"    Add members by recreating: agent-dispatch group remove {name} && "
                    f"agent-dispatch group add {name} --member agent1 --member agent2"
                )
            else:
                count = len(group.members)
                suffix = "member" if count == 1 else "members"
                ok(f"{name}: {count} {suffix}")

    issues = counters["issues"]
    warnings = counters["warnings"]
    if as_json:
        click.echo(json.dumps({
            "schema_version": 1,
            "status": "fail" if issues else "warn" if warnings else "ok",
            **counters,
            "checks": checks,
        }, ensure_ascii=False, indent=2))
        if issues or (strict and warnings):
            raise SystemExit(1)
        return

    section("Summary")
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
        if issues > 0 or (strict and warnings):
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


def _job_error(message: str, *, as_json: bool) -> NoReturn:
    """Keep operational errors parseable for callers using job commands as JSON."""
    if as_json:
        click.echo(json.dumps({"error": message}, ensure_ascii=False))
    else:
        click.echo(click.style(message, fg="red"))
    raise SystemExit(1)


@cli.command("jobs")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "running", "done", "failed", "cancelled"]),
    help="Filter by job status.",
)
@click.option("--limit", default=20, type=int, help="Max jobs shown (default: 20).")
@click.option("--agent", default=None, help="Filter by exact agent name, including removed agents.")
@click.option("--json", "as_json", is_flag=True, help="Output compact job summaries as JSON.")
def jobs_list(status: str | None, limit: int, agent: str | None, as_json: bool) -> None:
    """List async dispatch jobs (most recent first)."""
    try:
        jobs = _job_store().list(status=status, agent=agent)  # type: ignore[arg-type]
    except OSError as e:
        _job_error(f"Could not read job storage: {e}", as_json=as_json)
    jobs = jobs[: max(1, limit)]
    if as_json:
        click.echo(json.dumps([job.summary() for job in jobs], ensure_ascii=False, indent=2))
        return
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
@click.option("--json", "as_json", is_flag=True, help="Output the full, untruncated job record.")
def job_show(job_id: str, as_json: bool) -> None:
    """Show one async job in detail (status, progress tail, result preview)."""
    if not is_valid_job_id(job_id):
        _job_error(f"Invalid job id: {job_id!r} (expected 32 hex chars)", as_json=as_json)
    try:
        job = _job_store().get(job_id)
    except OSError as e:
        _job_error(f"Could not read job storage: {e}", as_json=as_json)
    if job is None:
        _job_error(f"Job not found: {job_id}", as_json=as_json)
    if as_json:
        click.echo(job.model_dump_json(indent=2, exclude_none=True))
        return

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
        if job.result.outcome:
            color = "green" if job.result.outcome == "done" else "yellow"
            click.echo(f"  outcome:    {click.style(job.result.outcome, fg=color)}")
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
    # Same guard as `jobs`/`job`: an uncreatable jobs dir (a file in the way,
    # a read-only volume) raised OSError out of JobStore() as a traceback.
    try:
        job, outcome = _job_store().cancel(job_id)
    except OSError as e:
        _job_error(f"Could not access job storage: {e}", as_json=False)
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
@click.option(
    "--all",
    "purge_all",
    is_flag=True,
    help="Purge every terminal job regardless of age (what --days 0 used to do silently).",
)
@click.option("--dry-run", is_flag=True, help="Show eligible jobs without deleting them.")
def jobs_gc(days: int, purge_all: bool, dry_run: bool) -> None:
    """Purge old terminal async jobs (done/failed/cancelled)."""
    # `--days 0` used to clamp to a cutoff of "now" and wipe every terminal job,
    # including return_ref results a caller was about to fetch — while the
    # dispatch_gc MCP tool rejected the same input. Reject it here too.
    if not purge_all and days <= 0:
        click.echo(
            click.style(
                f"Error: --days must be > 0 (got {days}). "
                "Use --all to purge every terminal job on purpose.",
                fg="red",
            )
        )
        raise SystemExit(1)
    max_age_seconds = 0 if purge_all else days * 86400
    try:
        store = _job_store()
        if dry_run:
            candidates = store.gc_candidates(max_age_seconds)
        else:
            deleted = store.gc(max_age_seconds)
    except OSError as e:
        _job_error(f"Could not access job storage: {e}", as_json=False)
    if dry_run:
        for job in candidates:
            task = job.task[:60].replace("\n", " ")
            click.echo(f"{job.id}  {job.status}  {job.agent}  {task}")
        click.echo(f"Would delete {len(candidates)} terminal job(s). No jobs deleted.")
        return
    if purge_all:
        click.echo(f"Deleted {deleted} terminal job(s) (all ages).")
    else:
        click.echo(f"Deleted {deleted} job(s) older than {days} day(s).")


def _fmt_seconds(ms: float) -> str:
    """Human duration from milliseconds: 850ms, 42s, 3m20s."""
    seconds = ms / 1000
    if seconds < 1:
        return f"{int(ms)}ms"
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def _fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items())


@cli.command("stats")
@click.option("--days", default=0, type=int, help="Only count the last N days (default: all).")
@click.option("--agent", default=None, help="Only this agent.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def usage_stats(days: int, agent: str | None, as_json: bool) -> None:
    """Show what dispatches actually cost: spend, durations, outcomes per agent."""
    from . import usage

    if days < 0:
        click.echo(click.style(f"Error: --days must be >= 0 (got {days}).", fg="red"))
        raise SystemExit(1)

    entries = usage.load(days=days, agent=agent)
    report = usage.summarize(entries)
    if as_json:
        # Always valid JSON, including the empty case — a caller piping this
        # into a parser must not get prose on a fresh install, which is
        # exactly when the journal is empty.
        # allow_nan=False: bare NaN/Infinity is not JSON, and json.dumps emits
        # it silently — the consumer's parser is what fails. usage.py bounds
        # the values; this is the backstop that keeps the output parseable.
        try:
            click.echo(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        except ValueError as e:
            click.echo(json.dumps({"error": f"Usage report has a non-finite value: {e}"}))
            raise SystemExit(1) from None
        return
    if not entries:
        window = f" in the last {days} day(s)" if days else ""
        who = f" for agent '{agent}'" if agent else ""
        click.echo(f"No dispatches recorded{who}{window}.")
        click.echo(f"    Journal: {usage.journal_path()}")
        click.echo("    (Recording is off if settings.usage_log is false in agents.yaml.)")
        return

    total = report["total"]
    window = f"last {days} day(s)" if days else "all recorded"
    click.echo(click.style(f"Usage ({window})", bold=True))
    cached = f", {total['cached_hits']} served from cache" if total["cached_hits"] else ""
    click.echo(f"  dispatches: {total['dispatches']}{cached}")
    click.echo(f"  spend:      ${total['cost_usd']:.4f}")
    if "median_ms" in total:
        click.echo(
            f"  duration:   median {_fmt_seconds(total['median_ms'])}, "
            f"p90 {_fmt_seconds(total['p90_ms'])}, max {_fmt_seconds(total['max_ms'])}"
        )
    if total.get("outcomes"):
        click.echo(f"  outcomes:   {_fmt_counts(total['outcomes'])}")
    if total.get("errors"):
        click.echo(click.style(f"  failures:   {_fmt_counts(total['errors'])}", fg="yellow"))

    click.echo()
    click.echo(click.style("Per agent", bold=True))
    for name, st in report["agents"].items():
        head = (
            f"  {click.style(name, bold=True):<24} {st['dispatches']:>4} "
            f"{'run ' if st['dispatches'] == 1 else 'runs'}  "
            f"${st['cost_usd']:>8.4f}"
        )
        if "median_ms" in st:
            head += (
                f"  median {_fmt_seconds(st['median_ms']):>6}  p90 {_fmt_seconds(st['p90_ms']):>6}"
            )
        click.echo(head)
        detail = []
        if st.get("outcomes"):
            detail.append(_fmt_counts(st["outcomes"]))
        if st.get("errors"):
            detail.append(click.style(_fmt_counts(st["errors"]), fg="yellow"))
        if st["cached_hits"]:
            detail.append(f"{st['cached_hits']} cached")
        if detail:
            click.echo(f"      {'  |  '.join(detail)}")

    click.echo()
    click.echo(f"Journal: {usage.journal_path()}")


@cli.command()
def serve() -> None:
    """Start the MCP server (stdio transport)."""
    from .server import main

    main()
