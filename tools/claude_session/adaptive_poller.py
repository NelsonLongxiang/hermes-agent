"""tools/claude_session/adaptive_poller.py — Adaptive polling engine."""

import inspect
import logging
import threading
import time
from typing import Callable, Optional

from tools.claude_session.state_machine import (
    ClaudeState, StateMachine, StateTransition,
)
from tools.claude_session.output_buffer import OutputBuffer
from tools.claude_session.output_parser import OutputParser, UserPromptInfo
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
        """Perform a single poll cycle.

        After detecting state, also detects user-input prompts when the state
        is IDLE or PERMISSION. Fires the callback with both the transition
        (if any) and the prompt_info (if detected).

        If no state transition occurred but a prompt was detected, a synthetic
        StateTransition is created so the callback still fires.
        """
        if not self._tmux.session_exists():
            transition = self._sm.transition(ClaudeState.DISCONNECTED)
            if transition:
                self._fire_callback(transition, None)
            return

        raw = self._tmux.capture_pane()
        lines = OutputParser.clean_lines(raw)

        # Update buffer with new lines
        if lines:
            self._buf.append_batch(lines)

        # Detect and update state
        result = OutputParser.detect_state(lines)
        transition = self._sm.transition(result.state)

        # Detect user-input prompt when in IDLE or PERMISSION state
        prompt_info: Optional[UserPromptInfo] = None
        if result.state in ("IDLE", "PERMISSION"):
            prompt_info = OutputParser.detect_user_prompt(lines, result.state)

        if transition:
            transition.tool_name = result.tool_name
            transition.tool_target = result.tool_target
            self._fire_callback(transition, prompt_info)
        elif prompt_info:
            # No state transition, but prompt detected — fire synthetic callback
            synthetic = StateTransition(
                from_state=self._sm.current_state,
                to_state=self._sm.current_state,
                timestamp=time.monotonic(),
            )
            self._fire_callback(synthetic, prompt_info)

    def _fire_callback(
        self,
        transition: StateTransition,
        prompt_info: Optional[UserPromptInfo],
    ) -> None:
        """Fire the state-change callback with backward compatibility.

        Uses inspect.signature to detect whether the callback accepts 1 or 2
        parameters, calling it appropriately so old callbacks still work.
        """
        if not self._on_state_change:
            return
        try:
            sig = inspect.signature(self._on_state_change)
            param_count = len(sig.parameters)
        except (ValueError, TypeError):
            param_count = 1
        if param_count >= 2:
            self._on_state_change(transition, prompt_info)
        else:
            self._on_state_change(transition)

    def _current_interval(self) -> float:
        """Get the polling interval for the current state."""
        return ClaudeState.POLL_INTERVALS.get(self._sm.current_state, 2.0)

    def poll_now(self) -> None:
        """Force an immediate poll (used after send operations)."""
        self._poll_once()
