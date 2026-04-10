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
    _current_depth,
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


class TestDispatch:
    def setup_method(self):
        self.agent = AgentConfig(directory="/tmp", description="test", timeout=10)
        self.settings = Settings()

    def test_missing_directory(self, tmp_path: Path):
        agent = AgentConfig(directory=tmp_path / "nonexistent", description="test")
        result = dispatch("test", "hello", agent, self.settings)
        assert not result.success
        assert "does not exist" in result.error

    def test_recursion_exceeded(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "5"}):
            result = dispatch("test", "hello", self.agent, self.settings)
            assert not result.success
            assert "depth" in result.error.lower()

    @patch("agent_dispatch.runner.shutil.which", return_value=None)
    def test_claude_not_found(self, _mock):
        result = dispatch("test", "hello", self.agent, self.settings)
        assert not result.success
        assert "not found" in result.error.lower()

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

    def test_missing_directory(self, tmp_path: Path):
        agent = AgentConfig(directory=tmp_path / "nonexistent", description="test")
        result = dispatch_stream("test", "hello", agent, self.settings)
        assert not result.success
        assert "does not exist" in result.error

    def test_recursion_exceeded(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "5"}):
            result = dispatch_stream("test", "hello", self.agent, self.settings)
            assert not result.success
            assert "depth" in result.error.lower()

    @patch("agent_dispatch.runner.shutil.which", return_value=None)
    def test_claude_not_found(self, _mock):
        result = dispatch_stream("test", "hello", self.agent, self.settings)
        assert not result.success
        assert "not found" in result.error.lower()

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
