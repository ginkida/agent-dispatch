"""Tests for the MCP server tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from unittest.mock import AsyncMock

from agent_dispatch.models import AgentConfig, CacheSettings, DispatchConfig, DispatchResult, Settings
from agent_dispatch import server


@pytest.fixture(autouse=True)
def _reset_globals():
    """Reset server-level cache and semaphore between tests."""
    server._cache = None
    server._semaphore = None
    server._semaphore_limit = 0
    yield
    server._cache = None
    server._semaphore = None
    server._semaphore_limit = 0


def _make_config(tmp_path: Path, cache_enabled: bool = True) -> DispatchConfig:
    for name in ("infra", "db", "monitoring", "backend"):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
    return DispatchConfig(
        agents={
            "infra": AgentConfig(directory=tmp_path / "infra", description="Infra agent"),
            "db": AgentConfig(directory=tmp_path / "db", description="DB agent"),
            "monitoring": AgentConfig(
                directory=tmp_path / "monitoring", description="Monitoring agent"
            ),
            "backend": AgentConfig(
                directory=tmp_path / "backend", description="Backend agent"
            ),
        },
        settings=Settings(cache=CacheSettings(enabled=cache_enabled, ttl=300)),
    )


def _ok_dispatch_result(agent: str, text: str = "ok", session_id: str | None = None) -> DispatchResult:
    return DispatchResult(
        agent=agent, success=True, result=text, cost_usd=0.01, duration_ms=1000, num_turns=1,
        session_id=session_id,
    )


class TestDispatchParallel:
    @pytest.mark.asyncio
    async def test_parallel_runs_all_agents(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return _ok_dispatch_result(name, f"result-from-{name}")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            dispatches = json.dumps([
                {"agent": "infra", "task": "check pods"},
                {"agent": "db", "task": "check migrations"},
                {"agent": "monitoring", "task": "check alerts"},
            ])
            raw = await server.dispatch_parallel(dispatches)
            results = json.loads(raw)

        assert len(results) == 3
        agents = {r["agent"] for r in results}
        assert agents == {"infra", "db", "monitoring"}
        for r in results:
            assert r["success"]
            assert r["result"] == f"result-from-{r['agent']}"

    @pytest.mark.asyncio
    async def test_parallel_invalid_json(self):
        raw = await server.dispatch_parallel("not json")
        assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_parallel_empty_array(self):
        raw = await server.dispatch_parallel("[]")
        assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_parallel_missing_task_key(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.dispatch_parallel(json.dumps([{"agent": "infra"}]))
            result = json.loads(raw)
            assert "error" in result
            assert "'task'" in result["error"]

    @pytest.mark.asyncio
    async def test_parallel_missing_agent_key(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.dispatch_parallel(json.dumps([{"task": "hello"}]))
            result = json.loads(raw)
            assert "error" in result
            assert "'agent'" in result["error"]

    @pytest.mark.asyncio
    async def test_parallel_non_object_item(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.dispatch_parallel(json.dumps(["just a string"]))
            result = json.loads(raw)
            assert "error" in result

    @pytest.mark.asyncio
    async def test_parallel_unknown_agent(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            dispatches = json.dumps([{"agent": "nonexistent", "task": "hello"}])
            raw = await server.dispatch_parallel(dispatches)
            assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_parallel_partial_failure(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            if name == "db":
                return DispatchResult(agent=name, success=False, result="", error="connection refused")
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            dispatches = json.dumps([
                {"agent": "infra", "task": "check"},
                {"agent": "db", "task": "check"},
            ])
            raw = await server.dispatch_parallel(dispatches)
            results = json.loads(raw)

        assert results[0]["success"]
        assert not results[1]["success"]


class TestDispatchCaching:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_dispatch(self, tmp_path: Path):
        config = _make_config(tmp_path)
        call_count = 0

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            nonlocal call_count
            call_count += 1
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            # First call — miss
            await server.dispatch("infra", "check pods")
            assert call_count == 1

            # Second call — should be cached
            raw = await server.dispatch("infra", "check pods")
            assert call_count == 1  # no new dispatch
            result = json.loads(raw)
            assert result.get("cached") is True

    @pytest.mark.asyncio
    async def test_cache_disabled(self, tmp_path: Path):
        config = _make_config(tmp_path, cache_enabled=False)
        call_count = 0

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            nonlocal call_count
            call_count += 1
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch("infra", "check pods")
            await server.dispatch("infra", "check pods")
            assert call_count == 2  # both dispatched

    @pytest.mark.asyncio
    async def test_parallel_uses_cache(self, tmp_path: Path):
        config = _make_config(tmp_path)
        call_count = 0

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            nonlocal call_count
            call_count += 1
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            # Prime cache for infra
            await server.dispatch("infra", "check pods")
            assert call_count == 1

            # Parallel dispatch — infra should be cached, db should dispatch
            dispatches = json.dumps([
                {"agent": "infra", "task": "check pods"},
                {"agent": "db", "task": "check pods"},
            ])
            raw = await server.dispatch_parallel(dispatches)
            results = json.loads(raw)
            assert call_count == 2  # only db dispatched

            infra_result = next(r for r in results if r["agent"] == "infra")
            assert infra_result.get("cached") is True


class TestDispatchSession:
    @pytest.mark.asyncio
    async def test_new_session(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            assert session_id is None
            return _ok_dispatch_result(name, "first turn", session_id="sess-new")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_session("infra", "start work")
            result = json.loads(raw)

        assert result["success"]
        assert result["session_id"] == "sess-new"

    @pytest.mark.asyncio
    async def test_resume_session(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            assert session_id == "sess-existing"
            return _ok_dispatch_result(name, "resumed", session_id="sess-existing")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_session(
                "infra", "follow up", session_id="sess-existing"
            )
            result = json.loads(raw)

        assert result["success"]
        assert result["result"] == "resumed"

    @pytest.mark.asyncio
    async def test_session_passes_caller_goal(self, tmp_path: Path):
        config = _make_config(tmp_path)
        captured = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            captured.update(kw)
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch_session(
                "infra", "check", caller="backend", goal="deploy"
            )

        assert captured["caller"] == "backend"
        assert captured["goal"] == "deploy"

    @pytest.mark.asyncio
    async def test_session_unknown_agent(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.dispatch_session("nonexistent", "hello")
            assert "error" in json.loads(raw)


class TestCallerGoal:
    @pytest.mark.asyncio
    async def test_caller_and_goal_passed_to_runner(self, tmp_path: Path):
        config = _make_config(tmp_path)
        captured_kwargs = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            captured_kwargs.update(kw)
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch("infra", "check", caller="backend", goal="deploy")

        assert captured_kwargs["caller"] == "backend"
        assert captured_kwargs["goal"] == "deploy"

    @pytest.mark.asyncio
    async def test_empty_caller_goal_become_none(self, tmp_path: Path):
        config = _make_config(tmp_path)
        captured_kwargs = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            captured_kwargs.update(kw)
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch("infra", "check")

        assert captured_kwargs.get("caller") is None
        assert captured_kwargs.get("goal") is None


class TestDispatchDialogue:
    @pytest.mark.asyncio
    async def test_resolves_on_first_responder_turn(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            if name == "db":
                return _ok_dispatch_result(
                    name, "All migrations applied. [RESOLVED]", session_id="sess-db"
                )
            return _ok_dispatch_result(name, "n/a", session_id="sess-be")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_dialogue("backend", "db", "are migrations applied?")
            result = json.loads(raw)

        assert result["resolved"] is True
        assert result["rounds"] == 1
        assert len(result["conversation"]) == 1
        assert result["conversation"][0]["agent"] == "db"
        assert "migrations applied" in result["final_answer"]
        assert "[RESOLVED]" not in result["final_answer"]

    @pytest.mark.asyncio
    async def test_multi_round_dialogue(self, tmp_path: Path):
        config = _make_config(tmp_path)
        call_sequence = []

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            call_sequence.append(name)
            if name == "db" and len([c for c in call_sequence if c == "db"]) == 1:
                # First responder turn: ask for clarification
                return _ok_dispatch_result(name, "Which environment?", session_id="sess-db")
            if name == "backend":
                # Requester answers
                return _ok_dispatch_result(name, "Staging cluster", session_id="sess-be")
            # Second responder turn: resolve
            return _ok_dispatch_result(
                name, "Staging has 1 pending migration. Applied. [RESOLVED]",
                session_id="sess-db",
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_dialogue("backend", "db", "check migrations")
            result = json.loads(raw)

        assert result["resolved"] is True
        assert result["rounds"] == 2
        assert len(result["conversation"]) == 3  # resp, req, resp
        assert result["conversation"][0]["agent"] == "db"
        assert result["conversation"][1]["agent"] == "backend"
        assert result["conversation"][2]["agent"] == "db"

    @pytest.mark.asyncio
    async def test_max_rounds_respected(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            # Never resolve
            return _ok_dispatch_result(name, "still working...", session_id=f"sess-{name}")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_dialogue("backend", "db", "topic", max_rounds=2)
            result = json.loads(raw)

        assert result["resolved"] is False
        # 2 rounds * 2 agents = 4 turns
        assert len(result["conversation"]) == 4

    @pytest.mark.asyncio
    async def test_unknown_agent_error(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.dispatch_dialogue("backend", "nonexistent", "topic")
            assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_cost_aggregated(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            return _ok_dispatch_result(name, "done [RESOLVED]", session_id=f"sess-{name}")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_dialogue("backend", "db", "topic")
            result = json.loads(raw)

        assert result["total_cost_usd"] == 0.01  # one dispatch at $0.01
        assert result["total_duration_ms"] == 1000

    @pytest.mark.asyncio
    async def test_requester_can_resolve(self, tmp_path: Path):
        config = _make_config(tmp_path)
        call_count = 0

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            nonlocal call_count
            call_count += 1
            if name == "db":
                return _ok_dispatch_result(name, "Check staging?", session_id="sess-db")
            # Requester resolves
            return _ok_dispatch_result(
                name, "Yes staging is fine, confirmed. [RESOLVED]", session_id="sess-be"
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_dialogue("backend", "db", "check env")
            result = json.loads(raw)

        assert result["resolved"] is True
        assert len(result["conversation"]) == 2  # responder + requester
        assert "confirmed" in result["final_answer"]


    @pytest.mark.asyncio
    async def test_dialogue_error_type_in_conversation(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            if name == "db":
                return DispatchResult(
                    agent=name, success=False, result="",
                    error="permission denied for tool Bash",
                    error_type="permission",
                )
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_dialogue("backend", "db", "check migrations")
            result = json.loads(raw)

        assert result["resolved"] is False
        assert len(result["conversation"]) == 1
        entry = result["conversation"][0]
        assert entry["error_type"] == "permission"
        assert "permission denied" in entry["error"]
        assert "permission denied" in result["final_answer"]


class TestAggregation:
    @pytest.mark.asyncio
    async def test_aggregate_dispatches_to_aggregator(self, tmp_path: Path):
        config = _make_config(tmp_path)
        dispatch_calls = []

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            dispatch_calls.append(name)
            if name == "monitoring":
                # Aggregator call — context contains results from others
                return _ok_dispatch_result(name, "Summary: all systems nominal")
            return _ok_dispatch_result(name, f"report-from-{name}")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            dispatches = json.dumps([
                {"agent": "infra", "task": "check"},
                {"agent": "db", "task": "check"},
            ])
            raw = await server.dispatch_parallel(dispatches, aggregate="monitoring")
            result = json.loads(raw)

        # Should have 3 dispatches: infra, db, then monitoring for aggregation
        assert "monitoring" in dispatch_calls
        assert "individual_results" in result
        assert len(result["individual_results"]) == 2
        assert result["aggregated"]["result"] == "Summary: all systems nominal"

    @pytest.mark.asyncio
    async def test_no_aggregate_returns_flat_list(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            dispatches = json.dumps([{"agent": "infra", "task": "check"}])
            raw = await server.dispatch_parallel(dispatches)
            result = json.loads(raw)

        # Without aggregate, result is a plain list
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_aggregate_unknown_agent(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            dispatches = json.dumps([{"agent": "infra", "task": "check"}])
            raw = await server.dispatch_parallel(dispatches, aggregate="nonexistent")
            assert "error" in json.loads(raw)


class TestListAgentsPermissions:
    @pytest.mark.asyncio
    async def test_list_shows_permission_config(self, tmp_path: Path):
        d = tmp_path / "proj"
        d.mkdir()
        config = DispatchConfig(
            agents={
                "proj": AgentConfig(
                    directory=d,
                    description="test",
                    permission_mode="bypassPermissions",
                    allowed_tools=["Bash", "Read"],
                    disallowed_tools=["Write"],
                ),
            }
        )
        mock_ctx = AsyncMock()
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.list_agents(ctx=mock_ctx)
            agents = json.loads(raw)
        assert agents[0]["permission_mode"] == "bypassPermissions"
        assert agents[0]["allowed_tools"] == ["Bash", "Read"]
        assert agents[0]["disallowed_tools"] == ["Write"]

    @pytest.mark.asyncio
    async def test_list_omits_empty_permissions(self, tmp_path: Path):
        d = tmp_path / "proj"
        d.mkdir()
        config = DispatchConfig(
            agents={"proj": AgentConfig(directory=d, description="test")}
        )
        mock_ctx = AsyncMock()
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.list_agents(ctx=mock_ctx)
            agents = json.loads(raw)
        assert "permission_mode" not in agents[0]
        assert "allowed_tools" not in agents[0]
        assert "disallowed_tools" not in agents[0]


class TestAddRemoveAgent:
    @pytest.mark.asyncio
    async def test_add_agent(self, tmp_path: Path):
        import os
        from agent_dispatch.config import load_config
        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "myproject"
            agent_dir.mkdir()
            (agent_dir / "CLAUDE.md").write_text("# My Project\nA cool API server.")

            raw = await server.add_agent("myproject", str(agent_dir))
            result = json.loads(raw)
            assert result["added"] == "myproject"
            assert "cool API server" in result["description"]

            # Verify it's persisted to disk
            loaded = load_config(config_file)
            assert "myproject" in loaded.agents
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)

    @pytest.mark.asyncio
    async def test_add_agent_custom_description(self, tmp_path: Path):
        import os
        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "proj"
            agent_dir.mkdir()

            raw = await server.add_agent("proj", str(agent_dir), description="My custom desc")
            result = json.loads(raw)
            assert result["description"] == "My custom desc"
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)

    @pytest.mark.asyncio
    async def test_add_agent_with_permissions(self, tmp_path: Path):
        import os
        from agent_dispatch.config import load_config
        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "secured"
            agent_dir.mkdir()

            raw = await server.add_agent(
                "secured", str(agent_dir),
                description="Secured agent",
                permission_mode="bypassPermissions",
                allowed_tools="Bash,Read,Edit",
                disallowed_tools="Write",
            )
            result = json.loads(raw)
            assert result["added"] == "secured"
            assert result["permission_mode"] == "bypassPermissions"
            assert result["allowed_tools"] == ["Bash", "Read", "Edit"]
            assert result["disallowed_tools"] == ["Write"]

            # Verify persisted
            loaded = load_config(config_file)
            agent = loaded.agents["secured"]
            assert agent.permission_mode == "bypassPermissions"
            assert agent.allowed_tools == ["Bash", "Read", "Edit"]
            assert agent.disallowed_tools == ["Write"]
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)

    @pytest.mark.asyncio
    async def test_add_agent_invalid_name(self):
        raw = await server.add_agent("-bad", "/tmp")
        assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_add_agent_nonexistent_dir(self):
        raw = await server.add_agent("test", "/nonexistent/path/xyz")
        assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_add_agent_duplicate(self, tmp_path: Path):
        import os
        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "proj"
            agent_dir.mkdir()
            await server.add_agent("proj", str(agent_dir))
            raw = await server.add_agent("proj", str(agent_dir))
            assert "already exists" in json.loads(raw)["error"]
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)

    @pytest.mark.asyncio
    async def test_remove_agent(self, tmp_path: Path):
        import os
        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "proj"
            agent_dir.mkdir()
            await server.add_agent("proj", str(agent_dir))

            raw = await server.remove_agent("proj")
            assert json.loads(raw)["removed"] == "proj"

            # Verify it's gone
            raw2 = await server.remove_agent("proj")
            assert "not found" in json.loads(raw2)["error"]
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.remove_agent("nonexistent")
            assert "not found" in json.loads(raw)["error"]


class TestUpdateAgent:
    @pytest.mark.asyncio
    async def test_update_permissions(self, tmp_path: Path):
        import os
        from agent_dispatch.config import load_config
        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "proj"
            agent_dir.mkdir()
            await server.add_agent("proj", str(agent_dir), description="test")

            raw = await server.update_agent(
                "proj",
                permission_mode="bypassPermissions",
                allowed_tools="Bash,Read",
            )
            result = json.loads(raw)
            assert result["updated"] == "proj"
            assert "permission_mode" in result["fields"]
            assert "allowed_tools" in result["fields"]

            # Verify persisted
            loaded = load_config(config_file)
            agent = loaded.agents["proj"]
            assert agent.permission_mode == "bypassPermissions"
            assert agent.allowed_tools == ["Bash", "Read"]
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)

    @pytest.mark.asyncio
    async def test_update_clear_fields(self, tmp_path: Path):
        import os
        from agent_dispatch.config import load_config
        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "proj"
            agent_dir.mkdir()
            await server.add_agent(
                "proj", str(agent_dir), description="test",
                permission_mode="bypassPermissions", allowed_tools="Bash",
            )

            raw = await server.update_agent(
                "proj", permission_mode="none", allowed_tools="none",
            )
            result = json.loads(raw)
            assert result["updated"] == "proj"

            loaded = load_config(config_file)
            agent = loaded.agents["proj"]
            assert agent.permission_mode is None
            assert agent.allowed_tools == []
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.update_agent("nonexistent", description="x")
            assert "not found" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_update_nothing(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.update_agent("infra")
            assert "error" in json.loads(raw)
            assert "Nothing to update" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_update_description_and_timeout(self, tmp_path: Path):
        import os
        from agent_dispatch.config import load_config
        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "proj"
            agent_dir.mkdir()
            await server.add_agent("proj", str(agent_dir), description="old")

            raw = await server.update_agent("proj", description="new desc", timeout=600)
            result = json.loads(raw)
            assert "description" in result["fields"]
            assert "timeout" in result["fields"]

            loaded = load_config(config_file)
            assert loaded.agents["proj"].description == "new desc"
            assert loaded.agents["proj"].timeout == 600
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)


class TestConcurrencyLimit:
    @pytest.mark.asyncio
    async def test_semaphore_limits_parallel(self, tmp_path: Path):
        config = _make_config(tmp_path)
        config.settings.max_concurrency = 2

        peak = 0
        current = 0
        lock = asyncio.Lock()

        original_to_thread = asyncio.to_thread

        async def tracked_to_thread(fn, *args, **kwargs):
            nonlocal peak, current
            async with lock:
                current += 1
                if current > peak:
                    peak = current
            try:
                return await original_to_thread(fn, *args, **kwargs)
            finally:
                async with lock:
                    current -= 1

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            import time
            time.sleep(0.05)  # simulate work
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
            patch("agent_dispatch.server.asyncio.to_thread", side_effect=tracked_to_thread),
        ):
            dispatches = json.dumps([
                {"agent": "infra", "task": "check"},
                {"agent": "db", "task": "check"},
                {"agent": "monitoring", "task": "check"},
                {"agent": "backend", "task": "check"},
            ])
            raw = await server.dispatch_parallel(dispatches)
            results = json.loads(raw)

        assert len(results) == 4
        assert all(r["success"] for r in results)
        assert peak <= 2  # semaphore should limit to 2

    @pytest.mark.asyncio
    async def test_default_concurrency_is_5(self, tmp_path: Path):
        config = _make_config(tmp_path)
        assert config.settings.max_concurrency == 5


class TestCacheTools:
    @pytest.mark.asyncio
    async def test_cache_stats(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.cache_stats()
            stats = json.loads(raw)
            assert stats["size"] == 0
            assert stats["ttl"] == 300

    @pytest.mark.asyncio
    async def test_cache_clear(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch("infra", "check")
            raw = await server.cache_clear()
            assert json.loads(raw)["cleared"] == 1

    @pytest.mark.asyncio
    async def test_cache_stats_disabled(self, tmp_path: Path):
        config = _make_config(tmp_path, cache_enabled=False)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.cache_stats()
            result = json.loads(raw)
            assert result["enabled"] is False
