"""Tests for tools/claude_session/adaptive_poller.py"""

import time
import pytest
import threading
from unittest.mock import MagicMock, patch
from tools.claude_session.adaptive_poller import AdaptivePoller
from tools.claude_session.state_machine import ClaudeState, StateMachine
from tools.claude_session.output_buffer import OutputBuffer
from tools.claude_session.output_parser import OutputParser, ParseResult


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
