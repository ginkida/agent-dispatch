"""Tests for config loading and saving."""

from __future__ import annotations

import json
from pathlib import Path

from agent_dispatch.config import auto_describe, load_config, save_config
from agent_dispatch.models import AgentConfig, DispatchConfig, Settings


def test_load_missing_file(tmp_path: Path):
    config = load_config(tmp_path / "nonexistent.yaml")
    assert config.agents == {}


def test_load_empty_file(tmp_path: Path):
    f = tmp_path / "empty.yaml"
    f.write_text("")
    config = load_config(f)
    assert config.agents == {}


def test_save_and_load_roundtrip(tmp_path: Path):
    f = tmp_path / "test.yaml"
    config = DispatchConfig(
        agents={
            "demo": AgentConfig(directory="/tmp", description="Demo agent", timeout=60),
        }
    )
    save_config(config, f)

    loaded = load_config(f)
    assert "demo" in loaded.agents
    assert loaded.agents["demo"].description == "Demo agent"
    assert loaded.agents["demo"].timeout == 60


def test_save_and_load_settings_roundtrip(tmp_path: Path):
    """Verify max_concurrency + cache settings survive YAML roundtrip."""
    from agent_dispatch.models import CacheSettings
    f = tmp_path / "test.yaml"
    config = DispatchConfig(
        settings=Settings(max_concurrency=3, cache=CacheSettings(enabled=False, ttl=120)),
    )
    save_config(config, f)
    loaded = load_config(f)
    assert loaded.settings.max_concurrency == 3
    assert loaded.settings.cache.enabled is False
    assert loaded.settings.cache.ttl == 120


def test_load_via_env_var(tmp_config: Path, sample_config: DispatchConfig):
    """Test that AGENT_DISPATCH_CONFIG env var is respected."""
    loaded = load_config()
    assert "test" in loaded.agents


def test_auto_describe_with_claude_md(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# My Project\nThis is a cool project.\n## Details\nMore.")
    desc = auto_describe(tmp_path)
    assert "cool project" in desc


def test_auto_describe_with_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test"\ndescription = "A fast API server"\n'
    )
    desc = auto_describe(tmp_path)
    assert "fast API server" in desc


def test_auto_describe_with_mcp_json(tmp_path: Path):
    mcp = {"mcpServers": {"portainer": {}, "postgres": {}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(mcp))
    desc = auto_describe(tmp_path)
    assert "portainer" in desc
    assert "postgres" in desc


def test_auto_describe_readme_fallback(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "# My Project\nThis is an awesome backend service for handling payments.\n"
    )
    desc = auto_describe(tmp_path)
    assert "awesome backend service" in desc


def test_auto_describe_claude_md_takes_priority_over_readme(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# Proj\nCLAUDE description here.")
    (tmp_path / "README.md").write_text("# Proj\nREADME description here.")
    desc = auto_describe(tmp_path)
    assert "CLAUDE description" in desc
    assert "README" not in desc


def test_auto_describe_db_indicators(tmp_path: Path):
    (tmp_path / "alembic.ini").write_text("[alembic]")
    (tmp_path / "migrations").mkdir()
    desc = auto_describe(tmp_path)
    assert "Alembic" in desc
    assert "migrations" in desc


def test_auto_describe_mcp_deduplication(tmp_path: Path):
    mcp1 = {"mcpServers": {"postgres": {}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(mcp1))
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    mcp2 = {"mcpServers": {"postgres": {}, "redis": {}}}
    (settings_dir / "settings.local.json").write_text(json.dumps(mcp2))
    desc = auto_describe(tmp_path)
    assert desc.count("postgres") == 1
    assert "redis" in desc


def test_auto_describe_with_stack_indicators(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12")
    (tmp_path / "go.mod").write_text("module example.com/foo")
    desc = auto_describe(tmp_path)
    assert "Docker" in desc
    assert "Go" in desc


def test_auto_describe_fallback(tmp_path: Path):
    desc = auto_describe(tmp_path)
    assert tmp_path.name in desc
