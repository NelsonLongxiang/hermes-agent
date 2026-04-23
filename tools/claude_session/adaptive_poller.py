"""tools/claude_session/adaptive_poller.py — Adaptive polling engine."""

import logging
import threading
import time
from typing import Callable, Optional

from tools.claude_session.state_machine import (
    ClaudeState, StateMachine, StateTransition,
)
from tools.claude_session.output_buffer import OutputBuffer
from tools.claude_session.output_parser import OutputParser
from tools.claude_session.tmux_interface import TmuxInterface

logger = logging.getLogger(__name__)


class AdaptivePoller:
    """Background thread that polls tmux and updates state machine.

    Adapts its polling interval based on the current state.
    Fires callbacks on state changes.
    """

    def __init__(
        self,
        state_machine: StateMachine,
        output_buffer: OutputBuffer,
        tmux: TmuxInterface,
        on_state_change: Optional[Callable[[StateTransition], None]] = None,
    ):
        self._sm = state_machine
        self._buf = output_buffer
        self._tmux = tmux
        self._on_state_change = on_state_change
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the polling thread."""
        if self.is_running():
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="claude-session-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the polling thread."""
        self._stop_event.set()
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _poll_loop(self) -> None:
        """Main polling loop."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logger.error("Poll error: %s", e)
            interval = self._current_interval()
            self._stop_event.wait(timeout=interval)

    def _poll_once(self) -> None:
        """Perform a single poll cycle."""
        if not self._tmux.session_exists():
            transition = self._sm.transition(ClaudeState.DISCONNECTED)
            if transition and self._on_state_change:
                self._on_state_change(transition)
            return

        raw = self._tmux.capture_pane()
        lines = OutputParser.clean_lines(raw)

        # Update buffer with new lines
        if lines:
            self._buf.append_batch(lines)

        # Detect and update state
        result = OutputParser.detect_state(lines)
        transition = self._sm.transition(result.state)
        if transition and self._on_state_change:
            transition.tool_name = result.tool_name
            transition.tool_target = result.tool_target
            self._on_state_change(transition)

    def _current_interval(self) -> float:
        """Get the polling interval for the current state."""
        return ClaudeState.POLL_INTERVALS.get(self._sm.current_state, 2.0)

    def poll_now(self) -> None:
        """Force an immediate poll (used after send operations)."""
        self._poll_once()
