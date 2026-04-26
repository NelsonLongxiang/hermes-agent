"""Tests for tools/claude_session_tool.py — Tool registration and dispatch."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock
from tools.claude_session_tool import (
    CLAUDE_SESSION_SCHEMA, _handle_claude_session,
    _check_claude_session, _diagnose_claude_session,
    _extract_mcp_failure_count, _get_active_sessions_output,
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
            "diagnose", "doctor_fix",
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
            assert len(result["checks"]) == 6

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


class TestExtractMcpFailureCount:
    """Tests for _extract_mcp_failure_count pure function."""

    def test_single_server_failure(self):
        text = "2 MCP servers failed · /mcp"
        assert _extract_mcp_failure_count(text) == 2

    def test_single_server_singular(self):
        text = "1 MCP server failed · /mcp"
        assert _extract_mcp_failure_count(text) == 1

    def test_no_failure(self):
        text = "All systems operational"
        assert _extract_mcp_failure_count(text) == 0

    def test_empty_string(self):
        assert _extract_mcp_failure_count("") == 0

    def test_large_count(self):
        text = "15 MCP servers failed · /mcp"
        assert _extract_mcp_failure_count(text) == 15


class TestSessionDiagnoseChecks:
    """Tests for session-level diagnose checks (THINKING, bypass, MCP)."""

    def _mock_session(self, state, duration, output_tail=""):
        """Build a mock session info dict for _get_active_sessions_output."""
        return [{
            "session_id": "abcd1234efgh5678",
            "state": state,
            "state_duration_seconds": duration,
            "output_tail": output_tail,
        }]

    def test_thinking_critical(self):
        """THINKING >300s → status='session_issues' with critical check."""
        with patch("tools.claude_session_tool._get_active_sessions_output") as mock_sessions, \
             patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            mock_sessions.return_value = self._mock_session("THINKING", 350.0)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
            assert result["status"] == "session_issues"
            thinking_checks = [c for c in result["checks"]
                               if "THINKING duration" in c.get("dependency", "")]
            assert len(thinking_checks) == 1
            assert thinking_checks[0]["status"] == "critical"

    def test_thinking_warning(self):
        """THINKING >120s but <300s → status='ready' with warning check."""
        with patch("tools.claude_session_tool._get_active_sessions_output") as mock_sessions, \
             patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            mock_sessions.return_value = self._mock_session("THINKING", 150.0)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
            assert result["status"] == "ready"
            thinking_checks = [c for c in result["checks"]
                               if "THINKING duration" in c.get("dependency", "")]
            assert len(thinking_checks) == 1
            assert thinking_checks[0]["status"] == "warning"

    def test_bypass_permissions_hang(self):
        """3+ 'bypass permissions on' → critical startup hang."""
        bypass_text = (
            "bypass permissions on (shift+tab to cycle)\n"
            "bypass permissions on (shift+tab to cycle)\n"
            "bypass permissions on (shift+tab to cycle)\n"
        )
        with patch("tools.claude_session_tool._get_active_sessions_output") as mock_sessions, \
             patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            mock_sessions.return_value = self._mock_session("READY", 5.0, bypass_text)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
            assert result["status"] == "session_issues"
            hang_checks = [c for c in result["checks"]
                           if "startup hang" in c.get("dependency", "")]
            assert len(hang_checks) == 1
            assert hang_checks[0]["status"] == "critical"

    def test_mcp_failure(self):
        """MCP server failure text → warning check."""
        mcp_text = "Some output\n2 MCP servers failed · /mcp\nMore output"
        with patch("tools.claude_session_tool._get_active_sessions_output") as mock_sessions, \
             patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            mock_sessions.return_value = self._mock_session("READY", 5.0, mcp_text)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
            mcp_checks = [c for c in result["checks"]
                          if "MCP servers" in c.get("dependency", "")]
            assert len(mcp_checks) == 1
            assert mcp_checks[0]["status"] == "warning"

    def test_no_sessions_ready_status(self):
        """No active sessions → status='ready' (no session-level checks)."""
        with patch("tools.claude_session_tool._get_active_sessions_output") as mock_sessions, \
             patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            mock_sessions.return_value = []
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
            assert result["status"] == "ready"

    def test_cli_migration_prompt(self):
        """CLI migration prompt → info check."""
        migration_text = "switched from npm to native installer\nSome other output"
        with patch("tools.claude_session_tool._get_active_sessions_output") as mock_sessions, \
             patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            mock_sessions.return_value = self._mock_session("READY", 5.0, migration_text)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
        cli_checks = [c for c in result["checks"]
                      if "CLI migration" in c.get("dependency", "")]
        assert len(cli_checks) == 1
        assert cli_checks[0]["status"] == "info"

    def test_tmux_focus_events_off(self):
        """tmux focus-events off → info check."""
        tmux_text = "tmux focus-events off · add 'set -g focus-events on'\nSome output"
        with patch("tools.claude_session_tool._get_active_sessions_output") as mock_sessions, \
             patch("tools.claude_session_tool.shutil.which") as mock_which, \
             patch.dict(os.environ, {"HERMES_STREAM_STALE_TIMEOUT": "300"}):
            mock_which.side_effect = lambda cmd: {
                "tmux": "/usr/bin/tmux",
                "claude": "/usr/local/bin/claude",
            }.get(cmd)
            mock_sessions.return_value = self._mock_session("READY", 5.0, tmux_text)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="tmux 3.4")
                result = _diagnose_claude_session()
        tmux_checks = [c for c in result["checks"]
                       if "tmux config" in c.get("dependency", "")]
        assert len(tmux_checks) == 1
        assert tmux_checks[0]["status"] == "info"
