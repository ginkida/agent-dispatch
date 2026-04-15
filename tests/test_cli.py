"""Tests for the CLI commands."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agent_dispatch.cli import cli
from agent_dispatch.config import load_config


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
