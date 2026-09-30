"""Shared test fixtures."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_dispatch import servers, usage
from agent_dispatch.config import save_config
from agent_dispatch.models import AgentConfig, DispatchConfig, Settings

_CLAUDE_NAMES = frozenset({"claude", "claude.exe", "claude.cmd"})


def _spawned_program_names(args, kwargs) -> list[str]:
    """Every name a Popen call could run: ``executable=`` and argv[0].

    Total by design — the guard must never be the thing that raises for an
    argument shape Popen itself accepts (a single str/bytes/PathLike, a
    sequence of any of those, bytes argv) or rejects (an empty list: let the
    real Popen report that). With ``shell=True`` a string is a command line,
    so its first word is the program.
    """
    names: list[str] = []
    try:
        executable = kwargs.get("executable")
        if executable is not None:
            names.append(os.fsdecode(executable))
        if isinstance(args, (str, bytes, os.PathLike)):
            program = os.fsdecode(args)
            if kwargs.get("shell"):
                program = (program.split() or [""])[0]
        else:
            program = os.fsdecode(next(iter(args)))
        names.append(program)
    except (TypeError, ValueError, StopIteration):
        pass
    return [Path(name).name for name in names]


@pytest.fixture(autouse=True)
def _prevent_real_claude(monkeypatch):
    """Fail the test if it spawns the real claude CLI (a forgotten subprocess mock).

    Raising alone is not enough: ``pytest.fail`` raises ``Failed``, a
    BaseException, and the spawn usually happens off the test's thread — the
    async-job worker ``_run_job`` catches only ``Exception``, so the thread
    died, pytest merely *warned*, and a test that forgot to mock
    ``dispatch_stream`` passed. Paths that swallow it (``gather(...,
    return_exceptions=True)``) hide it entirely. So every hit is also recorded
    and the test is failed at teardown, whichever thread tripped the guard.
    Yields the hit list so the guard's own tests can inspect and clear it.
    """
    real_popen = subprocess.Popen
    hits: list[str] = []

    def guarded_popen(args, *positional, **kwargs):
        names = _spawned_program_names(args, kwargs)
        if _CLAUDE_NAMES.intersection(names):
            hits.append(repr(args))
            pytest.fail(f"Tests must mock the claude CLI before invoking it: {args!r}")
        return real_popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    yield hits
    if hits:
        pytest.fail(
            f"Real claude CLI spawned {len(hits)} time(s) (mock it): {', '.join(hits)}",
            pytrace=False,
        )


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
