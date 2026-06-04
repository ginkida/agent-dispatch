"""Tests for the CLI commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agent_dispatch.cli import cli
from agent_dispatch.config import load_config
from agent_dispatch.models import DispatchResult


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path):
    """Point config at a temp file so tests don't touch the real config."""
    config_file = tmp_path / "agents.yaml"
    with patch.dict(os.environ, {"AGENT_DISPATCH_CONFIG": str(config_file)}):
        yield config_file

runner = CliRunner()


class TestAdd:
    def test_add_basic(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        result = runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test agent"])
        assert result.exit_code == 0
        assert "Added agent" in result.output

    def test_add_with_permissions(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        result = runner.invoke(cli, [
            "add", "proj", str(agent_dir),
            "-d", "Test",
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Bash,Read",
        ])
        assert result.exit_code == 0
        config = load_config()
        assert config.agents["proj"].permission_mode == "bypassPermissions"
        assert config.agents["proj"].allowed_tools == ["Bash", "Read"]

    def test_add_with_max_budget(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        result = runner.invoke(cli, [
            "add", "proj", str(agent_dir), "-d", "Test", "--max-budget", "1.5",
        ])
        assert result.exit_code == 0
        config = load_config()
        assert config.agents["proj"].max_budget_usd == 1.5

    def test_add_invalid_name(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        result = runner.invoke(cli, ["add", "-bad", str(agent_dir)])
        assert result.exit_code != 0

    def test_add_duplicate(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        result = runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_add_auto_describe(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        (agent_dir / "CLAUDE.md").write_text("# Proj\nA cool project for testing.")
        result = runner.invoke(cli, ["add", "proj", str(agent_dir)])
        assert result.exit_code == 0
        assert "Auto-generated description" in result.output

    def test_add_unknown_permission_mode_warns(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        result = runner.invoke(cli, [
            "add", "proj", str(agent_dir), "-d", "Test",
            "--permission-mode", "typoMode",
        ])
        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "Unknown" in result.output


class TestRemove:
    def test_remove(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        result = runner.invoke(cli, ["remove", "proj"])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_remove_nonexistent(self):
        result = runner.invoke(cli, ["remove", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestList:
    def test_list_empty(self):
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No agents configured" in result.output

    def test_list_shows_agents(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "My test agent"])
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "proj" in result.output
        assert "My test agent" in result.output

    def test_list_shows_extras(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, [
            "add", "proj", str(agent_dir), "-d", "Test",
            "--timeout", "600", "--model", "sonnet", "--max-budget", "2.0",
        ])
        result = runner.invoke(cli, ["list"])
        assert "timeout=600s" in result.output
        assert "model=sonnet" in result.output
        assert "budget=$2.0" in result.output

    def test_list_shows_permissions(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, [
            "add", "proj", str(agent_dir), "-d", "Test",
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Bash,Read",
        ])
        result = runner.invoke(cli, ["list"])
        assert "permission_mode: bypassPermissions" in result.output
        assert "allowed_tools: Bash, Read" in result.output

    def test_list_distinguishes_empty_from_inherit(self, tmp_path: Path):
        """Explicit allowed_tools=[] should show '(none)' to distinguish from inherit (None)."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        # Write config directly with explicit empty list
        import yaml

        from agent_dispatch.config import config_path
        cfg_path = config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        yaml.safe_dump({
            "agents": {
                "proj": {
                    "directory": str(agent_dir),
                    "description": "Test",
                    "allowed_tools": [],
                    "disallowed_tools": [],
                },
            },
        }, cfg_path.open("w"))

        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "allowed_tools: (none)" in result.output
        assert "disallowed_tools: (none)" in result.output

    def test_list_hides_tools_when_none(self, tmp_path: Path):
        """allowed_tools=None (the default, meaning 'inherit') should not appear in list."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        result = runner.invoke(cli, ["list"])
        assert "allowed_tools" not in result.output
        assert "disallowed_tools" not in result.output


class TestUpdate:
    def test_update_permission_mode(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        result = runner.invoke(cli, [
            "update", "proj", "--permission-mode", "bypassPermissions",
        ])
        assert result.exit_code == 0
        assert "Updated" in result.output
        config = load_config()
        assert config.agents["proj"].permission_mode == "bypassPermissions"

    def test_update_clear_permission_mode(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, [
            "add", "proj", str(agent_dir), "-d", "Test",
            "--permission-mode", "bypassPermissions",
        ])
        result = runner.invoke(cli, ["update", "proj", "--permission-mode", "none"])
        assert result.exit_code == 0
        config = load_config()
        assert config.agents["proj"].permission_mode is None

    def test_update_nonexistent(self):
        result = runner.invoke(cli, ["update", "nonexistent", "-d", "x"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_update_nothing(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        result = runner.invoke(cli, ["update", "proj"])
        assert result.exit_code != 0
        assert "Nothing to update" in result.output

    def test_update_unknown_permission_warns(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        result = runner.invoke(cli, ["update", "proj", "--permission-mode", "badMode"])
        assert result.exit_code == 0
        assert "Warning" in result.output

    def test_update_max_budget(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        runner.invoke(cli, ["update", "proj", "--max-budget", "2.5"])
        config = load_config()
        assert config.agents["proj"].max_budget_usd == 2.5
        # Clear it
        runner.invoke(cli, ["update", "proj", "--max-budget", "0"])
        config = load_config()
        assert config.agents["proj"].max_budget_usd is None

    def test_update_empty_string_clears_model(self, tmp_path: Path):
        """B2: --model "" should clear to None, not store empty string."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, [
            "add", "proj", str(agent_dir), "-d", "Test", "--model", "sonnet",
        ])
        result = runner.invoke(cli, ["update", "proj", "--model", ""])
        assert result.exit_code == 0
        config = load_config()
        assert config.agents["proj"].model is None

    def test_update_empty_string_clears_permission_mode(self, tmp_path: Path):
        """B2: --permission-mode "" should clear to None."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, [
            "add", "proj", str(agent_dir), "-d", "Test",
            "--permission-mode", "bypassPermissions",
        ])
        result = runner.invoke(cli, ["update", "proj", "--permission-mode", ""])
        assert result.exit_code == 0
        config = load_config()
        assert config.agents["proj"].permission_mode is None

    def test_update_allowed_tools_empty_clears_to_none(self, tmp_path: Path):
        """B1+B2: --allowed-tools "" clears to None (inherit defaults)."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, [
            "add", "proj", str(agent_dir), "-d", "Test",
            "--allowed-tools", "Bash,Read",
        ])
        runner.invoke(cli, ["update", "proj", "--allowed-tools", ""])
        config = load_config()
        assert config.agents["proj"].allowed_tools is None


class TestListUnreadable:
    def test_list_unreadable_directory(self, tmp_path: Path):
        """A4: is_dir() OSError should show UNREADABLE, not crash."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])

        with patch(
            "agent_dispatch.cli.Path.is_dir",
            side_effect=PermissionError("access denied"),
        ):
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "UNREADABLE" in result.output


class TestListMalformedConfig:
    def test_list_handles_bad_yaml(self, _isolated_config: Path):
        """A5: malformed YAML shows friendly error, not traceback."""
        _isolated_config.parent.mkdir(parents=True, exist_ok=True)
        _isolated_config.write_text("agents: [not a dict\n")  # invalid YAML
        result = runner.invoke(cli, ["list"])
        assert result.exit_code != 0
        assert "not valid YAML" in result.output

    def test_list_handles_bad_schema(self, _isolated_config: Path):
        """A5: YAML that doesn't match schema shows friendly error."""
        _isolated_config.parent.mkdir(parents=True, exist_ok=True)
        _isolated_config.write_text("agents: 42\nsettings: {}\n")
        result = runner.invoke(cli, ["list"])
        assert result.exit_code != 0
        assert "invalid schema" in result.output


class TestInit:
    def test_init_creates_config(self, _isolated_config: Path):
        config_file = _isolated_config
        assert not config_file.exists()
        with (
            patch("agent_dispatch.cli.shutil.which", return_value=None),
        ):
            result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "Created config" in result.output
        assert config_file.exists()

    def test_init_existing_config(self, _isolated_config: Path):
        config_file = _isolated_config
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("agents: {}\n")
        with patch("agent_dispatch.cli.shutil.which", return_value=None):
            result = runner.invoke(cli, ["init"])
        assert "already exists" in result.output

    def test_init_claude_not_found(self, _isolated_config: Path):
        with patch("agent_dispatch.cli.shutil.which", return_value=None):
            result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "claude CLI not found" in result.output

    def test_init_registers_mcp_server(self, _isolated_config: Path):
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            patch("agent_dispatch.cli.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = runner.invoke(cli, ["init"])
        assert "Registered MCP server" in result.output
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "claude" in cmd[0]
        assert "mcp" in cmd
        assert "add-json" in cmd

    def test_init_mcp_registration_fails(self, _isolated_config: Path):
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            patch("agent_dispatch.cli.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="registration failed"
            )
            result = runner.invoke(cli, ["init"])
        assert "Failed to register" in result.output


class TestDescribe:
    """Tests for `agent-dispatch describe <name>` command."""

    def test_describe_basic(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, [
            "add", "proj", str(agent_dir),
            "-d", "My agent",
            "--timeout", "600",
            "--model", "sonnet",
            "--max-budget", "1.5",
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Bash,Read",
        ])
        result = runner.invoke(cli, ["describe", "proj"])
        assert result.exit_code == 0
        assert "proj" in result.output
        assert "OK" in result.output
        assert "My agent" in result.output
        assert "600s" in result.output
        assert "sonnet" in result.output
        assert "$1.5" in result.output
        assert "bypassPermissions" in result.output
        assert "Bash, Read" in result.output

    def test_describe_nonexistent(self):
        result = runner.invoke(cli, ["describe", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_describe_inherit_vs_explicit_empty(self, _isolated_config: Path, tmp_path: Path):
        """Tools field should distinguish None (inherit) from [] (override)."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        # Explicit empty list (override defaults)
        _isolated_config.parent.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml
        _yaml.safe_dump({
            "agents": {
                "proj": {
                    "directory": str(agent_dir),
                    "description": "Test",
                    "allowed_tools": [],         # explicit override
                    # disallowed_tools omitted → None → inherit
                },
            },
        }, _isolated_config.open("w"))
        result = runner.invoke(cli, ["describe", "proj"])
        assert result.exit_code == 0
        # allowed_tools=[] → "(none — explicit override)"
        assert "explicit override" in result.output
        # disallowed_tools=None → "(inherit defaults)"
        assert "inherit defaults" in result.output

    def test_describe_missing_directory_shows_not_found(self, _isolated_config: Path):
        _isolated_config.parent.mkdir(parents=True, exist_ok=True)
        _isolated_config.write_text(
            "agents:\n"
            "  proj:\n"
            "    directory: /nonexistent/xyz\n"
            "    description: Test\n"
        )
        result = runner.invoke(cli, ["describe", "proj"])
        assert result.exit_code == 0
        assert "NOT FOUND" in result.output

    def test_describe_lists_project_files(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        (agent_dir / "CLAUDE.md").write_text("# Proj")
        (agent_dir / "README.md").write_text("# Proj README")
        (agent_dir / ".mcp.json").write_text("{}")
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        result = runner.invoke(cli, ["describe", "proj"])
        assert "CLAUDE.md" in result.output
        assert "README.md" in result.output
        assert ".mcp.json" in result.output


class TestDoctor:
    """Tests for `agent-dispatch doctor` diagnostic command."""

    def _patch_claude_mcp_list(
        self, *, registered: bool = True, fail: bool = False, stdout: str | None = None,
    ):
        """Helper: patch subprocess.run for `claude mcp list` calls.

        Mirrors the real CLI output format:
            agent-dispatch: /path/to/agent-dispatch serve - Connected
        """
        if fail:
            return patch(
                "agent_dispatch.cli.subprocess.run",
                side_effect=FileNotFoundError("no claude"),
            )
        if stdout is None:
            if registered:
                stdout = (
                    "agent-dispatch: /opt/homebrew/bin/agent-dispatch serve - Connected\n"
                    "foo: /usr/bin/foo serve - Connected\n"
                )
            else:
                stdout = "foo: /usr/bin/foo serve - Connected\n"
        return patch(
            "agent_dispatch.cli.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=stdout, stderr="",
            ),
        )

    def test_all_ok(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=True),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "claude CLI" in result.output
        assert "All checks passed" in result.output
        assert "FAIL" not in result.output

    def test_claude_cli_missing(self, tmp_path: Path):
        with patch("agent_dispatch.cli.shutil.which", return_value=None):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code != 0
        assert "claude CLI not found" in result.output
        assert "FAIL" in result.output

    def test_agent_dispatch_not_on_path_warns(self, tmp_path: Path):
        def which(name: str):
            return None if name == "agent-dispatch" else f"/usr/bin/{name}"

        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=which),
            self._patch_claude_mcp_list(registered=True),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert "agent-dispatch not on PATH" in result.output
        assert "WARN" in result.output

    def test_config_missing_warns(self, _isolated_config: Path):
        """No config file → WARN, exit 0."""
        assert not _isolated_config.exists()
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=True),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0  # warnings don't fail
        assert "Config not found" in result.output
        assert "agent-dispatch init" in result.output

    def test_config_invalid_yaml_fails(self, _isolated_config: Path):
        _isolated_config.parent.mkdir(parents=True, exist_ok=True)
        _isolated_config.write_text("agents: [not a dict\n")
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=True),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code != 0
        assert "not valid YAML" in result.output

    def test_config_invalid_schema_fails(self, _isolated_config: Path):
        _isolated_config.parent.mkdir(parents=True, exist_ok=True)
        _isolated_config.write_text("agents: 42\nsettings: {}\n")
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=True),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code != 0
        assert "schema invalid" in result.output

    def test_mcp_not_registered_warns(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=False),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "not registered with Claude Code" in result.output
        assert "WARN" in result.output

    def test_mcp_check_avoids_false_positive_in_path(self, tmp_path: Path):
        """A line mentioning 'agent-dispatch' only in a path/command — but no
        MCP server entry by that name — should NOT be treated as registered."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        # Notice: 'agent-dispatch' appears only as a path component of another server
        misleading = "other-server: /opt/bin/agent-dispatch-helper - Connected\n"
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(stdout=misleading),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert "not registered with Claude Code" in result.output

    def test_mcp_check_handles_timeout(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            patch(
                "agent_dispatch.cli.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=10),
            ),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "timed out" in result.output

    def test_missing_agent_directory_fails(self, _isolated_config: Path):
        """Agent's directory was deleted after `add` → FAIL exit."""
        # Write config pointing at a directory that doesn't exist
        _isolated_config.parent.mkdir(parents=True, exist_ok=True)
        _isolated_config.write_text(
            "agents:\n"
            "  proj:\n"
            "    directory: /nonexistent/path/xyz\n"
            "    description: Test\n"
        )
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=True),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code != 0
        assert "directory missing" in result.output

    def test_unreadable_directory_fails(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=True),
            patch(
                "agent_dispatch.cli.Path.is_dir",
                side_effect=PermissionError("denied"),
            ),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code != 0
        assert "unreadable" in result.output

    def test_no_agents_warns(self, _isolated_config: Path):
        """Config exists but has no agents → WARN, exit 0."""
        _isolated_config.parent.mkdir(parents=True, exist_ok=True)
        _isolated_config.write_text("agents: {}\nsettings: {}\n")
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=True),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "No agents configured" in result.output

    def test_lists_claude_md_and_mcp_json(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        (agent_dir / "CLAUDE.md").write_text("# Proj")
        (agent_dir / ".mcp.json").write_text("{}")
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with (
            patch("agent_dispatch.cli.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            self._patch_claude_mcp_list(registered=True),
        ):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "CLAUDE.md" in result.output
        assert ".mcp.json" in result.output

    def test_summary_singular_plural(self, tmp_path: Path):
        """One issue should say 'issue' not 'issues'."""
        with patch("agent_dispatch.cli.shutil.which", return_value=None):
            result = runner.invoke(cli, ["doctor"])
        # Exactly one FAIL (claude CLI missing)
        assert "1 issue" in result.output
        assert "1 issues" not in result.output


class TestTestCommand:
    def test_success(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test agent"])
        with patch("agent_dispatch.runner.dispatch") as mock_dispatch:
            mock_dispatch.return_value = DispatchResult(
                agent="proj", success=True, result="This is a test project.",
                cost_usd=0.01, num_turns=1,
            )
            result = runner.invoke(cli, ["test", "proj"])
        assert result.exit_code == 0
        assert "This is a test project" in result.output
        assert "$0.0100" in result.output

    def test_agent_not_found(self):
        result = runner.invoke(cli, ["test", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_permission_error_shows_diagnosis(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with patch("agent_dispatch.runner.dispatch") as mock_dispatch:
            mock_dispatch.return_value = DispatchResult(
                agent="proj", success=False, result="",
                error="permission denied for tool Bash",
                error_type="permission",
            )
            result = runner.invoke(cli, ["test", "proj"])
        assert result.exit_code != 0
        assert "Diagnosis: permission error" in result.output
        assert "bypassPermissions" in result.output

    def test_timeout_error_shows_diagnosis(self, tmp_path: Path):
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with patch("agent_dispatch.runner.dispatch") as mock_dispatch:
            mock_dispatch.return_value = DispatchResult(
                agent="proj", success=False, result="",
                error="timed out after 300s",
                error_type="timeout",
            )
            result = runner.invoke(cli, ["test", "proj"])
        assert result.exit_code != 0
        assert "Diagnosis: timeout" in result.output
        assert "--timeout 600" in result.output

    def test_stream_uses_dispatch_stream(self, tmp_path: Path):
        """--stream should call dispatch_stream, not dispatch, and forward progress."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])

        def _fake_stream(name, task, agent, settings, on_progress=None, **_kw):
            assert on_progress is not None
            on_progress("Reading file foo.py")
            on_progress("Using tool: Edit")
            return DispatchResult(
                agent=name, success=True, result="done",
                cost_usd=0.01, num_turns=1,
            )

        with (
            patch("agent_dispatch.runner.dispatch_stream", side_effect=_fake_stream),
            patch("agent_dispatch.runner.dispatch") as plain_dispatch,
        ):
            result = runner.invoke(cli, ["test", "proj", "--stream"])
        assert result.exit_code == 0
        assert "done" in result.output
        # Progress goes to stderr, captured into result.output by CliRunner
        assert "Reading file foo.py" in result.output
        assert "Using tool: Edit" in result.output
        # Non-stream path must NOT be invoked
        plain_dispatch.assert_not_called()

    def test_no_stream_uses_dispatch(self, tmp_path: Path):
        """Default (no --stream) should call dispatch, not dispatch_stream."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with (
            patch("agent_dispatch.runner.dispatch") as mock_dispatch,
            patch("agent_dispatch.runner.dispatch_stream") as mock_stream,
        ):
            mock_dispatch.return_value = DispatchResult(
                agent="proj", success=True, result="ok",
            )
            runner.invoke(cli, ["test", "proj"])
        mock_dispatch.assert_called_once()
        mock_stream.assert_not_called()

    def test_timeout_flag_overrides_agent_config(self, tmp_path: Path):
        """--timeout applies a one-off override without touching the config."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        seen: dict = {}

        def _fake_dispatch(name, task, agent, settings, **_kw):
            seen["timeout"] = agent.timeout
            return DispatchResult(agent=name, success=True, result="ok")

        with patch("agent_dispatch.runner.dispatch", side_effect=_fake_dispatch):
            result = runner.invoke(cli, ["test", "proj", "--timeout", "900"])
        assert result.exit_code == 0
        assert seen["timeout"] == 900
        # Persisted config keeps the original timeout
        from agent_dispatch.config import load_config
        assert load_config().agents["proj"].timeout == 300

    def test_success_with_hint_prints_note(self, tmp_path: Path):
        """A successful-but-degraded result (denied tools) surfaces the hint."""
        agent_dir = tmp_path / "proj"
        agent_dir.mkdir()
        runner.invoke(cli, ["add", "proj", str(agent_dir), "-d", "Test"])
        with patch("agent_dispatch.runner.dispatch") as mock_dispatch:
            mock_dispatch.return_value = DispatchResult(
                agent="proj", success=True, result="partial",
                denied_tools=["Bash"], hint="1 tool call(s) were denied: Bash.",
            )
            result = runner.invoke(cli, ["test", "proj"])
        assert result.exit_code == 0
        assert "Note:" in result.output
        assert "denied" in result.output
