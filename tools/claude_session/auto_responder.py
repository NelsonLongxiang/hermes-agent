"""tools/claude_session/auto_responder.py -- Auto-respond to Claude Code user-input prompts."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from tools.claude_session.decision_engine import Decision, DecisionEngine
from tools.claude_session.output_parser import UserPromptInfo
from tools.claude_session.state_machine import StateMachine
from tools.claude_session.tmux_interface import TmuxInterface

logger = logging.getLogger(__name__)


@dataclass
class AutoResponderConfig:
    """Configuration for the AutoResponder."""

    max_auto_responses_per_turn: int = 5
    cooldown_seconds: float = 2.0
    enabled: bool = True


@dataclass
class AutoResponseLog:
    """Log entry for a single auto-response action."""

    timestamp: float
    prompt_type: str
    decision: Decision
    executed: bool
    error: Optional[str] = None


class AutoResponder:
    """Automatically responds to Claude Code user-input prompts.

    Uses a DecisionEngine to determine the appropriate response and a
    TmuxInterface to send the response to the Claude Code TUI.

    Safety guards:
    - max_auto_responses_per_turn: limits how many auto-responses per turn
    - cooldown_seconds: minimum time between consecutive auto-responses
    - enabled: master switch to disable all auto-responses
    """

    def __init__(
        self,
        decision_engine: DecisionEngine,
        tmux: TmuxInterface,
        state_machine: StateMachine,
        config: Optional[AutoResponderConfig] = None,
    ):
        self._engine = decision_engine
        self._tmux = tmux
        self._sm = state_machine
        self._config = config or AutoResponderConfig()
        self._response_log: list[AutoResponseLog] = []
        self._response_count: int = 0
        self._last_response_time: float = 0.0

    @property
    def response_log(self) -> list[AutoResponseLog]:
        """Return a copy of the response log."""
        return list(self._response_log)

    def reset_turn(self) -> None:
        """Reset the per-turn response counter."""
        self._response_count = 0

    def handle_prompt(self, prompt: UserPromptInfo, context: dict) -> None:
        """Main entry point: decide and execute a response to a user-input prompt.

        Args:
            prompt: Parsed user-input prompt from the TUI.
            context: Dict with keys like 'current_message', 'history'.
        """
        # Guard: disabled
        if not self._config.enabled:
            return

        # Guard: max responses per turn
        if self._response_count >= self._config.max_auto_responses_per_turn:
            logger.info(
                "AutoResponder: max responses per turn reached (%d)",
                self._config.max_auto_responses_per_turn,
            )
            return

        # Guard: cooldown
        now = time.monotonic()
        elapsed = now - self._last_response_time
        if elapsed < self._config.cooldown_seconds:
            logger.info(
                "AutoResponder: cooldown active (%.1fs remaining)",
                self._config.cooldown_seconds - elapsed,
            )
            return

        # Ask the decision engine
        decision = self._engine.decide(prompt, context)
        if decision is None:
            logger.info("AutoResponder: engine returned None, no action taken")
            return

        # Execute the decision
        error: Optional[str] = None
        executed = False
        try:
            self._execute_decision(decision, prompt)
            executed = True
        except Exception as exc:
            error = str(exc)
            logger.error("AutoResponder: execution failed: %s", exc)

        # Update counters
        self._response_count += 1
        self._last_response_time = time.monotonic()

        # Log
        entry = AutoResponseLog(
            timestamp=time.time(),
            prompt_type=prompt.prompt_type,
            decision=decision,
            executed=executed,
            error=error,
        )
        self._response_log.append(entry)

        if executed:
            logger.info(
                "AutoResponder: executed %s (reason: %s)",
                decision.action,
                decision.reasoning,
            )

    def _execute_decision(self, decision: Decision, prompt: UserPromptInfo) -> None:
        """Execute a decision by sending appropriate tmux keystrokes.

        Args:
            decision: The decision to execute.
            prompt: The original prompt (needed for navigation offset).
        """
        if decision.action == "select":
            # Decision value is 1-based index from LLM; convert to 0-based
            target = int(decision.value) - 1
            self._navigate_and_confirm(prompt.selected_index, target)

        elif decision.action == "select_and_type":
            # Navigate to the last option (the "Other" / "Type something" option)
            last_idx = len(prompt.options) - 1
            self._navigate_and_confirm(prompt.selected_index, last_idx)
            time.sleep(0.5)
            self._tmux.send_keys(str(decision.value), enter=True)

        elif decision.action == "text":
            self._tmux.send_keys(str(decision.value), enter=True)

        elif decision.action == "confirm":
            keywords = ["yes", "allow"] if decision.value else ["no", "deny"]
            target = self._find_option_index(prompt.options, keywords)
            self._navigate_and_confirm(prompt.selected_index, target)

        elif decision.action == "permission":
            keywords = ["yes", "allow"] if decision.value else ["no", "deny"]
            target = self._find_option_index(prompt.options, keywords)
            self._navigate_and_confirm(prompt.selected_index, target)

        else:
            raise ValueError(f"Unknown action: {decision.action}")

    def _navigate_and_confirm(self, current: int, target: int) -> None:
        """Navigate from current selection to target and press Enter.

        Args:
            current: Currently selected 0-based index.
            target: Desired 0-based index.
        """
        delta = target - current
        if delta > 0:
            key = "Down"
        elif delta < 0:
            key = "Up"
        else:
            key = None

        if key:
            for _ in range(abs(delta)):
                self._tmux.send_special_key(key)

        self._tmux.send_special_key("Enter")

    @staticmethod
    def _find_option_index(options: list, keywords: list) -> int:
        """Find the first option whose text contains one of the keywords.

        Args:
            options: List of option label strings.
            keywords: List of keywords to search for (case-insensitive).

        Returns:
            0-based index of the matching option, or 0 if no match found.
        """
        for i, opt in enumerate(options):
            for kw in keywords:
                if kw in opt.lower():
                    return i
        return 0
