"""Tests for the live-server registry (which sessions run which code)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent_dispatch import servers

# A child that registers, then holds the lock until it is killed.
_LIVE_CHILD = (
    "import os,time,sys;"
    "os.environ['AGENT_DISPATCH_SERVERS_DIR']=sys.argv[1];"
    "from agent_dispatch import servers;"
    "servers.register(sys.argv[2]);"
    "sys.stdout.write('up');sys.stdout.flush();"
    "time.sleep(60)"
)
# A child that registers and exits immediately, leaving a stale entry behind.
_DEAD_CHILD = (
    "import os,sys;"
    "os.environ['AGENT_DISPATCH_SERVERS_DIR']=sys.argv[1];"
    "from agent_dispatch import servers;"
    "servers.register(sys.argv[2])"
)


def _spawn_live(directory: Path, version: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _LIVE_CHILD, str(directory), version],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "AGENT_DISPATCH_SERVERS_DIR": str(directory)},
    )
    assert proc.stdout is not None
    assert proc.stdout.read(2) == "up"  # registered before we look
    return proc


class TestRegistryPaths:
    def test_env_override_wins(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGENT_DISPATCH_SERVERS_DIR", str(tmp_path / "srv"))
        assert servers.servers_dir() == tmp_path / "srv"

    def test_defaults_next_to_the_config(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("AGENT_DISPATCH_SERVERS_DIR", raising=False)
        monkeypatch.setenv("AGENT_DISPATCH_CONFIG", str(tmp_path / "cfg" / "agents.yaml"))
        assert servers.servers_dir() == tmp_path / "cfg" / "servers"

    def test_missing_directory_is_empty_not_an_error(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGENT_DISPATCH_SERVERS_DIR", str(tmp_path / "never-created"))
        assert servers.live_servers() == []
        assert servers.version_drift("0.1.0") == {}


class TestRegister:
    def test_writes_an_owner_only_entry_naming_this_process(self, tmp_path: Path):
        assert servers.register("9.9.9", directory=tmp_path) is True
        path = tmp_path / f"{os.getpid()}.json"
        assert path.stat().st_mode & 0o777 == 0o600
        data = json.loads(path.read_text())
        assert data["pid"] == os.getpid()
        assert data["version"] == "9.9.9"
        assert data["started_at"] <= time.time()

    def test_re_registering_does_not_leak_the_previous_descriptor(self, tmp_path: Path):
        first = tmp_path / "a"
        second = tmp_path / "b"
        assert servers.register("1.0.0", directory=first)
        assert servers.register("2.0.0", directory=second)
        # The first entry's lock was released with its fd, so it now reads stale.
        assert servers.live_servers(directory=first) == []
        assert [e["version"] for e in servers.live_servers(directory=second)] == ["2.0.0"]

    def test_registering_never_raises_on_an_unusable_directory(self, tmp_path: Path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        assert servers.register("9.9.9", directory=blocker) is False


class TestLiveness:
    def test_a_running_process_is_reported_with_its_version(self, tmp_path: Path):
        proc = _spawn_live(tmp_path, "0.13.0")
        try:
            live = servers.live_servers(directory=tmp_path)
            assert [(e["pid"], e["version"]) for e in live] == [(proc.pid, "0.13.0")]
        finally:
            proc.kill()
            proc.wait()

    def test_an_exited_process_is_pruned(self, tmp_path: Path):
        subprocess.run(
            [sys.executable, "-c", _DEAD_CHILD, str(tmp_path), "0.13.0"],
            check=True,
            env={**os.environ, "AGENT_DISPATCH_SERVERS_DIR": str(tmp_path)},
        )
        assert list(tmp_path.glob("*.json"))  # the entry was written
        assert servers.live_servers(directory=tmp_path) == []
        assert not list(tmp_path.glob("*.json"))  # ...and cleaned up on read

    def test_a_killed_process_is_not_reported_as_live(self, tmp_path: Path):
        # SIGKILL gives no chance to clean up — this is exactly why liveness is
        # decided by the advisory lock rather than by the file's existence.
        proc = _spawn_live(tmp_path, "0.13.0")
        proc.kill()
        proc.wait()
        assert servers.live_servers(directory=tmp_path) == []

    def test_prune_false_leaves_stale_files_alone(self, tmp_path: Path):
        subprocess.run(
            [sys.executable, "-c", _DEAD_CHILD, str(tmp_path), "0.13.0"],
            check=True,
            env={**os.environ, "AGENT_DISPATCH_SERVERS_DIR": str(tmp_path)},
        )
        assert servers.live_servers(directory=tmp_path, prune=False) == []
        assert list(tmp_path.glob("*.json"))

    def test_a_corrupt_entry_is_skipped_not_fatal(self, tmp_path: Path):
        (tmp_path / "123456.json").write_text("{not json")
        assert servers.live_servers(directory=tmp_path) == []


class TestVersionDrift:
    def test_counts_only_versions_other_than_the_running_one(self, tmp_path: Path):
        old = _spawn_live(tmp_path, "0.13.0")
        older = _spawn_live(tmp_path, "0.12.1")
        try:
            assert servers.version_drift("0.14.0", directory=tmp_path) == {
                "0.12.1": 1,
                "0.13.0": 1,
            }
            assert servers.version_drift("0.13.0", directory=tmp_path) == {"0.12.1": 1}
        finally:
            for proc in (old, older):
                proc.kill()
                proc.wait()
