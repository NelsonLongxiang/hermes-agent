"""Tests for tools/claude_session/decision_engine.py"""

import json
import pytest
from unittest.mock import MagicMock

from tools.claude_session.decision_engine import Decision, DecisionEngine, _build_user_prompt
from tools.claude_session.output_parser import UserPromptInfo


# ── Helpers ──────────────────────────────────────────────────────────

def _mock_response(content: str) -> MagicMock:
    """Build a mock LLM response with the given content string."""
    return MagicMock(
        choices=[MagicMock(message=MagicMock(content=content))]
    )


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
        raw_context="mock context",
    )


def _engine_with_mock(mock_fn):
    """Create a DecisionEngine with the given mock as llm_call_fn."""
    return DecisionEngine(llm_call_fn=mock_fn)


# ── Core tests ───────────────────────────────────────────────────────

def test_select_option():
    """LLM returns a select action with an option index."""
    mock_fn = MagicMock(return_value=_mock_response(
        json.dumps({"action": "select", "value": 2, "reasoning": "选择第二个"})
    ))
    engine = _engine_with_mock(mock_fn)
    decision = engine.decide(_make_prompt())

    assert decision is not None
    assert decision.action == "select"
    assert decision.value == 2
    assert decision.reasoning == "选择第二个"
    mock_fn.assert_called_once()


def test_select_and_type():
    """LLM returns a select_and_type action with custom text."""
    mock_fn = MagicMock(return_value=_mock_response(
        json.dumps({"action": "select_and_type", "value": "custom text", "reasoning": "需要自定义"})
    ))
    engine = _engine_with_mock(mock_fn)
    decision = engine.decide(_make_prompt(has_other=True))

    assert decision is not None
    assert decision.action == "select_and_type"
    assert decision.value == "custom text"


def test_free_text_response():
    """LLM returns a text action for free-text input."""
    mock_fn = MagicMock(return_value=_mock_response(
        json.dumps({"action": "text", "value": "some text", "reasoning": "回答问题"})
    ))
    engine = _engine_with_mock(mock_fn)
    decision = engine.decide(_make_prompt(
        prompt_type="free_text", question="请输入描述", options=[]
    ))

    assert decision is not None
    assert decision.action == "text"
    assert decision.value == "some text"


def test_confirm_yes():
    """LLM returns a confirm action with true."""
    mock_fn = MagicMock(return_value=_mock_response(
        json.dumps({"action": "confirm", "value": True, "reasoning": "确认继续"})
    ))
    engine = _engine_with_mock(mock_fn)
    decision = engine.decide(_make_prompt(
        prompt_type="confirmation", question="Do you want to proceed?"
    ))

    assert decision is not None
    assert decision.action == "confirm"
    assert decision.value is True


def test_permission_approve():
    """LLM returns a permission action with true."""
    mock_fn = MagicMock(return_value=_mock_response(
        json.dumps({"action": "permission", "value": True, "reasoning": "批准操作"})
    ))
    engine = _engine_with_mock(mock_fn)
    decision = engine.decide(_make_prompt(
        prompt_type="permission", question="Allow Bash?"
    ))

    assert decision is not None
    assert decision.action == "permission"
    assert decision.value is True


def test_llm_failure_returns_none():
    """LLM call raises RuntimeError — should return None gracefully."""
    mock_fn = MagicMock(side_effect=RuntimeError("no provider"))
    engine = _engine_with_mock(mock_fn)
    decision = engine.decide(_make_prompt())

    assert decision is None


def test_invalid_json_returns_none():
    """LLM returns non-JSON content — should return None."""
    mock_fn = MagicMock(return_value=_mock_response("This is not JSON at all!"))
    engine = _engine_with_mock(mock_fn)
    decision = engine.decide(_make_prompt())

    assert decision is None


def test_missing_action_returns_none():
    """LLM returns valid JSON but without 'action' field — should return None."""
    mock_fn = MagicMock(return_value=_mock_response(
        json.dumps({"value": 1, "reasoning": "missing action"})
    ))
    engine = _engine_with_mock(mock_fn)
    decision = engine.decide(_make_prompt())

    assert decision is None


# ── Edge case: _build_user_prompt ────────────────────────────────────

class TestBuildUserPrompt:

    def test_includes_question_and_options(self):
        prompt = _make_prompt(question="Pick a color", options=["Red", "Green", "Blue"])
        result = _build_user_prompt(prompt)

        assert "Pick a color" in result
        assert "1. Red" in result
        assert "2. Green" in result
        assert "3. Blue" in result

    def test_includes_conversation_history(self):
        prompt = _make_prompt()
        context = {
            "current_message": "Fix the login bug",
            "history": [
                "User: fix the login bug",
                "Assistant: I will look at the auth module",
            ],
        }
        result = _build_user_prompt(prompt, context)

        assert "Fix the login bug" in result
        assert "fix the login bug" in result
        assert "auth module" in result

    def test_truncates_long_content(self):
        prompt = _make_prompt()
        long_entry = "x" * 500
        context = {"history": [long_entry]}
        result = _build_user_prompt(prompt, context)

        # Each history entry should be truncated to 200 chars
        assert long_entry not in result
        assert "x" * 200 in result  # truncated version present

    def test_marks_has_other(self):
        prompt = _make_prompt(
            has_other=True,
            options=["Option A", "Type something."],
        )
        result = _build_user_prompt(prompt)

        assert "select_and_type" in result


# ── Edge case: JSON parsing ─────────────────────────────────────────

class TestDecisionJsonParsing:

    def test_json_in_markdown_code_block(self):
        """LLM wraps JSON in ```json ... ``` — should still parse."""
        raw = '```json\n{"action": "select", "value": 2, "reasoning": "wrapped"}\n```'
        decision = DecisionEngine._parse_response(raw)

        assert decision is not None
        assert decision.action == "select"
        assert decision.value == 2

    def test_select_value_as_string_number(self):
        """LLM returns value as string "2" instead of int — should coerce to int."""
        raw = json.dumps({"action": "select", "value": "2", "reasoning": "coerce"})
        decision = DecisionEngine._parse_response(raw)

        assert decision is not None
        assert decision.action == "select"
        assert decision.value == 2
        assert isinstance(decision.value, int)

    def test_confirm_value_as_string_true(self):
        """LLM returns value as string "true" instead of bool — should coerce to True."""
        raw = json.dumps({"action": "confirm", "value": "true", "reasoning": "coerce"})
        decision = DecisionEngine._parse_response(raw)

        assert decision is not None
        assert decision.action == "confirm"
        assert decision.value is True
