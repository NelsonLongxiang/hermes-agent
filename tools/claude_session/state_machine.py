"""tools/claude_session/state_machine.py — 7-state machine for Claude Code TUI."""

import re
import time
from dataclasses import dataclass, field
from typing import Optional


class ClaudeState:
    """State constants and configuration."""

    IDLE = "IDLE"
    INPUTTING = "INPUTTING"
    THINKING = "THINKING"
    TOOL_CALL = "TOOL_CALL"
    PERMISSION = "PERMISSION"
    ERROR = "ERROR"
    DISCONNECTED = "DISCONNECTED"

    ALL = frozenset({
        IDLE, INPUTTING, THINKING, TOOL_CALL, PERMISSION, ERROR, DISCONNECTED,
    })

    ACTIVE_STATES = frozenset({THINKING, TOOL_CALL, PERMISSION})

    # Adaptive polling intervals per state
    POLL_INTERVALS: dict = {
        IDLE: 3.0,
        INPUTTING: 0.5,
        THINKING: 1.0,
        TOOL_CALL: 0.5,
        PERMISSION: 0.3,
        ERROR: 1.0,
        DISCONNECTED: 5.0,
    }

    # TUI output patterns for state detection
    TUI_PATTERNS = {
        "idle": re.compile(r"^❯\s*$"),
        "tool_call": re.compile(r"^●\s+(\w+)"),
        "permission": re.compile(r"Allow\s+.*\?|permission\s+to|❯\s*(Allow|Yes)\b", re.IGNORECASE),
        "error": re.compile(r"Error:|Failed:|error:", re.IGNORECASE),
    }


# Valid state transitions (from → set of allowed to states)
VALID_TRANSITIONS: dict = {
    ClaudeState.DISCONNECTED: {ClaudeState.IDLE, ClaudeState.ERROR, ClaudeState.DISCONNECTED},
    ClaudeState.IDLE: {ClaudeState.THINKING, ClaudeState.INPUTTING, ClaudeState.DISCONNECTED, ClaudeState.ERROR},
    ClaudeState.INPUTTING: {ClaudeState.THINKING, ClaudeState.IDLE, ClaudeState.DISCONNECTED, ClaudeState.ERROR},
    ClaudeState.THINKING: {
        ClaudeState.TOOL_CALL, ClaudeState.PERMISSION, ClaudeState.IDLE,
        ClaudeState.ERROR, ClaudeState.DISCONNECTED,
    },
    ClaudeState.TOOL_CALL: {
        ClaudeState.THINKING, ClaudeState.PERMISSION, ClaudeState.IDLE,
        ClaudeState.ERROR, ClaudeState.DISCONNECTED,
    },
    ClaudeState.PERMISSION: {
        ClaudeState.THINKING, ClaudeState.IDLE,
        ClaudeState.ERROR, ClaudeState.DISCONNECTED,
    },
    ClaudeState.ERROR: {
        ClaudeState.THINKING, ClaudeState.IDLE,
        ClaudeState.DISCONNECTED, ClaudeState.ERROR,
    },
}


def is_valid_transition(from_state: str, to_state: str) -> bool:
    """Check if a state transition is valid."""
    allowed = VALID_TRANSITIONS.get(from_state, set())
    return to_state in allowed


@dataclass
class StateTransition:
    """Record of a state change."""
    from_state: str
    to_state: str
    timestamp: float
    event_name: Optional[str] = None
    tool_name: Optional[str] = None
    tool_target: Optional[str] = None


class StateMachine:
    """Thread-safe state machine tracking Claude Code's TUI state."""

    def __init__(self):
        import threading
        self._state = ClaudeState.DISCONNECTED
        self._state_entered = time.monotonic()
        self._lock = threading.Lock()
        self._transition_log: list = []

    @property
    def current_state(self) -> str:
        with self._lock:
            return self._state

    def state_duration(self) -> float:
        """Seconds since entering current state."""
        with self._lock:
            return time.monotonic() - self._state_entered

    def transition(self, new_state: str) -> Optional[StateTransition]:
        """Attempt a state transition. Returns the transition record, or None if same state."""
        with self._lock:
            if new_state == self._state:
                return None
            old_state = self._state
            # Lenient: allow any transition for robustness (TUI parsing is imperfect)
            self._state = new_state
            self._state_entered = time.monotonic()
            t = StateTransition(
                from_state=old_state,
                to_state=new_state,
                timestamp=self._state_entered,
            )
            self._transition_log.append(t)
            return t

    def get_transition_log(self, since: float = 0.0) -> list:
        """Return transitions since given monotonic timestamp."""
        with self._lock:
            return [t for t in self._transition_log if t.timestamp >= since]
