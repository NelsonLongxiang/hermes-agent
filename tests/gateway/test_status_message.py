"""Tests for gateway/status_message.py — StatusMessageManager."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.status_message import StatusMessageManager


class FakeSendResult:
    def __init__(self, success=True, message_id="msg_1"):
        self.success = success
        self.message_id = message_id
        self.error = None


@pytest.fixture
def mock_adapter():
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=FakeSendResult(success=True, message_id="msg_42"))
    adapter.edit_message = AsyncMock(return_value=FakeSendResult(success=True, message_id="msg_42"))
    adapter.MAX_MESSAGE_LENGTH = 4096
    return adapter


@pytest.fixture
def mgr(mock_adapter):
    return StatusMessageManager(
        adapter=mock_adapter,
        chat_id="12345",
        metadata=None,
    )


SAMPLE_INFO = {
    "state": "THINKING",
    "tool_name": None,
    "tool_target": None,
    "turn_id": 1,
    "elapsed_seconds": 5,
    "tool_calls": [],
    "recent_output": "",
}


@pytest.mark.asyncio
async def test_manager_creation(mgr):
    """StatusMessageManager initializes with correct defaults."""
    assert mgr._chat_id == "12345"
    assert mgr._message_id is None
    assert mgr._last_edit_time == 0.0
    assert mgr._edit_count == 0


@pytest.mark.asyncio
async def test_first_update_sends_new_message(mgr, mock_adapter):
    """First update sends a new message (not edit)."""
    await mgr.update(SAMPLE_INFO)

    mock_adapter.send.assert_called_once()
    call_kwargs = mock_adapter.send.call_args
    assert call_kwargs.kwargs["chat_id"] == "12345"
    assert mgr._message_id == "msg_42"


@pytest.mark.asyncio
async def test_subsequent_update_edits_message(mgr, mock_adapter):
    """Subsequent updates edit the existing message."""
    await mgr.update(SAMPLE_INFO)

    # Bypass throttle for test
    mgr._last_edit_time = 0.0

    # Second update should edit
    info2 = {**SAMPLE_INFO, "state": "TOOL_CALL", "tool_name": "Read", "tool_target": "src/main.py", "elapsed_seconds": 10}
    await mgr.update(info2)

    mock_adapter.send.assert_called_once()  # Only one send
    mock_adapter.edit_message.assert_called_once()
    edit_kwargs = mock_adapter.edit_message.call_args.kwargs
    assert edit_kwargs["message_id"] == "msg_42"


@pytest.mark.asyncio
async def test_throttle_skips_rapid_updates(mgr, mock_adapter):
    """Second update within 2s is skipped."""
    await mgr.update(SAMPLE_INFO)
    assert mock_adapter.send.call_count == 1

    # Rapid second update — should be skipped
    info2 = {**SAMPLE_INFO, "state": "TOOL_CALL", "tool_name": "Read"}
    await mgr.update(info2)
    assert mock_adapter.edit_message.call_count == 0  # throttled


@pytest.mark.asyncio
async def test_deduplicate_skips_same_state(mgr, mock_adapter):
    """Update with same state+tool is skipped."""
    await mgr.update(SAMPLE_INFO)

    # Advance time past throttle
    mgr._last_edit_time = 0

    # Same state — should be skipped
    await mgr.update(SAMPLE_INFO)
    assert mock_adapter.edit_message.call_count == 0


@pytest.mark.asyncio
async def test_flood_control_disables_editing(mgr, mock_adapter):
    """3 consecutive flood failures permanently disables editing."""
    await mgr.update(SAMPLE_INFO)  # First send succeeds

    # Make edits fail with flood error
    mock_adapter.edit_message.return_value = FakeSendResult(
        success=False, message_id=None
    )
    mock_adapter.edit_message.return_value.error = "flood control: retry after 5"

    for i in range(3):
        mgr._last_edit_time = 0  # bypass throttle
        info = {**SAMPLE_INFO, "tool_name": f"Tool{i}"}
        await mgr.update(info)

    assert mgr._edit_disabled is True

    # Further updates should be no-ops
    mgr._last_edit_time = 0
    info = {**SAMPLE_INFO, "tool_name": "NewTool"}
    await mgr.update(info)
    # edit_message was called exactly 3 times (the flood failures)
    assert mock_adapter.edit_message.call_count == 3


@pytest.mark.asyncio
async def test_finish_replaces_with_summary(mgr, mock_adapter):
    """finish() edits the message with a summary."""
    await mgr.update(SAMPLE_INFO)

    summary = "\u2705 完成\n\u23f1 45s | turn 1"
    await mgr.finish(summary)

    # Last edit_message call should have the summary
    last_call = mock_adapter.edit_message.call_args
    assert "\u2705" in last_call.kwargs["content"]


@pytest.mark.asyncio
async def test_format_status_thinking():
    """Format THINKING state correctly."""
    mgr = StatusMessageManager(adapter=MagicMock(), chat_id="123")
    info = {**SAMPLE_INFO, "turn_id": 2, "elapsed_seconds": 15, "recent_output": "分析项目结构"}
    text = mgr._format_status(info)
    assert "\U0001f914" in text  # thinking emoji
    assert "turn 2" in text
    assert "15s" in text


@pytest.mark.asyncio
async def test_format_status_tool_call():
    """Format TOOL_CALL state with tool details."""
    mgr = StatusMessageManager(adapter=MagicMock(), chat_id="123")
    info = {
        "state": "TOOL_CALL",
        "tool_name": "Read",
        "tool_target": "src/main.py",
        "turn_id": 2,
        "elapsed_seconds": 25,
        "tool_calls": [{"tool": "Read", "target": "src/main.py"}],
        "recent_output": "",
    }
    text = mgr._format_status(info)
    assert "\U0001f527" in text  # wrench emoji
    assert "Read" in text
    assert "src/main.py" in text


@pytest.mark.asyncio
async def test_cancel_removes_status_message(mock_adapter):
    """cancel() best-effort replaces with '(cancelled)'."""
    mgr = StatusMessageManager(adapter=mock_adapter, chat_id="123")
    await mgr.update(SAMPLE_INFO)
    await mgr.cancel()
    last_call = mock_adapter.edit_message.call_args
    assert "(cancelled)" in last_call.kwargs["content"]


@pytest.mark.asyncio
async def test_cancel_noop_if_no_message(mock_adapter):
    """cancel() is a no-op if no message was ever created."""
    mgr = StatusMessageManager(adapter=mock_adapter, chat_id="123")
    await mgr.cancel()
    mock_adapter.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_finish_noop_if_no_message(mock_adapter):
    """finish() is a no-op if no message was ever created."""
    mgr = StatusMessageManager(adapter=mock_adapter, chat_id="123")
    await mgr.finish("summary")
    mock_adapter.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_edit_cap_prevents_excessive_edits(mock_adapter):
    """Updates stop after _MAX_EDITS_PER_SESSION edits."""
    mgr = StatusMessageManager(adapter=mock_adapter, chat_id="123")
    await mgr.update(SAMPLE_INFO)  # First send

    for i in range(35):
        mgr._last_edit_time = 0  # bypass throttle
        info = {**SAMPLE_INFO, "tool_name": f"Tool{i}"}
        await mgr.update(info)

    # send (1) + edits (up to 30) = max 31 total adapter calls
    total_calls = mock_adapter.send.call_count + mock_adapter.edit_message.call_count
    assert total_calls <= 31


def test_format_elapsed():
    """Test elapsed time formatting."""
    mgr = StatusMessageManager(adapter=MagicMock(), chat_id="123")
    assert mgr._format_elapsed(5) == "5s"
    assert mgr._format_elapsed(65) == "1m 5s"
    assert mgr._format_elapsed(3665) == "1h 1m"


def test_format_summary():
    """Test completion summary formatting."""
    mgr = StatusMessageManager(adapter=MagicMock(), chat_id="123")
    info = {
        "elapsed_seconds": 45,
        "tool_calls": [
            {"tool": "Read", "target": "a.py"},
            {"tool": "Edit", "target": "b.py"},
            {"tool": "Read", "target": "c.py"},
        ],
        "turn_id": 2,
    }
    text = mgr.format_summary(info)
    assert "\u2705" in text
    assert "Read x2" in text
    assert "Edit x1" in text
    assert "45s" in text
