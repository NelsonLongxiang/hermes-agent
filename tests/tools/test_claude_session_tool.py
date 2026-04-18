"""Tests for tools/claude_session_tool.py — Tool registration and dispatch."""

import json
import pytest
from unittest.mock import patch, MagicMock
from tools.claude_session_tool import CLAUDE_SESSION_SCHEMA, _handle_claude_session


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
