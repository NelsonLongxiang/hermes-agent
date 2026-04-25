"""Tests for tools/claude_session/auto_responder.py"""

import time
import pytest
from unittest.mock import MagicMock, patch, call

from tools.claude_session.auto_responder import AutoResponder, AutoResponderConfig, AutoResponseLog
from tools.claude_session.decision_engine import Decision
from tools.claude_session.output_parser import UserPromptInfo
from tools.claude_session.state_machine import StateMachine
from tools.claude_session.tmux_interface import TmuxInterface


# -- Helpers ---------------------------------------------------------------

def _make_prompt(
    prompt_type="ask_user",
    question="选择方案",
    options=None,
    selected_index=0,
    has_other=False,
):
    return UserPromptInfo(
        prompt_type=prompt_type,
        question=question,
        options=options or ["选项 A", "选项 B", "选项 C"],
        selected_index=selected_index,
        has_other=has_other,
        raw_context="mock",
    )


@pytest.fixture
def tmux_mock():
    return MagicMock(spec=TmuxInterface)


@pytest.fixture
def engine_mock():
    return MagicMock()


@pytest.fixture
def responder(tmux_mock, engine_mock):
    config = AutoResponderConfig(max_auto_responses_per_turn=5, cooldown_seconds=0.0)
    sm = StateMachine()
    sm.transition("IDLE")
    return AutoResponder(
        decision_engine=engine_mock,
        tmux=tmux_mock,
        state_machine=sm,
        config=config,
    )


# -- TestAutoResponderSelect (3 tests) ------------------------------------

class TestAutoResponderSelect:

    def test_navigate_down_and_enter(self, responder, tmux_mock, engine_mock):
        """selected_index=1, target option 3 -> 2 Down + Enter.
        Options: [A, B, C, D], cursor at index 1 (B), want index 3 (D).
        LLM returns value=4 (1-based), converted to 0-based target=3.
        delta = 3 - 1 = 2 Down."""
        prompt = _make_prompt(
            options=["选项 A", "选项 B", "选项 C", "选项 D"],
            selected_index=1,
        )
        engine_mock.decide.return_value = Decision(
            action="select", value=4, reasoning="pick option 4",
        )
        responder.handle_prompt(prompt, {})

        calls = tmux_mock.send_special_key.call_args_list
        assert calls == [call("Down"), call("Down"), call("Enter")]

    def test_navigate_up_and_enter(self, responder, tmux_mock, engine_mock):
        """selected_index=2, target option 1 -> 2 Up + Enter.
        Options: [A, B, C], cursor at index 2 (C), want index 0 (A).
        LLM returns value=1 (1-based), converted to 0-based target=0.
        delta = 0 - 2 = -2, so 2 Up."""
        prompt = _make_prompt(selected_index=2)
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="pick option 1",
        )
        responder.handle_prompt(prompt, {})

        calls = tmux_mock.send_special_key.call_args_list
        assert calls == [call("Up"), call("Up"), call("Enter")]

    def test_no_navigation_when_already_selected(self, responder, tmux_mock, engine_mock):
        """selected_index=1, target option 2 -> just Enter."""
        prompt = _make_prompt(selected_index=1)
        engine_mock.decide.return_value = Decision(
            action="select", value=2, reasoning="pick option 2",
        )
        responder.handle_prompt(prompt, {})

        calls = tmux_mock.send_special_key.call_args_list
        assert calls == [call("Enter")]


# -- TestAutoResponderSelectAndType (1 test) -------------------------------

class TestAutoResponderSelectAndType:

    def test_navigate_to_other_and_type(self, responder, tmux_mock, engine_mock):
        """4 options with 'Type something.', select_and_type action.
        Navigate to last option (3 Down from index 0), Enter, wait, type text."""
        prompt = _make_prompt(
            options=["Option A", "Option B", "Option C", "Type something."],
            selected_index=0,
            has_other=True,
        )
        engine_mock.decide.return_value = Decision(
            action="select_and_type", value="custom text", reasoning="need custom",
        )

        with patch("tools.claude_session.auto_responder.time.sleep") as mock_sleep:
            responder.handle_prompt(prompt, {})

        # 3 Down to reach last option (index 3), then Enter
        special_calls = tmux_mock.send_special_key.call_args_list
        assert special_calls == [call("Down"), call("Down"), call("Down"), call("Enter")]

        # Then send_keys with custom text + enter
        tmux_mock.send_keys.assert_called_once_with("custom text", enter=True)


# -- TestAutoResponderFreeText (1 test) ------------------------------------

class TestAutoResponderFreeText:

    def test_type_text_and_enter(self, responder, tmux_mock, engine_mock):
        """action='text' -> send_keys(text, enter=True), no special keys."""
        prompt = _make_prompt(prompt_type="free_text", question="请输入描述", options=[])
        engine_mock.decide.return_value = Decision(
            action="text", value="some text", reasoning="answer question",
        )
        responder.handle_prompt(prompt, {})

        tmux_mock.send_keys.assert_called_once_with("some text", enter=True)
        tmux_mock.send_special_key.assert_not_called()


# -- TestAutoResponderConfirm (2 tests) ------------------------------------

class TestAutoResponderConfirm:

    def test_confirm_yes(self, responder, tmux_mock, engine_mock):
        """options=['Yes','No'], selected_index=0, value=True -> just Enter."""
        prompt = _make_prompt(
            prompt_type="confirmation",
            question="Do you want to proceed?",
            options=["Yes", "No"],
            selected_index=0,
        )
        engine_mock.decide.return_value = Decision(
            action="confirm", value=True, reasoning="proceed",
        )
        responder.handle_prompt(prompt, {})

        calls = tmux_mock.send_special_key.call_args_list
        assert calls == [call("Enter")]

    def test_confirm_no(self, responder, tmux_mock, engine_mock):
        """options=['Yes','No'], selected_index=0, value=False -> 1 Down + Enter."""
        prompt = _make_prompt(
            prompt_type="confirmation",
            question="Do you want to proceed?",
            options=["Yes", "No"],
            selected_index=0,
        )
        engine_mock.decide.return_value = Decision(
            action="confirm", value=False, reasoning="decline",
        )
        responder.handle_prompt(prompt, {})

        calls = tmux_mock.send_special_key.call_args_list
        assert calls == [call("Down"), call("Enter")]


# -- TestAutoResponderPermission (1 test) ----------------------------------

class TestAutoResponderPermission:

    def test_approve_permission(self, responder, tmux_mock, engine_mock):
        """options=['Allow','Deny'], selected_index=0, value=True -> just Enter."""
        prompt = _make_prompt(
            prompt_type="permission",
            question="Allow Bash?",
            options=["Allow", "Deny"],
            selected_index=0,
        )
        engine_mock.decide.return_value = Decision(
            action="permission", value=True, reasoning="approve",
        )
        responder.handle_prompt(prompt, {})

        calls = tmux_mock.send_special_key.call_args_list
        assert calls == [call("Enter")]


# -- TestAutoResponderSafety (4 tests) -------------------------------------

class TestAutoResponderSafety:

    def test_max_responses_per_turn(self, tmux_mock, engine_mock):
        """max=2, send 3 prompts -> only 2 executed."""
        config = AutoResponderConfig(max_auto_responses_per_turn=2, cooldown_seconds=0.0)
        sm = StateMachine()
        sm.transition("IDLE")
        responder = AutoResponder(
            decision_engine=engine_mock, tmux=tmux_mock,
            state_machine=sm, config=config,
        )

        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="pick first",
        )
        prompt = _make_prompt()
        for _ in range(3):
            responder.handle_prompt(prompt, {})

        # Only 2 Enter calls should have been made (max=2)
        enter_calls = [c for c in tmux_mock.send_special_key.call_args_list if c == call("Enter")]
        assert len(enter_calls) == 2

    def test_cooldown_prevents_rapid_fire(self, tmux_mock, engine_mock):
        """cooldown=10.0, two immediate calls -> only first executes."""
        config = AutoResponderConfig(
            max_auto_responses_per_turn=5, cooldown_seconds=10.0,
        )
        sm = StateMachine()
        sm.transition("IDLE")
        responder = AutoResponder(
            decision_engine=engine_mock, tmux=tmux_mock,
            state_machine=sm, config=config,
        )

        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="pick first",
        )
        prompt = _make_prompt()

        # First call: executes immediately (last_response_time starts at 0.0,
        # which is always more than cooldown_seconds in the past)
        responder.handle_prompt(prompt, {})

        # Second call: happens immediately after (within cooldown window).
        # _last_response_time was just set to time.monotonic() by the first call,
        # and the second call happens essentially at the same instant.
        responder.handle_prompt(prompt, {})

        # Only 1 Enter call (first one executed, second blocked by cooldown)
        enter_calls = [c for c in tmux_mock.send_special_key.call_args_list if c == call("Enter")]
        assert len(enter_calls) == 1

    def test_engine_returns_none_no_action(self, responder, tmux_mock, engine_mock):
        """decide() returns None -> no tmux calls."""
        engine_mock.decide.return_value = None
        prompt = _make_prompt()
        responder.handle_prompt(prompt, {})

        tmux_mock.send_keys.assert_not_called()
        tmux_mock.send_special_key.assert_not_called()

    def test_disabled_config_no_action(self, tmux_mock, engine_mock):
        """enabled=False -> no tmux calls."""
        config = AutoResponderConfig(enabled=False)
        sm = StateMachine()
        sm.transition("IDLE")
        responder = AutoResponder(
            decision_engine=engine_mock, tmux=tmux_mock,
            state_machine=sm, config=config,
        )

        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="pick first",
        )
        prompt = _make_prompt()
        responder.handle_prompt(prompt, {})

        tmux_mock.send_keys.assert_not_called()
        tmux_mock.send_special_key.assert_not_called()


# -- Test decision logging (1 test) ----------------------------------------

class TestAutoResponderLogging:

    def test_decision_logged(self, responder, tmux_mock, engine_mock):
        """Verify response_log has 1 entry with correct fields."""
        engine_mock.decide.return_value = Decision(
            action="select", value=2, reasoning="pick second",
        )
        prompt = _make_prompt(prompt_type="ask_user")
        responder.handle_prompt(prompt, {})

        log = responder.response_log
        assert len(log) == 1
        entry = log[0]
        assert isinstance(entry, AutoResponseLog)
        assert entry.prompt_type == "ask_user"
        assert entry.decision.action == "select"
        assert entry.decision.value == 2
        assert entry.executed is True
        assert entry.error is None


# -- Task 3: Timing and error tests (4 tests) ------------------------------

class TestAutoResponderTimingAndErrors:

    def test_select_and_type_waits_before_typing(self, responder, tmux_mock, engine_mock):
        """Verify time.sleep(0.5) is called between Enter and typing."""
        prompt = _make_prompt(
            options=["Option A", "Option B", "Type something."],
            selected_index=0,
            has_other=True,
        )
        engine_mock.decide.return_value = Decision(
            action="select_and_type", value="custom", reasoning="need custom",
        )

        with patch("tools.claude_session.auto_responder.time.sleep") as mock_sleep:
            responder.handle_prompt(prompt, {})

        # sleep(0.5) should have been called once
        mock_sleep.assert_called_once_with(0.5)

    def test_navigate_zero_delta(self, responder, tmux_mock, engine_mock):
        """current==target -> just Enter, no Up/Down."""
        prompt = _make_prompt(selected_index=0)
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="already selected",
        )
        responder.handle_prompt(prompt, {})

        calls = tmux_mock.send_special_key.call_args_list
        assert calls == [call("Enter")]
        # Ensure no Up or Down was sent
        for c in calls:
            assert c[0][0] not in ("Up", "Down")

    def test_tmux_failure_logged(self, responder, tmux_mock, engine_mock):
        """send_special_key raises -> executed=False, error logged."""
        prompt = _make_prompt()
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="try it",
        )
        tmux_mock.send_special_key.side_effect = RuntimeError("tmux session lost")

        # Should not raise; error is caught internally
        responder.handle_prompt(prompt, {})

        log = responder.response_log
        assert len(log) == 1
        assert log[0].executed is False
        assert log[0].error is not None
        assert "tmux session lost" in log[0].error

    def test_reset_turn_clears_counter(self, tmux_mock, engine_mock):
        """max=1, send 2, reset, send 1 more -> 2 total executed."""
        config = AutoResponderConfig(max_auto_responses_per_turn=1, cooldown_seconds=0.0)
        sm = StateMachine()
        sm.transition("IDLE")
        responder = AutoResponder(
            decision_engine=engine_mock, tmux=tmux_mock,
            state_machine=sm, config=config,
        )

        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="pick first",
        )
        prompt = _make_prompt(selected_index=0)

        # First call executes
        responder.handle_prompt(prompt, {})
        # Second call is blocked (max=1)
        responder.handle_prompt(prompt, {})

        # Reset the counter
        responder.reset_turn()

        # Third call executes again
        responder.handle_prompt(prompt, {})

        # 2 total Enter calls
        enter_calls = [c for c in tmux_mock.send_special_key.call_args_list if c == call("Enter")]
        assert len(enter_calls) == 2
