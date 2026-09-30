"""Tests for the dispatch runner."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_dispatch import usage
from agent_dispatch.models import AgentConfig, Settings
from agent_dispatch.runner import (
    ArgInjectionError,
    _build_command,
    _build_prompt,
    _build_system_prompt,
    _check_recursion,
    _classify_error,
    _current_depth,
    _extract_denied_tools,
    _permission_hint,
    _split_outcome,
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
            "claude",
            "hello",
            self.agent,
            self.settings,
            new_session_id="11111111-2222-3333-4444-555555555555",
        )
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "11111111-2222-3333-4444-555555555555"
        assert "--resume" not in cmd

    def test_resume_wins_over_new_session_id(self):
        """When resuming, --session-id must NOT be passed (they conflict)."""
        cmd = _build_command(
            "claude",
            "hello",
            self.agent,
            self.settings,
            session_id="abc-123",
            new_session_id="should-be-ignored",
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
            directory="/tmp",
            model="sonnet",
            permission_mode="bypassPermissions",
            allowed_tools=["Bash(git diff)", "Read"],
        )
        cmd = _build_command("claude", "hi", agent, self.settings, session_id="abc-123-def")
        assert "sonnet" in cmd and "Bash(git diff)" in cmd and "abc-123-def" in cmd

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
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
    @patch("agent_dispatch.runner._run_captured")
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
    @patch("agent_dispatch.runner._run_captured")
    def test_timeout(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=10)
        result = dispatch("test", "slow task", self.agent, self.settings)
        assert not result.success
        assert "timed out" in result.error.lower()
        assert result.error_type == "timeout"

    @pytest.mark.parametrize("output", [
        "", "partial answer", '{"type":"result",', '{"result":"unfinished"}', "[]",
    ])
    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_timeout_requires_a_complete_cli_result(self, mock_run, _which, output):
        mock_run.side_effect = subprocess.TimeoutExpired([], 10, output=output)
        result = dispatch("test", "task", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "timeout"
        assert result.session_id

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_budget_result_outranks_cleanup_timeout(self, mock_run, _which):
        payload = {
            "type": "result", "is_error": True, "subtype": "error_max_budget_usd",
            "session_id": "paid-session", "total_cost_usd": 0.42,
        }
        mock_run.side_effect = subprocess.TimeoutExpired(
            [], 10, output=json.dumps(payload).encode(),
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "budget"
        assert result.budget_exceeded
        assert result.session_id == "paid-session"
        assert result.cost_usd == 0.42
        assert len(usage.load()) == 1

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_plain_text_fallback(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Just plain text\n", stderr=""
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.success
        assert result.result == "Just plain text"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
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
    @patch("agent_dispatch.runner._run_captured")
    def test_is_error_true_with_empty_result(self, mock_run, _which):
        """A3: is_error=true with empty/missing result should get a fallback error message."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"is_error": True, "result": ""}),
            stderr="",
        )
        result = dispatch("test", "x", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"
        assert "no details" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_is_error_true_with_none_result(self, mock_run, _which):
        """A1+A3: is_error=true with result=null should not crash."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"is_error": True, "result": None}),
            stderr="",
        )
        result = dispatch("test", "x", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"
        assert result.error  # non-empty fallback
        assert result.result == ""  # coerced safely

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
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
    @patch("agent_dispatch.runner._run_captured")
    def test_depth_incremented_in_env(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        dispatch("test", "hello", self.agent, self.settings)
        env = mock_run.call_args[1]["env"]
        assert env["AGENT_DISPATCH_DEPTH"] == "1"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_cwd_is_agent_directory(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        dispatch("test", "hello", self.agent, self.settings)
        cwd = mock_run.call_args[1]["cwd"]
        assert cwd == str(self.agent.directory)

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
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
    @patch("agent_dispatch.runner._run_captured")
    def test_permission_error_detected_from_is_error(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "Tool_use is not allowed in this permission mode",
                    "is_error": True,
                }
            ),
            stderr="",
        )
        result = dispatch("test", "run command", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "permission"
        assert "Hint" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_non_permission_cli_error(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="connection refused"
        )
        result = dispatch("test", "check", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "cli_error"
        assert "Hint" not in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_successful_dispatch_has_no_error_type(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        result = dispatch("test", "check", self.agent, self.settings)
        assert result.success
        assert result.error_type is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_caller_and_goal_in_prompt(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""
        )
        dispatch(
            "test",
            "check logs",
            self.agent,
            self.settings,
            caller="backend-api",
            goal="debug production crash",
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
    @patch("agent_dispatch.runner._run_captured")
    def test_fresh_dispatch_passes_session_id_flag(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False}),
            stderr="",
        )
        dispatch("test", "hello", self.agent, self.settings)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--session-id")
        # Must be a well-formed UUID (claude requires it)
        import uuid as _uuid

        _uuid.UUID(cmd[idx + 1])

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_resume_does_not_pass_session_id_flag(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False}),
            stderr="",
        )
        dispatch("test", "hello", self.agent, self.settings, session_id="prior-sess")
        cmd = mock_run.call_args[0][0]
        assert "--session-id" not in cmd
        assert "--resume" in cmd

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
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
    @patch("agent_dispatch.runner._run_captured")
    def test_timeout_on_resume_keeps_original_session(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=10)
        result = dispatch(
            "test",
            "slow",
            self.agent,
            self.settings,
            session_id="orig-sess",
        )
        assert result.error_type == "timeout"
        assert result.session_id == "orig-sess"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_json_session_id_wins_over_generated(self, mock_run, _which):
        """claude's reported session_id is authoritative when present."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False, "session_id": "from-cli"}),
            stderr="",
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.session_id == "from-cli"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_generated_session_id_fallback_when_missing(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False}),
            stderr="",
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        cmd = mock_run.call_args[0][0]
        generated = cmd[cmd.index("--session-id") + 1]
        assert result.session_id == generated

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
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
        data = {
            "permission_denials": [
                {"tool_name": "Bash", "tool_input": {}},
                {"tool_name": "WebFetch"},
                {"tool_name": "Bash"},
            ]
        }
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
    @patch("agent_dispatch.runner._run_captured")
    def test_success_with_denials_gets_hint(self, mock_run, _which):
        """The user-reported case: agent 'succeeds' but asks for permission."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "I need your permission for one read-only query.",
                    "is_error": False,
                    "permission_denials": [{"tool_name": "Bash", "tool_input": {}}],
                }
            ),
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
    @patch("agent_dispatch.runner._run_captured")
    def test_success_without_denials_no_hint(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": "all done", "is_error": False}),
            stderr="",
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert result.denied_tools is None
        assert result.hint is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_is_error_with_denials_classified_as_permission(self, mock_run, _which):
        """Denials are a stronger signal than substring matching on the text."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "could not finish the task",  # no permission keywords
                    "is_error": True,
                    "permission_denials": [{"tool_name": "Edit"}],
                }
            ),
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
        result_line = json.dumps(
            {
                "type": "result",
                "result": "partial answer",
                "is_error": False,
                "permission_denials": [{"tool_name": "WebFetch"}],
            }
        )
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
        assistant_line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "working..."}]},
            }
        )
        mock_popen.return_value = _FakePopen([assistant_line], returncode=1)
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert not result.success
        assert result.session_id  # partial transcript may exist on disk


class _FakeStderr:
    """Stand-in for a real stderr pipe.

    ``read(size)`` must accept a size and return "" at EOF: the runner drains
    stderr in a background thread with chunked reads (a stderr pipe that is only
    read after the stdout loop deadlocks a chatty child).
    """

    def __init__(self, text: str = ""):
        self._text = text
        self._pos = 0

    def read(self, size: int | None = None) -> str:
        if size is None:
            chunk, self._pos = self._text[self._pos :], len(self._text)
            return chunk
        chunk = self._text[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        pass


class _FakePopenWithStderr:
    """Like _FakePopen (defined below) but with configurable stderr text."""

    def __init__(self, stdout_lines, returncode=0, stderr_text=""):
        self.returncode = returncode
        self.stdout = iter(line + "\n" for line in stdout_lines)
        self.stderr = _FakeStderr(stderr_text)

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
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_retries_without_session_flag(self, mock_run, _which):
        ok = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": "ok", "is_error": False, "session_id": "from-cli"}),
            stderr="",
        )
        rejected = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
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
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_no_retry_on_other_errors(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="connection refused",
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert not result.success
        assert mock_run.call_count == 1  # no pointless retry

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_retry_failure_does_not_loop(self, mock_run, _which):
        rejected = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
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
            [],
            returncode=1,
            stderr_text="error: unknown option '--session-id'",
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
        self.stderr = _FakeStderr("")
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

    @pytest.mark.parametrize("event", [
        None, [], 42, "noise",
        {"type": "assistant", "message": None},
        {"type": "assistant", "message": []},
        {"type": "assistant", "message": {"content": [None, "noise", 42]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": 42}]}},
    ])
    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_malformed_progress_preserves_result(self, mock_popen, _which, event):
        answer = {"type": "result", "result": "paid answer", "total_cost_usd": 0.42}
        mock_popen.return_value = _FakePopen([
            json.dumps(event), json.dumps(answer), json.dumps(event),
        ])
        progress = []
        result = dispatch_stream(
            "test", "task", self.agent, self.settings, on_progress=progress.append,
        )
        assert result.success
        assert result.result == "paid answer"
        assert result.cost_usd == 0.42
        assert progress == []

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
        result_line = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "Found 3 errors",
                "session_id": "sess-stream",
                "total_cost_usd": 0.03,
                "duration_ms": 8000,
                "num_turns": 4,
                "is_error": False,
            }
        )
        mock_popen.return_value = _FakePopen([result_line])
        result = dispatch_stream("test", "find errors", self.agent, self.settings)
        assert result.success
        assert result.result == "Found 3 errors"
        assert result.session_id == "sess-stream"
        assert result.cost_usd == 0.03

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_progress_callback_receives_text(self, mock_popen, _which):
        assistant_line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Checking logs..."}],
                },
            }
        )
        result_line = json.dumps(
            {
                "type": "result",
                "result": "done",
                "is_error": False,
            }
        )
        mock_popen.return_value = _FakePopen([assistant_line, result_line])

        progress: list[str] = []
        dispatch_stream("test", "check", self.agent, self.settings, on_progress=progress.append)
        assert any("Checking logs" in p for p in progress)

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_failing_progress_callback_keeps_the_paid_result(self, mock_popen, _which):
        # An async job's callback writes the job file; ENOSPC there used to
        # escape the read loop, kill the tree and drop result + session.
        assistant_line = json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
        )
        result_line = json.dumps({"type": "result", "result": "done", "is_error": False})
        fake = _FakePopen([assistant_line, result_line])
        killed: list[bool] = []
        fake.kill = lambda: killed.append(True)
        mock_popen.return_value = fake

        def broken(_msg):
            raise OSError(28, "No space left on device")

        result = dispatch_stream("test", "check", self.agent, self.settings, on_progress=broken)
        assert result.success is True
        assert result.result == "done"
        assert result.session_id
        assert killed == []

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_thread_start_failure_kills_the_spawned_process(self, mock_popen, _which):
        # Without a timer or reader the claude process would run unsupervised.
        fake = _FakePopen([json.dumps({"type": "result", "result": "x", "is_error": False})])
        killed: list[bool] = []
        fake.kill = lambda: killed.append(True)
        fake.wait = lambda timeout=None: None  # real Popen.wait takes a timeout
        mock_popen.return_value = fake

        class _NoTimer:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

            def cancel(self):
                pass

        with patch("agent_dispatch.runner.threading.Timer", _NoTimer):
            with pytest.raises(RuntimeError, match="can't start new thread"):
                dispatch_stream("test", "check", self.agent, self.settings)
        assert killed == [True]

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_progress_callback_receives_tool_use(self, mock_popen, _which):
        tool_line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Read", "id": "x", "input": {}}],
                },
            }
        )
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
        assistant_line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "working..."}]},
            }
        )
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
        prompt = _build_prompt("do thing", caller="api", goal="audit", response_format="json")
        assert "## Goal" in prompt
        assert prompt.rstrip().endswith('{"error": "<reason>"}.')

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_parses_clean_json_object(self, mock_run, _which):
        from agent_dispatch.runner import dispatch

        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": '{"count": 3, "errors": ["a", "b"]}',
                    "is_error": False,
                }
            ),
            stderr="",
        )
        result = dispatch(
            "test",
            "find errors",
            self.agent,
            self.settings,
            response_format="json",
        )
        assert result.success
        assert result.parsed_result == {"count": 3, "errors": ["a", "b"]}

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_parses_fenced_json(self, mock_run, _which):
        from agent_dispatch.runner import dispatch

        fenced = '```json\n{"ok": true}\n```'
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": fenced, "is_error": False}),
            stderr="",
        )
        result = dispatch(
            "test",
            "task",
            self.agent,
            self.settings,
            response_format="json",
        )
        assert result.success
        assert result.parsed_result == {"ok": True}

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
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
            "test",
            "task",
            self.agent,
            self.settings,
            response_format="json",
        )
        assert result.parsed_result == [1, 2, 3]

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_unparseable_keeps_success_with_none_parsed(self, mock_run, _which):
        """Soft-mode: bad JSON doesn't fail the dispatch, just leaves parsed_result=None."""
        from agent_dispatch.runner import dispatch

        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "Sure! Here is what I found: 3 errors.",
                    "is_error": False,
                }
            ),
            stderr="",
        )
        result = dispatch(
            "test",
            "task",
            self.agent,
            self.settings,
            response_format="json",
        )
        assert result.success is True
        assert result.parsed_result is None
        assert "3 errors" in result.result

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_without_response_format_does_not_parse(self, mock_run, _which):
        from agent_dispatch.runner import dispatch

        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": '{"data": "yes"}',
                    "is_error": False,
                }
            ),
            stderr="",
        )
        # No response_format → parsed_result stays None even though result is valid JSON
        result = dispatch("test", "task", self.agent, self.settings)
        assert result.success
        assert result.parsed_result is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
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
            "test",
            "task",
            self.agent,
            self.settings,
            response_format="json",
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


class TestBudget:
    """_apply_budget: post-hoc flagging when cost_usd exceeds the budget."""

    def setup_method(self):
        self.settings = Settings()

    def _result(self, cost: float | None, success: bool = True, hint: str | None = None):
        from agent_dispatch.models import DispatchResult

        return DispatchResult(
            agent="a",
            success=success,
            result="x",
            cost_usd=cost,
            hint=hint,
        )

    def _agent(self, budget: float | None = None) -> AgentConfig:
        return AgentConfig(directory="/tmp", description="t", max_budget_usd=budget)

    def test_exceeded_sets_flag_and_hint(self):
        from agent_dispatch.runner import _apply_budget

        result = _apply_budget(self._result(0.05), self._agent(0.01), self.settings)
        assert result.budget_exceeded is True
        assert "exceeded" in result.hint
        assert "$0.0500" in result.hint
        assert "$0.01" in result.hint

    def test_within_budget_untouched(self):
        from agent_dispatch.runner import _apply_budget

        result = _apply_budget(self._result(0.005), self._agent(0.01), self.settings)
        assert result.budget_exceeded is None
        assert result.hint is None

    def test_no_budget_configured(self):
        from agent_dispatch.runner import _apply_budget

        result = _apply_budget(self._result(99.0), self._agent(None), self.settings)
        assert result.budget_exceeded is None

    def test_no_cost_untouched(self):
        from agent_dispatch.runner import _apply_budget

        result = _apply_budget(self._result(None), self._agent(0.01), self.settings)
        assert result.budget_exceeded is None

    def test_settings_default_fallback(self):
        from agent_dispatch.runner import _apply_budget

        settings = Settings(default_max_budget_usd=0.01)
        result = _apply_budget(self._result(0.05), self._agent(None), settings)
        assert result.budget_exceeded is True

    def test_agent_budget_overrides_settings_default(self):
        from agent_dispatch.runner import _apply_budget

        settings = Settings(default_max_budget_usd=0.001)
        result = _apply_budget(self._result(0.5), self._agent(1.0), settings)
        assert result.budget_exceeded is None

    def test_hint_appended_to_existing(self):
        from agent_dispatch.runner import _apply_budget

        result = _apply_budget(
            self._result(0.05, hint="tools were denied."),
            self._agent(0.01),
            self.settings,
        )
        assert result.hint.startswith("tools were denied.")
        assert "exceeded" in result.hint

    def test_applies_to_failed_result_too(self):
        from agent_dispatch.runner import _apply_budget

        result = _apply_budget(
            self._result(0.05, success=False),
            self._agent(0.01),
            self.settings,
        )
        assert result.budget_exceeded is True
        # success is NOT flipped — the flag is advisory
        assert result.success is False

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_flags_over_budget(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "done",
                    "session_id": "s1",
                    "total_cost_usd": 0.50,
                    "is_error": False,
                }
            ),
            stderr="",
        )
        agent = AgentConfig(directory="/tmp", description="t", max_budget_usd=0.10)
        result = dispatch("test", "task", agent, Settings())
        assert result.success
        assert result.budget_exceeded is True
        assert "exceeded" in result.hint

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_within_budget_no_flag(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "done",
                    "session_id": "s1",
                    "total_cost_usd": 0.05,
                    "is_error": False,
                }
            ),
            stderr="",
        )
        agent = AgentConfig(directory="/tmp", description="t", max_budget_usd=0.10)
        result = dispatch("test", "task", agent, Settings())
        assert result.success
        assert result.budget_exceeded is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_flags_over_budget(self, mock_popen, _which):
        result_line = json.dumps(
            {
                "type": "result",
                "result": "done",
                "session_id": "s1",
                "total_cost_usd": 0.50,
                "is_error": False,
            }
        )
        mock_popen.return_value = _FakePopen([result_line])
        agent = AgentConfig(directory="/tmp", description="t", max_budget_usd=0.10)
        result = dispatch_stream("test", "task", agent, Settings())
        assert result.success
        assert result.budget_exceeded is True


def _budget_payload(**extra):
    """The result payload a real claude CLI emits when --max-budget-usd is hit.

    Captured from claude 2.1.220: no `result` field at all — the reason lives
    in `errors` / `subtype` / `terminal_reason`.
    """
    payload = {
        "is_error": True,
        "subtype": "error_max_budget_usd",
        "terminal_reason": "budget_exhausted",
        "errors": ["Reached maximum budget ($0.0001)"],
        "session_id": "sess-budget",
        "total_cost_usd": 0.000583,
        "num_turns": 1,
        "type": "result",
    }
    payload.update(extra)
    return payload


class TestBudgetExhausted:
    """The CLI stops the session at --max-budget-usd -> error_type='budget'."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="t", max_budget_usd=0.0001)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_classifies_budget_error(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(_budget_payload()),
            stderr="",
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "budget"
        assert result.budget_exceeded is True
        # The real reason is surfaced instead of "no details"
        assert "Reached maximum budget" in result.error
        assert "no details" not in result.error
        # ...along with the recovery path
        assert "--max-budget-usd" in result.error
        assert "sess-budget" in result.error  # resumable
        assert result.session_id == "sess-budget"
        assert result.cost_usd == 0.000583
        assert result.num_turns == 1

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_terminal_reason_alone_is_enough(self, mock_run, _which):
        payload = _budget_payload()
        del payload["subtype"]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert result.error_type == "budget"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_subtype_alone_is_enough(self, mock_run, _which):
        payload = _budget_payload()
        del payload["terminal_reason"]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert result.error_type == "budget"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_budget_flag_set_even_when_cost_under_cap(self, mock_run, _which):
        """The cap was reached by definition — don't rely on the cost compare."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(_budget_payload(total_cost_usd=0.00009)),
            stderr="",
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert result.budget_exceeded is True
        # No duplicate post-hoc overage note — the error text already explains it
        assert result.hint is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_budget_wins_over_denied_tools(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(_budget_payload(permission_denials=[{"tool_name": "Bash"}])),
            stderr="",
        )
        result = dispatch("test", "task", self.agent, self.settings)
        assert result.error_type == "budget"  # the terminal reason, not the denial
        assert result.denied_tools == ["Bash"]  # still reported

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_hint_names_the_cap(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(_budget_payload()),
            stderr="",
        )
        agent = AgentConfig(directory="/tmp", description="t", max_budget_usd=0.25)
        result = dispatch("test", "task", agent, Settings())
        assert "$0.25" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_settings_default_budget_named_in_hint(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(_budget_payload()),
            stderr="",
        )
        agent = AgentConfig(directory="/tmp", description="t")
        result = dispatch("test", "task", agent, Settings(default_max_budget_usd=0.5))
        assert "$0.5" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_classifies_budget_error(self, mock_popen, _which):
        mock_popen.return_value = _FakePopen([json.dumps(_budget_payload())])
        result = dispatch_stream("test", "task", self.agent, self.settings)
        assert not result.success
        assert result.error_type == "budget"
        assert result.budget_exceeded is True
        assert "Reached maximum budget" in result.error
        assert result.session_id == "sess-budget"


class TestCliErrorDetails:
    """CLI-level failures carry their reason in `errors`/`subtype`, not `result`."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="t")
        self.settings = Settings()

    def _dispatch(self, mock_run, payload):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        return dispatch("test", "task", self.agent, self.settings)

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_errors_list_is_surfaced(self, mock_run, _which):
        result = self._dispatch(
            mock_run, {"is_error": True, "errors": ["Reached maximum turns (5)"]}
        )
        assert result.error_type == "cli_error"
        assert "Reached maximum turns (5)" in result.error
        assert "no details" not in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_multiple_errors_joined(self, mock_run, _which):
        result = self._dispatch(mock_run, {"is_error": True, "errors": ["first", "second"]})
        assert "first; second" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_subtype_used_when_errors_absent(self, mock_run, _which):
        result = self._dispatch(mock_run, {"is_error": True, "subtype": "error_during_execution"})
        assert "error_during_execution" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_result_text_still_wins(self, mock_run, _which):
        result = self._dispatch(
            mock_run, {"is_error": True, "result": "agent said no", "errors": ["ignored"]}
        )
        assert result.error.startswith("agent said no")
        assert result.result == "agent said no"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_fallback_when_nothing_available(self, mock_run, _which):
        result = self._dispatch(mock_run, {"is_error": True})
        assert "no details" in result.error
        assert "exit code 0" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_details_are_classified(self, mock_run, _which):
        """A permission failure reported only in `errors` is still classified."""
        result = self._dispatch(mock_run, {"is_error": True, "errors": ["Permission denied: Bash"]})
        assert result.error_type == "permission"
        assert "agent-dispatch update" in result.error  # permission hint appended

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_untrusted_errors_are_capped(self, mock_run, _which):
        result = self._dispatch(
            mock_run,
            {"is_error": True, "errors": ["x" * 5000] * 20},
        )
        # 5 entries x 300 chars + separators — bounded, not 100k of subprocess output
        assert len(result.error) < 2500

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_non_string_errors_do_not_crash(self, mock_run, _which):
        result = self._dispatch(mock_run, {"is_error": True, "errors": [{"code": 7}, None]})
        assert not result.success
        assert result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_blank_errors_fall_through_to_subtype(self, mock_run, _which):
        result = self._dispatch(
            mock_run, {"is_error": True, "errors": ["   ", ""], "subtype": "error_weird"}
        )
        assert "error_weird" in result.error


class TestOnProc:
    """dispatch_stream exposes its Popen handle via the on_proc callback."""

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_registration_failure_reaps_process_and_closes_pipes(self, mock_popen, _which):
        proc = mock_popen.return_value
        proc.pid = None  # exercise the kill fallback without signalling real PIDs

        def fail_registration(child):
            assert child is proc
            raise OSError("job storage unavailable")

        with pytest.raises(OSError, match="job storage unavailable"):
            dispatch_stream(
                "test", "task", AgentConfig(directory="/tmp"), Settings(),
                on_proc=fail_registration,
            )
        proc.kill.assert_called_once()
        proc.wait.assert_called_once()
        proc.stdout.close.assert_called_once()
        proc.stderr.close.assert_called_once()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_on_proc_receives_popen_handle(self, mock_popen, _which):
        result_line = json.dumps(
            {
                "type": "result",
                "result": "ok",
                "session_id": "s",
                "is_error": False,
            }
        )
        fake = _FakePopen([result_line])
        mock_popen.return_value = fake
        seen: list = []
        result = dispatch_stream(
            "test",
            "task",
            AgentConfig(directory="/tmp", description="t"),
            Settings(),
            on_proc=seen.append,
        )
        assert result.success
        assert seen == [fake]

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_no_on_proc_is_fine(self, mock_popen, _which):
        result_line = json.dumps(
            {
                "type": "result",
                "result": "ok",
                "session_id": "s",
                "is_error": False,
            }
        )
        mock_popen.return_value = _FakePopen([result_line])
        result = dispatch_stream(
            "test",
            "task",
            AgentConfig(directory="/tmp", description="t"),
            Settings(),
        )
        assert result.success


class TestStreamPipeHandling:
    """Regression tests for the stderr pipe.

    These spawn a real python subprocess (never the `claude` CLI) on purpose: a
    mocked Popen cannot reproduce a pipe deadlock, because the deadlock lives in
    the OS pipe buffer, not in our code's control flow. Each test finishes in
    well under a second.
    """

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @staticmethod
    def _fake_cli(tmp_path: Path, body: str) -> Path:
        script = tmp_path / "fake_cli.py"
        script.write_text(body)
        launcher = tmp_path / "fake_cli"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
        return launcher

    @staticmethod
    def _spawn_after_ready(ready: Path):
        """Exclude interpreter startup from deliberately short test deadlines."""
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready.exists():
                proc.kill()
                proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()
                pytest.fail("Test subprocess did not finish setting up")
            return proc

        return spawn

    def test_ordinary_dispatch_keeps_result_before_cleanup_timeout(self, tmp_path):
        ready = tmp_path / "ready"
        cli = self._fake_cli(
            tmp_path,
            "import json, time\nfrom pathlib import Path\n"
            "print(json.dumps({'type': 'result', 'result': 'paid answer', "
            "'total_cost_usd': 0.42, 'is_error': False}), end='', flush=True)\n"
            f"Path({str(ready)!r}).touch()\n"
            "time.sleep(3)\n",
        )
        agent = AgentConfig(directory=tmp_path, timeout=1)
        with (
            patch("agent_dispatch.runner.shutil.which", return_value=str(cli)),
            patch("agent_dispatch.runner.subprocess.Popen", self._spawn_after_ready(ready)),
        ):
            result = dispatch("test", "task", agent, Settings())
        assert result.success, result.error
        assert result.result == "paid answer"
        assert result.cost_usd == 0.42
        assert "cleanup" in result.hint

    @pytest.mark.parametrize("parent_exits", [False, True])
    def test_ordinary_timeout_stops_descendant_work(self, tmp_path, parent_exits):
        ready = tmp_path / "ready"
        unwanted = tmp_path / "work-after-timeout"
        child = (
            "import time; from pathlib import Path; "
            f"Path({str(ready)!r}).touch(); "
            f"time.sleep(2); Path({str(unwanted)!r}).touch()"
        )
        cli = self._fake_cli(
            tmp_path,
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            + ("sys.exit(0)\n" if parent_exits else "time.sleep(4)\n"),
        )
        agent = AgentConfig(directory=tmp_path, timeout=1)
        with (
            patch("agent_dispatch.runner.shutil.which", return_value=str(cli)),
            patch("agent_dispatch.runner.subprocess.Popen", self._spawn_after_ready(ready)),
        ):
            result = dispatch("test", "task", agent, Settings())
        assert result.error_type == "timeout"
        assert ready.exists(), "Descendant was not ready before the timeout"
        time.sleep(1.5)
        assert not unwanted.exists(), "Descendant continued work after dispatch timed out"

    def test_ordinary_cleanup_is_bounded_with_a_detached_writer(self, tmp_path):
        pid_file = tmp_path / "detached-pid"
        ready = tmp_path / "ready"
        cli = self._fake_cli(
            tmp_path,
            "import json, subprocess, sys, time\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'], "
            "start_new_session=True)\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            "print(json.dumps({'type': 'result', 'result': 'complete', "
            "'is_error': False}), end='', flush=True)\n"
            f"Path({str(ready)!r}).touch()\n"
            "time.sleep(15)\n",
        )
        agent = AgentConfig(directory=tmp_path, timeout=1)
        started = time.monotonic()
        try:
            with (
                patch("agent_dispatch.runner.shutil.which", return_value=str(cli)),
                patch("agent_dispatch.runner.subprocess.Popen", self._spawn_after_ready(ready)),
            ):
                result = dispatch("test", "task", agent, Settings())
            assert result.success, result.error
            assert result.result == "complete"
            assert time.monotonic() - started < 5, "Cleanup waited on a detached writer"
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @pytest.mark.parametrize("run", [dispatch, dispatch_stream], ids=["ordinary", "stream"])
    def test_chatty_stderr_does_not_deadlock(self, tmp_path: Path, run):
        # 1 MiB of stderr — far past the ~64 KiB pipe buffer. Before stderr was
        # drained concurrently the child blocked in write(2), never emitted its
        # result line, and the dispatch burned its whole timeout.
        cli = self._fake_cli(
            tmp_path,
            "import json, sys\n"
            "sys.stderr.write('W' * 1_000_000)\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({'type': 'result', 'is_error': False, "
            "'result': 'данные' * 20000, 'session_id': 's1'}, ensure_ascii=False))\n",
        )
        agent = AgentConfig(directory=tmp_path, description="t", timeout=10)
        with patch("agent_dispatch.runner.shutil.which", return_value=str(cli)):
            result = run("test", "hello", agent, Settings())
        assert result.success, result.error
        assert result.result == "данные" * 20000
        assert result.error_type is None

    @patch("agent_dispatch.runner.threading.Timer", _InstantTimer)
    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_result_survives_a_child_that_lingers_past_the_timeout(self, mock_popen, _which):
        """The agent answered (and was billed) but its process outlived the deadline.

        Checking ``timed_out`` before ``result_data`` threw the paid-for answer
        away and reported ``error_type="timeout"`` instead.
        """
        mock_popen.return_value = _FakePopen(
            [
                json.dumps(
                    {
                        "type": "result",
                        "is_error": False,
                        "result": "REAL ANSWER",
                        "session_id": "s2",
                        "total_cost_usd": 0.42,
                    }
                )
            ]
        )
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert result.success
        assert result.result == "REAL ANSWER"
        assert result.cost_usd == 0.42
        assert result.error_type is None
        assert "killed" in (result.hint or "").lower()

    def test_grandchild_holding_stderr_does_not_hang_the_dispatch(self, tmp_path: Path):
        """A stdio MCP server inherits fd 2 and can outlive `claude`.

        The stderr pipe then never reaches EOF, so the drain thread stays parked
        in read(). Closing that pipe from here would block on the BufferedReader
        lock *without a timeout* and hang dispatch_stream (and its caller's
        semaphore slot) forever — so the close is skipped while the reader lives.
        """
        cli = self._fake_cli(
            tmp_path,
            "import json, subprocess, sys\n"
            # stderr is inherited by the grandchild; stdin/stdout are not
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)\n"
            "print(json.dumps({'type': 'result', 'is_error': False, "
            "'result': 'ANSWER', 'session_id': 's3'}), flush=True)\n"
            "sys.exit(0)\n",
        )
        agent = AgentConfig(directory=tmp_path, description="t", timeout=30)
        started = time.monotonic()
        with patch("agent_dispatch.runner.shutil.which", return_value=str(cli)):
            result = dispatch_stream("test", "hello", agent, Settings())
        elapsed = time.monotonic() - started
        assert result.success
        assert result.result == "ANSWER"
        # Must not wait on stderr we do not need: the agent already answered.
        assert elapsed < 5, f"dispatch_stream took {elapsed:.1f}s — it waited on stderr"

    def test_backgrounded_grandchild_does_not_extend_the_timeout(self, tmp_path: Path):
        """The timeout must bound the call even when the agent leaves a process running.

        A grandchild inherits the child's stdout, so killing only the direct
        child leaves the read loop parked for the grandchild's whole lifetime —
        the dispatch overruns its deadline while holding a semaphore slot.
        The child is spawned in its own process group and the timer kills the
        group, so the pipe reaches EOF on time.
        """
        ready = tmp_path / "grandchild-ready"
        cli = self._fake_cli(
            tmp_path,
            "import json, subprocess, sys\nfrom pathlib import Path\n"
            # full default inheritance: the grandchild holds fd 1 and fd 2
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(json.dumps({'type': 'result', 'is_error': False, "
            "'result': 'ANSWER', 'session_id': 's4'}), flush=True)\n"
            f"Path({str(ready)!r}).touch()\n"
            "sys.exit(0)\n",
        )

        def wait_for_setup(proc):
            # on_proc runs before the dispatch timer starts. Separate process
            # startup from the one-second tree-cleanup deadline: interpreter
            # startup can itself consume that second on a busy machine.
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready.exists():
                from agent_dispatch.runner import _kill_process_tree

                _kill_process_tree(proc)
                proc.wait(timeout=5)
                pytest.fail("Test subprocess did not finish setting up its grandchild")

        agent = AgentConfig(directory=tmp_path, description="t", timeout=1)
        started = time.monotonic()
        with patch("agent_dispatch.runner.shutil.which", return_value=str(cli)):
            result = dispatch_stream("test", "hello", agent, Settings(), on_proc=wait_for_setup)
        elapsed = time.monotonic() - started
        assert result.success
        assert result.result == "ANSWER"
        assert elapsed < 10, f"overran its 1s timeout by {elapsed:.1f}s — the tree was not killed"

    def test_stderr_is_reported_when_no_result_line_arrives(self, tmp_path: Path):
        cli = self._fake_cli(
            tmp_path,
            "import sys\nsys.stderr.write('boom: everything is on fire\\n')\nsys.exit(3)\n",
        )
        agent = AgentConfig(directory=tmp_path, description="t", timeout=10)
        with patch("agent_dispatch.runner.shutil.which", return_value=str(cli)):
            result = dispatch_stream("test", "hello", agent, Settings())
        assert not result.success
        assert "everything is on fire" in result.error


    @staticmethod
    def _kill_pid_file(pid_file: Path) -> None:
        """Teardown for descendants a test leaves behind on purpose."""
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass

    def _stderr_holder_cli(self, tmp_path: Path, pid_file: Path, tail: str) -> Path:
        # The grandchild gets stdout=DEVNULL but inherits fd 2 — the shape of a
        # stdio MCP server that `claude` started and that outlives it.
        return self._fake_cli(
            tmp_path,
            "import json, subprocess, sys\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],\n"
            "                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n" + tail,
        )

    def test_ordinary_dispatch_does_not_wait_on_an_inherited_stderr(self, tmp_path):
        """Completion is stdout EOF + leader exit; a stderr holder must not stall it.

        Waiting for stderr EOF kept a cleanly finished dispatch until its
        deadline and then labelled the answer "killed after the timeout".
        """
        pid_file = tmp_path / "holder-pid"
        cli = self._stderr_holder_cli(
            tmp_path,
            pid_file,
            "print(json.dumps({'type': 'result', 'result': 'ANSWER', "
            "'is_error': False}), flush=True)\nsys.exit(0)\n",
        )
        agent = AgentConfig(directory=tmp_path, timeout=10)
        started = time.monotonic()
        try:
            with patch("agent_dispatch.runner.shutil.which", return_value=str(cli)):
                result = dispatch("test", "task", agent, Settings())
            elapsed = time.monotonic() - started
            assert result.success, result.error
            assert result.result == "ANSWER"
            assert result.hint is None  # not "killed after the timeout"
            assert elapsed < 2, f"dispatch waited {elapsed:.1f}s on a stderr holder"
        finally:
            self._kill_pid_file(pid_file)

    def test_ordinary_stderr_error_survives_an_inherited_stderr(self, tmp_path):
        """A real CLI failure reported on stderr (rc!=0) must not become a timeout."""
        pid_file = tmp_path / "holder-pid"
        cli = self._stderr_holder_cli(
            tmp_path,
            pid_file,
            "sys.stderr.write(\"You've hit your session limit \\u00b7 resets 6pm\\n\")\n"
            "sys.stderr.flush()\nsys.exit(1)\n",
        )
        agent = AgentConfig(directory=tmp_path, timeout=5)
        started = time.monotonic()
        try:
            with (
                patch("agent_dispatch.runner.shutil.which", return_value=str(cli)),
                # stdout is empty, so stderr is the only explanation and gets the
                # long join — shortened here only to keep the test fast.
                patch("agent_dispatch.runner._STDERR_JOIN_SECONDS", 0.3),
            ):
                result = dispatch("test", "task", agent, Settings())
            elapsed = time.monotonic() - started
            assert not result.success
            assert result.error_type == "usage_limit", result.error
            assert "session limit" in result.error
            assert elapsed < 3, f"dispatch waited {elapsed:.1f}s on a stderr holder"
        finally:
            self._kill_pid_file(pid_file)

    def _detached_stdout_holder_cli(self, tmp_path: Path, pid_file: Path, ready: Path) -> Path:
        # start_new_session: the grandchild leaves the dispatch's process group,
        # so killpg cannot reach it — and it inherits fd 1.
        return self._fake_cli(
            tmp_path,
            "import subprocess, sys, time\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "                         start_new_session=True)\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            f"Path({str(ready)!r}).touch()\n"
            "time.sleep(30)\n",
        )

    @staticmethod
    def _wait_ready(ready: Path, then=None):
        def on_proc(proc):
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready.exists():
                from agent_dispatch.runner import _kill_process_tree

                _kill_process_tree(proc)
                proc.wait(timeout=5)
                pytest.fail("Test subprocess did not finish setting up")
            if then is not None:
                then(proc)

        return on_proc

    def test_stream_timeout_is_bounded_with_a_detached_stdout_holder(self, tmp_path):
        """The timer's killpg cannot reach a detached descendant holding stdout.

        `for line in proc.stdout` then blocked for the descendant's whole life,
        and the caller's shared concurrency slot with it.
        """
        pid_file = tmp_path / "detached-pid"
        ready = tmp_path / "ready"
        cli = self._detached_stdout_holder_cli(tmp_path, pid_file, ready)
        agent = AgentConfig(directory=tmp_path, timeout=1)
        started = time.monotonic()
        try:
            with (
                patch("agent_dispatch.runner.shutil.which", return_value=str(cli)),
                patch("agent_dispatch.runner._STREAM_ORPHAN_GRACE_SECONDS", 0.3),
            ):
                result = dispatch_stream(
                    "test", "task", agent, Settings(), on_proc=self._wait_ready(ready)
                )
            elapsed = time.monotonic() - started
            assert result.error_type == "timeout", result.error
            assert result.session_id
            assert elapsed < 5, f"dispatch_stream overran its 1s timeout by {elapsed:.1f}s"
        finally:
            self._kill_pid_file(pid_file)

    def test_stream_cancel_is_bounded_with_a_detached_stdout_holder(self, tmp_path):
        """dispatch_cancel kills the tree from another thread — same unreachable pipe."""
        pid_file = tmp_path / "detached-pid"
        ready = tmp_path / "ready"
        cli = self._detached_stdout_holder_cli(tmp_path, pid_file, ready)
        agent = AgentConfig(directory=tmp_path, timeout=60)

        def cancel_soon(proc):
            from agent_dispatch.runner import _kill_process_tree

            threading.Timer(0.2, _kill_process_tree, args=(proc,)).start()

        started = time.monotonic()
        try:
            with (
                patch("agent_dispatch.runner.shutil.which", return_value=str(cli)),
                patch("agent_dispatch.runner._STREAM_ORPHAN_GRACE_SECONDS", 0.3),
            ):
                result = dispatch_stream(
                    "test", "task", agent, Settings(), on_proc=self._wait_ready(ready, cancel_soon)
                )
            elapsed = time.monotonic() - started
            assert not result.success
            assert elapsed < 5, f"cancelled dispatch_stream took {elapsed:.1f}s to return"
        finally:
            self._kill_pid_file(pid_file)


class TestKillProcessTreeReapedLeader:
    """killpg on a reaped leader's PID may signal an unrelated recycled group."""

    def test_reaped_leader_is_never_killpgd(self):
        from agent_dispatch.runner import _kill_process_tree

        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()
        with patch("agent_dispatch.runner.os.killpg") as spy:
            _kill_process_tree(proc)
        spy.assert_not_called()

    def test_waiting_for_the_leader_does_not_reap_it(self):
        """The guard above is only safe because the runner's waits keep the PID reserved."""
        from agent_dispatch.runner import _kill_process_tree, _wait_leader_exit

        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        try:
            assert _wait_leader_exit(proc, 5)
            assert proc.returncode is None  # exited, not reaped
            assert _wait_leader_exit(proc, 0)
            assert proc.returncode is None
            with patch("agent_dispatch.runner.os.killpg") as spy:
                _kill_process_tree(proc)
            spy.assert_called_once()  # the tree kill still reaches the group
        finally:
            proc.wait()
        assert proc.returncode == 0

    def test_wait_for_a_running_leader_times_out(self):
        from agent_dispatch.runner import _wait_leader_exit

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True
        )
        try:
            assert not _wait_leader_exit(proc, 0)
            assert not _wait_leader_exit(proc, 0.1)
        finally:
            proc.kill()
            proc.wait()


class TestDispatchPayloadRobustness:
    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_exit_zero_with_no_output_is_a_failure(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="   \n", stderr=""
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        # Reporting success here would hand back an empty answer AND cache it.
        assert not result.success
        assert result.error_type == "cli_error"
        assert result.session_id  # resumable

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_exit_zero_with_no_output_reports_stderr(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="model overloaded"
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert not result.success
        assert "model overloaded" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_non_object_json_falls_back_to_text(self, mock_run, _which):
        # Valid JSON but not the CLI's result object — every data.get() below
        # would raise AttributeError out of the runner.
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='"just a bare string"', stderr=""
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.success
        assert result.result == '"just a bare string"'

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_non_string_result_field_is_coerced(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"is_error": False, "result": {"nested": 1}}),
            stderr="",
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.success
        assert isinstance(result.result, str)
        assert "nested" in result.result

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_task_named_like_a_flag_does_not_corrupt_argv(self, mock_popen, _which):
        # cmd[2] is the caller-supplied prompt; searching the whole list for
        # "--output-format" would match it and rewrite the prompt instead.
        mock_popen.return_value = _FakePopen(
            [json.dumps({"type": "result", "is_error": False, "result": "ok"})]
        )
        result = dispatch_stream("test", "--output-format", self.agent, self.settings)
        cmd = mock_popen.call_args[0][0]
        assert cmd[2] == "--output-format"  # prompt untouched
        assert cmd[cmd.index("--output-format", 3) + 1] == "stream-json"
        assert result.success


class TestUndecodableOutput:
    """`text=True` decodes strictly: one bad byte used to raise UnicodeDecodeError
    (a ValueError — caught by nothing) out of an already-billed dispatch."""

    @staticmethod
    def _cli(tmp_path: Path, body: str) -> Path:
        script = tmp_path / "bad_cli.py"
        script.write_text(body)
        launcher = tmp_path / "bad_cli"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
        return launcher

    def test_dispatch_survives_an_undecodable_byte(self, tmp_path: Path):
        cli = self._cli(
            tmp_path,
            'import sys\nsys.stdout.buffer.write(b\'{"result":"caf\\xe9 ok","is_error":false}\')\n',
        )
        agent = AgentConfig(directory=tmp_path, description="t", timeout=10)
        with patch("agent_dispatch.runner.shutil.which", return_value=str(cli)):
            result = dispatch("test", "hello", agent, Settings())
        assert result.success
        assert "ok" in result.result  # the bad byte became U+FFFD, nothing raised

    def test_stream_survives_an_undecodable_byte(self, tmp_path: Path):
        cli = self._cli(
            tmp_path,
            "import sys\n"
            'sys.stdout.buffer.write(b\'{"type":"result","is_error":false,'
            '"result":"caf\\xe9 ok"}\\n\')\n',
        )
        agent = AgentConfig(directory=tmp_path, description="t", timeout=10)
        with patch("agent_dispatch.runner.shutil.which", return_value=str(cli)):
            result = dispatch_stream("test", "hello", agent, Settings())
        assert result.success
        assert "ok" in result.result


class TestSpawnFailureClassification:
    """dispatch_stream classified spawn errors; dispatch let them escape raw."""

    def setup_method(self):
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured", side_effect=FileNotFoundError("gone"))
    def test_missing_cwd_at_spawn_is_not_found(self, _run, _which):
        # is_dir() passing does not prove the child can chdir there, and the
        # directory can vanish between the check and the spawn.
        result = dispatch("test", "hi", AgentConfig(directory="/tmp", timeout=10), self.settings)
        assert not result.success
        assert result.error_type == "not_found"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured", side_effect=PermissionError("denied"))
    def test_unexecutable_cwd_is_permission(self, _run, _which):
        result = dispatch("test", "hi", AgentConfig(directory="/tmp", timeout=10), self.settings)
        assert not result.success
        assert result.error_type == "permission"
        assert "Hint" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured", side_effect=OSError("too many open files"))
    def test_other_oserror_is_cli_error(self, _run, _which):
        result = dispatch("test", "hi", AgentConfig(directory="/tmp", timeout=10), self.settings)
        assert not result.success
        assert result.error_type == "cli_error"


class TestSplitOutcome:
    """The trailing STATUS line the dispatch protocol asks for."""

    def test_plain_marker_is_lifted_and_removed(self):
        assert _split_outcome("All good.\n\nSTATUS: done") == ("All good.", "done")

    def test_case_and_markdown_decoration_are_tolerated(self):
        assert _split_outcome("x\n**Status: Partial**") == ("x", "partial")
        assert _split_outcome("x\n`STATUS: blocked`") == ("x", "blocked")

    def test_trailing_reason_keeps_the_line(self):
        text = "Ran 3 of 5 checks.\nSTATUS: partial — DB unreachable"
        assert _split_outcome(text) == (text, "partial")

    def test_only_the_last_line_counts(self):
        text = "The protocol says STATUS: done at the end.\nMore text."
        assert _split_outcome(text) == (text, None)

    def test_unknown_value_is_ignored(self):
        assert _split_outcome("x\nSTATUS: maybe") == ("x\nSTATUS: maybe", None)

    def test_empty_and_marker_only(self):
        assert _split_outcome("") == ("", None)
        assert _split_outcome("STATUS: done") == ("", "done")


class TestBuildSystemPrompt:
    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test")
        self.settings = Settings()

    def test_protocol_names_agent_caller_timeout_and_status(self):
        text = _build_system_prompt("infra", self.agent, self.settings, 600, caller="taylor")
        assert text.startswith("## Dispatch protocol")
        assert 'agent "infra"' in text
        assert 'dispatched by "taylor"' in text
        assert "about 600 seconds" in text
        assert "STATUS: done" in text and "STATUS: blocked" in text
        assert "Never end with a clarifying question" in text

    def test_json_mode_drops_the_status_and_lead_bullets(self):
        text = _build_system_prompt("a", self.agent, self.settings, 60, response_format="json")
        assert "STATUS:" not in text
        assert "lead with the outcome" not in text
        assert "non-interactively" in text  # the rest of the protocol stays

    def test_spend_cap_is_mentioned_when_set(self):
        agent = AgentConfig(directory="/tmp", max_budget_usd=1.5)
        assert "spend cap $1.5" in _build_system_prompt("a", agent, self.settings, 60)
        assert "spend cap" not in _build_system_prompt("a", self.agent, self.settings, 60)

    def test_instructions_follow_under_their_own_header(self):
        agent = AgentConfig(directory="/tmp", instructions="  Read-only SQL only.  ")
        text = _build_system_prompt("analytic", agent, self.settings, 60)
        assert text.index("## Dispatch protocol") < text.index(
            '## Standing instructions for "analytic"\nRead-only SQL only.'
        )

    def test_protocol_off_leaves_only_instructions(self):
        settings = Settings(dispatch_protocol=False)
        assert _build_system_prompt("a", self.agent, settings, 60) is None
        agent = AgentConfig(directory="/tmp", instructions="- bullet first")
        text = _build_system_prompt("a", agent, settings, 60)
        # Always starts with a header, never with "-": can't be parsed as a flag.
        assert text.startswith("## Standing instructions")
        assert "Dispatch protocol" not in text

    def test_command_carries_the_flag_only_when_there_is_text(self):
        cmd = _build_command("claude", "t", self.agent, self.settings, system_prompt="## P\nx")
        assert cmd[cmd.index("--append-system-prompt") + 1] == "## P\nx"
        cmd = _build_command("claude", "t", self.agent, self.settings, system_prompt=None)
        assert "--append-system-prompt" not in cmd

    def test_flag_is_passed_on_resume_too(self):
        cmd = _build_command(
            "claude", "t", self.agent, self.settings, "sess-1", system_prompt="## P"
        )
        assert "--resume" in cmd and "--append-system-prompt" in cmd


def _cli_json(text: str, **extra) -> str:
    payload = {"result": text, "session_id": "sess-1", "is_error": False, "total_cost_usd": 0.01}
    payload.update(extra)
    return json.dumps(payload)


class TestOutcomeThroughDispatch:
    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=42)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_sends_the_protocol(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_cli_json("ok\nSTATUS: done"), stderr=""
        )
        result = dispatch("infra", "hello", self.agent, self.settings, caller="taylor")
        cmd = mock_run.call_args[0][0]
        system_prompt = cmd[cmd.index("--append-system-prompt") + 1]
        assert 'agent "infra"' in system_prompt
        assert "about 42 seconds" in system_prompt
        assert '"taylor"' in system_prompt
        # The protocol lives in the system prompt; the task prompt keeps its
        # own (structured, caller-aware) shape and never contains it.
        assert cmd[2].endswith("## Task\nhello")
        assert "Dispatch protocol" not in cmd[2]
        assert result.outcome == "done"
        assert result.result == "ok"
        assert result.hint is None

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_protocol_off_sends_no_flag(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_cli_json("ok"), stderr=""
        )
        dispatch("infra", "hello", self.agent, Settings(dispatch_protocol=False))
        assert "--append-system-prompt" not in mock_run.call_args[0][0]

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_partial_and_blocked_carry_a_resume_hint(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_cli_json("Did half.\nSTATUS: partial"), stderr=""
        )
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert result.success and result.outcome == "partial"
        assert "PARTIAL" in result.hint
        assert "session_id='sess-1'" in result.hint

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_cli_json("Need DB creds.\nSTATUS: blocked"), stderr=""
        )
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert result.outcome == "blocked"
        assert "BLOCKED" in result.hint
        assert result.result == "Need DB creds."

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_outcome_hint_follows_the_denial_hint(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_cli_json(
                "x\nSTATUS: partial",
                permission_denials=[{"tool_name": "Bash", "tool_input": {}}],
            ),
            stderr="",
        )
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert result.denied_tools == ["Bash"]
        assert result.hint.index("Bash") < result.hint.index("PARTIAL")

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_status_line_in_json_mode_does_not_break_parsing(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_cli_json('{"n": 1}\nSTATUS: done'), stderr=""
        )
        result = dispatch("infra", "hello", self.agent, self.settings, response_format="json")
        assert result.parsed_result == {"n": 1}
        assert result.outcome == "done"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_plain_text_fallback_reports_outcome(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="plain answer\nSTATUS: blocked\n", stderr=""
        )
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert result.success and result.result == "plain answer"
        assert result.outcome == "blocked" and "BLOCKED" in result.hint

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_reports_outcome_and_sends_protocol(self, mock_popen, _which):
        line = json.dumps({"type": "result", "result": "half\nSTATUS: partial", "session_id": "s"})
        mock_popen.return_value = _FakePopen([line])
        result = dispatch_stream("infra", "go", self.agent, self.settings, caller="x")
        cmd = mock_popen.call_args[0][0]
        assert "--append-system-prompt" in cmd
        assert result.outcome == "partial" and result.result == "half"
        assert "PARTIAL" in result.hint

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_no_status_line_means_no_outcome(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_cli_json("just text"), stderr=""
        )
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert result.outcome is None
        assert "outcome" not in result.model_dump_json(exclude_none=True)


def _ok_stdout(**extra) -> str:
    payload = {"result": "ok", "is_error": False, "session_id": "s1", "total_cost_usd": 0.02}
    payload.update(extra)
    return json.dumps(payload)


class TestUsageJournaling:
    """Every dispatch lands in the usage journal — that is what makes spend visible."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_success_is_recorded_with_cost_duration_and_outcome(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_ok_stdout(result="done here\nSTATUS: done", duration_ms=4200, num_turns=3),
            stderr="",
        )
        dispatch("infra", "hello", self.agent, self.settings, caller="taylor")
        (entry,) = usage.load()
        assert entry["agent"] == "infra"
        assert entry["ok"] is True
        assert entry["cost"] == 0.02
        assert entry["ms"] == 4200
        assert entry["turns"] == 3
        assert entry["outcome"] == "done"
        assert entry["caller"] == "taylor"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_failures_are_recorded_with_their_error_type(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=10)
        dispatch("infra", "hello", self.agent, self.settings)
        (entry,) = usage.load()
        assert entry["ok"] is False
        assert entry["err"] == "timeout"
        # No CLI duration on a timeout: wall time stands in, so the agent still
        # shows up as "takes its whole budget" instead of as missing data.
        assert entry["ms"] >= 0

    @patch("agent_dispatch.runner.shutil.which", return_value=None)
    def test_even_a_pre_flight_failure_is_recorded(self, _which):
        dispatch("infra", "hello", self.agent, self.settings)
        (entry,) = usage.load()
        assert entry["err"] == "not_found"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_usage_log_false_records_nothing(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_ok_stdout(), stderr=""
        )
        dispatch("infra", "hello", self.agent, Settings(usage_log=False))
        assert usage.load() == []

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_records_once(self, mock_popen, _which):
        line = json.dumps({"type": "result", "result": "ok", "is_error": False})
        mock_popen.return_value = _FakePopen([line])
        dispatch_stream("infra", "go", self.agent, self.settings)
        assert len(usage.load()) == 1

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_the_old_cli_retry_records_one_dispatch_not_two(self, mock_popen, _which):
        # dispatch_stream re-enters itself for the no---session-id retry; without
        # the _use_session_flag guard one dispatch would be journalled twice and
        # every spend figure would drift high.
        line = json.dumps({"type": "result", "result": "ok", "is_error": False})
        rejected = _FakePopenWithStderr(
            [], returncode=1, stderr_text="error: unknown option '--session-id'"
        )
        mock_popen.side_effect = [rejected, _FakePopen([line])]
        result = dispatch_stream("infra", "go", self.agent, self.settings)
        assert result.success
        assert mock_popen.call_count == 2
        assert len(usage.load()) == 1

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_a_broken_journal_never_breaks_a_paid_dispatch(self, mock_run, _which, monkeypatch):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_ok_stdout(), stderr=""
        )

        def _explode(*_args, **_kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(usage, "record", _explode)
        # The wrapper must not let a journal failure destroy a completed result.
        with pytest.raises(OSError):
            usage.record("x", ok=True)  # sanity: the patch really raises
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert result.success


class TestUsageLimitClassification:
    """The account's own rate limit is its own error type, observed live."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    def test_classifier_recognizes_the_real_message(self):
        # Verbatim from claude 2.1.263 on 2026-09-09.
        assert (
            _classify_error("You've hit your session limit · resets 6pm (Asia/Almaty)")
            == "usage_limit"
        )
        assert _classify_error("Rate limit exceeded, try again later") == "usage_limit"
        assert _classify_error("429 too many requests") == "usage_limit"

    def test_a_permission_error_is_still_a_permission_error(self):
        assert _classify_error("permission denied for tool Bash") == "permission"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_the_hint_names_the_reset_and_the_account_wide_scope(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="You've hit your session limit · resets 6pm (Asia/Almaty)",
        )
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert result.error_type == "usage_limit"
        assert "resets 6pm" in result.error
        assert "ACCOUNT" in result.error
        # The actionable part: another agent is not a workaround.
        assert "different agent" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_the_stream_path_classifies_it_too(self, mock_popen, _which):
        mock_popen.return_value = _FakePopenWithStderr(
            [], returncode=1, stderr_text="You've hit your session limit · resets 6pm"
        )
        result = dispatch_stream("infra", "go", self.agent, self.settings)
        assert result.error_type == "usage_limit"
        assert "ACCOUNT" in result.error


class TestTimeoutSuggestion:
    """ "Use a longer timeout" becomes a number once there is history."""

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_suggests_a_value_derived_from_real_runs(self, mock_run, _which):
        for _ in range(5):
            usage.record("infra", ok=True, duration_ms=100_000)
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=10)
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert result.error_type == "timeout"
        assert "timeout_seconds=150" in result.error
        assert "--timeout 150" in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_falls_back_to_doubling_without_history(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=10)
        result = dispatch("infra", "hello", self.agent, self.settings)
        assert "--timeout 20" in result.error
        assert "suggest" not in result.error

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_a_suggestion_below_the_current_timeout_is_not_offered(self, mock_run, _which):
        # The agent normally answers in a second; this run failed for some other
        # reason, and telling the caller to *lower* the timeout would be wrong.
        for _ in range(5):
            usage.record("infra", ok=True, duration_ms=500)
        agent = AgentConfig(directory="/tmp", description="test", timeout=600)
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=600)
        result = dispatch("infra", "hello", agent, self.settings)
        assert "--timeout 1200" in result.error
        assert "suggest" not in result.error


class TestMalformedTelemetryKeepsThePaidResult:
    """Metadata fields come straight from the CLI's JSON. A malformed one must
    neither raise out of the runner nor discard the answer it rides on."""

    CASES = [
        {"duration_ms": 1234.5},
        {"duration_ms": "inf"},
        {"num_turns": "n/a"},
        {"num_turns": -1},
        {"session_id": 123},
        {"session_id": ""},
        {"session_id": {"nested": "x"}},
    ]

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @staticmethod
    def _generated_session(cmd: list[str]) -> str:
        return cmd[cmd.index("--session-id", 3) + 1]

    @pytest.mark.parametrize("extra", CASES)
    @pytest.mark.parametrize("is_error", [False, True], ids=["success", "error"])
    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch(self, mock_run, _which, extra, is_error):
        payload = {"type": "result", "is_error": is_error, "result": "paid answer", **extra}
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1 if is_error else 0, stdout=json.dumps(payload), stderr=""
        )
        result = dispatch("test", "hello", self.agent, self.settings)
        assert result.success is not is_error
        assert result.result == "paid answer"
        expected = self._generated_session(mock_run.call_args[0][0])
        if "session_id" in extra:
            assert result.session_id == expected  # still resumable
        assert isinstance(result.session_id, str) and result.session_id

    @pytest.mark.parametrize("extra", CASES)
    @pytest.mark.parametrize("is_error", [False, True], ids=["success", "error"])
    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_dispatch_stream(self, mock_popen, _which, extra, is_error):
        payload = {"type": "result", "is_error": is_error, "result": "paid answer", **extra}
        mock_popen.return_value = _FakePopen([json.dumps(payload)])
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert result.success is not is_error
        assert result.result == "paid answer"
        expected = self._generated_session(mock_popen.call_args[0][0])
        if "session_id" in extra:
            assert result.session_id == expected
        assert isinstance(result.session_id, str) and result.session_id

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_a_valid_reported_session_id_still_wins(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"is_error": False, "result": "ok", "session_id": "from-cli"}),
            stderr="",
        )
        assert dispatch("test", "hello", self.agent, self.settings).session_id == "from-cli"


class TestLoneSurrogatesAreScrubbedAtParse:
    """A ``\\udXXX`` escape without its pair is ASCII on the wire, so decoding
    with errors="replace" never sees it; json.loads manufactures an unencodable
    str. It must not reach DispatchResult, where model_dump_json raises."""

    # What the CLI (or an agent hand-escaping an emoji) actually prints.
    LONE = '"answer \\ud83d half"'

    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    @staticmethod
    def _serializable(result):
        result.model_dump_json()  # raised PydanticSerializationError before
        return result

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_dispatch_result_text(self, mock_run, _which):
        stdout = '{"type": "result", "is_error": false, "result": ' + self.LONE + "}"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""
        )
        result = self._serializable(dispatch("test", "t", self.agent, self.settings))
        assert result.success is True
        assert result.result == "answer � half"

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner._run_captured")
    def test_json_response_built_from_ascii_text(self, mock_run, _which):
        # The likelier vector: result is clean ASCII, the *second* parse
        # (response_format="json") is what creates the surrogate.
        answer = '{"icon": "\\ud83d", "pair": "\\ud83d\\ude00"}'
        stdout = json.dumps({"type": "result", "is_error": False, "result": answer})
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""
        )
        result = self._serializable(
            dispatch("test", "t", self.agent, self.settings, response_format="json")
        )
        assert result.parsed_result == {"icon": "�", "pair": "\U0001f600"}

    @patch("agent_dispatch.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("agent_dispatch.runner.subprocess.Popen")
    def test_stream_result_and_progress(self, mock_popen, _which):
        assistant = (
            '{"type": "assistant", "message": {"content": '
            '[{"type": "text", "text": ' + self.LONE + "}]}}"
        )
        final = '{"type": "result", "is_error": false, "result": ' + self.LONE + "}"
        mock_popen.return_value = _FakePopen([assistant, final])
        progress: list[str] = []
        result = self._serializable(
            dispatch_stream("test", "t", self.agent, self.settings, on_progress=progress.append)
        )
        assert result.result == "answer � half"
        for line in progress:
            line.encode("utf-8")
