"""Tests for config loading and saving."""

from __future__ import annotations

import json
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from agent_dispatch.config import (
    auto_describe,
    collect_mcp_servers,
    config_lock,
    load_config,
    save_config,
)
from agent_dispatch.models import (
    AgentConfig,
    DispatchConfig,
    DispatchGroup,
    GroupMember,
    Settings,
)


def test_load_missing_file(tmp_path: Path):
    config = load_config(tmp_path / "nonexistent.yaml")
    assert config.agents == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_save_config_is_owner_only(tmp_path: Path):
    f = tmp_path / "cfg" / "agents.yaml"
    save_config(DispatchConfig(agents={"a": AgentConfig(directory="/tmp")}), f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700


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


def test_save_config_omits_empty_capabilities(tmp_path: Path):
    """Agents without capabilities must not gain empty-list keys in YAML, but
    declared capabilities must survive the roundtrip."""
    f = tmp_path / "test.yaml"
    config = DispatchConfig(
        agents={
            "plain": AgentConfig(directory="/tmp", description="No caps"),
            "withcaps": AgentConfig(
                directory="/tmp",
                description="Has caps",
                capabilities=["docker_logs"],
                risky_capabilities=["restart_services"],
            ),
        }
    )
    save_config(config, f)

    text = f.read_text()
    plain_block = text.split("withcaps")[0]
    assert "capabilities" not in plain_block
    assert "risky_capabilities" not in plain_block

    loaded = load_config(f)
    assert loaded.agents["plain"].capabilities == []
    assert loaded.agents["plain"].risky_capabilities == []
    assert loaded.agents["withcaps"].capabilities == ["docker_logs"]
    assert loaded.agents["withcaps"].risky_capabilities == ["restart_services"]


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


def test_collect_mcp_servers_ignores_wrong_json_shapes(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text("[]")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text('{"mcpServers": []}')

    assert collect_mcp_servers(tmp_path) == []


def test_auto_describe_tolerates_malformed_metadata_shapes(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe")
    (tmp_path / "README.md").write_bytes(b"\xff\xfe")
    (tmp_path / "pyproject.toml").write_text("description\n")
    (tmp_path / "package.json").write_text("[]")
    (tmp_path / ".mcp.json").write_text('{"mcpServers": []}')

    assert auto_describe(tmp_path) == "Stack: Python, Node.js"


def test_auto_describe_ignores_empty_package_json_description(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"description": ""}')

    desc = auto_describe(tmp_path)

    assert not desc.startswith(" | ")
    assert "Stack: Node.js" == desc


def test_save_and_load_groups_roundtrip(tmp_path: Path):
    f = tmp_path / "test.yaml"
    config = DispatchConfig(
        agents={
            "web": AgentConfig(directory="/tmp", description="Web"),
            "infra": AgentConfig(directory="/tmp", description="Infra"),
        },
        groups={
            "shop": DispatchGroup(
                description="Coordinate the shop",
                shared_context="Prod stack shop. Counter 123.",
                members=[
                    GroupMember(agent="web", use_for="ui"),
                    GroupMember(agent="infra"),
                ],
            )
        },
    )
    save_config(config, f)

    loaded = load_config(f)
    grp = loaded.groups["shop"]
    assert grp.description == "Coordinate the shop"
    assert grp.shared_context == "Prod stack shop. Counter 123."
    assert [(m.agent, m.use_for) for m in grp.members] == [("web", "ui"), ("infra", "")]

    # Declaration order: groups sits between agents and settings in the YAML.
    assert list(yaml.safe_load(f.read_text()).keys()) == ["agents", "groups", "settings"]


def test_save_config_omits_empty_groups(tmp_path: Path):
    """A group-less config must not gain a `groups:` key, and round-trips to {}."""
    f = tmp_path / "test.yaml"
    save_config(DispatchConfig(agents={"x": AgentConfig(directory="/tmp")}), f)
    assert "groups:" not in f.read_text()
    assert load_config(f).groups == {}


def test_save_config_prunes_empty_members(tmp_path: Path):
    """A declared group with no members must not write an empty `members:` list."""
    f = tmp_path / "test.yaml"
    config = DispatchConfig(groups={"solo": DispatchGroup(description="standalone")})
    save_config(config, f)
    assert "members" not in yaml.safe_load(f.read_text())["groups"]["solo"]
    assert load_config(f).groups["solo"].members == []


class TestAtomicSave:
    """agents.yaml is the whole registry — a partial write must never be visible."""

    def test_failed_write_leaves_the_original_config_intact(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "agents.yaml"
        original = DispatchConfig(agents={"keep": AgentConfig(directory=tmp_path)})
        save_config(original, cfg)
        before = cfg.read_text()

        real_write_text = Path.write_text

        def explode(self, *args, **kwargs):
            if self.name.endswith(".tmp"):
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", explode)
        bigger = DispatchConfig(
            agents={
                "keep": AgentConfig(directory=tmp_path),
                "new": AgentConfig(directory=tmp_path),
            }
        )
        with pytest.raises(OSError):
            save_config(bigger, cfg)

        monkeypatch.undo()
        assert cfg.read_text() == before  # not truncated
        assert load_config(cfg).agents.keys() == {"keep"}
        assert not list(tmp_path.glob("*.tmp"))  # temp file cleaned up

    def test_no_temp_files_left_after_a_normal_save(self, tmp_path: Path):
        cfg = tmp_path / "agents.yaml"
        save_config(DispatchConfig(agents={"a": AgentConfig(directory=tmp_path)}), cfg)
        assert cfg.exists()
        assert not list(tmp_path.glob("*.tmp"))


class TestConfigLock:
    def test_lock_is_reentrant(self, tmp_path: Path, monkeypatch):
        # Nesting must not deadlock: mutation sites take the lock and may call
        # helpers that take it again.
        monkeypatch.setenv("AGENT_DISPATCH_CONFIG", str(tmp_path / "agents.yaml"))
        lock = config_lock()
        with lock, lock:
            save_config(DispatchConfig(), tmp_path / "agents.yaml")
        assert (tmp_path / "agents.yaml").exists()

    def test_same_path_returns_the_same_lock(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGENT_DISPATCH_CONFIG", str(tmp_path / "agents.yaml"))
        assert config_lock() is config_lock()

    def test_serializes_concurrent_writers(self, tmp_path: Path, monkeypatch):
        # Two threads each add one agent; with an unlocked read-modify-write one
        # of them is silently lost.
        cfg = tmp_path / "agents.yaml"
        monkeypatch.setenv("AGENT_DISPATCH_CONFIG", str(cfg))
        save_config(DispatchConfig(), cfg)
        barrier = threading.Barrier(2)

        def add(name: str) -> None:
            barrier.wait()
            with config_lock():
                config = load_config(cfg)
                time.sleep(0.01)  # widen the read-modify-write window
                config.agents[name] = AgentConfig(directory=tmp_path)
                save_config(config, cfg)

        threads = [threading.Thread(target=add, args=(n,)) for n in ("one", "two")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert load_config(cfg).agents.keys() == {"one", "two"}


class TestSymlinkedConfig:
    def test_save_writes_through_a_symlink(self, tmp_path: Path):
        """Dotfiles setups symlink agents.yaml; os.replace would eat the link."""
        real = tmp_path / "dotfiles" / "agents.yaml"
        real.parent.mkdir()
        save_config(DispatchConfig(agents={"a": AgentConfig(directory=tmp_path)}), real)

        link = tmp_path / "agents.yaml"
        link.symlink_to(real)

        save_config(DispatchConfig(agents={"b": AgentConfig(directory=tmp_path)}), link)

        assert link.is_symlink(), "the symlink was replaced by a regular file"
        assert load_config(real).agents.keys() == {"b"}, "the real file stopped receiving writes"
        assert not list(tmp_path.glob("*.tmp"))
        assert not list(real.parent.glob("*.tmp"))


class TestLockDoesNotBlockForever:
    def test_acquire_gives_up_and_proceeds(self, tmp_path: Path, monkeypatch):
        """A wedged holder must not freeze the caller (the MCP server takes this
        lock on its event-loop thread)."""
        import subprocess
        import sys

        from agent_dispatch.config import _acquire_lock, _release_lock

        monkeypatch.setattr("agent_dispatch.config._LOCK_TIMEOUT_SECONDS", 0.3)
        lock_path = tmp_path / "held.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import fcntl, time; f = open({str(lock_path)!r}, 'w'); "
                "fcntl.flock(f, fcntl.LOCK_EX); time.sleep(30)",
            ]
        )
        try:
            # Wait for the child to actually hold it.
            deadline = time.time() + 5
            while time.time() < deadline:
                fd = _acquire_lock(lock_path)
                if fd is None:
                    break  # contended: gave up as designed
                _release_lock(fd)
                time.sleep(0.05)
            else:
                pytest.fail("holder never acquired the lock")
        finally:
            holder.kill()
            holder.wait()

    def test_uncontended_acquire_succeeds(self, tmp_path: Path):
        from agent_dispatch.config import _acquire_lock, _release_lock

        fd = _acquire_lock(tmp_path / "free.lock")
        assert fd is not None
        _release_lock(fd)


class TestYamlLoaderChoice:
    """agents.yaml is re-read on every single MCP tool call by design.

    That parse is therefore on the hot path of all 21 tools and runs on the
    event-loop thread. On a real 38 KB config the pure-Python SafeLoader takes
    ~9.8 ms and libyaml's CSafeLoader ~0.6 ms.
    """

    def test_uses_the_c_loader_when_available(self):
        import yaml

        from agent_dispatch import config as config_module

        if hasattr(yaml, "CSafeLoader"):
            assert config_module._YamlLoader is yaml.CSafeLoader
        else:  # pragma: no cover - PyYAML built without libyaml
            assert config_module._YamlLoader is yaml.SafeLoader

    def test_loader_is_a_safe_loader(self):
        """Never a full loader: agents.yaml must not be able to construct objects."""
        import yaml

        from agent_dispatch import config as config_module

        assert config_module._YamlLoader in (
            getattr(yaml, "CSafeLoader", yaml.SafeLoader),
            yaml.SafeLoader,
        )

    def test_rejects_python_object_tags(self, tmp_path):
        """Proof the loader is safe: an arbitrary-object tag must not construct."""
        import yaml

        from agent_dispatch.config import load_config

        p = tmp_path / "agents.yaml"
        p.write_text("agents: !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            load_config(p)

    def test_roundtrips_non_ascii(self, tmp_path):
        from agent_dispatch.config import load_config, save_config
        from agent_dispatch.models import AgentConfig, DispatchConfig

        p = tmp_path / "agents.yaml"
        proj = tmp_path / "proj"
        proj.mkdir()
        save_config(
            DispatchConfig(
                agents={"ru": AgentConfig(directory=proj, description="Диагностика приложения")}
            ),
            p,
        )
        assert load_config(p).agents["ru"].description == "Диагностика приложения"
