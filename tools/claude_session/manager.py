"""tools/claude_session/manager.py — Main ClaudeSessionManager class."""

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from tools.claude_session.state_machine import ClaudeState, StateMachine, StateTransition
from tools.claude_session.output_buffer import OutputBuffer
from tools.claude_session.output_parser import OutputParser
from tools.claude_session.tmux_interface import TmuxInterface
from tools.claude_session.adaptive_poller import AdaptivePoller

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """A single tool call within a Turn."""
    tool_name: str
    target: Optional[str]
    start_time: float
    end_time: Optional[float] = None
    duration: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool_name,
            "target": self.target,
            "duration": self.duration,
        }


@dataclass
class Turn:
    """Tracks a full Hermes→Claude interaction cycle."""
    turn_id: int
    start_time: float
    end_time: Optional[float] = None
    state: str = ClaudeState.THINKING
    user_message: str = ""
    tool_calls: list = field(default_factory=list)
    thinking_cycles: int = 0
    total_duration: Optional[float] = None
    output_marker: int = 0  # OutputBuffer marker at send time

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "user_message": self.user_message,
            "total_duration": self.total_duration,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "thinking_cycles": self.thinking_cycles,
            "elapsed_seconds": (
                (self.end_time or time.monotonic()) - self.start_time
            ),
        }

    def finalize(self) -> None:
        """Mark turn as completed."""
        self.end_time = time.monotonic()
        self.total_duration = self.end_time - self.start_time
        for tc in self.tool_calls:
            if tc.end_time is None:
                tc.end_time = self.end_time
                tc.duration = tc.end_time - tc.start_time


class ClaudeSessionManager:
    """Singleton manager for Claude Code tmux sessions.

    Provides the full API: start, send, status, wait_for_idle, output, etc.
    """

    def __init__(self):
        self._session_id: Optional[str] = None
        self._tmux: Optional[TmuxInterface] = None
        self._sm = StateMachine()
        self._buf = OutputBuffer(max_lines=1000)
        self._poller: Optional[AdaptivePoller] = None
        self._session_active = False
        self._permission_mode = "normal"

        # Turn tracking
        self._turn_counter = 0
        self._current_turn: Optional[Turn] = None
        self._turn_history: list = []

        # Event system
        self._event_queue: queue.Queue = queue.Queue()
        self._event_mode = "notify"  # "notify" | "queue" | "none"
        self._on_event = None

        # Threading
        self._lock = threading.Lock()
        self._state_event = threading.Event()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        workdir: str,
        session_name: str = "claude-work",
        model: Optional[str] = None,
        permission_mode: str = "normal",
        on_event: str = "notify",
        completion_queue: Optional[queue.Queue] = None,
    ) -> dict:
        """Start a Claude Code session in tmux."""
        with self._lock:
            if self._session_active and self._tmux and self._tmux.session_exists():
                return {
                    "session_id": self._session_id,
                    "tmux_session": session_name,
                    "state": self._sm.current_state,
                    "permission_mode": self._permission_mode,
                    "note": "Session already active",
                }

            if permission_mode not in ("normal", "skip"):
                return {"error": f"Invalid permission_mode: {permission_mode}"}

            self._session_id = f"cs_{uuid.uuid4().hex[:8]}"
            self._permission_mode = permission_mode
            self._event_mode = on_event

            self._tmux = TmuxInterface(session_name)

            try:
                if not self._tmux.session_exists():
                    self._tmux.create_session(workdir=workdir)
                    # Start Claude Code
                    claude_cmd = "claude"
                    if permission_mode == "skip":
                        claude_cmd = "claude --dangerously-skip-permissions"
                    if model:
                        claude_cmd += f" --model {model}"
                    self._tmux.send_keys(claude_cmd, enter=True)
                    # Wait briefly for Claude Code to start
                    time.sleep(2)

                    # Handle skip permission warning dialog
                    if permission_mode == "skip":
                        time.sleep(1)
                        self._tmux.send_special_key("Down")
                        time.sleep(0.3)
                        self._tmux.send_special_key("Enter")
                        time.sleep(1)

            except Exception as e:
                return {"error": f"Failed to start session: {e}"}

            # Start polling
            self._poller = AdaptivePoller(
                state_machine=self._sm,
                output_buffer=self._buf,
                tmux=self._tmux,
                on_state_change=self._handle_state_change,
            )
            self._poller.start()
            self._session_active = True

            # Store completion_queue for event injection
            if completion_queue:
                self._on_event = completion_queue

            return {
                "session_id": self._session_id,
                "tmux_session": session_name,
                "state": self._sm.current_state,
                "permission_mode": self._permission_mode,
            }

    def stop(self) -> dict:
        """Stop the session and clean up."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}

            # Stop polling first
            if self._poller:
                self._poller.stop()

            # Finalize current turn
            if self._current_turn and self._current_turn.end_time is None:
                self._current_turn.finalize()
                self._turn_history.append(self._current_turn)
                self._current_turn = None

            # Kill tmux session
            if self._tmux:
                try:
                    self._tmux.kill_session()
                except Exception:
                    pass

            sid = self._session_id
            self._session_active = False
            self._session_id = None
            self._sm.transition(ClaudeState.DISCONNECTED)

            return {"stopped": True, "session_id": sid}

    # ------------------------------------------------------------------
    # Send operations
    # ------------------------------------------------------------------

    def send(self, message: str) -> dict:
        """Send a message atomically (type + Enter in one command)."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if not self._tmux:
                return {"error": "No tmux interface"}

            # Create a new Turn
            self._turn_counter += 1
            marker = self._buf.total_count()
            turn = Turn(
                turn_id=self._turn_counter,
                start_time=time.monotonic(),
                user_message=message,
                output_marker=marker,
            )
            self._current_turn = turn

            # Atomic send
            self._tmux.send_keys(message, enter=True)

        # Poll outside lock to avoid deadlock
        if self._poller:
            self._poller.poll_now()

        return {"sent": True, "state": self._sm.current_state}

    def type_text(self, text: str) -> dict:
        """Type text without pressing Enter (for multi-line input)."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            self._tmux.send_keys(text)
            self._sm.transition(ClaudeState.INPUTTING)
            return {"typed": True, "state": self._sm.current_state}

    def submit(self) -> dict:
        """Submit typed text by pressing Enter."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            self._tmux.send_special_key("Enter")

            # Create Turn if not exists
            if self._current_turn is None:
                self._turn_counter += 1
                self._current_turn = Turn(
                    turn_id=self._turn_counter,
                    start_time=time.monotonic(),
                    user_message="(typed input)",
                    output_marker=self._buf.total_count(),
                )

        # Poll outside lock to avoid deadlock
        if self._poller:
            self._poller.poll_now()
        return {"submitted": True, "state": self._sm.current_state}

    def cancel_input(self) -> dict:
        """Cancel current input (Ctrl+C)."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            self._tmux.send_special_key("C-c")
            self._sm.transition(ClaudeState.IDLE)
            return {"cancelled": True, "state": self._sm.current_state}

    # ------------------------------------------------------------------
    # Status and wait
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return current session state."""
        if not self._session_active:
            return {"state": ClaudeState.DISCONNECTED}

        result = {
            "state": self._sm.current_state,
            "state_duration_seconds": round(self._sm.state_duration(), 1),
            "output_tail": self._buf.last_n_chars(200),
        }

        if self._current_turn:
            result["current_turn"] = self._current_turn.to_dict()

        return result

    def wait_for_idle(self, timeout: int = 300) -> dict:
        """Block until Claude returns to IDLE or timeout."""
        if not self._session_active:
            return {"error": "No active session"}

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            state = self._sm.current_state

            if state == ClaudeState.IDLE:
                return self._build_idle_result()

            if state == ClaudeState.PERMISSION:
                return self._build_permission_result()

            if state == ClaudeState.ERROR:
                return self._build_error_result()

            if state == ClaudeState.DISCONNECTED:
                return {"error": "Session disconnected", "state": ClaudeState.DISCONNECTED}

            # Wait for state change event (set by _handle_state_change)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._state_event.clear()
            self._state_event.wait(timeout=min(0.5, remaining))

        # Timeout
        result = {
            "state": self._sm.current_state,
            "timeout_reached": True,
        }
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        result["output_since_send"] = self._get_output_since_send()
        return result

    def wait_for_state(self, target_state: str, timeout: int = 60) -> dict:
        """Wait for a specific state."""
        if not self._session_active:
            return {"error": "No active session"}

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sm.current_state == target_state:
                return {
                    "state": target_state,
                    "duration_seconds": round(self._sm.state_duration(), 1),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._state_event.clear()
            self._state_event.wait(timeout=min(0.3, remaining))

        return {"state": self._sm.current_state, "timeout_reached": True}

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def output(self, offset: int = 0, limit: int = 50) -> dict:
        """Get output lines with pagination."""
        lines = self._buf.read(offset=offset, limit=limit)
        return {
            "lines": [{"text": l.text, "index": l.index} for l in lines],
            "total": self._buf.total_count(),
            "has_more": (offset + limit) < self._buf.total_count(),
        }

    # ------------------------------------------------------------------
    # Permission handling
    # ------------------------------------------------------------------

    def respond_permission(self, response: str) -> dict:
        """Respond to a permission request."""
        if self._sm.current_state != ClaudeState.PERMISSION:
            return {"error": "Not in PERMISSION state"}

        if response == "allow":
            self._tmux.send_keys("y", enter=True)
        elif response == "deny":
            self._tmux.send_keys("n", enter=True)
        else:
            return {"error": f"Invalid response: {response}"}

        if self._poller:
            self._poller.poll_now()
        return {"responded": True, "state": self._sm.current_state}

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def history(self) -> dict:
        """Return turn history from OutputBuffer."""
        turns = []
        all_turns = list(self._turn_history)
        if self._current_turn and self._current_turn.end_time is not None:
            all_turns.append(self._current_turn)

        for t in all_turns:
            turns.append({
                "role": "user",
                "message": t.user_message,
                "turn_id": t.turn_id,
                "tools": [tc.to_dict() for tc in t.tool_calls],
                "duration": t.total_duration,
            })

        all_tools = sum(len(t.tool_calls) for t in all_turns)
        return {
            "turns": turns,
            "total_turns": len(turns),
            "total_tools_called": all_tools,
        }

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def events(self, since_turn: int = 0) -> dict:
        """Get queued events since a given turn."""
        collected = []
        while not self._event_queue.empty():
            try:
                evt = self._event_queue.get_nowait()
                if evt.get("turn_id", 0) >= since_turn:
                    collected.append(evt)
            except queue.Empty:
                break
        return {"events": collected}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_state_change(self, transition: StateTransition) -> None:
        """Called by poller on state changes. Updates turns and fires events.

        Runs in the poller background thread. All shared state mutations
        are protected by self._lock.
        """
        now = time.monotonic()

        with self._lock:
            # Update current turn
            if self._current_turn:
                if transition.to_state == ClaudeState.TOOL_CALL:
                    tc = ToolCall(
                        tool_name=getattr(transition, "tool_name", "Unknown") or "Unknown",
                        target=getattr(transition, "tool_target", None),
                        start_time=now,
                    )
                    self._current_turn.tool_calls.append(tc)
                elif transition.to_state == ClaudeState.THINKING:
                    self._current_turn.thinking_cycles += 1
                elif transition.to_state == ClaudeState.IDLE:
                    self._current_turn.finalize()
                    self._turn_history.append(self._current_turn)
                    self._fire_event("turn_completed", {
                        "turn_id": self._current_turn.turn_id,
                        "duration": self._current_turn.total_duration,
                        "tool_calls": [tc.to_dict() for tc in self._current_turn.tool_calls],
                    })
                    self._current_turn = None

            # Fire state_changed event
            self._fire_event("state_changed", {
                "from_state": transition.from_state,
                "to_state": transition.to_state,
            })

        # Signal waiters outside lock
        self._state_event.set()

    def _fire_event(self, event_type: str, data: dict) -> None:
        """Push event to queue and optionally to completion_queue."""
        if self._event_mode == "none":
            return

        evt = {"type": event_type, "timestamp": time.time(), **data}
        self._event_queue.put(evt)

        if self._event_mode == "notify" and self._on_event:
            try:
                self._on_event.put(evt)
            except Exception:
                pass

    def _build_idle_result(self) -> dict:
        """Build result for wait_for_idle when state is IDLE."""
        result = {"state": ClaudeState.IDLE}
        # Check turn_history for the latest completed turn
        if self._turn_history:
            last_turn = self._turn_history[-1]
            result["turn"] = last_turn.to_dict()
        result["output_since_send"] = self._get_output_since_send()
        return result

    def _build_permission_result(self) -> dict:
        """Build result for wait_for_idle when state is PERMISSION."""
        result = {"state": ClaudeState.PERMISSION}
        # Single read call, then slice for last 10 lines
        all_lines = self._buf.read()
        last_lines = all_lines[-10:] if len(all_lines) > 10 else all_lines
        for line in reversed(last_lines):
            if "allow" in line.text.lower() or "permission" in line.text.lower():
                result["permission_request"] = line.text
                break
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        return result

    def _build_error_result(self) -> dict:
        """Build result for wait_for_idle when state is ERROR."""
        result = {"state": ClaudeState.ERROR}
        tail = self._buf.last_n_chars(500)
        result["error_output"] = tail
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        return result

    def _get_output_since_send(self) -> str:
        """Get all output since last send operation."""
        if self._current_turn:
            lines = self._buf.since(self._current_turn.output_marker)
        elif self._turn_history:
            last = self._turn_history[-1]
            lines = self._buf.since(last.output_marker)
        else:
            lines = self._buf.read()
        return "\n".join(l.text for l in lines)
