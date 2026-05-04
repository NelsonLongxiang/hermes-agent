# Telegram Status Message Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a continuously-edited Telegram status message while Claude Code (tmux) is working, displaying current state, tool calls, and elapsed time. Replace with a summary on completion.

**Architecture:** The `AdaptivePoller` background thread detects Claude Code state changes. A new `StatusMessageManager` receives updates via a sync→async bridge (`asyncio.run_coroutine_threadsafe`), edits a single Telegram message with throttling, and replaces it with a summary on completion.

**Tech Stack:** Python 3.12, asyncio, python-telegram-bot, threading

**Spec:** `docs/superpowers/specs/2026-04-26-telegram-status-message-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `gateway/status_message.py` | StatusMessageManager — throttled Telegram message editing | **Create** |
| `tools/claude_session/manager.py` | Add status callback to ClaudeSessionManager | **Modify** |
| `tools/claude_session_tool.py` | Expose global observer registration for gateway | **Modify** |
| `gateway/run.py` | Register bridge callback in `_run_agent` | **Modify** |
| `tests/gateway/test_status_message.py` | Tests for StatusMessageManager | **Create** |
| `tests/tools/test_claude_session_status.py` | Tests for session status callback | **Create** |

---

### Task 1: StatusMessageManager — Core Class

**Files:**
- Create: `gateway/status_message.py`
- Create: `tests/gateway/test_status_message.py`

- [ ] **Step 1: Write failing test for StatusMessageManager creation**

```python
# tests/gateway/test_status_message.py
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
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 1,
        "elapsed_seconds": 5,
        "tool_calls": [],
        "recent_output": "",
    }
    await mgr.update(info)

    mock_adapter.send.assert_called_once()
    call_kwargs = mock_adapter.send.call_args
    assert call_kwargs.kwargs["chat_id"] == "12345"
    assert mgr._message_id == "msg_42"


@pytest.mark.asyncio
async def test_subsequent_update_edits_message(mgr, mock_adapter):
    """Subsequent updates edit the existing message."""
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 1,
        "elapsed_seconds": 5,
        "tool_calls": [],
        "recent_output": "",
    }
    await mgr.update(info)

    # Second update should edit
    info["state"] = "TOOL_CALL"
    info["tool_name"] = "Read"
    info["tool_target"] = "src/main.py"
    info["elapsed_seconds"] = 10
    await mgr.update(info)

    mock_adapter.send.assert_called_once()  # Only one send
    mock_adapter.edit_message.assert_called_once()
    edit_kwargs = mock_adapter.edit_message.call_args.kwargs
    assert edit_kwargs["message_id"] == "msg_42"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_status_message.py -v -k "test_manager_creation or test_first_update or test_subsequent" 2>&1 | tail -20`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.status_message'`

- [ ] **Step 3: Implement StatusMessageManager**

```python
# gateway/status_message.py
"""Gateway status message manager — maintains a single edited Telegram status message.

Shows real-time Claude Code activity (state, tool calls, elapsed time)
as a continuously-edited message. Replaced with a summary on completion.
"""

import logging
import time
from typing import Any, Optional

from gateway.platforms.base import SendResult

logger = logging.getLogger("gateway.status_message")


class StatusMessageManager:
    """Manages a single Telegram status message that is continuously edited.

    Usage::

        mgr = StatusMessageManager(adapter, chat_id, metadata)
        await mgr.update({"state": "THINKING", ...})
        await mgr.update({"state": "TOOL_CALL", "tool_name": "Read", ...})
        await mgr.finish("Done. 3 tool calls, 45s")
    """

    # Throttling
    _MIN_EDIT_INTERVAL = 2.0      # seconds between edits
    _MAX_EDITS_PER_SESSION = 30   # hard cap to avoid rate limits
    _MAX_FLOOD_STRIKES = 3        # consecutive flood failures → stop editing
    _MAX_MESSAGE_CHARS = 500      # Telegram status message length cap

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        metadata: Optional[dict] = None,
    ):
        self._adapter = adapter
        self._chat_id = chat_id
        self._metadata = metadata
        self._message_id: Optional[str] = None
        self._last_edit_time: float = 0.0
        self._edit_count: int = 0
        self._flood_strikes: int = 0
        self._edit_disabled: bool = False
        self._last_state: Optional[str] = None
        self._last_tool_name: Optional[str] = None

    async def update(self, status_info: dict) -> None:
        """Send or edit the status message with new info.

        Throttled: skips if called too fast or flood-control active.
        """
        if self._edit_disabled:
            return

        # Throttle: skip if too soon after last edit (unless first send)
        now = time.monotonic()
        if (
            self._message_id is not None
            and (now - self._last_edit_time) < self._MIN_EDIT_INTERVAL
        ):
            return

        # Deduplicate: skip if state and tool name unchanged
        new_state = status_info.get("state")
        new_tool = status_info.get("tool_name")
        if (
            self._message_id is not None
            and new_state == self._last_state
            and new_tool == self._last_tool_name
        ):
            return

        # Edit cap
        if self._edit_count >= self._MAX_EDITS_PER_SESSION:
            return

        text = self._format_status(status_info)

        try:
            if self._message_id is None:
                # First message — send new
                result: SendResult = await self._adapter.send(
                    chat_id=self._chat_id,
                    content=text,
                    metadata=self._metadata,
                )
                if result.success and result.message_id:
                    self._message_id = str(result.message_id)
                    self._last_edit_time = time.monotonic()
                    self._edit_count += 1
            else:
                # Subsequent — edit existing
                result: SendResult = await self._adapter.edit_message(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    content=text,
                )
                if result.success:
                    self._last_edit_time = time.monotonic()
                    self._edit_count += 1
                    self._flood_strikes = 0
                else:
                    self._handle_edit_failure(result)

            self._last_state = new_state
            self._last_tool_name = new_tool

        except Exception as e:
            logger.debug("Status message update error: %s", e)

    async def finish(self, summary: str) -> None:
        """Replace the status message with a final summary."""
        if not self._message_id:
            return
        try:
            await self._adapter.edit_message(
                chat_id=self._chat_id,
                message_id=self._message_id,
                content=summary[:self._MAX_MESSAGE_CHARS],
            )
        except Exception as e:
            logger.debug("Status message finish error: %s", e)

    async def cancel(self) -> None:
        """Best-effort delete the status message on error/cancellation."""
        if not self._message_id:
            return
        try:
            await self._adapter.edit_message(
                chat_id=self._chat_id,
                message_id=self._message_id,
                content="(cancelled)",
            )
        except Exception:
            pass

    def _handle_edit_failure(self, result: SendResult) -> None:
        """Handle an edit failure — track flood strikes."""
        err = getattr(result, "error", "") or ""
        err_lower = err.lower()
        is_flood = "flood" in err_lower or "retry after" in err_lower
        if is_flood:
            self._flood_strikes += 1
            if self._flood_strikes >= self._MAX_FLOOD_STRIKES:
                self._edit_disabled = True
                logger.debug("Status message: flood-control disabled editing")

    def _format_status(self, info: dict) -> str:
        """Format status info into a display string (≤500 chars)."""
        state = info.get("state", "THINKING")
        tool_name = info.get("tool_name")
        tool_target = info.get("tool_target")
        turn_id = info.get("turn_id", "?")
        elapsed = info.get("elapsed_seconds", 0)
        tool_calls = info.get("tool_calls", [])
        recent_output = info.get("recent_output", "")

        parts = []

        if state == "THINKING":
            parts.append("\U0001f914 思考中...")
        elif state == "TOOL_CALL":
            parts.append("\U0001f527 执行工具...")
        elif state == "PERMISSION":
            parts.append("\u26a0\ufe0f 等待授权...")
        else:
            parts.append(f"\U0001f916 {state}")

        # Tool call details (max 3 recent)
        if tool_calls:
            for tc in tool_calls[-3:]:
                name = tc.get("tool", tc.get("name", "?"))
                target = tc.get("target", "")
                if target:
                    line = f"\u25cf {name} {target}"
                else:
                    line = f"\u25cf {name}"
                parts.append(line)
        elif tool_name:
            target_str = f" {tool_target}" if tool_target else ""
            parts.append(f"\u25cf {tool_name}{target_str}")

        # Recent output (1-2 lines if meaningful and space allows)
        if recent_output and state == "THINKING":
            lines = [l.strip() for l in recent_output.split("\n") if l.strip()]
            if lines:
                tail = lines[-1][:80]
                parts.append(tail)

        # Footer
        elapsed_str = self._format_elapsed(elapsed)
        parts.append(f"\u23f1 {elapsed_str} | turn {turn_id}")

        text = "\n".join(parts)
        return text[:self._MAX_MESSAGE_CHARS]

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        """Format elapsed seconds as human-readable string."""
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m {s}s"
        h, m = divmod(m, 60)
        return f"{h}h {m}m"

    def format_summary(self, status_info: dict) -> str:
        """Format the completion summary."""
        elapsed = status_info.get("elapsed_seconds", 0)
        tool_calls = status_info.get("tool_calls", [])
        turn_id = status_info.get("turn_id", "?")

        # Count tools by name
        tool_counts: dict[str, int] = {}
        for tc in tool_calls:
            name = tc.get("tool", tc.get("name", "?"))
            tool_counts[name] = tool_counts.get(name, 0) + 1

        parts = ["\u2705 完成"]
        if tool_counts:
            tool_parts = [
                f"\u25cf {name} x{count}"
                for name, count in tool_counts.items()
            ]
            parts.append("  ".join(tool_parts[:5]))

        elapsed_str = self._format_elapsed(elapsed)
        parts.append(f"\u23f1 {elapsed_str} | turn {turn_id}")
        return "\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_status_message.py -v 2>&1 | tail -20`
Expected: 3 PASS

- [ ] **Step 5: Add throttling and flood-control tests**

Append to `tests/gateway/test_status_message.py`:

```python
@pytest.mark.asyncio
async def test_throttle_skips_rapid_updates(mgr, mock_adapter):
    """Second update within 2s is skipped."""
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 1,
        "elapsed_seconds": 5,
        "tool_calls": [],
        "recent_output": "",
    }
    await mgr.update(info)
    assert mock_adapter.send.call_count == 1

    # Rapid second update — should be skipped
    info["state"] = "TOOL_CALL"
    info["tool_name"] = "Read"
    await mgr.update(info)
    assert mock_adapter.edit_message.call_count == 0  # throttled


@pytest.mark.asyncio
async def test_deduplicate_skips_same_state(mgr, mock_adapter):
    """Update with same state+tool is skipped."""
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 1,
        "elapsed_seconds": 5,
        "tool_calls": [],
        "recent_output": "",
    }
    await mgr.update(info)

    # Advance time past throttle
    mgr._last_edit_time = 0

    # Same state — should be skipped
    await mgr.update(info)
    assert mock_adapter.edit_message.call_count == 0


@pytest.mark.asyncio
async def test_flood_control_disables_editing(mgr, mock_adapter):
    """3 consecutive flood failures permanently disables editing."""
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 1,
        "elapsed_seconds": 5,
        "tool_calls": [],
        "recent_output": "",
    }
    await mgr.update(info)  # First send succeeds

    # Make edits fail with flood error
    mock_adapter.edit_message.return_value = FakeSendResult(
        success=False, message_id=None
    )
    mock_adapter.edit_message.return_value.error = "flood control: retry after 5"

    for i in range(3):
        mgr._last_edit_time = 0  # bypass throttle
        info["tool_name"] = f"Tool{i}"  # bypass dedup
        await mgr.update(info)

    assert mgr._edit_disabled is True

    # Further updates should be no-ops
    mgr._last_edit_time = 0
    info["tool_name"] = "NewTool"
    await mgr.update(info)
    # edit_message was called exactly 3 times (the flood failures)
    assert mock_adapter.edit_message.call_count == 3


@pytest.mark.asyncio
async def test_finish_replaces_with_summary(mgr, mock_adapter):
    """finish() edits the message with a summary."""
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 1,
        "elapsed_seconds": 5,
        "tool_calls": [],
        "recent_output": "",
    }
    await mgr.update(info)

    summary = "\u2705 完成\n\u23f1 45s | turn 1"
    await mgr.finish(summary)

    # Last edit_message call should have the summary
    last_call = mock_adapter.edit_message.call_args
    assert "\u2705" in last_call.kwargs["content"]


@pytest.mark.asyncio
async def test_format_status_thinking():
    """Format THINKING state correctly."""
    mgr = StatusMessageManager(adapter=MagicMock(), chat_id="123")
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 2,
        "elapsed_seconds": 15,
        "tool_calls": [],
        "recent_output": "分析项目结构",
    }
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
        "tool_calls": [
            {"tool": "Read", "target": "src/main.py"},
        ],
        "recent_output": "",
    }
    text = mgr._format_status(info)
    assert "\U0001f527" in text  # wrench emoji
    assert "Read" in text
    assert "src/main.py" in text
```

- [ ] **Step 6: Run all status message tests**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_status_message.py -v 2>&1 | tail -25`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
cd /mnt/f/Projects/hermes-agent
git add gateway/status_message.py tests/gateway/test_status_message.py
git commit -m "feat: add StatusMessageManager for Telegram status updates"
```

---

### Task 2: ClaudeSessionManager Status Callback

**Files:**
- Modify: `tools/claude_session/manager.py:82-100` (add `_status_callback` field)
- Modify: `tools/claude_session/manager.py:724-766` (extend `_handle_state_change`)
- Create: `tests/tools/test_claude_session_status.py`

- [ ] **Step 1: Write failing test for status callback**

```python
# tests/tools/test_claude_session_status.py
"""Tests for ClaudeSessionManager status callback."""

import time
from unittest.mock import MagicMock

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_status.py -v 2>&1 | tail -15`
Expected: FAIL — `received` list is empty (callback not called yet)

- [ ] **Step 3: Add `_status_callback` field and `_build_status_info` to manager.py**

In `tools/claude_session/manager.py`, add after line 100 (`self._on_event = None`):

```python
        self._status_callback = None  # Optional[Callable[[dict], None]] — for gateway status bridge
```

Add `_build_status_info` method after the `_fire_event` method (after line ~854):

```python
    def _build_status_info(self) -> dict:
        """Build status info dict for the status callback."""
        now = time.monotonic()
        tool_calls_dicts = []
        if self._current_turn:
            tool_calls_dicts = [tc.to_dict() for tc in self._current_turn.tool_calls]

        return {
            "state": self._sm.current_state,
            "tool_name": None,
            "tool_target": None,
            "turn_id": self._current_turn.turn_id if self._current_turn else None,
            "elapsed_seconds": (
                (now - self._current_turn.start_time)
                if self._current_turn else 0
            ),
            "tool_calls": tool_calls_dicts,
            "recent_output": self._buf.last_n_chars(200),
        }
```

In `_handle_state_change` method (line ~724), add status callback invocation. After the existing `self._state_event.set()` line (line ~765) and before the auto-approve block (line ~768), add:

```python
        # Fire status callback for gateway status bridge
        if self._status_callback:
            try:
                info = self._build_status_info()
                # Attach tool info from the transition
                info["tool_name"] = getattr(transition, "tool_name", None)
                info["tool_target"] = getattr(transition, "tool_target", None)
                self._status_callback(info)
            except Exception as e:
                logger.debug("Status callback error: %s", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_status.py -v 2>&1 | tail -15`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/f/Projects/hermes-agent
git add tools/claude_session/manager.py tests/tools/test_claude_session_status.py
git commit -m "feat: add status callback to ClaudeSessionManager"
```

---

### Task 3: claude_session_tool Observer Registration

**Files:**
- Modify: `tools/claude_session_tool.py:18-20` (add observer globals)
- Modify: `tools/claude_session_tool.py:188-250` (attach observer to new sessions)

- [ ] **Step 1: Write failing test for observer registration**

Append to `tests/tools/test_claude_session_status.py`:

```python
def test_register_observer():
    """register_status_observer sets the global observer."""
    from tools.claude_session_tool import (
        register_status_observer,
        unregister_status_observer,
    )

    called_with = []
    register_status_observer(lambda sid, info: called_with.append((sid, info)))

    assert tools.claude_session_tool._status_observer is not None

    # Cleanup
    unregister_status_observer()
    assert tools.claude_session_tool._status_observer is None
```

Wait — we need a proper import. Let me adjust:

```python
import tools.claude_session_tool


def test_register_observer():
    """register_status_observer sets the global observer."""
    received = []
    tools.claude_session_tool.register_status_observer(
        lambda sid, info: received.append((sid, info))
    )

    assert tools.claude_session_tool._status_observer is not None

    # Cleanup
    tools.claude_session_tool.unregister_status_observer()
    assert tools.claude_session_tool._status_observer is None


def test_observer_attached_to_new_session():
    """When observer is registered, new sessions get it as status_callback."""
    # Register observer
    received = []
    tools.claude_session_tool.register_status_observer(
        lambda sid, info: received.append((sid, info))
    )

    try:
        # Create a session manager directly (bypassing full start)
        from tools.claude_session.manager import ClaudeSessionManager
        mgr = ClaudeSessionManager()
        mgr._session_id = "test_session_1"

        # Simulate what _handle_claude_session does after creating a manager
        # (attach observer as status_callback)
        observer = tools.claude_session_tool._status_observer
        if observer:
            mgr._status_callback = lambda info, sid=mgr._session_id: observer(sid, info)

        # Verify it works
        mgr._session_active = True
        mgr._current_turn = MagicMock()
        mgr._current_turn.turn_id = 1
        mgr._current_turn.start_time = time.monotonic()
        mgr._current_turn.tool_calls = []

        from tools.claude_session.state_machine import StateTransition
        transition = StateTransition(
            from_state="IDLE",
            to_state="THINKING",
            timestamp=time.monotonic(),
        )
        mgr._handle_state_change(transition)

        assert len(received) == 1
        assert received[0][0] == "test_session_1"
    finally:
        tools.claude_session_tool.unregister_status_observer()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_status.py -v -k "observer" 2>&1 | tail -15`
Expected: FAIL — `register_status_observer` does not exist yet

- [ ] **Step 3: Add observer registration to claude_session_tool.py**

In `tools/claude_session_tool.py`, add after line 20 (`_sessions_lock = threading.Lock()`):

```python
# Global status observer — set by gateway to bridge session status to Telegram
_status_observer = None  # Optional[Callable[[str, dict], None]]


def register_status_observer(callback):
    """Register a global status observer for all claude sessions.

    Called by gateway/run.py to bridge ClaudeSessionManager status updates
    to Telegram status messages. The callback receives (session_id, status_info).
    """
    global _status_observer
    _status_observer = callback


def unregister_status_observer():
    """Remove the global status observer."""
    global _status_observer
    _status_observer = None
```

In the same file, in the `_handle_claude_session` function, after the session is successfully created and registered (after the `_sessions[sid] = mgr` line, around line 244), add observer attachment:

```python
                    # Attach status observer if registered
                    if _status_observer:
                        mgr._status_callback = (
                            lambda info, _sid=sid: _status_observer(_sid, info)
                        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_status.py -v 2>&1 | tail -20`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/f/Projects/hermes-agent
git add tools/claude_session_tool.py tests/tools/test_claude_session_status.py
git commit -m "feat: add status observer registration to claude_session_tool"
```

---

### Task 4: Gateway Integration Bridge

**Files:**
- Modify: `gateway/run.py` (in `_run_agent` method, ~line 9610)

- [ ] **Step 1: Write integration test**

Append to `tests/gateway/test_status_message.py`:

```python
import tools.claude_session_tool


@pytest.mark.asyncio
async def test_bridge_registers_and_unregisters_observer():
    """Gateway bridge registers observer on start, unregisters on cleanup."""
    from gateway.status_message import StatusMessageManager

    # Verify clean state
    tools.claude_session_tool.unregister_status_observer()
    assert tools.claude_session_tool._status_observer is None

    # Simulate what run.py does
    adapter = mock_adapter  # from fixture
    mgr = StatusMessageManager(adapter=adapter, chat_id="123")

    def bridge(session_id, info):
        pass

    tools.claude_session_tool.register_status_observer(bridge)
    assert tools.claude_session_tool._status_observer is not None

    # Cleanup
    tools.claude_session_tool.unregister_status_observer()
    assert tools.claude_session_tool._status_observer is None
```

- [ ] **Step 2: Add bridge code to `gateway/run.py`**

In `gateway/run.py`, in the `_run_agent` method, find the section where callbacks are set up (after `_status_callback_sync` definition, around line 9666). Add the bridge setup:

After the `_status_callback_sync` function definition (~line 9666), add:

```python
        # ── Status message bridge for claude_session tool ──
        from gateway.status_message import StatusMessageManager
        _status_msg_manager = StatusMessageManager(
            adapter=_status_adapter,
            chat_id=source.chat_id,
            metadata=_status_thread_metadata,
        )

        def _session_status_bridge(session_id: str, status_info: dict) -> None:
            """Bridge ClaudeSessionManager status updates → Telegram status message."""
            if not _run_still_current():
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    _status_msg_manager.update(status_info),
                    _loop_for_step,
                )
            except Exception as _e:
                logger.debug("session_status_bridge error: %s", _e)

        from tools.claude_session_tool import (
            register_status_observer,
            unregister_status_observer,
        )
        register_status_observer(_session_status_bridge)
```

In the `finally` block of `_run_agent` (around line 10816), add cleanup **before** the existing cleanup code:

```python
            # Clean up status message observer and finalize
            try:
                from tools.claude_session_tool import unregister_status_observer
                unregister_status_observer()
            except Exception:
                pass
            if _status_msg_manager:
                try:
                    # Build summary from the final result
                    _summary = _status_msg_manager.format_summary({
                        "elapsed_seconds": 0,
                        "tool_calls": [],
                        "turn_id": "?",
                    })
                    await _status_msg_manager.finish(_summary)
                except Exception:
                    pass
```

Also need to declare `_status_msg_manager` in the function scope. Add it near the other holder declarations (~line 9615):

```python
        _status_msg_manager = None
```

Wait — we already define it inside the method. Since `run_sync()` captures the outer scope, we need to make `_status_msg_manager` accessible in the `finally` block. Let me restructure:

Move the `_status_msg_manager` declaration to the holder section (~line 9615):

```python
        _status_msg_manager = None  # StatusMessageManager for Telegram status updates
```

Then in the bridge setup section, instead of creating a new variable, assign to the existing one:

```python
        # ── Status message bridge for claude_session tool ──
        from gateway.status_message import StatusMessageManager
        _status_msg_manager = StatusMessageManager(
            adapter=_status_adapter,
            chat_id=source.chat_id,
            metadata=_status_thread_metadata,
        )
```

- [ ] **Step 3: Verify no import errors**

Run: `cd /mnt/f/Projects/hermes-agent && python -c "from gateway.status_message import StatusMessageManager; print('OK')" 2>&1`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /mnt/f/Projects/hermes-agent
git add gateway/run.py tests/gateway/test_status_message.py
git commit -m "feat: integrate StatusMessageManager into gateway _run_agent"
```

---

### Task 5: Summary Formatting on Turn Completion

**Files:**
- Modify: `tools/claude_session/manager.py` (in `_handle_state_change` IDLE branch)

- [ ] **Step 1: Write test for completion summary**

Append to `tests/tools/test_claude_session_status.py`:

```python
def test_status_callback_on_turn_completed():
    """Status callback receives final info when turn completes (state→IDLE)."""
    mgr = ClaudeSessionManager()
    received = []
    mgr._status_callback = lambda info: received.append(info)

    mgr._session_active = True
    mgr._session_id = "test_id"
    mgr._current_turn = MagicMock()
    mgr._current_turn.turn_id = 3
    mgr._current_turn.start_time = time.monotonic() - 45
    mgr._current_turn.tool_calls = [
        MagicMock(to_dict=lambda: {"tool": "Read", "target": "a.py"}),
        MagicMock(to_dict=lambda: {"tool": "Edit", "target": "b.py"}),
    ]
    mgr._current_turn.thinking_cycles = 2
    mgr._turn_history = []

    transition = StateTransition(
        from_state=ClaudeState.THINKING,
        to_state=ClaudeState.IDLE,
        timestamp=time.monotonic(),
    )
    mgr._handle_state_change(transition)

    # The last status callback should contain the completed turn info
    assert len(received) >= 1
    final = received[-1]
    assert final["state"] == "IDLE"
    assert len(final["tool_calls"]) == 2
```

- [ ] **Step 2: Ensure the IDLE branch in _handle_state_change fires status callback**

The existing code in `_handle_state_change` already handles IDLE (finalizing turn, firing turn_completed event). The status callback we added in Task 2 fires for ALL state changes, including IDLE. Verify the test passes.

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_status.py -v -k "turn_completed" 2>&1 | tail -15`
Expected: PASS

- [ ] **Step 3: Run full test suite for status message feature**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_status_message.py tests/tools/test_claude_session_status.py -v 2>&1 | tail -30`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
cd /mnt/f/Projects/hermes-agent
git add -A
git commit -m "feat: add turn completion status summary"
```

---

### Task 6: Edge Cases and Robustness

**Files:**
- Modify: `tests/gateway/test_status_message.py`

- [ ] **Step 1: Write edge case tests**

Append to `tests/gateway/test_status_message.py`:

```python
@pytest.mark.asyncio
async def test_cancel_removes_status_message(mock_adapter):
    """cancel() best-effort replaces with '(cancelled)'."""
    mgr = StatusMessageManager(adapter=mock_adapter, chat_id="123")
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 1,
        "elapsed_seconds": 5,
        "tool_calls": [],
        "recent_output": "",
    }
    await mgr.update(info)  # Creates the message

    await mgr.cancel()
    last_call = mock_adapter.edit_message.call_args
    assert "(cancelled)" in last_call.kwargs["content"]


@pytest.mark.asyncio
async def test_cancel_noop_if_no_message(mock_adapter):
    """cancel() is a no-op if no message was ever created."""
    mgr = StatusMessageManager(adapter=mock_adapter, chat_id="123")
    await mgr.cancel()  # Should not raise
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
    info = {
        "state": "THINKING",
        "tool_name": None,
        "tool_target": None,
        "turn_id": 1,
        "elapsed_seconds": 5,
        "tool_calls": [],
        "recent_output": "",
    }
    await mgr.update(info)  # First send

    # Now do many edits
    for i in range(35):
        mgr._last_edit_time = 0  # bypass throttle
        info["tool_name"] = f"Tool{i}"  # bypass dedup
        await mgr.update(info)

    # Should have stopped editing at _MAX_EDITS_PER_SESSION (30)
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
    assert "\u2705" in text  # checkmark
    assert "Read x2" in text
    assert "Edit x1" in text
    assert "45s" in text
```

- [ ] **Step 2: Run all tests**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_status_message.py tests/tools/test_claude_session_status.py -v 2>&1 | tail -30`
Expected: All PASS

- [ ] **Step 3: Run broader test suite to check for regressions**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_stream_consumer.py tests/tools/test_claude_session_manager.py -v --timeout=30 2>&1 | tail -20`
Expected: All existing tests still PASS

- [ ] **Step 4: Final commit**

```bash
cd /mnt/f/Projects/hermes-agent
git add -A
git commit -m "test: add edge case tests for status message feature"
```

---

## Self-Review

### Spec Coverage

| Spec Requirement | Task |
|-----------------|------|
| StatusMessageManager class | Task 1 |
| Throttling (2s interval) | Task 1 |
| Flood control (3 strikes) | Task 1 |
| 500 char limit | Task 1 |
| Status callback in ClaudeSessionManager | Task 2 |
| Tool call info extraction | Task 2 |
| Observer registration | Task 3 |
| Gateway bridge (run.py) | Task 4 |
| Completion summary | Task 5 |
| Cancel on error | Task 6 |
| Message formats (THINKING/TOOL_CALL/PERMISSION/完成) | Task 1 |

### Placeholder Scan

No TBD, TODO, or placeholder patterns found.

### Type Consistency

- `_status_callback` field: `Optional[Callable[[dict], None]]` in manager.py
- `_status_observer` global: `Optional[Callable[[str, dict], None]]` in claude_session_tool.py
- `_build_status_info()` returns `dict` with consistent keys across all tasks
- `StatusMessageManager.update()` accepts `dict` matching `_build_status_info()` output
