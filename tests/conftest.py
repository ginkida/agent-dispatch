"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_dispatch import servers, usage
from agent_dispatch.config import save_config
from agent_dispatch.models import AgentConfig, DispatchConfig, Settings


@pytest.fixture(autouse=True)
def _isolated_local_state(tmp_path: Path, monkeypatch):
    """Redirect the usage journal and server registry into tmp_path for EVERY test.

    `runner.dispatch` records each result, and runner tests build a bare
    `Settings()` with no config env override — without this fixture a plain
    `pytest` run appended a hundred fake "test"/"infra" dispatches to the
    developer's real ~/.config/agent-dispatch/usage.jsonl (observed, then
    cleaned, on 2026-09-09). Same rationale as the config/jobs redirects in
    test_server.py's `_reset_globals`.
    """
    monkeypatch.setenv("AGENT_DISPATCH_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    # Same reasoning for the live-server registry: `doctor` reads it, so without
    # this a test's output would depend on how many real MCP servers happen to
    # be running on the developer's machine.
    monkeypatch.setenv("AGENT_DISPATCH_SERVERS_DIR", str(tmp_path / "servers"))
    # The profile memo is a module-level global keyed by the journal's
    # fingerprint; clearing it keeps one test's timings out of the next.
    usage._profile_memo = None
    yield
    usage._profile_memo = None
    if servers._registration_fd is not None:  # a test that registered this process
        os.close(servers._registration_fd)
        servers._registration_fd = None


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
