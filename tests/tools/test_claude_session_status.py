"""Tests for ClaudeSessionManager status callback."""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from tools.claude_session.manager import ClaudeSessionManager
from tools.claude_session.state_machine import ClaudeState, StateTransition


def test_status_callback_on_state_change():
    """Status callback receives state info on transition."""
    mgr = ClaudeSessionManager()
    received = []
    mgr._status_callback = lambda info: received.append(info)

    # Simulate state change to THINKING with a turn
    mgr._session_active = True
    mgr._current_turn = MagicMock()
    mgr._current_turn.turn_id = 1
    mgr._current_turn.start_time = time.monotonic() - 10
    mgr._current_turn.tool_calls = []
    mgr._current_turn.thinking_cycles = 0

    transition = StateTransition(
        from_state=ClaudeState.IDLE,
        to_state=ClaudeState.THINKING,
        timestamp=time.monotonic(),
    )
    mgr._handle_state_change(transition)

    assert len(received) == 1
    info = received[0]
    assert info["state"] == "THINKING"
    assert info["turn_id"] == 1
    assert info["elapsed_seconds"] >= 9


def test_status_callback_includes_tool_calls():
    """Status callback includes tool call info."""
    mgr = ClaudeSessionManager()
    received = []
    mgr._status_callback = lambda info: received.append(info)

    mgr._session_active = True
    mgr._current_turn = MagicMock()
    mgr._current_turn.turn_id = 2
    mgr._current_turn.start_time = time.monotonic() - 25
    mgr._current_turn.tool_calls = [
        MagicMock(to_dict=lambda: {"tool": "Read", "target": "src/main.py"}),
    ]

    transition = StateTransition(
        from_state=ClaudeState.THINKING,
        to_state=ClaudeState.TOOL_CALL,
        timestamp=time.monotonic(),
        tool_name="Read",
        tool_target="src/main.py",
    )
    mgr._handle_state_change(transition)

    assert len(received) == 1
    info = received[0]
    assert info["state"] == "TOOL_CALL"
    assert info["tool_name"] == "Read"
    assert info["tool_target"] == "src/main.py"


def test_no_callback_when_not_set():
    """No error when status_callback is None."""
    mgr = ClaudeSessionManager()
    mgr._session_active = True
    mgr._current_turn = MagicMock()
    mgr._current_turn.turn_id = 1
    mgr._current_turn.start_time = time.monotonic()
    mgr._current_turn.tool_calls = []

    transition = StateTransition(
        from_state=ClaudeState.THINKING,
        to_state=ClaudeState.TOOL_CALL,
        timestamp=time.monotonic(),
        tool_name="Read",
        tool_target="file.py",
    )
    # Should not raise
    mgr._handle_state_change(transition)


# ---------------------------------------------------------------------------
# Observer registration tests (claude_session_tool.py)
# ---------------------------------------------------------------------------

import tools.claude_session_tool as _cst


def test_register_status_observer():
    """register_status_observer sets the global observer for a gateway session."""
    callback = MagicMock()
    gw_key = "test-gw-key"
    try:
        _cst.register_status_observer(callback, gateway_session_key=gw_key)
        assert _cst._status_observers.get(gw_key) is callback
    finally:
        _cst.unregister_status_observer(gateway_session_key=gw_key)


def test_unregister_status_observer():
    """unregister_status_observer clears the global observer for a gateway session."""
    callback = MagicMock()
    gw_key = "test-gw-key"
    _cst.register_status_observer(callback, gateway_session_key=gw_key)
    _cst.unregister_status_observer(gateway_session_key=gw_key)
    assert gw_key not in _cst._status_observers


def test_observer_auto_binding():
    """When observer is registered, start action wires callback to manager.

    Simulates the binding code in _handle_claude_session 'start' action
    (lines 308-312 of claude_session_tool.py).
    """
    callback = MagicMock()
    gw_key = "test-gw-key"
    _cst.register_status_observer(callback, gateway_session_key=gw_key)

    try:
        mgr = MagicMock()
        sid = "test-session-42"

        # Replicate binding logic from start action
        # Use default parameter to bind observer at creation time
        _observer = _cst._status_observers.get(gw_key)
        if _observer:
            mgr._status_callback = (
                lambda info, _sid=sid, _obs=_observer: _obs(_sid, info)
            )

        # Trigger callback as manager would on state change
        mgr._status_callback({"state": "THINKING", "turn_id": 3})

        callback.assert_called_once_with("test-session-42", {"state": "THINKING", "turn_id": 3})
    finally:
        _cst.unregister_status_observer(gateway_session_key=gw_key)


def test_register_replaces_previous_observer():
    """Re-registering replaces observer for the same gateway session key."""
    gw_key = "test-gw-key"
    callback1 = MagicMock()
    callback2 = MagicMock()

    _cst.register_status_observer(callback1, gateway_session_key=gw_key)
    _cst.register_status_observer(callback2, gateway_session_key=gw_key)

    try:
        # callback2 should replace callback1 for the same gw_key
        assert _cst._status_observers.get(gw_key) is callback2
        assert _cst._status_observers.get(gw_key) is not callback1

        # Only callback2 should receive calls for this gw_key
        _observer = _cst._status_observers.get(gw_key)
        if _observer:
            _observer("sid-1", {"state": "IDLE"})

        callback1.assert_not_called()
        callback2.assert_called_once_with("sid-1", {"state": "IDLE"})
    finally:
        _cst.unregister_status_observer(gateway_session_key=gw_key)


def test_concurrent_sessions_isolated():
    """Different gateway sessions have independent observers (no cross-talk)."""
    gw_key1 = "group-chat-key"
    gw_key2 = "dm-chat-key"
    callback1 = MagicMock()
    callback2 = MagicMock()

    _cst.register_status_observer(callback1, gateway_session_key=gw_key1)
    _cst.register_status_observer(callback2, gateway_session_key=gw_key2)

    try:
        # Both observers should be registered independently
        assert _cst._status_observers.get(gw_key1) is callback1
        assert _cst._status_observers.get(gw_key2) is callback2

        # Trigger each observer independently
        obs1 = _cst._status_observers.get(gw_key1)
        obs2 = _cst._status_observers.get(gw_key2)
        if obs1 and obs2:
            obs1("sid-group", {"state": "THINKING"})
            obs2("sid-dm", {"state": "TOOL_CALL"})

        # Each callback should receive its own call only
        callback1.assert_called_once_with("sid-group", {"state": "THINKING"})
        callback2.assert_called_once_with("sid-dm", {"state": "TOOL_CALL"})

        # No cross-talk
        assert len(callback1.call_args_list) == 1
        assert len(callback2.call_args_list) == 1
    finally:
        _cst.unregister_status_observer(gateway_session_key=gw_key1)
        _cst.unregister_status_observer(gateway_session_key=gw_key2)


# ---------------------------------------------------------------------------
# StatusMessageManager boundary tests (gateway/status_message.py)
# ---------------------------------------------------------------------------

import asyncio
from gateway.status_message import StatusMessageManager


def _make_adapter():
    """Create a mock adapter for StatusMessageManager tests."""
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=type("R", (), {"success": True, "message_id": "msg-1"})())
    adapter.edit_message = AsyncMock(return_value=type("R", (), {"success": True})())
    return adapter


def test_format_thinking_status():
    """THINKING status formats correctly."""
    mgr = StatusMessageManager.__new__(StatusMessageManager)
    text = mgr._format_status({
        "state": "THINKING",
        "turn_id": 1,
        "elapsed_seconds": 15,
        "tool_calls": [],
    })
    assert "思考中" in text
    assert "15s" in text
    assert "turn 1" in text
    assert len(text) <= 500


def test_format_tool_call_status():
    """TOOL_CALL status includes tool name and target."""
    mgr = StatusMessageManager.__new__(StatusMessageManager)
    text = mgr._format_status({
        "state": "TOOL_CALL",
        "tool_name": "Read",
        "tool_target": "src/main.py",
        "turn_id": 2,
        "elapsed_seconds": 25,
        "tool_calls": [],
    })
    assert "执行工具" in text
    assert "Read" in text
    assert "src/main.py" in text


def test_format_permission_status():
    """PERMISSION status shows authorization prompt."""
    mgr = StatusMessageManager.__new__(StatusMessageManager)
    text = mgr._format_status({
        "state": "PERMISSION",
        "tool_name": "Bash",
        "tool_target": "npm run build",
        "turn_id": 3,
        "elapsed_seconds": 30,
        "tool_calls": [],
    })
    assert "等待授权" in text
    assert "Bash" in text


def test_format_summary():
    """Completion summary includes tool counts and elapsed time."""
    mgr = StatusMessageManager.__new__(StatusMessageManager)
    text = mgr.format_summary({
        "elapsed_seconds": 125,
        "turn_id": 4,
        "tool_calls": [
            {"tool": "Read", "target": "a.py"},
            {"tool": "Read", "target": "b.py"},
            {"tool": "Edit", "target": "c.py"},
            {"tool": "Bash", "target": "test"},
        ],
    })
    assert "完成" in text
    assert "Read x2" in text
    assert "Edit x1" in text
    assert "2m 5s" in text


def test_format_summary_no_tools():
    """Summary with no tool calls still shows elapsed and turn."""
    mgr = StatusMessageManager.__new__(StatusMessageManager)
    text = mgr.format_summary({
        "elapsed_seconds": 5,
        "turn_id": 1,
        "tool_calls": [],
    })
    assert "完成" in text
    assert "5s" in text


def test_format_elapsed():
    """Elapsed time formatting covers seconds, minutes, hours."""
    f = StatusMessageManager._format_elapsed
    assert f(0) == "0s"
    assert f(45) == "45s"
    assert f(125) == "2m 5s"
    assert f(3661) == "1h 1m"


def test_format_status_500_char_limit():
    """Status message never exceeds 500 chars even with many tool calls."""
    mgr = StatusMessageManager.__new__(StatusMessageManager)
    many_tools = [
        {"tool": f"Tool{i}", "target": f"/very/long/path/to/file_{i}_with_long_name.py"}
        for i in range(20)
    ]
    text = mgr._format_status({
        "state": "TOOL_CALL",
        "turn_id": 99,
        "elapsed_seconds": 9999,
        "tool_calls": many_tools,
    })
    assert len(text) <= 500


@pytest.mark.asyncio
async def test_status_message_throttle():
    """Rapid updates are throttled — second call skipped."""
    adapter = _make_adapter()
    mgr = StatusMessageManager(adapter, "chat-1")

    await mgr.update({"state": "THINKING", "turn_id": 1, "elapsed_seconds": 0})
    assert mgr._message_id == "msg-1"
    assert mgr._edit_count == 1

    # Second update too fast — should be throttled
    await mgr.update({"state": "TOOL_CALL", "tool_name": "Read", "turn_id": 1, "elapsed_seconds": 1})
    assert mgr._edit_count == 1  # still 1


@pytest.mark.asyncio
async def test_status_message_dedup():
    """Duplicate state+tool updates are skipped (even after throttle window)."""
    adapter = _make_adapter()
    mgr = StatusMessageManager(adapter, "chat-1")
    mgr._MIN_EDIT_INTERVAL = 0.0  # disable throttle for this test

    await mgr.update({"state": "THINKING", "turn_id": 1, "elapsed_seconds": 0})
    assert mgr._edit_count == 1

    # Same state, no tool_name — dedup
    await mgr.update({"state": "THINKING", "turn_id": 1, "elapsed_seconds": 5})
    assert mgr._edit_count == 1

    # Different state — goes through
    await mgr.update({"state": "TOOL_CALL", "tool_name": "Read", "turn_id": 1, "elapsed_seconds": 10})
    assert mgr._edit_count == 2


@pytest.mark.asyncio
async def test_status_message_flood_control():
    """3 consecutive flood failures disable editing."""
    adapter = MagicMock()

    adapter.send = AsyncMock(return_value=type("R", (), {"success": True, "message_id": "msg-1"})())
    adapter.edit_message = AsyncMock(
        return_value=type("R", (), {"success": False, "error": "Flood control: retry after 5s"})()
    )

    mgr = StatusMessageManager(adapter, "chat-1")
    mgr._MIN_EDIT_INTERVAL = 0.0

    await mgr.update({"state": "THINKING", "turn_id": 1, "elapsed_seconds": 0})
    assert mgr._message_id == "msg-1"

    # 3 flood failures
    for i in range(3):
        await mgr.update({"state": "TOOL_CALL", "tool_name": f"Tool{i}", "turn_id": 1, "elapsed_seconds": i})

    assert mgr._edit_disabled is True

    # Subsequent updates are silently dropped
    await mgr.update({"state": "IDLE", "turn_id": 1, "elapsed_seconds": 100})
    assert mgr._edit_count == 1  # never incremented past first send


@pytest.mark.asyncio
async def test_status_message_edit_cap():
    """Editing stops after MAX_EDITS_PER_SESSION edits."""
    adapter = _make_adapter()
    mgr = StatusMessageManager(adapter, "chat-1")
    mgr._MIN_EDIT_INTERVAL = 0.0
    mgr._MAX_EDITS_PER_SESSION = 3

    await mgr.update({"state": "THINKING", "turn_id": 1, "elapsed_seconds": 0})
    for i in range(5):
        await mgr.update({"state": "TOOL_CALL", "tool_name": f"T{i}", "turn_id": 1, "elapsed_seconds": i})

    assert mgr._edit_count == 3  # capped at 3


@pytest.mark.asyncio
async def test_status_message_finish():
    """finish() replaces the status message with summary."""
    adapter = _make_adapter()
    mgr = StatusMessageManager(adapter, "chat-1")

    await mgr.update({"state": "THINKING", "turn_id": 1, "elapsed_seconds": 0})

    await mgr.finish("✅ Done")
    adapter.edit_message.assert_called_once()
    call_kwargs = adapter.edit_message.call_args
    assert "✅ Done" in str(call_kwargs)


@pytest.mark.asyncio
async def test_status_message_cancel_no_message():
    """cancel() is a no-op when no message was created."""
    adapter = _make_adapter()
    mgr = StatusMessageManager(adapter, "chat-1")
    await mgr.cancel()  # should not raise
    adapter.edit_message.assert_not_called()
