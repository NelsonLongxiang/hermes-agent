"""Tests for tools/claude_session/state_machine.py"""

import pytest
from tools.claude_session.state_machine import (
    ClaudeState,
    StateMachine,
    StateTransition,
    is_valid_transition,
)


class TestClaudeState:
    def test_all_states_defined(self):
        expected = {"IDLE", "INPUTTING", "THINKING", "TOOL_CALL", "PERMISSION", "ERROR", "DISCONNECTED"}
        assert set(ClaudeState.ALL) == expected

    def test_poll_intervals(self):
        assert ClaudeState.POLL_INTERVALS["IDLE"] == 3.0
        assert ClaudeState.POLL_INTERVALS["TOOL_CALL"] == 0.5
        assert ClaudeState.POLL_INTERVALS["PERMISSION"] == 0.3

    def test_tui_patterns(self):
        assert ClaudeState.TUI_PATTERNS["idle"] is not None
        assert ClaudeState.TUI_PATTERNS["tool_call"] is not None


class TestStateMachine:
    def test_initial_state_is_disconnected(self):
        sm = StateMachine()
        assert sm.current_state == "DISCONNECTED"

    def test_valid_transition(self):
        sm = StateMachine()
        transition = sm.transition("IDLE")
        assert transition.from_state == "DISCONNECTED"
        assert transition.to_state == "IDLE"

    def test_same_state_returns_none(self):
        sm = StateMachine()
        sm.transition("IDLE")
        result = sm.transition("IDLE")
        assert result is None

    def test_state_duration(self):
        sm = StateMachine()
        sm.transition("IDLE")
        import time
        time.sleep(0.1)
        assert sm.state_duration() >= 0.05

    def test_transition_log(self):
        sm = StateMachine()
        sm.transition("IDLE")
        sm.transition("THINKING")
        log = sm.get_transition_log()
        assert len(log) == 2

    def test_transition_log_since(self):
        sm = StateMachine()
        sm.transition("IDLE")
        import time
        time.sleep(0.05)
        cutoff = time.monotonic()
        sm.transition("THINKING")
        log = sm.get_transition_log(since=cutoff)
        assert len(log) == 1
        assert log[0].to_state == "THINKING"


class TestIsValidTransition:
    def test_send_starts_thinking(self):
        assert is_valid_transition("IDLE", "THINKING")

    def test_thinking_to_tool_call(self):
        assert is_valid_transition("THINKING", "TOOL_CALL")

    def test_tool_call_to_thinking(self):
        assert is_valid_transition("TOOL_CALL", "THINKING")

    def test_any_to_error(self):
        for state in ClaudeState.ALL:
            assert is_valid_transition(state, "ERROR")

    def test_any_to_disconnected(self):
        for state in ClaudeState.ALL:
            assert is_valid_transition(state, "DISCONNECTED")

    def test_idle_to_inputting(self):
        assert is_valid_transition("IDLE", "INPUTTING")

    def test_inputting_to_thinking(self):
        assert is_valid_transition("INPUTTING", "THINKING")

    def test_inputting_to_idle(self):
        assert is_valid_transition("INPUTTING", "IDLE")
