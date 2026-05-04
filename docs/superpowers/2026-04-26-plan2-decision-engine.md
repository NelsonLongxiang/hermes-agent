# Plan 2: DecisionEngine 决策引擎

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `DecisionEngine` 模块，接收 `UserPromptInfo` 和对话上下文，调用 LLM 返回 `Decision` 对象，支持 select、select_and_type、text、confirm、permission 五种动作。

**Architecture:** 纯逻辑模块，不依赖 tmux 或任何 IO。通过依赖注入接收 LLM 调用函数（`call_llm`），内部构造 system/user prompt，解析 JSON 响应为 `Decision` 数据类。同步接口，LLM 调用也是同步的（复用 `agent.auxiliary_client.call_llm`）。

**Tech Stack:** Python 3.10+, dataclasses, json, `agent.auxiliary_client.call_llm`

**Depends on:** Plan 1（`UserPromptInfo` 数据类）

---

### Task 1: 新增 Decision 数据类

**Files:**
- Create: `tools/claude_session/decision_engine.py`

- [ ] **Step 1: 创建 decision_engine.py，定义 Decision 数据类**

```python
"""tools/claude_session/decision_engine.py — LLM-based auto-decision for Claude Code prompts."""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from tools.claude_session.output_parser import UserPromptInfo

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """Result of an LLM decision for a user-input prompt."""
    action: str     # "select" | "select_and_type" | "text" | "confirm" | "permission"
    value: Any      # select: int (1-based index)
                    # select_and_type: str (custom text to type after selecting "Other")
                    # text: str
                    # confirm: bool
                    # permission: bool
    reasoning: str  # LLM's explanation for the decision

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "value": self.value,
            "reasoning": self.reasoning,
        }
```

- [ ] **Step 2: 验证模块可导入**

Run: `cd /mnt/f/Projects/hermes-agent && python -c "from tools.claude_session.decision_engine import Decision; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: Commit**

```bash
git add tools/claude_session/decision_engine.py
git commit -m "feat(claude-session): add Decision dataclass for auto-decision engine"
```

---

### Task 2: 实现 DecisionEngine 核心逻辑

**Files:**
- Modify: `tools/claude_session/decision_engine.py`

- [ ] **Step 1: 编写 DecisionEngine 的失败测试**

创建 `tests/tools/test_decision_engine.py`：

```python
"""Tests for tools/claude_session/decision_engine.py"""

import json
import pytest
from unittest.mock import MagicMock, patch
from tools.claude_session.decision_engine import Decision, DecisionEngine
from tools.claude_session.output_parser import UserPromptInfo


@pytest.fixture
def mock_llm_call():
    """Mock call_llm that returns a fixed JSON response."""
    with patch("agent.auxiliary_client.call_llm") as mock:
        yield mock


@pytest.fixture
def engine():
    return DecisionEngine()


def _make_prompt(prompt_type="ask_user", question="选择方案", options=None,
                 selected_index=0, has_other=False):
    return UserPromptInfo(
        prompt_type=prompt_type,
        question=question,
        options=options or ["选项 A", "选项 B", "选项 C"],
        selected_index=selected_index,
        has_other=has_other,
        raw_context="mock context",
    )


class TestDecisionEngine:
    def test_select_option(self, engine, mock_llm_call):
        """LLM selects option 2 (1-based)."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    content='{"action": "select", "value": 2, "reasoning": "方案 B 更优"}'
                )
            )]
        )
        prompt = _make_prompt(question="选择实现方案")
        context = {"current_message": "帮我实现功能", "conversation_history": []}
        decision = engine.decide(prompt, context)
        assert decision.action == "select"
        assert decision.value == 2
        assert decision.reasoning == "方案 B 更优"

    def test_select_and_type(self, engine, mock_llm_call):
        """LLM chooses 'Other' and provides custom text."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    content='{"action": "select_and_type", "value": "混合方案：A 和 B 结合", "reasoning": "用户需要更灵活的选择"}'
                )
            )]
        )
        prompt = _make_prompt(has_other=True)
        context = {"current_message": "帮我设计", "conversation_history": []}
        decision = engine.decide(prompt, context)
        assert decision.action == "select_and_type"
        assert "混合" in decision.value

    def test_free_text_response(self, engine, mock_llm_call):
        """LLM provides free text response."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    content='{"action": "text", "value": "我倾向方案 A，因为更稳定", "reasoning": "用户重视稳定性"}'
                )
            )]
        )
        prompt = _make_prompt(prompt_type="free_text", question="你觉得哪个方案更好？", options=[])
        context = {"current_message": "重构代码", "conversation_history": []}
        decision = engine.decide(prompt, context)
        assert decision.action == "text"
        assert "方案 A" in decision.value

    def test_confirm_yes(self, engine, mock_llm_call):
        """LLM confirms an action."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    content='{"action": "confirm", "value": true, "reasoning": "操作安全，可以继续"}'
                )
            )]
        )
        prompt = _make_prompt(prompt_type="confirmation", question="是否继续？",
                              options=["Yes", "No"])
        context = {"current_message": "执行部署", "conversation_history": []}
        decision = engine.decide(prompt, context)
        assert decision.action == "confirm"
        assert decision.value is True

    def test_permission_approve(self, engine, mock_llm_call):
        """LLM approves a permission request."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    content='{"action": "permission", "value": true, "reasoning": "文件修改符合预期"}'
                )
            )]
        )
        prompt = _make_prompt(prompt_type="permission", question="Allow Edit to src/main.py?",
                              options=["Allow", "Deny"])
        context = {"current_message": "修复 bug", "conversation_history": []}
        decision = engine.decide(prompt, context)
        assert decision.action == "permission"
        assert decision.value is True

    def test_llm_failure_returns_none(self, engine, mock_llm_call):
        """LLM call failure returns None (error degradation)."""
        mock_llm_call.side_effect = RuntimeError("No provider")
        prompt = _make_prompt()
        context = {"current_message": "test", "conversation_history": []}
        decision = engine.decide(prompt, context)
        assert decision is None

    def test_invalid_json_returns_none(self, engine, mock_llm_call):
        """Invalid JSON response returns None."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(content="This is not JSON")
            )]
        )
        prompt = _make_prompt()
        context = {"current_message": "test", "conversation_history": []}
        decision = engine.decide(prompt, context)
        assert decision is None

    def test_missing_action_returns_none(self, engine, mock_llm_call):
        """JSON without action field returns None."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(content='{"value": 1, "reasoning": "missing action"}')
            )]
        )
        prompt = _make_prompt()
        context = {"current_message": "test", "conversation_history": []}
        decision = engine.decide(prompt, context)
        assert decision is None
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_decision_engine.py -v`
Expected: FAIL — `ImportError: cannot import name 'DecisionEngine'`

- [ ] **Step 3: 实现 DecisionEngine 类**

在 `tools/claude_session/decision_engine.py` 中 `Decision` 类之后添加：

```python
# ── System prompt for auto-decision ──
_SYSTEM_PROMPT = """你是 Hermes 的自主决策代理。Claude Code 在执行任务过程中遇到了需要用户输入的场景。
你需要根据用户的原始意图和对话上下文，做出最合理的选择。

规则：
- 始终以 JSON 格式返回：{"action": "...", "value": ..., "reasoning": "..."}
- action 必须是: "select"（选择选项）、"select_and_type"（选择 Other 并输入文本）、"text"（输入文本）、"confirm"（确认/拒绝）、"permission"（批准/拒绝）
- select 时 value 是选项编号（从 1 开始的整数）
- select_and_type 时 value 是要输入的自定义文本字符串（先选中 Other 选项，再输入此文本）
- text 时 value 是要输入的文本字符串
- confirm/permission 时 value 是 true 或 false
- reasoning 简要说明决策理由（一句话）"""


def _build_user_prompt(prompt: UserPromptInfo, context: dict) -> str:
    """Construct the user-facing prompt for the LLM."""
    parts = [f"## 场景类型: {prompt.prompt_type}"]
    parts.append(f"## 问题: {prompt.question}")

    if prompt.options:
        parts.append("## 可选选项:")
        for i, opt in enumerate(prompt.options):
            marker = " ← 当前选中" if i == prompt.selected_index else ""
            parts.append(f"  {i + 1}. {opt}{marker}")
        if prompt.has_other:
            parts.append("  (最后一项是 'Other/Type something'，可以用 select_and_type 来选择并输入自定义文本)")
        parts.append(f"## 当前选中: {prompt.selected_index + 1}")
    else:
        parts.append("## (无选项，需要自由文本输入)")

    parts.append(f"\n## 用户原始消息:\n{context.get('current_message', '(无)')}")

    history = context.get("conversation_history", [])
    if history:
        parts.append("\n## 对话历史 (最近几条):")
        for msg in history[-5:]:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 200:
                content = content[:200] + "..."
            parts.append(f"  [{role}] {content}")

    parts.append(f"\n## TUI 输出上下文:\n{prompt.raw_context}")

    return "\n".join(parts)


class DecisionEngine:
    """Calls LLM to make autonomous decisions for Claude Code user-input prompts."""

    def __init__(self, llm_call_fn=None):
        """
        Args:
            llm_call_fn: Callable matching agent.auxiliary_client.call_llm signature.
                         If None, imports and uses call_llm at call time.
        """
        self._llm_call = llm_call_fn

    def decide(self, prompt: UserPromptInfo, context: dict) -> Optional[Decision]:
        """Make a decision based on the prompt and conversation context.

        Args:
            prompt: Parsed user-input scene from OutputParser.
            context: dict with "current_message", "conversation_history", etc.

        Returns:
            Decision if successful, None on error (error degradation).
        """
        llm_fn = self._llm_call
        if llm_fn is None:
            from agent.auxiliary_client import call_llm
            llm_fn = call_llm

        user_prompt = _build_user_prompt(prompt, context)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            response = llm_fn(
                task="auto_decision",
                messages=messages,
                temperature=0.3,
                max_tokens=200,
                timeout=15.0,
            )
        except Exception as e:
            logger.error("DecisionEngine LLM call failed: %s", e)
            return None

        # Extract content from response
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as e:
            logger.error("DecisionEngine unexpected response format: %s", e)
            return None

        # Parse JSON from content
        try:
            # Handle content that may have markdown code blocks
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                # Remove first and last line (``` markers)
                lines = [l for l in lines if not l.strip().startswith("```")]
                content = "\n".join(lines)
            data = json.loads(content)
        except json.JSONDecodeError as e:
            logger.error("DecisionEngine JSON parse failed: %s | content: %s", e, content[:200])
            return None

        # Validate required fields
        action = data.get("action")
        value = data.get("value")
        reasoning = data.get("reasoning", "")

        if not action or value is None:
            logger.error("DecisionEngine missing action/value: %s", data)
            return None

        valid_actions = {"select", "select_and_type", "text", "confirm", "permission"}
        if action not in valid_actions:
            logger.error("DecisionEngine invalid action '%s', valid: %s", action, valid_actions)
            return None

        # Type coercion for select: ensure value is int
        if action == "select" and not isinstance(value, int):
            try:
                value = int(value)
            except (ValueError, TypeError):
                logger.error("DecisionEngine select value not int: %s", value)
                return None

        # Type coercion for confirm/permission: ensure value is bool
        if action in ("confirm", "permission") and not isinstance(value, bool):
            if isinstance(value, str):
                value = value.lower() in ("true", "yes", "1")
            else:
                value = bool(value)

        return Decision(action=action, value=value, reasoning=reasoning)
```

- [ ] **Step 4: 运行所有 DecisionEngine 测试**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_decision_engine.py -v`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tools/claude_session/decision_engine.py tests/tools/test_decision_engine.py
git commit -m "feat(claude-session): implement DecisionEngine with LLM-based auto-decision"
```

---

### Task 3: Prompt 构建和 JSON 解析的边界测试

**Files:**
- Modify: `tests/tools/test_decision_engine.py`

- [ ] **Step 1: 编写边界测试**

在 `tests/tools/test_decision_engine.py` 末尾添加：

```python
class TestBuildUserPrompt:
    """Test _build_user_prompt helper function."""

    def test_includes_question_and_options(self):
        from tools.claude_session.decision_engine import _build_user_prompt
        prompt = _make_prompt(question="选择方案", options=["A", "B", "C"])
        ctx = {"current_message": "帮我做", "conversation_history": []}
        result = _build_user_prompt(prompt, ctx)
        assert "选择方案" in result
        assert "A" in result
        assert "B" in result
        assert "帮我做" in result

    def test_includes_conversation_history(self):
        from tools.claude_session.decision_engine import _build_user_prompt
        prompt = _make_prompt()
        ctx = {
            "current_message": "test",
            "conversation_history": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        }
        result = _build_user_prompt(prompt, ctx)
        assert "hello" in result
        assert "hi there" in result

    def test_truncates_long_content(self):
        from tools.claude_session.decision_engine import _build_user_prompt
        prompt = _make_prompt()
        ctx = {
            "current_message": "test",
            "conversation_history": [
                {"role": "assistant", "content": "x" * 500},
            ],
        }
        result = _build_user_prompt(prompt, ctx)
        assert "..." in result

    def test_marks_has_other(self):
        from tools.claude_session.decision_engine import _build_user_prompt
        prompt = _make_prompt(has_other=True)
        ctx = {"current_message": "test", "conversation_history": []}
        result = _build_user_prompt(prompt, ctx)
        assert "select_and_type" in result


class TestDecisionJsonParsing:
    """Test JSON parsing edge cases."""

    def test_json_in_markdown_code_block(self, engine, mock_llm_call):
        """LLM wraps JSON in markdown code block."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    content='```json\n{"action": "select", "value": 1, "reasoning": "best option"}\n```'
                )
            )]
        )
        prompt = _make_prompt()
        ctx = {"current_message": "test", "conversation_history": []}
        decision = engine.decide(prompt, ctx)
        assert decision is not None
        assert decision.action == "select"
        assert decision.value == 1

    def test_select_value_as_string_number(self, engine, mock_llm_call):
        """LLM returns select value as string '2' instead of int 2."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    content='{"action": "select", "value": "2", "reasoning": "second is better"}'
                )
            )]
        )
        prompt = _make_prompt()
        ctx = {"current_message": "test", "conversation_history": []}
        decision = engine.decide(prompt, ctx)
        assert decision is not None
        assert decision.value == 2
        assert isinstance(decision.value, int)

    def test_confirm_value_as_string_true(self, engine, mock_llm_call):
        """LLM returns confirm value as string 'true'."""
        mock_llm_call.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    content='{"action": "confirm", "value": "true", "reasoning": "safe"}'
                )
            )]
        )
        prompt = _make_prompt(prompt_type="confirmation", options=["Yes", "No"])
        ctx = {"current_message": "test", "conversation_history": []}
        decision = engine.decide(prompt, ctx)
        assert decision is not None
        assert decision.value is True
```

- [ ] **Step 2: 运行所有 DecisionEngine 测试**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_decision_engine.py -v`
Expected: 14 tests PASS（8 原有 + 6 新增）

- [ ] **Step 3: Commit**

```bash
git add tests/tools/test_decision_engine.py
git commit -m "test(claude-session): add edge case tests for DecisionEngine"
```
