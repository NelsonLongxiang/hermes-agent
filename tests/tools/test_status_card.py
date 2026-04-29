"""Tests for status_card.py — JSONL parsing, formatting, and StatusCard lifecycle."""

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.claude_session.status_card import (
    StatusCard,
    _format_tool_detail,
    format_status_card,
    get_jsonl_path,
    parse_jsonl,
)


# ---------------------------------------------------------------------------
# get_jsonl_path
# ---------------------------------------------------------------------------

class TestGetJsonlPath:
    def test_correct_path_structure(self):
        """get_jsonl_path returns a path under ~/.claude/projects/"""
        uuid = "deadbeef-1234-5678-abcd-ef0123456789"
        path = get_jsonl_path(uuid)
        assert path.name == f"{uuid}.jsonl"
        assert ".claude" in str(path)
        assert "projects" in str(path)

    def test_invalid_uuid_returns_fallback(self):
        """Non-UUID input returns a fallback path."""
        path = get_jsonl_path("abc-123")
        assert path.name == "abc-123.jsonl"

    def test_returns_path_object(self):
        result = get_jsonl_path("uuid")
        assert isinstance(result, Path)

    def test_preserves_uuid(self):
        result = get_jsonl_path("deadbeef-1234-5678-abcd-ef0123456789")
        assert result.name == "deadbeef-1234-5678-abcd-ef0123456789.jsonl"


# ---------------------------------------------------------------------------
# parse_jsonl
# ---------------------------------------------------------------------------

class TestParseJsonl:
    def test_file_not_exists(self, tmp_path):
        result = parse_jsonl(tmp_path / "nonexistent.jsonl")
        assert result == {"status": "no_session"}

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        result = parse_jsonl(p)
        assert result == {"status": "empty"}

    def test_file_with_only_whitespace(self, tmp_path):
        p = tmp_path / "blank.jsonl"
        p.write_text("  \n\n  \n")
        result = parse_jsonl(p)
        assert result == {"status": "empty"}

    def test_invalid_json_lines_skipped(self, tmp_path):
        p = tmp_path / "mixed.jsonl"
        p.write_text("not json\n{bad json}\n")
        result = parse_jsonl(p)
        assert result == {"status": "empty"}

    def test_assistant_and_user_counts(self, tmp_path):
        p = tmp_path / "session.jsonl"
        entries = [
            {"type": "assistant", "timestamp": "2026-01-01T00:00:00", "message": {"content": []}},
            {"type": "user", "timestamp": "2026-01-01T00:01:00", "message": {"content": []}},
            {"type": "assistant", "timestamp": "2026-01-01T00:02:00", "message": {"content": []}},
            {"type": "user", "timestamp": "2026-01-01T00:03:00", "message": {"content": []}},
        ]
        p.write_text("\n".join(json.dumps(e) for e in entries))
        result = parse_jsonl(p)
        assert result["status"] == "active"
        assert result["assistant_count"] == 2
        assert result["user_count"] == 2
        assert result["total_entries"] == 4

    def test_latest_meaningful_entry_is_last_user(self, tmp_path):
        p = tmp_path / "session.jsonl"
        entries = [
            {"type": "assistant", "timestamp": "2026-01-01T00:00:00", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:01:00", "message": {"content": [{"type": "text", "text": "world"}]}},
        ]
        p.write_text("\n".join(json.dumps(e) for e in entries))
        result = parse_jsonl(p)
        assert result["entry_type"] == "user"
        assert result["timestamp"] == "2026-01-01T00:01:00"

    def test_latest_meaningful_entry_walks_backwards(self, tmp_path):
        p = tmp_path / "session.jsonl"
        entries = [
            {"type": "system", "timestamp": "2026-01-01T00:00:00"},
            {"type": "user", "timestamp": "2026-01-01T00:01:00", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "system", "timestamp": "2026-01-01T00:02:00"},
        ]
        p.write_text("\n".join(json.dumps(e) for e in entries))
        result = parse_jsonl(p)
        assert result["entry_type"] == "user"
        assert result["timestamp"] == "2026-01-01T00:01:00"

    def test_assistant_entry_with_tool_use(self, tmp_path):
        p = tmp_path / "session.jsonl"
        entries = [
            {"type": "assistant", "timestamp": "2026-01-01T00:00:00", "message": {
                "model": "claude-4",
                "content": [
                    {"type": "text", "text": "Let me read that file"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/test.py"}},
                ],
            }},
        ]
        p.write_text(json.dumps(entries[0]))
        result = parse_jsonl(p)
        assert result["tool"] == "Read"
        assert result["tool_input"] == {"file_path": "/tmp/test.py"}
        assert result["model"] == "claude-4"

    def test_user_entry_with_tool_result(self, tmp_path):
        p = tmp_path / "session.jsonl"
        entries = [
            {"type": "user", "timestamp": "2026-01-01T00:00:00", "message": {
                "content": [{"type": "tool_result", "content": "file contents here"}],
            }},
        ]
        p.write_text(json.dumps(entries[0]))
        result = parse_jsonl(p)
        assert result["tool_result"] == "file contents here"

    def test_text_truncated_to_200_chars(self, tmp_path):
        long_text = "x" * 500
        p = tmp_path / "session.jsonl"
        entry = {"type": "assistant", "timestamp": "2026-01-01T00:00:00", "message": {
            "content": [{"type": "text", "text": long_text}],
        }}
        p.write_text(json.dumps(entry))
        result = parse_jsonl(p)
        assert len(result["text"]) == 200

    def test_tool_result_truncated_to_200_chars(self, tmp_path):
        long_result = "y" * 500
        p = tmp_path / "session.jsonl"
        entry = {"type": "user", "timestamp": "2026-01-01T00:00:00", "message": {
            "content": [{"type": "tool_result", "content": long_result}],
        }}
        p.write_text(json.dumps(entry))
        result = parse_jsonl(p)
        assert len(result["tool_result"]) == 200

    def test_mixed_valid_invalid_lines(self, tmp_path):
        p = tmp_path / "session.jsonl"
        lines = [
            "garbage",
            json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00", "message": {"content": [{"type": "text", "text": "hi"}]}}),
            "",
            "more garbage",
            json.dumps({"type": "user", "timestamp": "2026-01-01T00:01:00", "message": {"content": []}}),
        ]
        p.write_text("\n".join(lines))
        result = parse_jsonl(p)
        assert result["status"] == "active"
        assert result["total_entries"] == 2
        assert result["assistant_count"] == 1
        assert result["user_count"] == 1

    def test_non_dict_content_items_skipped(self, tmp_path):
        p = tmp_path / "session.jsonl"
        entry = {"type": "assistant", "timestamp": "2026-01-01T00:00:00", "message": {
            "content": ["not a dict", {"type": "text", "text": "valid text"}],
        }}
        p.write_text(json.dumps(entry))
        result = parse_jsonl(p)
        assert result["text"] == "valid text"


# ---------------------------------------------------------------------------
# format_status_card
# ---------------------------------------------------------------------------

class TestFormatStatusCard:
    def test_no_session(self):
        assert format_status_card({"status": "no_session"}) == "⏳ Starting session..."

    def test_empty(self):
        assert format_status_card({"status": "empty"}) == "⏳ Waiting for activity..."

    def test_thinking_state(self):
        result = format_status_card({"status": "active", "state": "THINKING"})
        assert "🤔 Thinking..." in result

    def test_tool_call_state(self):
        result = format_status_card({"status": "active", "state": "TOOL_CALL"})
        assert "🔧 Working..." in result

    def test_permission_state(self):
        result = format_status_card({"status": "active", "state": "PERMISSION"})
        assert "⏸️ Waiting for permission" in result

    def test_idle_state(self):
        result = format_status_card({"status": "active", "state": "IDLE"})
        assert "✅ Idle" in result

    def test_default_state_is_idle(self):
        result = format_status_card({"status": "active"})
        assert "✅ Idle" in result

    def test_message_counts(self):
        result = format_status_card({"status": "active", "assistant_count": 5, "user_count": 3})
        assert "💬 5 / 3" in result

    def test_tool_with_icon(self):
        result = format_status_card({
            "status": "active",
            "tool": "Read",
            "tool_input": {"file_path": "/some/path.py"},
        })
        assert "📖 Read:" in result

    def test_unknown_tool_gets_default_icon(self):
        result = format_status_card({
            "status": "active",
            "tool": "UnknownTool",
            "tool_input": {},
        })
        assert "🔧 UnknownTool" in result

    def test_text_preview_truncated_to_80(self):
        long_text = "a" * 200
        result = format_status_card({"status": "active", "text": long_text})
        # preview should be "a"*80 + "..."
        assert "💭 " in result
        for line in result.split("\n"):
            if line.startswith("💭 "):
                assert len(line) <= 86  # "💭 " (4 chars including emoji) + 80 + "..."

    def test_text_preview_short_text_unchanged(self):
        result = format_status_card({"status": "active", "text": "short"})
        assert "💭 short" in result

    def test_text_preview_first_line_only(self):
        result = format_status_card({"status": "active", "text": "line1\nline2\nline3"})
        assert "💭 line1" in result
        assert "line2" not in result

    def test_max_length_truncation(self):
        result = format_status_card(
            {"status": "active", "text": "x" * 1000},
            max_length=50,
        )
        assert len(result) == 50
        assert result.endswith("...")

    def test_max_length_not_truncated_when_short(self):
        result = format_status_card({"status": "active"}, max_length=500)
        assert len(result) < 500

    def test_observer_state_overrides(self):
        state = {"status": "active", "state": "IDLE", "assistant_count": 1, "user_count": 1}
        observer = {"state": "TOOL_CALL", "current_activity": "writing", "assistant_count": 5, "user_count": 3}
        result = format_status_card(state, observer_state=observer)
        assert "🔧 Working..." in result
        assert "💬 5 / 3" in result
        assert "⚡ writing" in result

    def test_observer_activity_with_detail(self):
        observer = {"current_activity": "reading", "activity_detail": "auth.py"}
        result = format_status_card({"status": "active"}, observer_state=observer)
        assert "⚡ reading: auth.py" in result

    def test_observer_activity_idle_hidden(self):
        observer = {"current_activity": "idle"}
        result = format_status_card({"status": "active"}, observer_state=observer)
        assert "⚡ idle" not in result

    def test_observer_recent_output_used(self):
        state = {"status": "active", "text": "old text"}
        observer = {"recent_output": "new output from observer"}
        result = format_status_card(state, observer_state=observer)
        assert "💭 new output from observer" in result
        assert "old text" not in result

    def test_observer_tool_name_overrides(self):
        state = {"status": "active", "tool": "Read", "tool_input": {"file_path": "old.py"}}
        observer = {"tool_name": "Bash", "activity_detail": "npm test"}
        result = format_status_card(state, observer_state=observer)
        assert "⚡ Bash" in result
        assert "Read" not in result


# ---------------------------------------------------------------------------
# _format_tool_detail
# ---------------------------------------------------------------------------

class TestFormatToolDetail:
    def test_write_file_path(self):
        assert _format_tool_detail("Write", {"file_path": "/home/user/project/src/main.py"}) == "main.py"

    def test_edit_file_path_short(self):
        assert _format_tool_detail("Edit", {"file_path": "src/main.py"}) == "src/main.py"

    def test_read_file_path_long(self):
        result = _format_tool_detail("Read", {"file_path": "/a/b/c/d/e/file.py"})
        assert "file.py" in result

    def test_bash_command(self):
        assert _format_tool_detail("Bash", {"command": "ls -la"}) == "ls -la"

    def test_bash_command_truncated(self):
        long_cmd = "x" * 100
        result = _format_tool_detail("Bash", {"command": long_cmd})
        assert len(result) == 53  # 50 + "..."
        assert result.endswith("...")

    def test_task_update(self):
        result = _format_tool_detail("TaskUpdate", {"taskId": "42", "status": "in_progress"})
        assert result == "#42 in_progress"

    def test_task_create(self):
        result = _format_tool_detail("TaskCreate", {"taskId": "7", "status": "pending"})
        assert result == "#7 pending"

    def test_task_missing_fields(self):
        assert _format_tool_detail("TaskUpdate", {}) == "#? "

    def test_agent_description(self):
        assert _format_tool_detail("Agent", {"description": "Search for files"}) == "Search for files"

    def test_unknown_tool_returns_empty(self):
        assert _format_tool_detail("Grep", {"pattern": "test"}) == ""

    def test_empty_tool_input(self):
        assert _format_tool_detail("Bash", {}) == ""


# ---------------------------------------------------------------------------
# StatusCard._should_bump
# ---------------------------------------------------------------------------

class TestShouldBump:
    def _make_card(self, **kwargs):
        loop = asyncio.new_event_loop()
        send = AsyncMock()
        edit = AsyncMock()
        delete = AsyncMock()
        card = StatusCard("test-uuid", loop=loop, send_func=send, edit_func=edit, delete_func=delete, chat_id="12345", **kwargs)
        card._loop = loop
        return card

    def test_first_call_no_bump(self):
        card = self._make_card()
        assert card._should_bump("THINKING") is False

    def test_idle_to_thinking(self):
        card = self._make_card()
        card._should_bump("IDLE")
        assert card._should_bump("THINKING") is True

    def test_thinking_to_tool_call(self):
        card = self._make_card()
        card._should_bump("THINKING")
        assert card._should_bump("TOOL_CALL") is True

    def test_tool_call_to_thinking(self):
        card = self._make_card()
        card._should_bump("TOOL_CALL")
        assert card._should_bump("THINKING") is True

    def test_thinking_to_idle(self):
        card = self._make_card()
        card._should_bump("THINKING")
        assert card._should_bump("IDLE") is True

    def test_tool_call_to_idle(self):
        card = self._make_card()
        card._should_bump("TOOL_CALL")
        assert card._should_bump("IDLE") is True

    def test_permission_to_idle(self):
        card = self._make_card()
        card._should_bump("PERMISSION")
        assert card._should_bump("IDLE") is True

    def test_same_state_no_bump(self):
        card = self._make_card()
        card._should_bump("THINKING")
        assert card._should_bump("THINKING") is False
        card._should_bump("IDLE")
        assert card._should_bump("IDLE") is False

    def test_non_significant_transition_no_bump(self):
        card = self._make_card()
        card._should_bump("PERMISSION")
        assert card._should_bump("THINKING") is False


# ---------------------------------------------------------------------------
# StatusCard.update_from_observer
# ---------------------------------------------------------------------------

class TestUpdateFromObserver:
    def _make_card(self, **kwargs):
        loop = asyncio.new_event_loop()
        send = AsyncMock()
        edit = AsyncMock()
        delete = AsyncMock()
        card = StatusCard("test-uuid", loop=loop, send_func=send, edit_func=edit, delete_func=delete, chat_id="12345", **kwargs)
        card._loop = loop
        return card

    def test_caches_observer_state(self):
        card = self._make_card()
        card.update_from_observer({
            "state": "TOOL_CALL",
            "current_activity": "writing",
            "activity_detail": "auth.py",
            "recent_output": "edited",
            "tool_name": "Edit",
            "tool_target": "auth.py",
            "assistant_count": 10,
            "user_count": 5,
        })
        obs = card._observer_state
        assert obs["state"] == "TOOL_CALL"
        assert obs["current_activity"] == "writing"
        assert obs["activity_detail"] == "auth.py"
        assert obs["recent_output"] == "edited"
        assert obs["tool_name"] == "Edit"
        assert obs["assistant_count"] == 10
        assert obs["user_count"] == 5

    def test_caches_default_values(self):
        card = self._make_card()
        card.update_from_observer({})
        obs = card._observer_state
        assert obs["state"] == "IDLE"
        assert obs["assistant_count"] == 0
        assert obs["user_count"] == 0

    def test_no_async_send_when_not_running(self):
        card = self._make_card()
        card._running = False
        # Should not raise
        card.update_from_observer({"state": "THINKING"})


# ---------------------------------------------------------------------------
# StatusCard._edit_telegram
# ---------------------------------------------------------------------------

def _make_simple_card():
    """Helper to create a minimal StatusCard with mocked adapter for unit tests."""
    loop = asyncio.new_event_loop()
    send = AsyncMock()
    edit = AsyncMock()
    delete = AsyncMock()
    card = StatusCard("uuid", loop=loop, send_func=send, edit_func=edit, delete_func=delete, chat_id="chat")
    card._loop = loop
    return card, send, edit, delete


class TestEditTelegram:
    @pytest.mark.asyncio
    async def test_edit_success(self):
        card, _send, edit, _delete = _make_simple_card()
        card._message_id = "100"
        # Configure edit mock to return object with success=True
        edit_result = MagicMock()
        edit_result.success = True
        edit.return_value = edit_result
        result = await card._edit_telegram("new text")
        assert result is True
        edit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_edit_no_message_id(self):
        card, _send, edit, _delete = _make_simple_card()
        card._message_id = None
        result = await card._edit_telegram("text")
        assert result is False
        edit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edit_message_not_modified(self):
        card, _send, edit, _delete = _make_simple_card()
        card._message_id = "100"
        edit.return_value = MagicMock(success=True)  # "not modified" treated as success
        result = await card._edit_telegram("same text")
        assert result is True

    @pytest.mark.asyncio
    async def test_edit_flood_control(self):
        card, _send, edit, _delete = _make_simple_card()
        card._message_id = "100"
        edit.side_effect = Exception("Flood control exceeded")
        result = await card._edit_telegram("text")
        assert result is False

    @pytest.mark.asyncio
    async def test_edit_other_error(self):
        card, _send, edit, _delete = _make_simple_card()
        card._message_id = "100"
        edit.side_effect = Exception("Bad Request: message to edit not found")
        result = await card._edit_telegram("text")
        assert result is False


# ---------------------------------------------------------------------------
# StatusCard._send_new_message
# ---------------------------------------------------------------------------

class TestSendNewMessage:
    @pytest.mark.asyncio
    async def test_send_success(self):
        card, send, _edit, _delete = _make_simple_card()
        card._message_id = None
        send.return_value = MagicMock(success=True, message_id="42")
        result = await card._send_new_message("new msg")
        assert result is True
        assert card._message_id == "42"

    @pytest.mark.asyncio
    async def test_send_failure(self):
        card, send, _edit, _delete = _make_simple_card()
        card._message_id = None
        send.side_effect = Exception("network error")
        result = await card._send_new_message("fail msg")
        assert result is False


# ---------------------------------------------------------------------------
# StatusCard._bump_message
# ---------------------------------------------------------------------------

class TestBumpMessage:
    @pytest.mark.asyncio
    async def test_bump_deletes_old_and_sends_new(self):
        card, send, _edit, delete = _make_simple_card()
        card._message_id = "100"
        send.return_value = MagicMock(success=True, message_id="200")
        result = await card._bump_message("bumped text")
        assert result is True
        assert card._message_id == "200"
        delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bump_delete_failure_still_sends(self):
        card, send, _edit, delete = _make_simple_card()
        card._message_id = "100"
        delete.side_effect = Exception("already deleted")
        send.return_value = MagicMock(success=True, message_id="200")
        result = await card._bump_message("text")
        assert result is True
        assert card._message_id == "200"

    @pytest.mark.asyncio
    async def test_bump_no_existing_message(self):
        card, send, _edit, delete = _make_simple_card()
        card._message_id = None
        send.return_value = MagicMock(success=True, message_id="50")
        result = await card._bump_message("fresh text")
        assert result is True
        assert card._message_id == "50"
        delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# StatusCard start/stop lifecycle
# ---------------------------------------------------------------------------

class TestStatusCardLifecycle:
    def _make_card(self):
        loop = asyncio.new_event_loop()
        send = AsyncMock()
        edit = AsyncMock()
        delete = AsyncMock()
        card = StatusCard("uuid", loop=loop, send_func=send, edit_func=edit, delete_func=delete, chat_id="chat")
        card._loop = loop
        return card

    def test_start_returns_none_when_not_running(self):
        card = self._make_card()
        card.start()
        assert card._running is True
        assert card._thread is not None
        card._running = False
        card._stop_event.set()
        card._thread.join(timeout=2)

    def test_start_idempotent(self):
        card = self._make_card()
        card._running = True
        card._message_id = "42"
        result = card.start()
        assert result == "42"

    def test_stop_cleans_up(self):
        card = self._make_card()
        card._running = True
        card._stop_event.clear()
        card._thread = None
        card.stop()
        assert card._running is False

    def test_message_id_property(self):
        card = self._make_card()
        assert card.message_id is None
        card._message_id = "99"
        assert card.message_id == "99"


# ---------------------------------------------------------------------------
# StatusCard _run_loop poll logic (mocked bot, temporary JSONL)
# ---------------------------------------------------------------------------

class TestRunLoop:
    def _make_card_with_jsonl(self, tmp_path, **kwargs):
        jsonl_path = tmp_path / "test-session.jsonl"
        jsonl_path.write_text("")
        loop = asyncio.new_event_loop()
        send = AsyncMock()
        edit = AsyncMock()
        delete = AsyncMock()
        card = StatusCard("test-session", loop=loop, send_func=send, edit_func=edit, delete_func=delete, chat_id="12345", **kwargs)
        card._jsonl_path = jsonl_path
        card._loop = loop
        card._send_func = send
        card._edit_func = edit
        card._delete_func = delete
        return card

    def test_poll_detects_state_change(self, tmp_path):
        """Simulate one poll cycle: JSONL changes, card text changes, edit is called."""
        card = self._make_card_with_jsonl(tmp_path, poll_interval=0.1)

        # Configure edit mock to return success
        edit_result = MagicMock()
        edit_result.success = True
        card._edit_func.return_value = edit_result

        # Send initial message
        send_result = MagicMock()
        send_result.success = True
        send_result.message_id = "1"
        card._send_func.return_value = send_result
        card._loop.run_until_complete(card._send_new_message("⏳ Waiting for activity..."))
        card._last_card_text = "⏳ Waiting for activity..."

        # Write new data to JSONL
        entry = {"type": "assistant", "timestamp": "2026-01-01T00:00:00", "message": {
            "content": [{"type": "text", "text": "I'm thinking about this"}],
        }}
        card._jsonl_path.write_text(json.dumps(entry))

        # Run one poll iteration
        state = parse_jsonl(card._jsonl_path)
        card_text = format_status_card(state, max_length=card._max_card_length)
        assert card_text != card._last_card_text

        success = card._loop.run_until_complete(card._edit_telegram(card_text))
        assert success is True
        card._edit_func.assert_awaited()

        card._loop.close()

    def test_bump_threshold_triggers_new_message(self, tmp_path):
        """After bump_threshold consecutive edit failures, sends a new message."""
        card = self._make_card_with_jsonl(tmp_path, poll_interval=0.1, bump_threshold=2)

        # Configure edit mock to fail
        card._edit_func.side_effect = Exception("message not found")

        # Configure send mock to succeed
        send_result = MagicMock()
        send_result.success = True
        send_result.message_id = "2"
        card._send_func.return_value = send_result

        card._message_id = "1"
        card._last_card_text = "old"

        # Write JSONL data to generate different card text
        entry = {"type": "assistant", "timestamp": "2026-01-01T00:00:00", "message": {
            "content": [{"type": "text", "text": "new text"}],
        }}
        card._jsonl_path.write_text(json.dumps(entry))

        state = parse_jsonl(card._jsonl_path)
        card_text = format_status_card(state, max_length=card._max_card_length)

        # Simulate bump_threshold failed edits
        for _ in range(card._bump_threshold):
            success = card._loop.run_until_complete(card._edit_telegram(card_text))
            assert success is False
            card._bump_count += 1

        # After threshold, send new message
        if card._bump_count >= card._bump_threshold:
            card._loop.run_until_complete(card._send_new_message(card_text))
            assert card._message_id == "2"

        card._loop.close()
