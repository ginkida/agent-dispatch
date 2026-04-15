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
