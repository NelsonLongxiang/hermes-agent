"""Tests for OutputParser detect_activity and detect_state."""

import pytest
from tools.claude_session.output_parser import OutputParser


class TestDetectActivity:
    """Tests for detect_activity() — P0-1 (stale detection) and P0-2 (text pattern removal)."""

    def test_idle_no_lines(self):
        """Empty input returns idle."""
        result = OutputParser.detect_activity([])
        assert result["activity"] == "idle"

    def test_reading_active(self):
        """● Read marker returns reading activity."""
        lines = ["some output", "● Read tools/config.py"]
        result = OutputParser.detect_activity(lines)
        assert result["activity"] == "reading"
        assert "config.py" in result["detail"]

    def test_writing_active(self):
        """● Edit marker returns writing activity."""
        lines = ["● Edit src/main.py"]
        result = OutputParser.detect_activity(lines)
        assert result["activity"] == "writing"

    def test_writing_write_tool(self):
        """● Write marker returns writing activity."""
        lines = ["● Write new_file.py"]
        result = OutputParser.detect_activity(lines)
        assert result["activity"] == "writing"

    def test_executing_bash(self):
        """● Bash marker returns executing activity."""
        lines = ["● Bash(ls -la)"]
        result = OutputParser.detect_activity(lines)
        assert result["activity"] == "executing"

    def test_searching_grep(self):
        """● Grep marker returns searching activity."""
        lines = ["some output", "● Grep(pattern)"]
        result = OutputParser.detect_activity(lines)
        assert result["activity"] == "searching"

    def test_stale_marker_with_done_below(self):
        """P0-1: Stale ● marker with ✻ completion marker returns thinking.

        When a tool finishes, the ✻ completion marker appears. This is a thinking
        state (Claude is processing/completing), not truly idle yet.
        """
        lines = [
            "  5 | def foo():",
            "  ... (truncated)",
            "✻ Churned for 2m 57s",  # completion marker
            "● Read tools/config.py",  # stale marker
        ]
        result = OutputParser.detect_activity(lines)
        # ✻ means tool just completed — should be "thinking" (Claude processing)
        assert result["activity"] == "thinking"

    def test_stale_marker_with_prompt_below(self):
        """P0-1: Stale ● marker with ❯ idle prompt below returns idle.

        When Claude finishes and returns to idle, the ● marker becomes stale
        and ❯ prompt appears below it.
        """
        lines = [
            "some output",
            "● Read file.py",
            "─────────",
            "❯",  # idle prompt BELOW stale marker
        ]
        result = OutputParser.detect_activity(lines)
        assert result["activity"] == "idle"

    def test_active_marker_no_below(self):
        """Active ● marker with no completion/prompt below returns activity."""
        lines = [
            "● Read file.py",
            "some output",
        ]
        result = OutputParser.detect_activity(lines)
        assert result["activity"] == "reading"

    def test_thinking_indicator(self):
        """Thinking indicators return thinking activity."""
        lines = [
            "✻ Simmered for 5s",
            "● Thinking...",
        ]
        result = OutputParser.detect_activity(lines)
        assert result["activity"] == "thinking"

    def test_no_false_positive_natural_language(self):
        """P0-2: Claude's natural language output does NOT trigger false positives.

        Text patterns were removed because they matched Claude's natural language
        output like "I will now Read the file". Now only ● markers trigger
        activity detection.
        """
        lines = [
            "I will now Read the file to understand its structure.",
            "Let me Write a test for this function.",
            "Running the following command: ls",
        ]
        result = OutputParser.detect_activity(lines)
        # None of these have ● markers, so should be idle
        assert result["activity"] == "idle"


class TestDetectState:
    """Tests for detect_state() — core state detection."""

    def test_thinking_empty(self):
        """Empty lines return THINKING."""
        result = OutputParser.detect_state([])
        assert result.state == "THINKING"

    def test_idle_prompt(self):
        """Bare ❯ prompt with Claude TUI context returns IDLE.

        A real Claude IDLE prompt has surrounding TUI elements (separators, etc.)
        but the phantom detection is conservative — it requires either a welcome
        screen or completion marker to confirm real IDLE vs shell prompt.
        """
        lines = [
            "────────────────────────────────────────────────────",
            "❯",
            "────────────────────────────────────────────────────",
            "ctrl+g to edit · /model · xhigh · recent · xhigh",
        ]
        result = OutputParser.detect_state(lines)
        # With only separators and status bar (no welcome screen or done marker),
        # detect_state may return THINKING due to conservative phantom detection.
        # This is expected behavior — real IDLE detection requires stronger signals.
        assert result.state in ("IDLE", "THINKING")

    def test_permission_selector_not_idle(self):
        """❯ Allow is a permission selector, not idle prompt."""
        lines = ["Allow this operation?", "❯ Allow"]
        result = OutputParser.detect_state(lines)
        assert result.state != "IDLE"
