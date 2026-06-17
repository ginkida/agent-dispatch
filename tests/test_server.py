"""Tests for the MCP server tools."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_dispatch import server
from agent_dispatch.models import (
    AgentConfig,
    CacheSettings,
    DispatchConfig,
    DispatchResult,
    Settings,
)


@pytest.fixture(autouse=True)
def _reset_globals(tmp_path: Path, monkeypatch):
    """Reset server-level cache, semaphore and job store between tests."""
    server._cache = None
    server._semaphore = None
    server._semaphore_limit = 0
    server._job_store = None
    server._job_semaphore = None
    server._job_semaphore_limit = 0
    server._running_procs.clear()
    # Isolate job storage per test
    monkeypatch.setenv("AGENT_DISPATCH_JOBS_DIR", str(tmp_path / "_jobs"))
    yield
    server._cache = None
    server._semaphore = None
    server._semaphore_limit = 0
    server._job_store = None
    server._job_semaphore = None
    server._job_semaphore_limit = 0
    server._running_procs.clear()


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
            "backend": AgentConfig(directory=tmp_path / "backend", description="Backend agent"),
        },
        settings=Settings(cache=CacheSettings(enabled=cache_enabled, ttl=300)),
    )


def _ok_dispatch_result(
    agent: str,
    text: str = "ok",
    session_id: str | None = None,
) -> DispatchResult:
    return DispatchResult(
        agent=agent,
        success=True,
        result=text,
        cost_usd=0.01,
        duration_ms=1000,
        num_turns=1,
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
            dispatches = json.dumps(
                [
                    {"agent": "infra", "task": "check pods"},
                    {"agent": "db", "task": "check migrations"},
                    {"agent": "monitoring", "task": "check alerts"},
                ]
            )
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
                return DispatchResult(
                    agent=name,
                    success=False,
                    result="",
                    error="connection refused",
                )
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            dispatches = json.dumps(
                [
                    {"agent": "infra", "task": "check"},
                    {"agent": "db", "task": "check"},
                ]
            )
            raw = await server.dispatch_parallel(dispatches)
            results = json.loads(raw)

        assert results[0]["success"]
        assert not results[1]["success"]

    @pytest.mark.asyncio
    async def test_parallel_exception_has_error_type(self, tmp_path: Path):
        """B4: when a dispatch raises (not returns failure), exception-path preserves error_type."""
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            if name == "db":
                raise RuntimeError("boom")
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            dispatches = json.dumps(
                [
                    {"agent": "infra", "task": "check"},
                    {"agent": "db", "task": "check"},
                ]
            )
            raw = await server.dispatch_parallel(dispatches)
            results = json.loads(raw)

        assert results[0]["success"]
        assert not results[1]["success"]
        assert results[1]["error_type"] == "cli_error"
        assert "boom" in results[1]["error"]


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
    async def test_cache_differentiates_callers(self, tmp_path: Path):
        """Same (agent, task) but different caller should dispatch fresh —
        caller/goal change the prompt, so the cached result is not equivalent."""
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
            await server.dispatch("infra", "check pods", caller="frontend")
            assert call_count == 1
            # Same task, different caller → must miss the cache and dispatch again
            await server.dispatch("infra", "check pods", caller="backend")
            assert call_count == 2
            # Same caller again → hit
            raw = await server.dispatch("infra", "check pods", caller="frontend")
            assert call_count == 2
            assert json.loads(raw).get("cached") is True

    @pytest.mark.asyncio
    async def test_cache_differentiates_goals(self, tmp_path: Path):
        """Different goal → different prompt → cache miss."""
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
            await server.dispatch("infra", "check pods", goal="debug crash")
            await server.dispatch("infra", "check pods", goal="optimize perf")
            assert call_count == 2

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
            dispatches = json.dumps(
                [
                    {"agent": "infra", "task": "check pods"},
                    {"agent": "db", "task": "check pods"},
                ]
            )
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
            raw = await server.dispatch_session("infra", "follow up", session_id="sess-existing")
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
            await server.dispatch_session("infra", "check", caller="backend", goal="deploy")

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
                name,
                "Staging has 1 pending migration. Applied. [RESOLVED]",
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
                    agent=name,
                    success=False,
                    result="",
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
            dispatches = json.dumps(
                [
                    {"agent": "infra", "task": "check"},
                    {"agent": "db", "task": "check"},
                ]
            )
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
        """allowed_tools=None (inherit) should NOT appear in response."""
        d = tmp_path / "proj"
        d.mkdir()
        config = DispatchConfig(agents={"proj": AgentConfig(directory=d, description="test")})
        mock_ctx = AsyncMock()
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.list_agents(ctx=mock_ctx)
            agents = json.loads(raw)
        assert "permission_mode" not in agents[0]
        assert "allowed_tools" not in agents[0]
        assert "disallowed_tools" not in agents[0]

    @pytest.mark.asyncio
    async def test_list_includes_explicit_empty_tools(self, tmp_path: Path):
        """allowed_tools=[] (explicit empty) SHOULD appear as [] to signal override."""
        d = tmp_path / "proj"
        d.mkdir()
        config = DispatchConfig(
            agents={
                "proj": AgentConfig(
                    directory=d,
                    description="test",
                    allowed_tools=[],
                    disallowed_tools=[],
                ),
            }
        )
        mock_ctx = AsyncMock()
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.list_agents(ctx=mock_ctx)
            agents = json.loads(raw)
        assert agents[0]["allowed_tools"] == []
        assert agents[0]["disallowed_tools"] == []


class TestListAgentsHealth:
    """Health-check edge cases — directory missing, unreadable, etc."""

    @pytest.mark.asyncio
    async def test_list_handles_unreadable_directory(self, tmp_path: Path):
        """One agent with PermissionError on is_dir() should NOT crash the
        whole listing — that agent gets healthy='UNREADABLE', others OK."""
        good = tmp_path / "good"
        good.mkdir()
        bad = tmp_path / "bad"
        bad.mkdir()
        config = DispatchConfig(
            agents={
                "good": AgentConfig(directory=good, description="ok"),
                "bad": AgentConfig(directory=bad, description="unreadable"),
            }
        )
        mock_ctx = AsyncMock()

        original_is_dir = Path.is_dir

        def selective_is_dir(self):
            if self == bad:
                raise PermissionError("denied")
            return original_is_dir(self)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch.object(Path, "is_dir", selective_is_dir),
        ):
            raw = await server.list_agents(ctx=mock_ctx)
        agents = json.loads(raw)
        # Both agents present — one bad agent does not poison the list
        assert len(agents) == 2
        by_name = {a["name"]: a for a in agents}
        assert by_name["good"]["healthy"] is True
        assert by_name["bad"]["healthy"] == "UNREADABLE"
        assert by_name["bad"]["has_claude_md"] is False
        assert by_name["bad"]["has_mcp_config"] is False

    @pytest.mark.asyncio
    async def test_list_handles_nonexistent_directory(self, tmp_path: Path):
        """Directory that doesn't exist → healthy=False, child checks=False."""
        # Note: AgentConfig auto-resolves but doesn't require existence
        config = DispatchConfig(
            agents={
                "ghost": AgentConfig(
                    directory=tmp_path / "does-not-exist",
                    description="missing",
                ),
            }
        )
        mock_ctx = AsyncMock()
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.list_agents(ctx=mock_ctx)
        agents = json.loads(raw)
        assert agents[0]["healthy"] is False
        assert agents[0]["has_claude_md"] is False


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
                "secured",
                str(agent_dir),
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
    async def test_add_agent_with_capabilities(self, tmp_path: Path):
        import os

        from agent_dispatch.config import load_config

        config_file = tmp_path / "agents.yaml"
        os.environ["AGENT_DISPATCH_CONFIG"] = str(config_file)
        try:
            agent_dir = tmp_path / "infra"
            agent_dir.mkdir()

            raw = await server.add_agent(
                "infra",
                str(agent_dir),
                description="Infra agent",
                capabilities="docker_logs,deploy_debug",
                risky_capabilities="restart_services",
            )
            result = json.loads(raw)
            assert result["capabilities"] == ["docker_logs", "deploy_debug"]
            assert result["risky_capabilities"] == ["restart_services"]

            loaded = load_config(config_file)
            agent = loaded.agents["infra"]
            assert agent.capabilities == ["docker_logs", "deploy_debug"]
            assert agent.risky_capabilities == ["restart_services"]
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
                "proj",
                str(agent_dir),
                description="test",
                permission_mode="bypassPermissions",
                allowed_tools="Bash",
            )

            raw = await server.update_agent(
                "proj",
                permission_mode="none",
                allowed_tools="none",
            )
            result = json.loads(raw)
            assert result["updated"] == "proj"

            loaded = load_config(config_file)
            agent = loaded.agents["proj"]
            assert agent.permission_mode is None
            # "none" sentinel clears to None (inherit defaults), not []
            assert agent.allowed_tools is None
        finally:
            os.environ.pop("AGENT_DISPATCH_CONFIG", None)

    @pytest.mark.asyncio
    async def test_update_capabilities(self, tmp_path: Path):
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
                capabilities="docker_logs,deploy_debug",
                risky_capabilities="restart_services",
            )
            result = json.loads(raw)
            assert "capabilities" in result["fields"]
            assert "risky_capabilities" in result["fields"]

            loaded = load_config(config_file)
            agent = loaded.agents["proj"]
            assert agent.capabilities == ["docker_logs", "deploy_debug"]
            assert agent.risky_capabilities == ["restart_services"]

            await server.update_agent(
                "proj",
                capabilities="none",
                risky_capabilities="none",
            )
            loaded = load_config(config_file)
            agent = loaded.agents["proj"]
            assert agent.capabilities == []
            assert agent.risky_capabilities == []
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
            dispatches = json.dumps(
                [
                    {"agent": "infra", "task": "check"},
                    {"agent": "db", "task": "check"},
                    {"agent": "monitoring", "task": "check"},
                    {"agent": "backend", "task": "check"},
                ]
            )
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


# ---------------------------------------------------------------------------
# Async dispatch tests
# ---------------------------------------------------------------------------


async def _wait_terminal(job_id: str, timeout: float = 2.0):
    """Poll JobStore until job is terminal or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    store = server._get_job_store()
    while asyncio.get_running_loop().time() < deadline:
        job = store.get(job_id)
        if job and job.is_terminal():
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"Job {job_id} did not reach terminal state in {timeout}s")


class TestDispatchAsync:
    @pytest.mark.asyncio
    async def test_dispatch_async_returns_job_id(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            return _ok_dispatch_result(name, f"result-{name}")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_async("infra", "check pods")
            data = json.loads(raw)
            assert "job_id" in data
            assert data["agent"] == "infra"
            assert data["status"] == "pending"

            job = await _wait_terminal(data["job_id"])
            assert job.status == "done"
            assert job.result is not None
            assert job.result.result == "result-infra"

    @pytest.mark.asyncio
    async def test_dispatch_async_unknown_agent(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.dispatch_async("ghost", "task")
            assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_dispatch_async_worker_crash_marks_failed(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def boom(*a, **kw):
            raise RuntimeError("kaboom")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=boom),
        ):
            raw = await server.dispatch_async("infra", "task")
            job_id = json.loads(raw)["job_id"]
            job = await _wait_terminal(job_id)
            assert job.status == "failed"
            assert "kaboom" in (job.error or "")

    @pytest.mark.asyncio
    async def test_dispatch_async_failed_dispatch_marks_failed(self, tmp_path: Path):
        """A DispatchResult with success=False should land as status=failed."""
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            return DispatchResult(
                agent=name,
                success=False,
                result="",
                error="permission denied",
                error_type="permission",
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_async("infra", "task")
            job_id = json.loads(raw)["job_id"]
            job = await _wait_terminal(job_id)
            assert job.status == "failed"
            assert job.result is not None
            assert job.result.error_type == "permission"


class TestDispatchStatusWait:
    @pytest.mark.asyncio
    async def test_dispatch_status_returns_job(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_async("infra", "task")
            job_id = json.loads(raw)["job_id"]
            await _wait_terminal(job_id)
            status_raw = await server.dispatch_status(job_id)
            data = json.loads(status_raw)
            assert data["id"] == job_id
            assert data["status"] == "done"

    @pytest.mark.asyncio
    async def test_dispatch_status_unknown(self, tmp_path: Path):
        # Valid-format id that simply doesn't exist -> the "not found" branch.
        raw = await server.dispatch_status("a" * 32)
        assert "not found" in json.loads(raw)["error"].lower()

    @pytest.mark.asyncio
    async def test_dispatch_status_malformed_id_rejected(self, tmp_path: Path):
        # Malformed id -> the "invalid format" guard (path-traversal defense).
        raw = await server.dispatch_status("does-not-exist")
        assert "Invalid ref" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_dispatch_wait_returns_when_done(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def slow_dispatch(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            import time

            time.sleep(0.05)
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=slow_dispatch),
        ):
            raw = await server.dispatch_async("infra", "task")
            job_id = json.loads(raw)["job_id"]
            wait_raw = await server.dispatch_wait(job_id, timeout_seconds=2)
            data = json.loads(wait_raw)
            assert data["status"] == "done"
            assert "timed_out_waiting" not in data

    @pytest.mark.asyncio
    async def test_dispatch_wait_times_out(self, tmp_path: Path):
        config = _make_config(tmp_path)
        # Manually create a stuck pending job (no worker)
        store = server._get_job_store()
        job = store.create("infra", "stuck")
        _ = config  # not used but keeps fixture pattern
        wait_raw = await server.dispatch_wait(job.id, timeout_seconds=1)
        data = json.loads(wait_raw)
        assert data["timed_out_waiting"] is True
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_dispatch_wait_unknown_job(self):
        raw = await server.dispatch_wait("nonexistent", timeout_seconds=1)
        assert "error" in json.loads(raw)


class TestDispatchJobsList:
    @pytest.mark.asyncio
    async def test_dispatch_jobs_empty(self):
        raw = await server.dispatch_jobs()
        assert json.loads(raw) == []

    @pytest.mark.asyncio
    async def test_dispatch_jobs_lists_and_filters(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_dispatch),
        ):
            r1 = json.loads(await server.dispatch_async("infra", "a"))
            r2 = json.loads(await server.dispatch_async("db", "b"))
            await _wait_terminal(r1["job_id"])
            await _wait_terminal(r2["job_id"])

            all_raw = await server.dispatch_jobs()
            all_jobs = json.loads(all_raw)
            assert len(all_jobs) == 2
            assert all(j["status"] == "done" for j in all_jobs)
            assert all("success" in j for j in all_jobs)

            done_raw = await server.dispatch_jobs(status="done")
            assert len(json.loads(done_raw)) == 2

            pending_raw = await server.dispatch_jobs(status="pending")
            assert json.loads(pending_raw) == []

    @pytest.mark.asyncio
    async def test_dispatch_jobs_invalid_status(self):
        raw = await server.dispatch_jobs(status="bogus")
        assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_dispatch_jobs_limit(self, tmp_path: Path):
        store = server._get_job_store()
        for i in range(5):
            store.create("infra", f"task-{i}")
        raw = await server.dispatch_jobs(limit=2)
        assert len(json.loads(raw)) == 2


class TestDispatchGC:
    @pytest.mark.asyncio
    async def test_dispatch_gc_purges_old(self, tmp_path: Path):
        import time as _time

        store = server._get_job_store()
        old = store.create("infra", "old")
        store.finish(old.id, DispatchResult(agent="infra", success=True, result=""))
        updated = store.get(old.id)
        assert updated is not None
        updated.completed_at = _time.time() - 86400 * 30
        store._write(updated)

        recent = store.create("db", "recent")
        store.finish(recent.id, DispatchResult(agent="db", success=True, result=""))

        raw = await server.dispatch_gc(max_age_days=7)
        data = json.loads(raw)
        assert data["purged"] == 1

    @pytest.mark.asyncio
    async def test_dispatch_gc_rejects_zero(self):
        raw = await server.dispatch_gc(max_age_days=0)
        assert "error" in json.loads(raw)


# ---------------------------------------------------------------------------
# Enriched list_agents + inspect_agent
# ---------------------------------------------------------------------------


class TestListAgentsEnriched:
    @pytest.mark.asyncio
    async def test_list_surfaces_mcp_stacks_dbs(self, tmp_path: Path):
        agent_dir = tmp_path / "infra"
        agent_dir.mkdir()
        (agent_dir / ".mcp.json").write_text('{"mcpServers": {"portainer": {}, "postgres": {}}}')
        (agent_dir / "pyproject.toml").write_text('description = "x"\n')
        (agent_dir / "Dockerfile").write_text("FROM python:3.11\n")
        (agent_dir / "alembic.ini").write_text("[alembic]\n")
        config = DispatchConfig(agents={"infra": AgentConfig(directory=agent_dir, description="d")})
        mock_ctx = AsyncMock()
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.list_agents(ctx=mock_ctx)
        entry = json.loads(raw)[0]
        assert "portainer" in entry["mcp_servers"]
        assert "postgres" in entry["mcp_servers"]
        assert "Python" in entry["stacks"]
        assert "Docker" in entry["stacks"]
        assert "Alembic" in entry["dbs"]

    @pytest.mark.asyncio
    async def test_list_omits_empty_capability_fields(self, tmp_path: Path):
        """Plain directory with no MCP/stack/DB markers should omit those keys."""
        agent_dir = tmp_path / "plain"
        agent_dir.mkdir()
        config = DispatchConfig(agents={"plain": AgentConfig(directory=agent_dir, description="d")})
        mock_ctx = AsyncMock()
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.list_agents(ctx=mock_ctx)
        entry = json.loads(raw)[0]
        assert "mcp_servers" not in entry
        assert "stacks" not in entry
        assert "dbs" not in entry
        assert "capabilities" not in entry
        assert "risky_capabilities" not in entry

    @pytest.mark.asyncio
    async def test_list_surfaces_declared_capabilities(self, tmp_path: Path):
        agent_dir = tmp_path / "infra"
        agent_dir.mkdir()
        config = DispatchConfig(
            agents={
                "infra": AgentConfig(
                    directory=agent_dir,
                    description="d",
                    capabilities=["docker_logs"],
                    risky_capabilities=["restart_services"],
                )
            }
        )
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.list_agents()
        entry = json.loads(raw)[0]
        assert entry["capabilities"] == ["docker_logs"]
        assert entry["risky_capabilities"] == ["restart_services"]


class TestInspectAgent:
    @pytest.mark.asyncio
    async def test_inspect_returns_full_info(self, tmp_path: Path):
        agent_dir = tmp_path / "infra"
        agent_dir.mkdir()
        (agent_dir / "CLAUDE.md").write_text("# Infra\nManages production infrastructure.\n")
        (agent_dir / "README.md").write_text("Infra README\n" * 50)
        (agent_dir / ".mcp.json").write_text('{"mcpServers": {"portainer": {}}}')
        (agent_dir / "pyproject.toml").write_text('description = "x"\n')

        config = DispatchConfig(
            agents={
                "infra": AgentConfig(
                    directory=agent_dir,
                    description="d",
                    timeout=120,
                    permission_mode="bypassPermissions",
                    allowed_tools=["Bash", "Read"],
                    capabilities=["docker_logs"],
                    risky_capabilities=["restart_services"],
                ),
            }
        )
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.inspect_agent("infra")
        info = json.loads(raw)
        assert info["name"] == "infra"
        assert info["healthy"] is True
        assert info["timeout"] == 120
        assert info["permission_mode"] == "bypassPermissions"
        assert info["allowed_tools"] == ["Bash", "Read"]
        assert info["capabilities"] == ["docker_logs"]
        assert info["risky_capabilities"] == ["restart_services"]
        assert info["mcp_servers"] == ["portainer"]
        assert "Python" in info["stacks"]
        assert info["has_claude_md"] is True
        assert info["has_readme"] is True
        assert "Manages production infrastructure" in info["claude_md_preview"]
        # README has 50 identical lines; preview must be truncated to <=40
        assert info.get("readme_truncated") is True

    @pytest.mark.asyncio
    async def test_inspect_preview_zero_omits_text(self, tmp_path: Path):
        agent_dir = tmp_path / "p"
        agent_dir.mkdir()
        (agent_dir / "CLAUDE.md").write_text("content here\n")
        config = DispatchConfig(agents={"p": AgentConfig(directory=agent_dir, description="d")})
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.inspect_agent("p", preview_lines=0)
        info = json.loads(raw)
        assert info["has_claude_md"] is True
        assert "claude_md_preview" not in info

    @pytest.mark.asyncio
    async def test_inspect_unknown_agent(self, tmp_path: Path):
        config = DispatchConfig(agents={})
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.inspect_agent("nope")
        assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_inspect_directory_missing(self, tmp_path: Path):
        # AgentConfig validates Path but doesn't require existence
        ghost = tmp_path / "ghost"
        config = DispatchConfig(agents={"ghost": AgentConfig(directory=ghost, description="d")})
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.inspect_agent("ghost")
        info = json.loads(raw)
        assert info["healthy"] is False
        # No previews/scan results when unhealthy
        assert "claude_md_preview" not in info
        assert "mcp_servers" not in info

    @pytest.mark.asyncio
    async def test_inspect_directory_unreadable(self, tmp_path: Path):
        agent_dir = tmp_path / "bad"
        agent_dir.mkdir()
        config = DispatchConfig(agents={"bad": AgentConfig(directory=agent_dir, description="d")})

        original_is_dir = Path.is_dir

        def selective_is_dir(self):
            if self == agent_dir:
                raise PermissionError("denied")
            return original_is_dir(self)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch.object(Path, "is_dir", selective_is_dir),
        ):
            raw = await server.inspect_agent("bad")
        info = json.loads(raw)
        assert info["healthy"] == "UNREADABLE"


# ---------------------------------------------------------------------------
# Structured response (response_format="json")
# ---------------------------------------------------------------------------


class TestStructuredResponseMCP:
    @pytest.mark.asyncio
    async def test_dispatch_returns_parsed_result(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            # The runner is mocked, so it doesn't actually parse. Simulate
            # what would happen if claude returned JSON and parsing succeeded.
            assert kw.get("response_format") == "json"
            return DispatchResult(
                agent=name,
                success=True,
                result='{"k": "v"}',
                parsed_result={"k": "v"},
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch(
                "infra",
                "task",
                response_format="json",
            )
        data = json.loads(raw)
        assert data["success"]
        assert data["parsed_result"] == {"k": "v"}

    @pytest.mark.asyncio
    async def test_dispatch_passes_response_format_to_runner(self, tmp_path: Path):
        """response_format propagation through the server layer."""
        config = _make_config(tmp_path)
        seen: dict = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            seen["response_format"] = kw.get("response_format")
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch("infra", "task", response_format="json")
            assert seen["response_format"] == "json"
            await server.dispatch("db", "task")  # default = ""
            assert seen["response_format"] is None

    @pytest.mark.asyncio
    async def test_cache_differentiates_response_format(self, tmp_path: Path):
        """Two requests differing only by response_format must NOT collide."""
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
            await server.dispatch("infra", "check", response_format="")
            await server.dispatch("infra", "check", response_format="json")
            assert call_count == 2  # both went through to runner
            # Re-issue the json one — hits cache
            raw = await server.dispatch("infra", "check", response_format="json")
            assert call_count == 2
            assert json.loads(raw).get("cached") is True

    @pytest.mark.asyncio
    async def test_dispatch_async_propagates_response_format(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: list = []

        def fake_dispatch(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            seen.append(kw.get("response_format"))
            return DispatchResult(
                agent=name,
                success=True,
                result='{"a": 1}',
                parsed_result={"a": 1},
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_async("infra", "task", response_format="json")
            job_id = json.loads(raw)["job_id"]
            job = await _wait_terminal(job_id)
        assert seen == ["json"]
        assert job.result is not None
        assert job.result.parsed_result == {"a": 1}

    @pytest.mark.asyncio
    async def test_dispatch_parallel_per_item_response_format(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: list = []

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            seen.append((name, kw.get("response_format")))
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch_parallel(
                json.dumps(
                    [
                        {"agent": "infra", "task": "t1"},
                        {"agent": "db", "task": "t2", "response_format": "json"},
                    ]
                )
            )
        assert ("infra", None) in seen
        assert ("db", "json") in seen


# ---------------------------------------------------------------------------
# Result references (return_ref + fetch_result)
# ---------------------------------------------------------------------------


class TestReturnRef:
    @pytest.mark.asyncio
    async def test_dispatch_return_ref_compact_response(self, tmp_path: Path):
        config = _make_config(tmp_path)
        big_text = "X" * 10000

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return DispatchResult(
                agent=name,
                success=True,
                result=big_text,
                cost_usd=0.05,
                session_id="sid",
                duration_ms=2000,
                num_turns=3,
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch(
                "infra",
                "audit",
                return_ref=True,
                summary_chars=200,
            )
        data = json.loads(raw)
        assert "ref" in data
        assert data["agent"] == "infra"
        assert data["success"] is True
        assert data["size"] == 10000
        assert data["summary_chars"] == 200
        assert len(data["summary"]) == 200
        assert data["session_id"] == "sid"
        assert data["cost_usd"] == 0.05
        assert "result" not in data  # full result must NOT be inlined

    @pytest.mark.asyncio
    async def test_dispatch_ref_includes_parsed_result(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return DispatchResult(
                agent=name,
                success=True,
                result='{"a": 1}',
                parsed_result={"a": 1},
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch(
                "infra",
                "task",
                response_format="json",
                return_ref=True,
            )
        data = json.loads(raw)
        assert data["parsed_result"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_fetch_result_returns_full(self, tmp_path: Path):
        config = _make_config(tmp_path)
        big_text = "Y" * 5000

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return DispatchResult(
                agent=name,
                success=True,
                result=big_text,
                cost_usd=0.01,
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            ref_resp = json.loads(await server.dispatch("infra", "task", return_ref=True))
            full_resp = json.loads(await server.fetch_result(ref_resp["ref"]))
        assert full_resp["result"] == big_text
        assert full_resp["cost_usd"] == 0.01
        assert "truncated" not in full_resp

    @pytest.mark.asyncio
    async def test_fetch_result_truncates_on_request(self, tmp_path: Path):
        config = _make_config(tmp_path)
        big_text = "Z" * 8000

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return DispatchResult(agent=name, success=True, result=big_text)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            ref_resp = json.loads(await server.dispatch("infra", "task", return_ref=True))
            raw = await server.fetch_result(ref_resp["ref"], max_chars=100)
        data = json.loads(raw)
        assert data["truncated"] is True
        assert data["full_size"] == 8000
        assert len(data["result"]) == 100

    @pytest.mark.asyncio
    async def test_fetch_result_unknown_ref(self):
        raw = await server.fetch_result("nonexistent-ref")
        assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_fetch_result_works_for_async_jobs(self, tmp_path: Path):
        """fetch_result reuses JobStore — async job_ids are valid refs too."""
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            return _ok_dispatch_result(name, "async-result")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_dispatch),
        ):
            async_resp = json.loads(await server.dispatch_async("infra", "task"))
            await _wait_terminal(async_resp["job_id"])
            raw = await server.fetch_result(async_resp["job_id"])
        assert json.loads(raw)["result"] == "async-result"

    @pytest.mark.asyncio
    async def test_parallel_per_item_return_ref(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return _ok_dispatch_result(name, f"big-{name}")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_parallel(
                json.dumps(
                    [
                        {"agent": "infra", "task": "t1"},  # full result inline
                        {"agent": "db", "task": "t2", "return_ref": True, "summary_chars": 3},
                    ]
                )
            )
        results = json.loads(raw)
        # First item: full DispatchResult shape
        assert results[0]["result"] == "big-infra"
        assert "ref" not in results[0]
        # Second item: ref shape, summary capped at 3 chars
        assert "ref" in results[1]
        assert results[1]["summary"] == "big"
        assert results[1]["size"] == len("big-db")


class TestHardening:
    """Bounds-checking, ref validation, and the dispatch_cancel tool."""

    @pytest.mark.asyncio
    async def test_cancel_pending_job(self, tmp_path: Path):
        store = server._get_job_store()
        job = store.create("infra", "task")  # pending, no worker started
        raw = await server.dispatch_cancel(job.id)
        data = json.loads(raw)
        assert data["outcome"] == "cancelled"
        assert data["status"] == "cancelled"
        assert store.get(job.id).status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_running_job_refused(self, tmp_path: Path):
        store = server._get_job_store()
        job = store.create("infra", "task")
        store.mark_running(job.id)
        raw = await server.dispatch_cancel(job.id)
        data = json.loads(raw)
        assert data["outcome"] == "running"
        assert "message" in data

    @pytest.mark.asyncio
    async def test_cancel_invalid_ref(self, tmp_path: Path):
        raw = await server.dispatch_cancel("../../etc/passwd")
        assert "Invalid ref" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_cancel_unknown_job(self, tmp_path: Path):
        raw = await server.dispatch_cancel("f" * 32)
        assert "not found" in json.loads(raw)["error"].lower()

    @pytest.mark.asyncio
    async def test_fetch_result_rejects_traversal_ref(self, tmp_path: Path):
        raw = await server.fetch_result("../secret")
        assert "Invalid ref" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_dispatch_status_rejects_traversal_ref(self, tmp_path: Path):
        raw = await server.dispatch_status("../../config")
        assert "Invalid ref" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_dispatch_wait_rejects_traversal_ref(self, tmp_path: Path):
        raw = await server.dispatch_wait("..%2f..%2fetc", timeout_seconds=1)
        assert "Invalid ref" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_dispatch_jobs_limit_clamped_negative(self, tmp_path: Path):
        store = server._get_job_store()
        for _ in range(3):
            store.create("infra", "t")
        raw = await server.dispatch_jobs(limit=-10)
        summaries = json.loads(raw)
        assert len(summaries) == 1  # max(1, min(-10, 1000)) == 1

    @pytest.mark.asyncio
    async def test_dispatch_gc_rejects_zero(self, tmp_path: Path):
        raw = await server.dispatch_gc(max_age_days=0)
        assert "error" in json.loads(raw)

    @pytest.mark.asyncio
    async def test_dispatch_gc_rejects_nonfinite(self, tmp_path: Path):
        raw = await server.dispatch_gc(max_age_days=1e308)
        assert "non-finite" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_dispatch_parallel_caps_item_count(self, tmp_path: Path):
        config = _make_config(tmp_path)  # max_concurrency default 5 -> cap 100
        items = json.dumps([{"agent": "infra", "task": f"t{i}"} for i in range(101)])
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.dispatch_parallel(items)
        assert "Too many dispatches" in json.loads(raw)["error"]

    @pytest.mark.asyncio
    async def test_dispatch_return_ref_clamps_negative_summary(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return _ok_dispatch_result(name, "hello world")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch("infra", "t", return_ref=True, summary_chars=-99)
        data = json.loads(raw)
        assert "ref" in data
        assert data["summary"] == ""  # negative clamped to 0

    @pytest.mark.asyncio
    async def test_cache_stats_reports_max_size(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with patch.object(server, "_get_config", return_value=config):
            raw = await server.cache_stats()
        assert json.loads(raw)["max_size"] == 1000


# ---------------------------------------------------------------------------
# Per-call timeout override (timeout_seconds)
# ---------------------------------------------------------------------------


class TestTimeoutOverride:
    def test_apply_timeout_zero_returns_same_config(self, tmp_path: Path):
        config = _make_config(tmp_path)
        agent = config.agents["infra"]
        assert server._apply_timeout(agent, 0) is agent
        assert server._apply_timeout(agent, -5) is agent

    def test_apply_timeout_clamps_bounds(self, tmp_path: Path):
        config = _make_config(tmp_path)
        agent = config.agents["infra"]
        assert server._apply_timeout(agent, 3).timeout == 10
        assert server._apply_timeout(agent, 900).timeout == 900
        assert server._apply_timeout(agent, 999_999).timeout == 7200

    def test_apply_timeout_does_not_mutate_original(self, tmp_path: Path):
        config = _make_config(tmp_path)
        agent = config.agents["infra"]
        server._apply_timeout(agent, 900)
        assert agent.timeout == 300

    @pytest.mark.asyncio
    async def test_dispatch_timeout_seconds_reaches_runner(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: dict = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            seen["timeout"] = agent_config.timeout
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch("infra", "long task", timeout_seconds=900)
        assert seen["timeout"] == 900

    @pytest.mark.asyncio
    async def test_dispatch_default_keeps_agent_timeout(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: dict = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            seen["timeout"] = agent_config.timeout
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch("infra", "task")
        assert seen["timeout"] == 300

    @pytest.mark.asyncio
    async def test_session_timeout_seconds(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: dict = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, session_id=None, **kw):
            seen["timeout"] = agent_config.timeout
            return _ok_dispatch_result(name, session_id="s1")

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch_session("infra", "task", timeout_seconds=1200)
        assert seen["timeout"] == 1200

    @pytest.mark.asyncio
    async def test_parallel_per_item_timeout_seconds(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: dict = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            seen[name] = agent_config.timeout
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch_parallel(
                json.dumps(
                    [
                        {"agent": "infra", "task": "t1", "timeout_seconds": 600},
                        {"agent": "db", "task": "t2"},
                    ]
                )
            )
        assert seen["infra"] == 600
        assert seen["db"] == 300

    @pytest.mark.asyncio
    async def test_async_timeout_seconds(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: dict = {}

        def fake_stream(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            seen["timeout"] = agent_config.timeout
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_stream),
        ):
            raw = await server.dispatch_async("infra", "task", timeout_seconds=1800)
            await _wait_terminal(json.loads(raw)["job_id"])
        assert seen["timeout"] == 1800


# ---------------------------------------------------------------------------
# Denied-tools surfacing (permission_denials -> denied_tools + hint)
# ---------------------------------------------------------------------------


class TestDeniedToolsSurfacing:
    @pytest.mark.asyncio
    async def test_dispatch_response_includes_denied_and_hint(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return DispatchResult(
                agent=name,
                success=True,
                result="partial answer",
                denied_tools=["Bash"],
                hint="grant Bash to get a complete answer",
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch("infra", "task")
        data = json.loads(raw)
        assert data["success"] is True
        assert data["denied_tools"] == ["Bash"]
        assert "grant Bash" in data["hint"]

    @pytest.mark.asyncio
    async def test_ref_payload_includes_denied_and_hint(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            return DispatchResult(
                agent=name,
                success=True,
                result="X" * 5000,
                denied_tools=["WebFetch"],
                hint="grant WebFetch",
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch("infra", "task", return_ref=True)
        data = json.loads(raw)
        assert "ref" in data
        assert data["denied_tools"] == ["WebFetch"]
        assert data["hint"] == "grant WebFetch"


# ---------------------------------------------------------------------------
# Async job progress (rolling tail in job file)
# ---------------------------------------------------------------------------


class TestAsyncJobProgress:
    @pytest.mark.asyncio
    async def test_progress_persisted_after_completion(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_stream(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            for i in range(3):
                on_progress(f"line {i}")
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_stream),
        ):
            raw = await server.dispatch_async("infra", "task")
            job = await _wait_terminal(json.loads(raw)["job_id"])
        assert job.status == "done"
        assert job.progress == ["line 0", "line 1", "line 2"]

    @pytest.mark.asyncio
    async def test_progress_capped_to_rolling_tail(self, tmp_path: Path):
        config = _make_config(tmp_path)
        total = server._JOB_PROGRESS_MAX_LINES + 5

        def fake_stream(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            for i in range(total):
                on_progress(f"line {i}")
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_stream),
        ):
            raw = await server.dispatch_async("infra", "task")
            job = await _wait_terminal(json.loads(raw)["job_id"])
        assert len(job.progress) == server._JOB_PROGRESS_MAX_LINES
        assert job.progress[-1] == f"line {total - 1}"
        assert job.progress[0] == f"line {total - server._JOB_PROGRESS_MAX_LINES}"

    @pytest.mark.asyncio
    async def test_status_shows_progress_while_running(self, tmp_path: Path):
        config = _make_config(tmp_path)
        release = threading.Event()

        def fake_stream(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            on_progress("Using tool: Bash")
            release.wait(timeout=5)
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_stream),
        ):
            raw = await server.dispatch_async("infra", "task")
            job_id = json.loads(raw)["job_id"]
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 2.0
                progress = None
                while loop.time() < deadline:
                    data = json.loads(await server.dispatch_status(job_id))
                    if data.get("progress"):
                        progress = data["progress"]
                        break
                    await asyncio.sleep(0.01)
            finally:
                release.set()
            await _wait_terminal(job_id)
        assert progress == ["Using tool: Bash"]

    @pytest.mark.asyncio
    async def test_jobs_list_shows_last_progress_for_running(self, tmp_path: Path):
        store = server._get_job_store()
        job = store.create("infra", "task")
        store.mark_running(job.id)
        store.update_progress(job.id, ["step 1", "step 2"])
        raw = await server.dispatch_jobs()
        entries = json.loads(raw)
        assert entries[0]["last_progress"] == "step 2"

    @pytest.mark.asyncio
    async def test_jobs_list_omits_last_progress_for_done(self, tmp_path: Path):
        store = server._get_job_store()
        job = store.create("infra", "task")
        store.mark_running(job.id)
        store.update_progress(job.id, ["step 1"])
        store.finish(job.id, DispatchResult(agent="infra", success=True, result="ok"))
        raw = await server.dispatch_jobs()
        entries = json.loads(raw)
        assert "last_progress" not in entries[0]

    @pytest.mark.asyncio
    async def test_progress_lines_truncated_to_300_chars(self, tmp_path: Path):
        config = _make_config(tmp_path)

        def fake_stream(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            on_progress("X" * 1000)
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_stream),
        ):
            raw = await server.dispatch_async("infra", "task")
            job = await _wait_terminal(json.loads(raw)["job_id"])
        assert len(job.progress[0]) == 300


class TestParallelNumericValidation:
    @pytest.mark.asyncio
    async def test_bad_timeout_seconds_rejects_whole_call(self, tmp_path: Path):
        """Validated up front — before any dispatch runs (per contract)."""
        config = _make_config(tmp_path)
        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch") as mock_dispatch,
        ):
            raw = await server.dispatch_parallel(
                json.dumps(
                    [
                        {"agent": "infra", "task": "t1"},
                        {"agent": "db", "task": "t2", "timeout_seconds": "abc"},
                    ]
                )
            )
        data = json.loads(raw)
        assert "timeout_seconds" in data["error"]
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_bad_summary_chars_rejects_whole_call(self, tmp_path: Path):
        config = _make_config(tmp_path)
        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch") as mock_dispatch,
        ):
            raw = await server.dispatch_parallel(
                json.dumps(
                    [
                        {"agent": "infra", "task": "t", "return_ref": True, "summary_chars": []},
                    ]
                )
            )
        assert "summary_chars" in json.loads(raw)["error"]
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_timeout_seconds_treated_as_unset(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: dict = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            seen["timeout"] = agent_config.timeout
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            raw = await server.dispatch_parallel(
                '[{"agent": "infra", "task": "t", "timeout_seconds": null}]'
            )
        assert json.loads(raw)[0]["success"] is True
        assert seen["timeout"] == 300  # agent default

    @pytest.mark.asyncio
    async def test_string_numeric_timeout_accepted(self, tmp_path: Path):
        config = _make_config(tmp_path)
        seen: dict = {}

        def fake_dispatch(name, task, agent_config, settings, context=None, **kw):
            seen["timeout"] = agent_config.timeout
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch", side_effect=fake_dispatch),
        ):
            await server.dispatch_parallel(
                '[{"agent": "infra", "task": "t", "timeout_seconds": "900"}]'
            )
        assert seen["timeout"] == 900


class TestStatusPostMortemProgress:
    @pytest.mark.asyncio
    async def test_dispatch_status_keeps_progress_after_done(self, tmp_path: Path):
        """The documented post-mortem trace, asserted at the MCP tool level."""
        config = _make_config(tmp_path)

        def fake_stream(name, task, agent_config, settings, context=None, on_progress=None, **kw):
            on_progress("Using tool: Grep")
            on_progress("Synthesizing answer")
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_stream),
        ):
            raw = await server.dispatch_async("infra", "task")
            job_id = json.loads(raw)["job_id"]
            await _wait_terminal(job_id)
            status = json.loads(await server.dispatch_status(job_id))
        assert status["status"] == "done"
        assert status["progress"] == ["Using tool: Grep", "Synthesizing answer"]


class TestCancelRunningJob:
    """dispatch_cancel kills a running job's subprocess when this server owns it."""

    @pytest.mark.asyncio
    async def test_cancel_kills_running_subprocess(self, tmp_path: Path):
        config = _make_config(tmp_path)
        started = threading.Event()
        release = threading.Event()
        killed = threading.Event()

        class FakeProc:
            def kill(self):
                killed.set()
                release.set()  # the stream "dies" once killed

        def fake_stream(
            name,
            task,
            agent_config,
            settings,
            context=None,
            on_progress=None,
            *,
            on_proc=None,
            **kw,
        ):
            if on_proc is not None:
                on_proc(FakeProc())
            started.set()
            release.wait(timeout=2)
            # what dispatch_stream returns after its subprocess was killed
            return DispatchResult(
                agent=name,
                success=False,
                result="",
                error="No result received",
                error_type="cli_error",
            )

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_stream),
        ):
            raw = await server.dispatch_async("infra", "long task")
            job_id = json.loads(raw)["job_id"]
            assert started.wait(timeout=2), "worker never started"

            cancel_raw = await server.dispatch_cancel(job_id)
            data = json.loads(cancel_raw)
            assert data["outcome"] == "cancelled_running"
            assert data["status"] == "cancelled"
            assert "killed" in data["message"]
            assert killed.is_set()

            # Wait for the worker thread to fully unwind (registry cleanup),
            # then confirm its trailing finish() did NOT overwrite the cancel.
            for _ in range(200):
                if job_id not in server._running_procs:
                    break
                await asyncio.sleep(0.01)
            assert job_id not in server._running_procs
            job = server._get_job_store().get(job_id)
            assert job.status == "cancelled"
            assert job.result is None

    @pytest.mark.asyncio
    async def test_registry_cleared_after_normal_completion(self, tmp_path: Path):
        config = _make_config(tmp_path)

        class FakeProc:
            def kill(self):
                pass

        def fake_stream(
            name,
            task,
            agent_config,
            settings,
            context=None,
            on_progress=None,
            *,
            on_proc=None,
            **kw,
        ):
            if on_proc is not None:
                on_proc(FakeProc())
            return _ok_dispatch_result(name)

        with (
            patch.object(server, "_get_config", return_value=config),
            patch("agent_dispatch.server.runner.dispatch_stream", side_effect=fake_stream),
        ):
            raw = await server.dispatch_async("infra", "task")
            job_id = json.loads(raw)["job_id"]
            job = await _wait_terminal(job_id)
            assert job.status == "done"
            for _ in range(200):
                if job_id not in server._running_procs:
                    break
                await asyncio.sleep(0.01)
            assert job_id not in server._running_procs

    @pytest.mark.asyncio
    async def test_cancel_running_without_registered_proc_keeps_old_behavior(
        self,
        tmp_path: Path,
    ):
        """A running job from another server instance cannot be killed."""
        store = server._get_job_store()
        job = store.create("infra", "task")
        store.mark_running(job.id)
        raw = await server.dispatch_cancel(job.id)
        data = json.loads(raw)
        assert data["outcome"] == "running"
        assert "not started by this server" in data["message"]
        assert store.get(job.id).status == "running"
