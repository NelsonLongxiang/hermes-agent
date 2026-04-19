"""Tests for tools/claude_session_tool.py — Tool registration and dispatch."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock
from tools.claude_session_tool import (
    CLAUDE_SESSION_SCHEMA, _handle_claude_session,
    _check_claude_session, _diagnose_claude_session,
)


class TestSchema:
    def test_schema_has_name(self):
        assert CLAUDE_SESSION_SCHEMA["name"] == "claude_session"

    def test_schema_has_required_action(self):
        assert "action" in CLAUDE_SESSION_SCHEMA["parameters"]["required"]

    def test_schema_action_enum(self):
        actions = CLAUDE_SESSION_SCHEMA["parameters"]["properties"]["action"]["enum"]
        expected = [
            "start", "send", "type", "submit", "cancel_input",
            "status", "wait_for_idle", "wait_for_state",
            "output", "respond_permission", "stop", "history", "events",
            "diagnose",
        ]
        assert set(actions) == set(expected)


class TestHandlerDispatch:
    def test_status_no_session(self):
        result = _handle_claude_session({"action": "status"})
        data = json.loads(result)
        assert data["state"] == "DISCONNECTED"

    def test_unknown_action(self):
        result = _handle_claude_session({"action": "nonexistent"})
        data = json.loads(result)
        assert "error" in data

    def test_send_no_session(self):
        result = _handle_claude_session({"action": "send", "message": "test"})
        data = json.loads(result)
        assert "error" in data

    def test_stop_no_session(self):
        result = _handle_claude_session({"action": "stop"})
        data = json.loads(result)
        assert "error" in data

    def test_send_missing_message(self):
        result = _handle_claude_session({"action": "send"})
        data = json.loads(result)
        assert "error" in data

    def test_type_missing_text(self):
        result = _handle_claude_session({"action": "type"})
        data = json.loads(result)
        assert "error" in data

    def test_wait_for_state_missing_target(self):
        result = _handle_claude_session({"action": "wait_for_state"})
        data = json.loads(result)
        assert "error" in data

    def test_respond_permission_missing_response(self):
        result = _handle_claude_session({"action": "respond_permission"})
        data = json.loads(result)
        assert "error" in data

    def test_history_no_session(self):
        result = _handle_claude_session({"action": "history"})
        data = json.loads(result)
        assert data["total_turns"] == 0

    def test_events_no_session(self):
        result = _handle_claude_session({"action": "events"})
        data = json.loads(result)
        assert data["events"] == []

    def test_output_no_session(self):
        result = _handle_claude_session({"action": "output"})
        data = json.loads(result)
        assert "lines" in data


class TestToolRegistration:
    def test_tool_registered(self):
        """Verify the tool is discoverable in the registry."""
        from tools.registry import registry
        entry = registry.get_entry("claude_session")
        assert entry is not None
        assert entry.toolset == "claude_session"
        assert entry.emoji == "🤖"

    def test_schema_matches_registry(self):
        from tools.registry import registry
        entry = registry.get_entry("claude_session")
        assert entry is not None
        assert entry.schema["name"] == "claude_session"


class TestCheckFn:
    """Tests for _check_claude_session availability check."""

    def test_returns_true_when_tmux_available(self):
        with patch("tools.claude_session_tool.shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: "/usr/bin/tmux" if cmd == "tmux" else None
            assert _check_claude_session() is True

    def test_returns_false_when_tmux_missing(self):
        with patch("tools.claude_session_tool.shutil.which") as mock_which:
            mock_which.return_value = None
            assert _check_claude_session() is False

    def test_logs_warning_when_claude_missing(self):
        with patch("tools.claude_session_tool.shutil.which") as mock_which:
            with patch("tools.claude_session_tool.logger") as mock_logger:
                mock_which.side_effect = lambda cmd: "/usr/bin/tmux" if cmd == "tmux" else None
                _check_claude_session()
                mock_logger.warning.assert_called_once()
                assert "Claude Code CLI not found" in mock_logger.warning.call_args[0][0]

    def test_returns_true_even_when_claude_missing(self):
        """tmux is the hard dep; claude CLI is soft — still registers."""
        with patch("tools.claude_session_tool.shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: "/usr/bin/tmux" if cmd == "tmux" else None
            assert _check_claude_session() is True


class TestDiagnose:
    """Tests for _diagnose_claude_session and the diagnose action."""

    def test_diagnose_function_all_ok(self):
        with patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
            assert result["status"] == "ready"
            assert len(result["checks"]) == 5

    def test_diagnose_function_missing_deps(self):
        with patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {}, clear=True):
            mock_which.return_value = None
            result = _diagnose_claude_session()
            assert result["status"] == "missing_deps"
            dep_names = [c["dependency"] for c in result["checks"]]
            assert "tmux" in dep_names
            assert "Claude Code CLI" in dep_names

    def test_diagnose_action_dispatch(self):
        """diagnose action should return JSON via handler."""
        with patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _handle_claude_session({"action": "diagnose"})
            data = json.loads(result)
            assert data["status"] == "ready"

    def test_diagnose_timeout_too_low(self):
        with patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "120"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
            timeout_check = next(c for c in result["checks"] if c["dependency"] == "HERMES_STREAM_STALE_TIMEOUT")
            assert timeout_check["status"] == "too_low"

    def test_diagnose_timeout_not_set(self):
        with patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {}, clear=True):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
            timeout_check = next(c for c in result["checks"] if c["dependency"] == "HERMES_STREAM_STALE_TIMEOUT")
            assert timeout_check["status"] == "not_set"

    def test_diagnose_has_hints_for_missing(self):
        with patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {}, clear=True):
            mock_which.return_value = None
            result = _diagnose_claude_session()
            for check in result["checks"]:
                if check["status"] == "missing":
                    assert check.get("hint") is not None
                    assert len(check["hint"]) > 0
