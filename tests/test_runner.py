"""Tests for the dispatch runner."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_dispatch.models import AgentConfig, Settings
from agent_dispatch.runner import (
    ArgInjectionError,
    _build_command,
    _build_prompt,
    _check_recursion,
    _classify_error,
    _current_depth,
    _extract_denied_tools,
    _permission_hint,
    dispatch,
    dispatch_stream,
)


class TestRecursionProtection:
    def test_depth_zero_by_default(self):
        assert _current_depth() == 0

    def test_depth_from_env(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "2"}):
            assert _current_depth() == 2

    def test_check_recursion_ok(self):
        _check_recursion(max_depth=3)  # should not raise

    def test_depth_invalid_env_returns_zero(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "not_a_number"}):
            assert _current_depth() == 0

    def test_check_recursion_exceeded(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "3"}):
            with pytest.raises(RecursionError, match="depth 3 >= max 3"):
                _check_recursion(max_depth=3)


class TestErrorClassification:
    def test_permission_patterns(self):
        assert _classify_error("Error: permission denied for tool Bash") == "permission"
        assert _classify_error("Tool_use is not allowed in this mode") == "permission"
        assert _classify_error("Tool is not available for this agent") == "permission"
        assert _classify_error("Action not permitted by policy") == "permission"
        assert _classify_error("Unauthorized access attempt") == "permission"

    def test_non_permission_errors(self):
        assert _classify_error("connection refused") == "cli_error"
        assert _classify_error("claude exited with code 1") == "cli_error"
        assert _classify_error("some random error") == "cli_error"

    def test_permission_hint_contains_agent_name(self):
        hint = _permission_hint("myagent")
        assert "myagent" in hint
        assert "bypassPermissions" in hint
        assert "allowed-tools" in hint

    def test_classify_error_handles_none(self):
        """A1: don't crash on None input."""
        assert _classify_error(None) == "cli_error"

    def test_classify_error_handles_empty_string(self):
        """A1: don't crash on empty string."""
        assert _classify_error("") == "cli_error"

    def test_classify_error_handles_dict(self):
        """A1: don't crash on non-string (e.g. dict from malformed JSON)."""
        assert _classify_error({"weird": "value"}) == "cli_error"

    def test_classify_error_handles_integer(self):
        """A1: don't crash on int."""
        assert _classify_error(42) == "cli_error"


class TestBuildPrompt:
    def test_task_only(self):
        assert _build_prompt("do stuff") == "do stuff"

    def test_with_context_no_metadata(self):
        result = _build_prompt("fix bug", context="Error: NPE")
        assert result == "Context:\nError: NPE\n\nTask:\nfix bug"

    def test_with_caller_and_goal(self):
        result = _build_prompt("check logs", caller="backend", goal="debug crash")
        assert "## Goal\ndebug crash" in result
        assert "## Dispatched by\nbackend" in result
        assert "## Task\ncheck logs" in result

    def test_with_all_fields(self):
        result = _build_prompt("fix it", context="trace", caller="api", goal="incident")
        assert "## Goal\nincident" in result
        assert "## Dispatched by\napi" in result
        assert "## Context\ntrace" in result
        assert "## Task\nfix it" in result

    def test_caller_without_goal(self):
        result = _build_prompt("check", caller="infra")
        assert "## Dispatched by\ninfra" in result
        assert "## Goal" not in result

    def test_goal_without_caller(self):
        result = _build_prompt("check", goal="deploy")
        assert "## Goal\ndeploy" in result
        assert "## Dispatched by" not in result


class TestBuildCommand:
    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test")
        self.settings = Settings()

    def test_basic_command(self):
        cmd = _build_command("claude", "hello", self.agent, self.settings)
        assert cmd == ["claude", "-p", "hello", "--output-format", "json"]

    def test_with_session_id(self):
        cmd = _build_command("claude", "hello", self.agent, self.settings, session_id="abc-123")
        assert "--resume" in cmd
        assert "abc-123" in cmd

    def test_with_model(self):
        agent = AgentConfig(directory="/tmp", model="sonnet")
        cmd = _build_command("claude", "hello", agent, self.settings)
        assert "--model" in cmd
        assert "sonnet" in cmd

    def test_with_budget(self):
        agent = AgentConfig(directory="/tmp", max_budget_usd=0.5)
        cmd = _build_command("claude", "hello", agent, self.settings)
        assert "--max-budget-usd" in cmd
        assert "0.5" in cmd

    def test_with_allowed_tools(self):
        agent = AgentConfig(directory="/tmp", allowed_tools=["Read", "Grep"])
        cmd = _build_command("claude", "hello", agent, self.settings)
        assert cmd.count("--allowedTools") == 2

    def test_with_permission_mode(self):
        agent = AgentConfig(directory="/tmp", permission_mode="auto")
        cmd = _build_command("claude", "hello", agent, self.settings)
        assert "--permission-mode" in cmd
        assert "auto" in cmd

    def test_default_permission_mode_from_settings(self):
        settings = Settings(default_permission_mode="bypassPermissions")
        agent = AgentConfig(directory="/tmp")  # no agent-level override
        cmd = _build_command("claude", "hello", agent, settings)
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd

    def test_agent_permission_mode_overrides_default(self):
        settings = Settings(default_permission_mode="plan")
        agent = AgentConfig(directory="/tmp", permission_mode="bypassPermissions")
        cmd = _build_command("claude", "hello", agent, settings)
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "plan" not in cmd

    def test_default_allowed_tools_from_settings(self):
        settings = Settings(default_allowed_tools=["Bash", "Read"])
        agent = AgentConfig(directory="/tmp")
        cmd = _build_command("claude", "hello", agent, settings)
        assert cmd.count("--allowedTools") == 2
        assert "Bash" in cmd
        assert "Read" in cmd

    def test_explicit_empty_allowed_tools_overrides_default(self):
        """B1: allowed_tools=[] means 'explicitly none', not 'inherit'."""
        settings = Settings(default_allowed_tools=["Bash", "Read"])
        agent = AgentConfig(directory="/tmp", allowed_tools=[])
        cmd = _build_command("claude", "hello", agent, settings)
        assert "--allowedTools" not in cmd
        assert "Bash" not in cmd
        assert "Read" not in cmd

    def test_agent_allowed_tools_overrides_default(self):
        settings = Settings(default_allowed_tools=["Bash", "Read"])
        agent = AgentConfig(directory="/tmp", allowed_tools=["Edit"])
        cmd = _build_command("claude", "hello", agent, settings)
        assert cmd.count("--allowedTools") == 1
        assert "Edit" in cmd
        assert "Bash" not in cmd

    def test_default_disallowed_tools_from_settings(self):
        settings = Settings(default_disallowed_tools=["Write"])
        agent = AgentConfig(directory="/tmp")
        cmd = _build_command("claude", "hello", agent, settings)
        assert "--disallowedTools" in cmd
        assert "Write" in cmd

    def test_new_session_id_flag(self):
        cmd = _build_command(
            "claude", "hello", self.agent, self.settings,
            new_session_id="11111111-2222-3333-4444-555555555555",
        )
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "11111111-2222-3333-4444-555555555555"
        assert "--resume" not in cmd

    def test_resume_wins_over_new_session_id(self):
        """When resuming, --session-id must NOT be passed (they conflict)."""
        cmd = _build_command(
            "claude", "hello", self.agent, self.settings,
            session_id="abc-123", new_session_id="should-be-ignored",
        )
        assert "--resume" in cmd
        assert "--session-id" not in cmd
        assert "should-be-ignored" not in cmd


class TestArgInjection:
    """Structured CLI fields must not smuggle extra flags (option injection)."""

    def setup_method(self):
        self.settings = Settings()

    def test_flaglike_session_id_rejected(self):
        agent = AgentConfig(directory="/tmp")
        with pytest.raises(ArgInjectionError, match="session_id"):
            _build_command("claude", "hi", agent, self.settings, session_id="--permission-mode")

    def test_flaglike_model_rejected(self):
        agent = AgentConfig(directory="/tmp", model="--dangerously-skip")
        with pytest.raises(ArgInjectionError, match="model"):
            _build_command("claude", "hi", agent, self.settings)

    def test_flaglike_permission_mode_rejected(self):
        agent = AgentConfig(directory="/tmp", permission_mode="-x")
        with pytest.raises(ArgInjectionError, match="permission_mode"):
            _build_command("claude", "hi", agent, self.settings)

    def test_flaglike_tool_rejected(self):
        agent = AgentConfig(directory="/tmp", allowed_tools=["--resume"])
        with pytest.raises(ArgInjectionError, match="tool"):
            _build_command("claude", "hi", agent, self.settings)

    def test_normal_values_still_build(self):
        agent = AgentConfig(
            directory="/tmp", model="sonnet", permission_mode="bypassPermissions",
            allowed_tools=["Bash(git diff)", "Read"],
        )
        cmd = _build_command("claude", "hi", agent, self.settings, session_id="abc-123-def")
        assert "sonnet" in cmd and "Bash(git diff)" in cmd and "abc-123-def" in cmd

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_returns_error_not_raise(self, mock_run, _which):
        """dispatch() must surface injection as a clean failure, never spawn claude."""
        agent = AgentConfig(directory="/tmp", description="t", timeout=10)
        result = dispatch("test", "hi", agent, self.settings, session_id="--model")
        assert not result.success
        assert result.error_type == "cli_error"
        assert "flag" in result.error.lower()
        mock_run.assert_not_called()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_dispatch_stream_returns_error_not_raise(self, mock_popen, _which):
        agent = AgentConfig(directory="/tmp", description="t", timeout=10, model="--evil")
        result = dispatch_stream("test", "hi", agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"
        mock_popen.assert_not_called()


class TestDispatch:
    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    def test_missing_directory(self, _which, tmp_path: Path):
        agent = AgentConfig(directory=tmp_path / "nonexistent", description="test")
        result = dispatch("test", "hello", agent, self.settings)
        assert not result.success
        assert "does not exist" in result.error
        assert result.error_type == "not_found"

    def test_recursion_exceeded(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "5"}):
            result = dispatch("test", "hello", self.agent, self.settings)
            assert not result.success
            assert "depth" in result.error.lower()
            assert result.error_type == "recursion"

    @patch("agent_dispatch.runner.shutil.which", return_value=None)
    def test_claude_not_found(self, _mock):
        result = dispatch("test", "hello", self.agent, self.settings)
        assert not result.success
        assert "not found" in result.error.lower()
        assert result.error_type == "not_found"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_successful_json_response(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "Found 3 errors in logs",
                    "session_id": "sess-abc",
                    "total_cost_usd": 0.02,
                    "duration_ms": 5000,
                    "num_turns": 2,
                    "is_error": False,
                }
            ),
            stderr="",
        )
        result = dispatch("test", "find errors", self.agent, self.settings)
        assert result.success
        assert result.result == "Found 3 errors in logs"
        assert result.session_id == "sess-abc"
        assert result.cost_usd == 0.02

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_timeout(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=10)
        result = dispatch("test", "slow task", self.agent, self.settings)
        assert not result.success
        assert "timed out" in result.error.lower()
        assert result.error_type == "timeout"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_plain_text_fallback(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Just plain text\n", stderr=""
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.success
        assert result.result == "Just plain text"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_plain_text_fallback_failure_sets_error_type(self, mock_run, _which):
        """A2: non-JSON stdout with non-zero exit should set error_type."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="something broke", stderr=""
        )
        result = dispatch("test", "x", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"
        assert "something broke" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_is_error_true_with_empty_result(self, mock_run, _which):
        """A3: is_error=true with empty/missing result should get a fallback error message."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"is_error": True, "result": ""}),
            stderr="",
        )
        result = dispatch("test", "x", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"
        assert "no details" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_is_error_true_with_none_result(self, mock_run, _which):
        """A1+A3: is_error=true with result=null should not crash."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"is_error": True, "result": None}),
            stderr="",
        )
        result = dispatch("test", "x", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"
        assert result.error  # non-empty fallback
        assert result.result == ""  # coerced safely

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_context_is_prepended(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        dispatch("test", "fix this", self.agent, self.settings, context="Error: NPE at line 42")
        call_args = mock_run.call_args[0][0]
        prompt = call_args[call_args.index("-p") + 1]
        assert "Error: NPE at line 42" in prompt
        assert "fix this" in prompt

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_depth_incremented_in_env(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        dispatch("test", "hello", self.agent, self.settings)
        env = mock_run.call_args[1]["env"]
        assert env["AGENT_DISPATCH_DEPTH"] == "1"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_cwd_is_agent_directory(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        dispatch("test", "hello", self.agent, self.settings)
        cwd = mock_run.call_args[1]["cwd"]
        assert cwd == str(self.agent.directory)

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_permission_error_detected_from_stderr(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: permission denied for tool Bash"
        )
        result = dispatch("test", "run command", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "permission"
        assert "Hint" in result.error
        assert "bypassPermissions" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_permission_error_detected_from_is_error(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "result": "Tool_use is not allowed in this permission mode",
                "is_error": True,
            }),
            stderr="",
        )
        result = dispatch("test", "run command", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "permission"
        assert "Hint" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_non_permission_cli_error(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="connection refused"
        )
        result = dispatch("test", "check", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"
        assert "Hint" not in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_successful_dispatch_has_no_error_type(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        result = dispatch("test", "check", self.agent, self.settings)
        assert result.success
        assert result.error_type is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_caller_and_goal_in_prompt(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        dispatch(
            "test", "check logs", self.agent, self.settings,
            caller="backend-api", goal="debug production crash",
        )
        prompt = mock_run.call_args[0][0][mock_run.call_args[0][0].index("-p") + 1]
        assert "backend-api" in prompt
        assert "debug production crash" in prompt
        assert "check logs" in prompt


class TestResumableTimeout:
    """Timed-out dispatches return a resumable session_id + actionable hint."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_fresh_dispatch_passes_session_id_flag(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False}), stderr="",
        )
        dispatch("test", "hello", self.agent, self.settings)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--session-id")
        # Must be a well-formed UUID (claude requires it)
        import uuid as _uuid
        _uuid.UUID(cmd[idx + 1])

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_resume_does_not_pass_session_id_flag(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False}), stderr="",
        )
        dispatch("test", "hello", self.agent, self.settings, session_id="prior-sess")
        cmd = mock_run.call_args[0][0]
        assert "--session-id" not in cmd
        assert "--resume" in cmd

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_timeout_returns_resumable_session_id(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=10)
        result = dispatch("test", "slow task", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "timeout"
        assert result.session_id  # pre-generated uuid survives the kill
        assert result.session_id in result.error  # mentioned in the hint
        assert "dispatch_session" in result.error
        assert "timeout_seconds" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_timeout_on_resume_keeps_original_session(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=10)
        result = dispatch(
            "test", "slow", self.agent, self.settings, session_id="orig-sess",
        )
        assert result.error_type == "timeout"
        assert result.session_id == "orig-sess"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_json_session_id_wins_over_generated(self, mock_run, _which):
        """claude's reported session_id is authoritative when present."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(
                {"result": "ok", "is_error": False, "session_id": "from-cli"}
            ),
            stderr="",
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.session_id == "from-cli"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_generated_session_id_fallback_when_missing(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False}), stderr="",
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        cmd = mock_run.call_args[0][0]
        generated = cmd[cmd.index("--session-id") + 1]
        assert result.session_id == generated

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_plain_text_success_carries_session_id(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Just plain text\n", stderr=""
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.success
        assert result.session_id  # session exists — claude ran to completion


class TestDeniedTools:
    """permission_denials in CLI output surfaces as denied_tools + hint."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    def test_extract_dedupes_and_preserves_order(self):
        data = {"permission_denials": [
            {"tool_name": "Bash", "tool_input": {}},
            {"tool_name": "WebFetch"},
            {"tool_name": "Bash"},
        ]}
        assert _extract_denied_tools(data) == ["Bash", "WebFetch"]

    def test_extract_handles_missing_or_empty(self):
        assert _extract_denied_tools({}) is None
        assert _extract_denied_tools({"permission_denials": []}) is None
        assert _extract_denied_tools({"permission_denials": "weird"}) is None

    def test_extract_handles_non_dict_entries(self):
        data = {"permission_denials": ["Bash", {"tool": "Read"}, {"junk": 1}]}
        assert _extract_denied_tools(data) == ["Bash", "Read", "unknown"]

    def test_extract_caps_entry_count(self):
        """Untrusted subprocess output must not inflate results/job files."""
        data = {"permission_denials": [{"tool_name": f"Tool{i}"} for i in range(500)]}
        names = _extract_denied_tools(data)
        assert len(names) == 10
        assert names[0] == "Tool0"

    def test_extract_truncates_long_names(self):
        data = {"permission_denials": [{"tool_name": "X" * 5000}]}
        names = _extract_denied_tools(data)
        assert len(names[0]) == 100

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_success_with_denials_gets_hint(self, mock_run, _which):
        """The user-reported case: agent 'succeeds' but asks for permission."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "result": "I need your permission for one read-only query.",
                "is_error": False,
                "permission_denials": [{"tool_name": "Bash", "tool_input": {}}],
            }),
            stderr="",
        )
        result = dispatch("analysis", "map the data", self.agent, self.settings)
        assert result.success  # stays a success — soft signal
        assert result.denied_tools == ["Bash"]
        assert result.hint is not None
        assert "incomplete" in result.hint
        assert "analysis" in result.hint
        assert "Bash" in result.hint

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_success_without_denials_no_hint(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": "all done", "is_error": False}),
            stderr="",
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert result.denied_tools is None
        assert result.hint is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_is_error_with_denials_classified_as_permission(self, mock_run, _which):
        """Denials are a stronger signal than substring matching on the text."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "result": "could not finish the task",  # no permission keywords
                "is_error": True,
                "permission_denials": [{"tool_name": "Edit"}],
            }),
            stderr="",
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "permission"
        assert result.denied_tools == ["Edit"]
        assert "Hint" in result.error


class _InstantTimer:
    """threading.Timer stand-in that fires synchronously on start()."""

    def __init__(self, interval, func):
        self.func = func

    def start(self):
        self.func()

    def cancel(self):
        pass


class TestStreamResumableTimeout:
    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_passes_session_id_flag(self, mock_popen, _which):
        result_line = json.dumps({"type": "result", "result": "ok", "is_error": False})
        mock_popen.return_value = _FakePopen([result_line])
        dispatch_stream("test", "hello", self.agent, self.settings)
        cmd = mock_popen.call_args[0][0]
        assert "--session-id" in cmd

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_success_with_denials(self, mock_popen, _which):
        result_line = json.dumps({
            "type": "result",
            "result": "partial answer",
            "is_error": False,
            "permission_denials": [{"tool_name": "WebFetch"}],
        })
        mock_popen.return_value = _FakePopen([result_line])
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert result.success
        assert result.denied_tools == ["WebFetch"]
        assert result.hint and "WebFetch" in result.hint

    @patch("agent_dispatch.runner.threading.Timer", _InstantTimer)
    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_timeout_returns_resumable_session_id(self, mock_popen, _which):
        """The Timer-kill path must carry the pre-generated session id."""
        mock_popen.return_value = _FakePopen([])  # killed before any output
        result = dispatch_stream("test", "slow", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "timeout"
        assert result.session_id
        assert result.session_id in result.error
        assert "dispatch_session" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_no_result_fallback_carries_session_id(self, mock_popen, _which):
        """Crash mid-stream (no result line) must stay resumable."""
        assistant_line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "working..."}]},
        })
        mock_popen.return_value = _FakePopen([assistant_line], returncode=1)
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert not result.success
        assert result.session_id  # partial transcript may exist on disk


class _FakePopenWithStderr:
    """Like _FakePopen (defined below) but with configurable stderr text."""

    def __init__(self, stdout_lines, returncode=0, stderr_text=""):
        self.returncode = returncode
        self.stdout = iter(line + "\n" for line in stdout_lines)
        self.stderr = type(
            "FakeStderr", (), {"read": lambda _self: stderr_text}
        )()

    def wait(self):
        pass

    def kill(self):
        pass

    def poll(self):
        return self.returncode


class TestOldCliSessionFlagFallback:
    """Old claude CLIs reject --session-id — dispatch retries once without it."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_retries_without_session_flag(self, mock_run, _which):
        ok = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(
                {"result": "ok", "is_error": False, "session_id": "from-cli"}
            ),
            stderr="",
        )
        rejected = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="error: unknown option '--session-id'",
        )
        mock_run.side_effect = [rejected, ok]
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.success
        assert result.result == "ok"
        assert mock_run.call_count == 2
        retry_cmd = mock_run.call_args_list[1][0][0]
        assert "--session-id" not in retry_cmd

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_no_retry_on_other_errors(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="connection refused",
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert not result.success
        assert mock_run.call_count == 1  # no pointless retry

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_retry_failure_does_not_loop(self, mock_run, _which):
        rejected = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="error: unknown option '--session-id'",
        )
        mock_run.side_effect = [rejected, rejected]
        result = dispatch("test", "hello", self.agent, self.settings)
        assert not result.success
        assert mock_run.call_count == 2  # exactly one retry, never more

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_retries_without_session_flag(self, mock_popen, _which):
        result_line = json.dumps({"type": "result", "result": "ok", "is_error": False})
        rejected = _FakePopenWithStderr(
            [], returncode=1, stderr_text="error: unknown option '--session-id'",
        )
        mock_popen.side_effect = [rejected, _FakePopen([result_line])]
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert result.success
        assert mock_popen.call_count == 2
        retry_cmd = mock_popen.call_args_list[1][0][0]
        assert "--session-id" not in retry_cmd


class _FakePopen:
    """Minimal Popen mock that yields stdout lines and supports wait/kill."""

    def __init__(self, stdout_lines: list[str], returncode: int = 0):
        self._stdout_lines = stdout_lines
        self.returncode = returncode
        self.stderr = type("FakeStderr", (), {"read": lambda self: ""})()
        self.stdout = iter(line + "\n" for line in stdout_lines)

    def wait(self):
        pass

    def kill(self):
        pass

    def poll(self):
        return self.returncode  # process already finished


class TestDispatchStream:
    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    def test_missing_directory(self, _which, tmp_path: Path):
        agent = AgentConfig(directory=tmp_path / "nonexistent", description="test")
        result = dispatch_stream("test", "hello", agent, self.settings)
        assert not result.success
        assert "does not exist" in result.error
        assert result.error_type == "not_found"

    def test_recursion_exceeded(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "5"}):
            result = dispatch_stream("test", "hello", self.agent, self.settings)
            assert not result.success
            assert "depth" in result.error.lower()
            assert result.error_type == "recursion"

    @patch("agent_dispatch.runner.shutil.which", return_value=None)
    def test_claude_not_found(self, _mock):
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert not result.success
        assert "not found" in result.error.lower()
        assert result.error_type == "not_found"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_successful_stream_result(self, mock_popen, _which):
        result_line = json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "Found 3 errors",
            "session_id": "sess-stream",
            "total_cost_usd": 0.03,
            "duration_ms": 8000,
            "num_turns": 4,
            "is_error": False,
        })
        mock_popen.return_value = _FakePopen([result_line])
        result = dispatch_stream("test", "find errors", self.agent, self.settings)
        assert result.success
        assert result.result == "Found 3 errors"
        assert result.session_id == "sess-stream"
        assert result.cost_usd == 0.03

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_progress_callback_receives_text(self, mock_popen, _which):
        assistant_line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Checking logs..."}],
            },
        })
        result_line = json.dumps({
            "type": "result",
            "result": "done",
            "is_error": False,
        })
        mock_popen.return_value = _FakePopen([assistant_line, result_line])

        progress: list[str] = []
        dispatch_stream(
            "test", "check", self.agent, self.settings, on_progress=progress.append
        )
        assert any("Checking logs" in p for p in progress)

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_progress_callback_receives_tool_use(self, mock_popen, _which):
        tool_line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "id": "x", "input": {}}],
            },
        })
        result_line = json.dumps({"type": "result", "result": "ok", "is_error": False})
        mock_popen.return_value = _FakePopen([tool_line, result_line])

        progress: list[str] = []
        dispatch_stream(
            "test", "read files", self.agent, self.settings, on_progress=progress.append
        )
        assert any("Using tool: Read" in p for p in progress)

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_uses_stream_json_format(self, mock_popen, _which):
        result_line = json.dumps({"type": "result", "result": "ok", "is_error": False})
        mock_popen.return_value = _FakePopen([result_line])
        dispatch_stream("test", "hello", self.agent, self.settings)
        cmd = mock_popen.call_args[0][0]
        fmt_idx = cmd.index("--output-format")
        assert cmd[fmt_idx + 1] == "stream-json"
        # claude refuses `--print --output-format stream-json` without --verbose
        assert "--verbose" in cmd

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_no_result_line_returns_error(self, mock_popen, _which):
        # Only an assistant message, no result line
        assistant_line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "working..."}]},
        })
        mock_popen.return_value = _FakePopen([assistant_line], returncode=1)
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert not result.success
        assert "No result received" in result.error or result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_popen_file_not_found_error_type(self, mock_popen, _which):
        """B5: Popen FileNotFoundError maps to not_found."""
        mock_popen.side_effect = FileNotFoundError("claude not found")
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "not_found"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_popen_permission_error_type(self, mock_popen, _which):
        """B5: Popen PermissionError maps to permission."""
        mock_popen.side_effect = PermissionError("not executable")
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "permission"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_popen_generic_os_error_type(self, mock_popen, _which):
        """B5: Popen generic OSError maps to cli_error."""
        mock_popen.side_effect = OSError("disk full")
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"


class TestStructuredResponse:
    """response_format='json' behavior — prompt footer + post-parse."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    def test_build_prompt_no_footer_by_default(self):
        prompt = _build_prompt("do thing", caller="api", goal="audit")
        assert "Respond with a single valid JSON" not in prompt

    def test_build_prompt_appends_json_footer_simple(self):
        prompt = _build_prompt("do thing", response_format="json")
        assert "do thing" in prompt
        assert "Respond with a single valid JSON" in prompt

    def test_build_prompt_appends_json_footer_structured(self):
        prompt = _build_prompt(
            "do thing", caller="api", goal="audit", response_format="json"
        )
        assert "## Goal" in prompt
        assert prompt.rstrip().endswith(
            '{"error": "<reason>"}.'
        )

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_parses_clean_json_object(self, mock_run, _which):
        from agent_dispatch.runner import dispatch
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "result": '{"count": 3, "errors": ["a", "b"]}',
                "is_error": False,
            }),
            stderr="",
        )
        result = dispatch(
            "test", "find errors", self.agent, self.settings,
            response_format="json",
        )
        assert result.success
        assert result.parsed_result == {"count": 3, "errors": ["a", "b"]}

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_parses_fenced_json(self, mock_run, _which):
        from agent_dispatch.runner import dispatch
        fenced = "```json\n{\"ok\": true}\n```"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": fenced, "is_error": False}),
            stderr="",
        )
        result = dispatch(
            "test", "task", self.agent, self.settings, response_format="json",
        )
        assert result.success
        assert result.parsed_result == {"ok": True}

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_parses_fenced_without_lang(self, mock_run, _which):
        from agent_dispatch.runner import dispatch
        fenced = "```\n[1, 2, 3]\n```"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": fenced, "is_error": False}),
            stderr="",
        )
        result = dispatch(
            "test", "task", self.agent, self.settings, response_format="json",
        )
        assert result.parsed_result == [1, 2, 3]

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_unparseable_keeps_success_with_none_parsed(self, mock_run, _which):
        """Soft-mode: bad JSON doesn't fail the dispatch, just leaves parsed_result=None."""
        from agent_dispatch.runner import dispatch
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "result": "Sure! Here is what I found: 3 errors.",
                "is_error": False,
            }),
            stderr="",
        )
        result = dispatch(
            "test", "task", self.agent, self.settings, response_format="json",
        )
        assert result.success is True
        assert result.parsed_result is None
        assert "3 errors" in result.result

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_without_response_format_does_not_parse(self, mock_run, _which):
        from agent_dispatch.runner import dispatch
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "result": '{"data": "yes"}',
                "is_error": False,
            }),
            stderr="",
        )
        # No response_format → parsed_result stays None even though result is valid JSON
        result = dispatch("test", "task", self.agent, self.settings)
        assert result.success
        assert result.parsed_result is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.run")
    def test_dispatch_plaintext_fallback_with_json_format(self, mock_run, _which):
        """Non-claude-wrapper stdout → plain text branch still records None
        for parsed_result when content isn't valid JSON."""
        from agent_dispatch.runner import dispatch
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Plain agent reply, not JSON-wrapped.",
            stderr="",
        )
        result = dispatch(
            "test", "task", self.agent, self.settings, response_format="json",
        )
        assert result.success
        assert result.parsed_result is None
        assert "Plain agent reply" in result.result

    def test_parse_helper_handles_scalars(self):
        from agent_dispatch.runner import _parse_structured_response
        assert _parse_structured_response("42") == 42
        assert _parse_structured_response('"hello"') == "hello"
        assert _parse_structured_response("true") is True
        assert _parse_structured_response("null") is None

    def test_parse_helper_returns_none_on_garbage(self):
        from agent_dispatch.runner import _parse_structured_response
        assert _parse_structured_response("") is None
        assert _parse_structured_response("not json at all") is None
        assert _parse_structured_response("```python\nprint(1)\n```") is None
