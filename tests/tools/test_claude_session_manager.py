"""Tests for tools/claude_session/manager.py"""

import json
import time
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from tools.claude_session.manager import ClaudeSessionManager, Turn, ToolCall


@pytest.fixture
def manager():
    return ClaudeSessionManager()


class TestStart:
    @patch("tools.claude_session.manager.TmuxInterface")
    def test_start_creates_session(self, MockTmux, manager):
        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = False
        mock_tmux.capture_pane.return_value = "❯ "
        MockTmux.return_value = mock_tmux

        result = manager.start(workdir="/tmp/test")
        assert "session_id" in result
        assert result["session_id"].startswith("cs_")

    @patch("tools.claude_session.manager.TmuxInterface")
    def test_start_idempotent(self, MockTmux, manager):
        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = True
        MockTmux.return_value = mock_tmux

        r1 = manager.start(workdir="/tmp/test")
        r2 = manager.start(workdir="/tmp/test")
        assert "note" in r2 or r2.get("session_id") == r1.get("session_id")

    def test_start_invalid_permission(self, manager):
        result = manager.start(workdir="/tmp/test", permission_mode="invalid")
        assert "error" in result


class TestSend:
    def test_send_no_session(self, manager):
        result = manager.send("hello")
        assert "error" in result

    @patch("tools.claude_session.manager.TmuxInterface")
    def test_send_creates_turn(self, MockTmux, manager):
        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = True
        mock_tmux.capture_pane.return_value = "❯ "
        MockTmux.return_value = mock_tmux

        manager.start(workdir="/tmp/test")
        # Reset to IDLE for send
        manager._sm.transition("IDLE")
        result = manager.send("Fix the bug")
        assert result["sent"] is True
        assert manager._current_turn is not None
        assert manager._current_turn.user_message == "Fix the bug"


class TestWaitForIdle:
    def test_already_idle(self, manager):
        manager._session_active = True
        manager._sm.transition("IDLE")
        manager._turn_history.append(Turn(
            turn_id=1, start_time=time.monotonic(), end_time=time.monotonic(),
            state="IDLE", user_message="test", tool_calls=[],
            thinking_cycles=0, total_duration=1.0,
        ))
        result = manager.wait_for_idle(timeout=5)
        assert result["state"] == "IDLE"

    def test_timeout(self, manager):
        manager._session_active = True
        manager._sm.transition("THINKING")
        manager._current_turn = Turn(
            turn_id=1, start_time=time.monotonic(), end_time=None,
            state="THINKING", user_message="test", tool_calls=[],
            thinking_cycles=0, total_duration=None,
        )
        result = manager.wait_for_idle(timeout=1)
        assert result.get("timeout_reached") is True

    def test_no_session(self, manager):
        result = manager.wait_for_idle(timeout=1)
        assert "error" in result


class TestStatus:
    def test_status_no_session(self, manager):
        result = manager.status()
        assert result["state"] == "DISCONNECTED"

    @patch("tools.claude_session.manager.TmuxInterface")
    def test_status_with_session(self, MockTmux, manager):
        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = True
        MockTmux.return_value = mock_tmux

        manager.start(workdir="/tmp/test")
        manager._sm.transition("IDLE")
        manager._sm.transition("TOOL_CALL")
        result = manager.status()
        assert result["state"] == "TOOL_CALL"
        assert "state_duration_seconds" in result


class TestStop:
    def test_stop_no_session(self, manager):
        result = manager.stop()
        assert "error" in result

    @patch("tools.claude_session.manager.TmuxInterface")
    def test_stop_active_session(self, MockTmux, manager):
        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = True
        MockTmux.return_value = mock_tmux

        manager.start(workdir="/tmp/test")
        result = manager.stop()
        assert result["stopped"] is True
        assert "session_id" in result


class TestOutput:
    def test_output_no_session(self, manager):
        result = manager.output()
        assert "lines" in result

    def test_output_with_data(self, manager):
        manager._session_active = True
        manager._buf.append("line1")
        manager._buf.append("line2")
        result = manager.output(offset=0, limit=10)
        assert len(result["lines"]) == 2
        assert result["total"] == 2


class TestHistory:
    def test_empty_history(self, manager):
        result = manager.history()
        assert result["total_turns"] == 0

    def test_with_completed_turns(self, manager):
        t = Turn(
            turn_id=1, start_time=time.monotonic(), end_time=time.monotonic(),
            state="IDLE", user_message="test msg", tool_calls=[],
            thinking_cycles=1, total_duration=2.0,
        )
        manager._turn_history.append(t)
        result = manager.history()
        assert result["total_turns"] == 1
        assert result["turns"][0]["message"] == "test msg"


class TestRespondPermission:
    def test_not_in_permission_state(self, manager):
        result = manager.respond_permission("allow")
        assert "error" in result

    def test_invalid_response(self, manager):
        manager._session_active = True
        manager._sm.transition("IDLE")
        manager._sm.transition("PERMISSION")
        manager._tmux = MagicMock()
        result = manager.respond_permission("maybe")
        assert "error" in result


class TestTypeText:
    def test_no_session(self, manager):
        result = manager.type_text("hello")
        assert "error" in result

    @patch("tools.claude_session.manager.TmuxInterface")
    def test_type_enters_inputting(self, MockTmux, manager):
        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = True
        MockTmux.return_value = mock_tmux

        manager.start(workdir="/tmp/test")
        manager._sm.transition("IDLE")
        result = manager.type_text("some code here")
        assert result["typed"] is True
        assert result["state"] == "INPUTTING"


class TestSubmit:
    def test_no_session(self, manager):
        result = manager.submit()
        assert "error" in result

    @patch("tools.claude_session.manager.TmuxInterface")
    def test_submit_creates_turn(self, MockTmux, manager):
        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = True
        # Return thinking state so poll doesn't finalize the turn immediately
        mock_tmux.capture_pane.return_value = "processing..."
        MockTmux.return_value = mock_tmux

        manager.start(workdir="/tmp/test")
        manager._sm.transition("IDLE")
        manager.type_text("some code")
        result = manager.submit()
        assert result["submitted"] is True
        assert manager._current_turn is not None


class TestCancelInput:
    def test_no_session(self, manager):
        result = manager.cancel_input()
        assert "error" in result

    @patch("tools.claude_session.manager.TmuxInterface")
    def test_cancel_returns_to_idle(self, MockTmux, manager):
        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = True
        MockTmux.return_value = mock_tmux

        manager.start(workdir="/tmp/test")
        manager._sm.transition("IDLE")
        manager.type_text("oops")
        result = manager.cancel_input()
        assert result["cancelled"] is True
        assert result["state"] == "IDLE"


class TestEvents:
    def test_events_empty(self, manager):
        result = manager.events()
        assert result["events"] == []

    def test_events_with_data(self, manager):
        manager._event_queue.put({"type": "state_changed", "turn_id": 1})
        manager._event_queue.put({"type": "turn_completed", "turn_id": 2})
        result = manager.events(since_turn=2)
        assert len(result["events"]) == 1
        assert result["events"][0]["turn_id"] == 2


class TestBuildPermissionResult:
    def test_extracts_permission_text(self, manager):
        manager._session_active = True
        manager._sm.transition("IDLE")
        manager._sm.transition("PERMISSION")
        manager._current_turn = Turn(
            turn_id=1, start_time=time.monotonic(),
            user_message="test", tool_calls=[],
        )
        manager._buf.append("some output")
        manager._buf.append("Allow Edit to auth.py?")
        result = manager._build_permission_result()
        assert result["state"] == "PERMISSION"
        assert "permission_request" in result
        assert "Allow Edit" in result["permission_request"]


class TestBuildErrorResult:
    def test_includes_error_output(self, manager):
        manager._session_active = True
        manager._sm.transition("IDLE")
        manager._sm.transition("ERROR")
        manager._current_turn = Turn(
            turn_id=1, start_time=time.monotonic(),
            user_message="test", tool_calls=[],
        )
        manager._buf.append("Error: something broke")
        result = manager._build_error_result()
        assert result["state"] == "ERROR"
        assert "error_output" in result
        assert "something broke" in result["error_output"]
