"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_dispatch.config import save_config
from agent_dispatch.models import AgentConfig, DispatchConfig, Settings


@pytest.fixture()
def tmp_config(tmp_path: Path):
    """Provide a temporary config file and set env var."""
    config_file = tmp_path / "agents.yaml"
    os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
    yield config_file
    os.environ.pop("AGENT_DISPATCH_CONFIG", None)


@pytest.fixture()
def sample_config(tmp_config: Path, tmp_path: Path) -> DispatchConfig:
    """Create and save a sample config with a test agent."""
    agent_dir = tmp_path / "test-project"
    agent_dir.mkdir()
    (agent_dir / "CLAUDE.md").write_text("# Test Project\nA test project for unit tests.")

    config = DispatchConfig(
        agents={
            "test": AgentConfig(
                directory=agent_dir,
                description="Test agent",
                timeout=30,
            ),
        },
        settings=Settings(max_dispatch_depth=3),
    )
    save_config(config, tmp_config)
    return config
