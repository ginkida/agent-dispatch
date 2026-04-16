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
    _build_command,
    _build_prompt,
    _check_recursion,
    _classify_error,
    _current_depth,
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
