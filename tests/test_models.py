"""Tests for data models."""

from __future__ import annotations

import pytest

from agent_dispatch.models import (
    AgentConfig,
    DispatchConfig,
    DispatchResult,
    Settings,
    check_permission_mode,
    validate_agent_name,
)


def test_agent_config_expands_home():
    agent = AgentConfig(directory="~/some/path", description="test")
    assert "~" not in str(agent.directory)
    assert agent.directory.is_absolute()


def test_agent_config_defaults():
    agent = AgentConfig(directory="/tmp")
    assert agent.timeout == 300
    assert agent.description == ""
    assert agent.max_budget_usd is None
    assert agent.model is None
    assert agent.allowed_tools == []
    assert agent.disallowed_tools == []


def test_settings_defaults():
    s = Settings()
    assert s.default_timeout == 300
    assert s.max_dispatch_depth == 3
    assert s.default_max_budget_usd is None


def test_dispatch_config_empty():
    config = DispatchConfig()
    assert config.agents == {}
    assert config.settings.default_timeout == 300


def test_dispatch_config_from_dict():
    config = DispatchConfig.model_validate(
        {
            "agents": {
                "test": {
                    "directory": "/tmp",
                    "description": "A test agent",
                    "timeout": 60,
                }
            },
            "settings": {"max_dispatch_depth": 5},
        }
    )
    assert "test" in config.agents
    assert config.agents["test"].timeout == 60
    assert config.settings.max_dispatch_depth == 5


def test_dispatch_result_success():
    r = DispatchResult(agent="test", success=True, result="hello")
    assert r.success
    assert r.session_id is None
    assert r.error is None
    assert r.error_type is None


def test_dispatch_result_error_type():
    r = DispatchResult(
        agent="test", success=False, result="", error="permission denied",
        error_type="permission",
    )
    assert not r.success
    assert r.error_type == "permission"


class TestAgentNameValidation:
    def test_valid_names(self):
        for name in ["infra", "backend-api", "agent_1", "A1"]:
            assert validate_agent_name(name) == name

    def test_invalid_names(self):
        for name in ["", "-start", "_start", "has space", "special!", "a/b"]:
            with pytest.raises(ValueError, match="Invalid agent name"):
                validate_agent_name(name)


class TestPermissionModeValidation:
    def test_known_modes_no_warning(self):
        for mode in ("default", "plan", "bypassPermissions"):
            assert check_permission_mode(mode) is None

    def test_unknown_mode_returns_warning(self):
        warning = check_permission_mode("bypassPermision")  # typo
        assert warning is not None
        assert "Unknown" in warning
        assert "bypassPermision" in warning

    def test_none_no_warning(self):
        assert check_permission_mode(None) is None

    def test_empty_string_no_warning(self):
        assert check_permission_mode("") is None


def test_settings_default_permissions():
    s = Settings(
        default_permission_mode="bypassPermissions",
        default_allowed_tools=["Bash", "Read"],
    )
    assert s.default_permission_mode == "bypassPermissions"
    assert s.default_allowed_tools == ["Bash", "Read"]
    assert s.default_disallowed_tools == []


def test_dispatch_result_with_metadata():
    r = DispatchResult(
        agent="test",
        success=True,
        result="done",
        session_id="abc-123",
        cost_usd=0.05,
        duration_ms=1200,
        num_turns=3,
    )
    assert r.session_id == "abc-123"
    assert r.cost_usd == 0.05


class TestSettingsValidation:
    def test_max_concurrency_default(self):
        s = Settings()
        assert s.max_concurrency == 5

    def test_max_concurrency_zero_rejected(self):
        with pytest.raises(Exception):
            Settings(max_concurrency=0)

    def test_max_concurrency_negative_rejected(self):
        with pytest.raises(Exception):
            Settings(max_concurrency=-1)

    def test_max_concurrency_one_ok(self):
        s = Settings(max_concurrency=1)
        assert s.max_concurrency == 1

    def test_max_dispatch_depth_zero_rejected(self):
        with pytest.raises(Exception):
            Settings(max_dispatch_depth=0)

    def test_cache_ttl_zero_allowed(self):
        from agent_dispatch.models import CacheSettings
        c = CacheSettings(ttl=0)
        assert c.ttl == 0

    def test_cache_ttl_negative_rejected(self):
        from agent_dispatch.models import CacheSettings
        with pytest.raises(Exception):
            CacheSettings(ttl=-1)
