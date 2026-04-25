"""tools/claude_session/decision_engine.py — LLM-based auto-decision for Claude Code prompts."""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from tools.claude_session.output_parser import UserPromptInfo

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"select", "select_and_type", "text", "confirm", "permission"}

_SYSTEM_PROMPT = """\
你是 Hermes 的自主决策代理。Claude Code 在执行任务过程中遇到了需要用户输入的场景。
你需要根据用户的原始意图和对话上下文，做出最合理的选择。

规则：
- 始终以 JSON 格式返回：{"action": "...", "value": ..., "reasoning": "..."}
- action 必须是: "select"（选择选项）、"select_and_type"（选择 Other 并输入文本）、"text"（输入文本）、"confirm"（确认/拒绝）、"permission"（批准/拒绝）
- select 时 value 是选项编号（从 1 开始的整数）
- select_and_type 时 value 是要输入的自定义文本字符串
- text 时 value 是要输入的文本字符串
- confirm/permission 时 value 是 true 或 false
- reasoning 简要说明决策理由（一句话）"""


@dataclass
class Decision:
    """Result of an LLM decision for a user-input prompt."""

    action: str  # "select" | "select_and_type" | "text" | "confirm" | "permission"
    value: Any
    reasoning: str

    def to_dict(self) -> dict:
        return {"action": self.action, "value": self.value, "reasoning": self.reasoning}


def _build_user_prompt(
    prompt: UserPromptInfo,
    context: Optional[dict] = None,
) -> str:
    """Build the user message sent to the LLM for decision-making.

    Args:
        prompt: Parsed user-input prompt from the TUI.
        context: Optional dict with keys like 'current_message', 'history'.

    Returns:
        Formatted user prompt string.
    """
    parts: list[str] = []

    # Scene type and question
    parts.append(f"场景类型: {prompt.prompt_type}")
    parts.append(f"问题: {prompt.question}")

    # Numbered options with selection marker
    if prompt.options:
        parts.append("选项:")
        for i, opt in enumerate(prompt.options):
            marker = " ← 当前选中" if i == prompt.selected_index else ""
            parts.append(f"  {i + 1}. {opt}{marker}")
    else:
        parts.append("(无选项，需要自由输入)")

    # Notes about has_other
    if prompt.has_other:
        parts.append("注意: 最后一项是 'Other / Type something'，可以使用 select_and_type 输入自定义内容。")

    # Context information
    if context:
        current_msg = context.get("current_message", "")
        if current_msg:
            parts.append(f"\n当前用户消息: {current_msg}")

        history = context.get("history", [])
        if history:
            parts.append("\n最近的对话历史:")
            for entry in history[-5:]:
                truncated = str(entry)[:200]
                parts.append(f"  - {truncated}")

    # Raw context from TUI
    if prompt.raw_context:
        parts.append(f"\nTUI 原始上下文:\n{prompt.raw_context}")

    return "\n".join(parts)


class DecisionEngine:
    """LLM-based engine that decides how to respond to Claude Code prompts."""

    def __init__(self, llm_call_fn: Optional[Callable] = None):
        """Initialize the decision engine.

        Args:
            llm_call_fn: Optional callable for LLM invocation. If None,
                agent.auxiliary_client.call_llm is imported lazily at call time.
        """
        self._llm_call_fn = llm_call_fn

    def decide(
        self,
        prompt: UserPromptInfo,
        context: Optional[dict] = None,
    ) -> Optional[Decision]:
        """Make a decision for a user-input prompt.

        Args:
            prompt: Parsed user-input prompt from the TUI.
            context: Optional dict with keys like 'current_message', 'history'.

        Returns:
            Decision object, or None on any error (graceful degradation).
        """
        try:
            llm_fn = self._llm_call_fn
            if llm_fn is None:
                from agent.auxiliary_client import call_llm

                llm_fn = call_llm

            user_content = _build_user_prompt(prompt, context)

            response = llm_fn(
                task="auto_decision",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.3,
                max_tokens=200,
                timeout=15.0,
            )

            raw_content = response.choices[0].message.content
            return self._parse_response(raw_content)

        except Exception as exc:
            logger.error("DecisionEngine.decide failed: %s", exc)
            return None

    @staticmethod
    def _parse_response(raw_content: str) -> Optional[Decision]:
        """Parse LLM response content into a Decision.

        Handles:
        - Plain JSON
        - JSON wrapped in markdown code blocks (```json ... ```)
        - Type coercion for value field

        Returns:
            Decision object, or None on parse/validation failure.
        """
        # Strip markdown code block wrapper if present
        content = raw_content.strip()
        code_block_re = re.compile(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL
        )
        m = code_block_re.search(content)
        if m:
            content = m.group(1).strip()

        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            logger.error("DecisionEngine: failed to parse JSON: %s", content[:200])
            return None

        if not isinstance(data, dict) or "action" not in data:
            logger.error("DecisionEngine: missing 'action' in response: %s", content[:200])
            return None

        action = data["action"]
        if action not in _VALID_ACTIONS:
            logger.error("DecisionEngine: invalid action '%s'", action)
            return None

        value = data.get("value")
        reasoning = data.get("reasoning", "")

        # Type coercion
        if action == "select":
            try:
                value = int(value)
            except (ValueError, TypeError):
                logger.error("DecisionEngine: select value is not int-convertible: %r", value)
                return None
        elif action in ("confirm", "permission"):
            if isinstance(value, str):
                value = value.lower() in ("true", "yes", "1")
            else:
                value = bool(value)

        return Decision(action=action, value=value, reasoning=reasoning)
