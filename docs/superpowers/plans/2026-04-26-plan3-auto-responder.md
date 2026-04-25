# Plan 3: AutoResponder 自动响应器

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `AutoResponder` 模块，监听状态变化，检测到"等待用户输入"场景时调用 DecisionEngine 决策，并通过 TmuxInterface 注入回复（方向键导航 + Enter + 可选文本输入）。

**Architecture:** 纯协调器模块。持有 `DecisionEngine` 和 `TmuxInterface` 引用，通过 `on_state_change()` 被调用。同步执行，DecisionEngine 的 LLM 调用在当前线程中同步完成。内置重试限制、冷却期、决策审计日志。

**Tech Stack:** Python 3.10+, threading, time, logging

**Depends on:** Plan 1（`UserPromptInfo`）、Plan 2（`DecisionEngine`、`Decision`）

---

### Task 1: 新增 AutoResponder 数据类和配置

**Files:**
- Create: `tools/claude_session/auto_responder.py`

- [ ] **Step 1: 创建 auto_responder.py 基础结构**

```python
"""tools/claude_session/auto_responder.py — Auto-respond to Claude Code user-input prompts."""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from tools.claude_session.decision_engine import Decision, DecisionEngine
from tools.claude_session.output_parser import UserPromptInfo
from tools.claude_session.state_machine import ClaudeState, StateMachine, StateTransition
from tools.claude_session.tmux_interface import TmuxInterface

logger = logging.getLogger(__name__)


@dataclass
class AutoResponderConfig:
    """Configuration for AutoResponder behavior."""
    max_auto_responses_per_turn: int = 5
    cooldown_seconds: float = 2.0
    enabled: bool = True


@dataclass
class AutoResponseLog:
    """Record of a single auto-response action."""
    timestamp: float
    prompt_type: str
    decision: Decision
    executed: bool
    error: Optional[str] = None
```

- [ ] **Step 2: 验证模块可导入**

Run: `cd /mnt/f/Projects/hermes-agent && python -c "from tools.claude_session.auto_responder import AutoResponderConfig; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: Commit**

```bash
git add tools/claude_session/auto_responder.py
git commit -m "feat(claude-session): add AutoResponder config and data structures"
```

---

### Task 2: 实现 AutoResponder 核心类

**Files:**
- Modify: `tools/claude_session/auto_responder.py`
- Create: `tests/tools/test_auto_responder.py`

- [ ] **Step 1: 编写 AutoResponder 的失败测试**

创建 `tests/tools/test_auto_responder.py`：

```python
"""Tests for tools/claude_session/auto_responder.py"""

import time
import pytest
from unittest.mock import MagicMock, patch, call
from tools.claude_session.auto_responder import AutoResponder, AutoResponderConfig, AutoResponseLog
from tools.claude_session.decision_engine import Decision
from tools.claude_session.output_parser import UserPromptInfo
from tools.claude_session.state_machine import StateMachine, StateTransition


def _make_prompt(prompt_type="ask_user", question="选择方案",
                 options=None, selected_index=0, has_other=False):
    return UserPromptInfo(
        prompt_type=prompt_type,
        question=question,
        options=options or ["选项 A", "选项 B", "选项 C"],
        selected_index=selected_index,
        has_other=has_other,
        raw_context="mock context",
    )


def _make_transition(from_state="THINKING", to_state="IDLE"):
    return StateTransition(
        from_state=from_state,
        to_state=to_state,
        timestamp=time.monotonic(),
    )


@pytest.fixture
def tmux_mock():
    return MagicMock(spec=MockTmuxInterface)


@pytest.fixture
def engine_mock():
    return MagicMock()


@pytest.fixture
def responder(tmux_mock, engine_mock):
    config = AutoResponderConfig(
        max_auto_responses_per_turn=5,
        cooldown_seconds=0.0,  # No cooldown in tests
    )
    sm = StateMachine()
    sm.transition("IDLE")
    return AutoResponder(
        decision_engine=engine_mock,
        tmux=tmux_mock,
        state_machine=sm,
        config=config,
    )


class MockTmuxInterface:
    """Minimal mock matching TmuxInterface's public API."""
    def send_keys(self, text, enter=False):
        pass

    def send_special_key(self, key):
        pass


class TestAutoResponderSelect:
    def test_navigate_down_and_enter(self, responder, tmux_mock, engine_mock):
        """Select option 3 when current is 1: press Down twice, then Enter."""
        engine_mock.decide.return_value = Decision(
            action="select", value=3, reasoning="选项 C 最优"
        )
        prompt = _make_prompt(selected_index=1, options=["A", "B", "C"])

        responder.handle_prompt(prompt, {})

        # Should have called send_special_key("Down") twice, then "Enter"
        key_calls = tmux_mock.send_special_key.call_args_list
        assert key_calls.count(call("Down")) == 2
        assert key_calls.count(call("Enter")) == 1

    def test_navigate_up_and_enter(self, responder, tmux_mock, engine_mock):
        """Select option 1 when current is 2: press Up once, then Enter."""
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="选项 A"
        )
        prompt = _make_prompt(selected_index=2, options=["A", "B", "C"])

        responder.handle_prompt(prompt, {})

        key_calls = tmux_mock.send_special_key.call_args_list
        assert key_calls.count(call("Up")) == 1
        assert key_calls.count(call("Enter")) == 1

    def test_no_navigation_when_already_selected(self, responder, tmux_mock, engine_mock):
        """Select current option: just press Enter."""
        engine_mock.decide.return_value = Decision(
            action="select", value=2, reasoning="already selected"
        )
        prompt = _make_prompt(selected_index=1, options=["A", "B", "C"])

        responder.handle_prompt(prompt, {})

        key_calls = tmux_mock.send_special_key.call_args_list
        assert key_calls.count(call("Down")) == 0
        assert key_calls.count(call("Up")) == 0
        assert key_calls.count(call("Enter")) == 1


class TestAutoResponderSelectAndType:
    def test_navigate_to_other_and_type(self, responder, tmux_mock, engine_mock):
        """Select 'Other' (option 4) and type custom text."""
        engine_mock.decide.return_value = Decision(
            action="select_and_type",
            value="混合方案：A 和 B 结合",
            reasoning="需要更灵活",
        )
        prompt = _make_prompt(
            selected_index=0,
            options=["A", "B", "C", "Type something."],
            has_other=True,
        )

        responder.handle_prompt(prompt, {})

        # Navigate from index 0 to index 3: 3 Down presses
        key_calls = tmux_mock.send_special_key.call_args_list
        assert key_calls.count(call("Down")) == 3
        # Enter to select "Other"
        assert key_calls.count(call("Enter")) == 1
        # Then type custom text + Enter
        tmux_mock.send_keys.assert_called_once_with("混合方案：A 和 B 结合", enter=True)


class TestAutoResponderFreeText:
    def test_type_text_and_enter(self, responder, tmux_mock, engine_mock):
        """Free-text: just type the response and press Enter."""
        engine_mock.decide.return_value = Decision(
            action="text",
            value="我倾向方案 A",
            reasoning="更稳定",
        )
        prompt = _make_prompt(prompt_type="free_text", options=[])

        responder.handle_prompt(prompt, {})

        tmux_mock.send_keys.assert_called_once_with("我倾向方案 A", enter=True)


class TestAutoResponderConfirm:
    def test_confirm_yes(self, responder, tmux_mock, engine_mock):
        """Confirm Yes: navigate to Yes option and Enter."""
        engine_mock.decide.return_value = Decision(
            action="confirm", value=True, reasoning="safe"
        )
        prompt = _make_prompt(
            prompt_type="confirmation",
            options=["Yes", "No"],
            selected_index=0,
        )

        responder.handle_prompt(prompt, {})

        # Already on Yes (index 0), just Enter
        key_calls = tmux_mock.send_special_key.call_args_list
        assert key_calls.count(call("Enter")) == 1

    def test_confirm_no(self, responder, tmux_mock, engine_mock):
        """Confirm No: navigate to No and Enter."""
        engine_mock.decide.return_value = Decision(
            action="confirm", value=False, reasoning="risky"
        )
        prompt = _make_prompt(
            prompt_type="confirmation",
            options=["Yes", "No"],
            selected_index=0,
        )

        responder.handle_prompt(prompt, {})

        key_calls = tmux_mock.send_special_key.call_args_list
        assert key_calls.count(call("Down")) == 1
        assert key_calls.count(call("Enter")) == 1


class TestAutoResponderPermission:
    def test_approve_permission(self, responder, tmux_mock, engine_mock):
        """Approve permission: select Allow/Yes."""
        engine_mock.decide.return_value = Decision(
            action="permission", value=True, reasoning="expected change"
        )
        prompt = _make_prompt(
            prompt_type="permission",
            options=["Allow", "Deny"],
            selected_index=0,
        )

        responder.handle_prompt(prompt, {})

        key_calls = tmux_mock.send_special_key.call_args_list
        assert key_calls.count(call("Enter")) == 1


class TestAutoResponderSafety:
    def test_max_responses_per_turn(self, responder, tmux_mock, engine_mock):
        """Auto-responder stops after max_auto_responses_per_turn."""
        responder._config.max_auto_responses_per_turn = 2
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="test"
        )

        # Handle 3 prompts
        for i in range(3):
            prompt = _make_prompt(question=f"问题 {i}")
            responder.handle_prompt(prompt, {})

        # Only first 2 should have executed
        enter_calls = tmux_mock.send_special_key.call_args_list
        assert enter_calls.count(call("Enter")) == 2
        assert responder._response_count == 2

    def test_cooldown_prevents_rapid_fire(self, tmux_mock, engine_mock):
        """Auto-responder enforces cooldown between responses."""
        config = AutoResponderConfig(
            max_auto_responses_per_turn=5,
            cooldown_seconds=10.0,  # Long cooldown
        )
        sm = StateMachine()
        sm.transition("IDLE")
        responder = AutoResponder(
            decision_engine=engine_mock,
            tmux=tmux_mock,
            state_machine=sm,
            config=config,
        )
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="test"
        )

        # First call succeeds
        responder.handle_prompt(_make_prompt(), {})
        # Immediate second call is blocked by cooldown
        responder.handle_prompt(_make_prompt(), {})

        enter_calls = tmux_mock.send_special_key.call_args_list
        assert enter_calls.count(call("Enter")) == 1

    def test_engine_returns_none_no_action(self, responder, tmux_mock, engine_mock):
        """When DecisionEngine returns None, no tmux action is taken."""
        engine_mock.decide.return_value = None
        prompt = _make_prompt()

        responder.handle_prompt(prompt, {})

        tmux_mock.send_keys.assert_not_called()
        tmux_mock.send_special_key.assert_not_called()

    def test_disabled_config_no_action(self, responder, tmux_mock, engine_mock):
        """When enabled=False, no action is taken."""
        responder._config.enabled = False
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="test"
        )
        prompt = _make_prompt()

        responder.handle_prompt(prompt, {})

        tmux_mock.send_keys.assert_not_called()
        tmux_mock.send_special_key.assert_not_called()

    def test_decision_logged(self, responder, engine_mock):
        """Each auto-response is logged for audit."""
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="best"
        )
        prompt = _make_prompt()

        responder.handle_prompt(prompt, {})

        assert len(responder._response_log) == 1
        log = responder._response_log[0]
        assert log.prompt_type == "ask_user"
        assert log.executed is True
        assert log.decision.reasoning == "best"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_auto_responder.py -v`
Expected: FAIL — `ImportError` 或 `AttributeError`

- [ ] **Step 3: 实现 AutoResponder 类**

在 `tools/claude_session/auto_responder.py` 中 `AutoResponseLog` 之后添加：

```python
class AutoResponder:
    """Coordinates auto-response to Claude Code user-input prompts.

    Called by AdaptivePoller on state changes. When a user-input prompt is
    detected, uses DecisionEngine to decide and TmuxInterface to inject input.
    """

    def __init__(
        self,
        decision_engine: DecisionEngine,
        tmux: TmuxInterface,
        state_machine: StateMachine,
        config: AutoResponderConfig = None,
    ):
        self._engine = decision_engine
        self._tmux = tmux
        self._sm = state_machine
        self._config = config or AutoResponderConfig()
        self._response_log: list = []
        self._response_count = 0
        self._last_response_time = 0.0

    @property
    def response_log(self) -> list:
        return list(self._response_log)

    def reset_turn(self) -> None:
        """Reset per-turn counters. Called when a new user message is sent."""
        self._response_count = 0

    def handle_prompt(self, prompt: UserPromptInfo, context: dict) -> None:
        """Main entry point: decide and respond to a detected prompt.

        Args:
            prompt: Detected user-input scene from OutputParser.
            context: dict with "current_message", "conversation_history", etc.
        """
        if not self._config.enabled:
            return

        # Safety: max responses per turn
        if self._response_count >= self._config.max_auto_responses_per_turn:
            logger.warning(
                "AutoResponder: max responses reached (%d), skipping",
                self._config.max_auto_responses_per_turn,
            )
            return

        # Safety: cooldown
        now = time.monotonic()
        if now - self._last_response_time < self._config.cooldown_seconds:
            logger.debug("AutoResponder: cooldown active, skipping")
            return

        # Decide
        decision = self._engine.decide(prompt, context)
        if decision is None:
            logger.warning("AutoResponder: DecisionEngine returned None, no action")
            return

        # Execute
        try:
            self._execute_decision(decision, prompt)
            executed = True
            error = None
        except Exception as e:
            logger.error("AutoResponder: execution failed: %s", e)
            executed = False
            error = str(e)

        # Audit log
        self._response_log.append(AutoResponseLog(
            timestamp=time.monotonic(),
            prompt_type=prompt.prompt_type,
            decision=decision,
            executed=executed,
            error=error,
        ))

        self._response_count += 1
        self._last_response_time = time.monotonic()

    def _execute_decision(self, decision: Decision, prompt: UserPromptInfo) -> None:
        """Convert Decision to tmux operations."""
        if decision.action == "select":
            self._navigate_and_confirm(prompt.selected_index, decision.value - 1)

        elif decision.action == "select_and_type":
            # Navigate to "Other" option (last option) and select it
            last_idx = len(prompt.options) - 1
            self._navigate_and_confirm(prompt.selected_index, last_idx)
            # Wait a moment for TUI to switch to text input mode
            time.sleep(0.5)
            # Type custom text
            self._tmux.send_keys(str(decision.value), enter=True)

        elif decision.action == "text":
            self._tmux.send_keys(str(decision.value), enter=True)

        elif decision.action == "confirm":
            if decision.value:
                # Find "Yes" option index
                target = self._find_option_index(prompt.options, ["yes", "allow"])
                self._navigate_and_confirm(prompt.selected_index, target)
            else:
                target = self._find_option_index(prompt.options, ["no", "deny"])
                self._navigate_and_confirm(prompt.selected_index, target)

        elif decision.action == "permission":
            if decision.value:
                target = self._find_option_index(prompt.options, ["yes", "allow"])
                self._navigate_and_confirm(prompt.selected_index, target)
            else:
                target = self._find_option_index(prompt.options, ["no", "deny"])
                self._navigate_and_confirm(prompt.selected_index, target)

    def _navigate_and_confirm(self, current: int, target: int) -> None:
        """Move ❯ from current index to target index using Up/Down, then Enter."""
        delta = target - current
        key = "Down" if delta > 0 else "Up"
        for _ in range(abs(delta)):
            self._tmux.send_special_key(key)
        self._tmux.send_special_key("Enter")

    @staticmethod
    def _find_option_index(options: list, keywords: list) -> int:
        """Find the first option matching any keyword (case-insensitive)."""
        for i, opt in enumerate(options):
            opt_lower = opt.lower()
            for kw in keywords:
                if kw in opt_lower:
                    return i
        return 0  # Default to first option
```

- [ ] **Step 4: 运行所有 AutoResponder 测试**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_auto_responder.py -v`
Expected: 13 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tools/claude_session/auto_responder.py tests/tools/test_auto_responder.py
git commit -m "feat(claude-session): implement AutoResponder with safety limits and decision execution"
```

---

### Task 3: tmux 操作时序和错误处理测试

**Files:**
- Modify: `tests/tools/test_auto_responder.py`

- [ ] **Step 1: 编写时序和错误处理测试**

在 `tests/tools/test_auto_responder.py` 末尾添加：

```python
class TestAutoResponderTiming:
    def test_select_and_type_waits_before_typing(self, responder, tmux_mock, engine_mock):
        """select_and_type should wait 0.5s between navigation and typing."""
        engine_mock.decide.return_value = Decision(
            action="select_and_type",
            value="custom text",
            reasoning="test",
        )
        prompt = _make_prompt(
            options=["A", "B", "Type something."],
            selected_index=0,
            has_other=True,
        )

        with patch("tools.claude_session.auto_responder.time.sleep") as mock_sleep:
            responder.handle_prompt(prompt, {})
            # Should sleep 0.5s between Enter and typing
            mock_sleep.assert_called_with(0.5)

    def test_navigate_zero_delta(self, responder, tmux_mock, engine_mock):
        """Zero delta (already at target) should just Enter."""
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="first"
        )
        prompt = _make_prompt(selected_index=0, options=["A", "B"])

        responder.handle_prompt(prompt, {})

        key_calls = tmux_mock.send_special_key.call_args_list
        assert len(key_calls) == 1
        assert key_calls[0] == call("Enter")

    def test_tmux_failure_logged(self, responder, tmux_mock, engine_mock):
        """tmux failure during execution is caught and logged."""
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="test"
        )
        tmux_mock.send_special_key.side_effect = RuntimeError("tmux died")
        prompt = _make_prompt()

        # Should not raise
        responder.handle_prompt(prompt, {})

        # Should be logged as failed
        assert len(responder._response_log) == 1
        assert responder._response_log[0].executed is False
        assert "tmux died" in responder._response_log[0].error

    def test_reset_turn_clears_counter(self, responder, tmux_mock, engine_mock):
        """reset_turn() resets the response counter."""
        responder._config.max_auto_responses_per_turn = 1
        engine_mock.decide.return_value = Decision(
            action="select", value=1, reasoning="test"
        )

        responder.handle_prompt(_make_prompt(), {})
        assert responder._response_count == 1

        # Second call blocked
        responder.handle_prompt(_make_prompt(), {})
        assert responder._response_count == 1

        # Reset
        responder.reset_turn()
        assert responder._response_count == 0

        # Now can respond again
        responder.handle_prompt(_make_prompt(), {})
        assert responder._response_count == 1
```

- [ ] **Step 2: 运行所有 AutoResponder 测试**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_auto_responder.py -v`
Expected: 17 tests PASS（13 原有 + 4 新增）

- [ ] **Step 3: Commit**

```bash
git add tests/tools/test_auto_responder.py
git commit -m "test(claude-session): add timing and error handling tests for AutoResponder"
```
