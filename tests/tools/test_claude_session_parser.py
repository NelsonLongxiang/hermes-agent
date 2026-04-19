"""Tests for tools/claude_session/output_parser.py"""

import pytest
from tools.claude_session.output_parser import OutputParser, ParseResult


class TestAnsiStrip:
    def test_removes_color_codes(self):
        result = OutputParser.strip_ansi("\x1b[32mgreen\x1b[0m")
        assert result == "green"

    def test_removes_cursor_codes(self):
        result = OutputParser.strip_ansi("\x1b[?25lhidden cursor\x1b[?25h")
        assert result == "hidden cursor"

    def test_preserves_plain_text(self):
        result = OutputParser.strip_ansi("hello world")
        assert result == "hello world"

    def test_removes_complex_ansi(self):
        result = OutputParser.strip_ansi("\x1b[1;32;40mbold green on black\x1b[0m")
        assert result == "bold green on black"


class TestDetectState:
    def test_idle_with_prompt(self):
        lines = ["some output", "❯ "]
        result = OutputParser.detect_state(lines)
        assert result.state == "IDLE"

    def test_idle_with_typed_text(self):
        lines = ["some output", "❯ fix the bug"]
        result = OutputParser.detect_state(lines)
        assert result.state == "IDLE"

    def test_thinking_with_spinner(self):
        # No prompt, no tool markers, no error → THINKING
        lines = ["some output", "processing..."]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"

    def test_tool_call_detected(self):
        lines = ["some output", "● Edit src/auth.py"]
        result = OutputParser.detect_state(lines)
        assert result.state == "TOOL_CALL"
        assert result.tool_name == "Edit"
        assert result.tool_target == "src/auth.py"

    def test_tool_call_bash(self):
        lines = ["● Bash(npm test)"]
        result = OutputParser.detect_state(lines)
        assert result.state == "TOOL_CALL"
        assert result.tool_name == "Bash"
        assert result.tool_target == "npm test"

    def test_permission_detected(self):
        lines = ["Allow Edit to src/auth.py?", "Yes / No"]
        result = OutputParser.detect_state(lines)
        assert result.state == "PERMISSION"
        assert result.permission_text is not None

    def test_permission_with_allow_selector(self):
        """Claude Code's permission UI uses '❯ Allow' as selector."""
        lines = ["Allow Edit to src/auth.py?", "❯ Allow", "  Deny"]
        result = OutputParser.detect_state(lines)
        assert result.state == "PERMISSION"

    def test_permission_with_numbered_selector(self):
        """Claude Code may show numbered options."""
        lines = [
            "Allow Bash?",
            "❯ 1. Yes, allow this time",
            "  2. Yes, and don't ask again",
            "  3. No",
        ]
        result = OutputParser.detect_state(lines)
        assert result.state == "PERMISSION"

    def test_permission_bash_short(self):
        """Short form: 'Allow Bash?' with Allow/Deny selector."""
        lines = ["  Allow Bash?", "  ❯ Allow", "    Deny"]
        result = OutputParser.detect_state(lines)
        assert result.state == "PERMISSION"

    def test_permission_not_confused_with_idle(self):
        """Permission selector '❯ Allow' must NOT be detected as IDLE."""
        lines = ["Allow Write to config.yaml?", "❯ Allow for this session", "  Deny"]
        result = OutputParser.detect_state(lines)
        assert result.state == "PERMISSION"
        assert result.state != "IDLE"

    def test_permission_with_tool_call_above(self):
        """Permission prompt after tool call should be PERMISSION, not TOOL_CALL."""
        lines = ["● Edit src/auth.py", "  Allow Edit to src/auth.py?", "❯ Allow"]
        result = OutputParser.detect_state(lines)
        assert result.state == "PERMISSION"

    def test_error_detected(self):
        lines = ["Error: something went wrong"]
        result = OutputParser.detect_state(lines)
        assert result.state == "ERROR"

    def test_empty_lines_is_thinking(self):
        lines = []
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"

    def test_bypass_permissions_status_bar_not_permission(self):
        """Bottom status bar 'bypass permissions on' should not trigger PERMISSION."""
        lines = [
            "✻ Thinking...",
            "❯ Press up to edit",
            "────────────────────────",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ]
        result = OutputParser.detect_state(lines)
        assert result.state != "PERMISSION"


class TestExtractToolCalls:
    def test_single_tool_call(self):
        lines = ["● Edit src/auth.py", "  editing file...", "● Read tests/test_auth.py"]
        calls = OutputParser.extract_tool_calls(lines)
        assert len(calls) == 2
        assert calls[0]["tool_name"] == "Edit"
        assert calls[0]["target"] == "src/auth.py"
        assert calls[1]["tool_name"] == "Read"

    def test_bash_with_command(self):
        lines = ["● Bash(pytest -xvs tests/)"]
        calls = OutputParser.extract_tool_calls(lines)
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "Bash"
        assert calls[0]["target"] == "pytest -xvs tests/"

    def test_no_tool_calls(self):
        lines = ["just regular output", "no tools here"]
        calls = OutputParser.extract_tool_calls(lines)
        assert len(calls) == 0
