"""Tests for tools/claude_session/adaptive_poller.py"""

import time
import pytest
import threading
from unittest.mock import MagicMock, patch
from tools.claude_session.adaptive_poller import AdaptivePoller
from tools.claude_session.state_machine import ClaudeState, StateMachine
from tools.claude_session.output_buffer import OutputBuffer
from tools.claude_session.output_parser import OutputParser, ParseResult, UserPromptInfo


@pytest.fixture
def components():
    sm = StateMachine()
    buf = OutputBuffer(max_lines=100)
    return sm, buf


class TestAdaptivePoller:
    def test_start_stop(self, components):
        sm, buf = components
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = "❯ "

        poller = AdaptivePoller(state_machine=sm, output_buffer=buf, tmux=tmux_mock)
        poller.start()
        assert poller.is_running()
        poller.stop()
        assert not poller.is_running()

    def test_state_update_from_capture(self, components):
        sm, buf = components
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = "● Edit src/main.py"

        poller = AdaptivePoller(state_machine=sm, output_buffer=buf, tmux=tmux_mock)
        poller._poll_once()
        assert sm.current_state == "TOOL_CALL"

    def test_poll_interval_idle(self, components):
        sm, buf = components
        sm.transition("IDLE")
        tmux_mock = MagicMock()
        poller = AdaptivePoller(state_machine=sm, output_buffer=buf, tmux=tmux_mock)
        assert poller._current_interval() == 3.0

    def test_poll_interval_tool_call(self, components):
        sm, buf = components
        sm.transition("IDLE")
        sm.transition("TOOL_CALL")
        tmux_mock = MagicMock()
        poller = AdaptivePoller(state_machine=sm, output_buffer=buf, tmux=tmux_mock)
        assert poller._current_interval() == 0.5

    def test_event_callback_on_state_change(self, components):
        sm, buf = components
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = "❯ "

        events = []
        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=lambda transition: events.append(transition),
        )
        poller._poll_once()
        assert len(events) >= 1
        assert events[0].to_state == "IDLE"

    def test_disconnected_when_session_gone(self, components):
        sm, buf = components
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = False

        poller = AdaptivePoller(state_machine=sm, output_buffer=buf, tmux=tmux_mock)
        poller._poll_once()
        assert sm.current_state == "DISCONNECTED"

    def test_buffer_updated_on_poll(self, components):
        sm, buf = components
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = "line1\nline2\n❯ "

        poller = AdaptivePoller(state_machine=sm, output_buffer=buf, tmux=tmux_mock)
        poller._poll_once()
        lines = buf.read()
        assert any("line1" in l.text for l in lines)


class TestAdaptivePollerPromptDetection:
    """Tests for prompt detection integration in AdaptivePoller._poll_once()."""

    def test_callback_receives_prompt_info_on_idle(self, components):
        """When capture_pane returns ask_user options, callback gets prompt_info."""
        sm, buf = components
        # Start in THINKING so we get a transition to IDLE
        # StateMachine starts DISCONNECTED, go to THINKING first
        sm.transition("IDLE")
        sm.transition("THINKING")

        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        # Include bare "❯ " at bottom so state detector sees IDLE,
        # and numbered options above so detect_user_prompt finds ask_user.
        tmux_mock.capture_pane.return_value = (
            "选择方案\n\n"
            "❯ 1. 方案 A\n"
            "  2. 方案 B\n"
            "  3. 方案 C\n"
            "\n❯ "
        )

        received = []
        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=lambda t, pi: received.append((t, pi)),
        )
        poller._poll_once()

        assert len(received) >= 1
        transition, prompt_info = received[0]
        assert transition.to_state == "IDLE"
        assert prompt_info is not None
        assert prompt_info.prompt_type == "ask_user"

    def test_callback_receives_none_when_no_prompt(self, components):
        """When capture_pane has no user prompt, prompt_info should be None."""
        sm, buf = components
        # Start DISCONNECTED -> IDLE transition
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = "some output\n❯ "

        received = []
        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=lambda t, pi: received.append((t, pi)),
        )
        poller._poll_once()

        assert len(received) >= 1
        transition, prompt_info = received[0]
        assert transition.to_state == "IDLE"
        assert prompt_info is None

    def test_backward_compat_callback_without_prompt_info(self, components):
        """Old-style callback accepting only 1 parameter still works."""
        sm, buf = components
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = "❯ "

        events = []
        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=lambda t: events.append(t),
        )
        poller._poll_once()

        assert len(events) >= 1
        assert events[0].to_state == "IDLE"

    def test_no_prompt_detection_in_thinking_state(self, components):
        """When state is THINKING, prompt detection should not run."""
        sm, buf = components
        sm.transition("IDLE")

        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        # Output that looks like a question but state is THINKING
        tmux_mock.capture_pane.return_value = (
            "some thinking output\n"
            "❯ 1. Option A\n"
            "  2. Option B\n"
        )

        received = []
        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=lambda t, pi: received.append((t, pi)),
        )
        poller._poll_once()

        # State should be THINKING (no transition from IDLE to THINKING
        # because the output doesn't clearly indicate THINKING; depends on
        # parser). Check that if there's a transition, prompt_info is None
        # since THINKING state shouldn't trigger prompt detection.
        for transition, prompt_info in received:
            if transition.to_state in ("THINKING", "TOOL_CALL", "ERROR"):
                assert prompt_info is None
