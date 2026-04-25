"""Tests for tools/claude_session/output_parser.py"""

import pytest
from tools.claude_session.output_parser import OutputParser, ParseResult, UserPromptInfo


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


class TestCompactDetection:
    """Tests for compact operation detection in output_parser."""

    def test_compact_keyword_compacting(self):
        lines = ["some output", "Compacting conversation history..."]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True

    def test_compact_keyword_compressing(self):
        lines = ["✻ Thinking...", "compressing conversation to save tokens"]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True

    def test_compact_keyword_context_compression(self):
        lines = ["performing context compression"]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True

    def test_compact_keyword_condensing(self):
        lines = ["condensing the conversation history"]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True

    def test_compact_keyword_summarizing(self):
        lines = ["summarizing conversation for context window"]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True

    def test_compact_keyword_checkmark(self):
        lines = ["✓ compact completed"]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True

    def test_compact_keyword_concise_summary(self):
        lines = ["creating concise summary of history"]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True

    def test_compact_keyword_compact_history(self):
        lines = ["compact history to reduce token usage"]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True

    def test_no_compact_normal_thinking(self):
        """Normal thinking output should NOT trigger compact detection."""
        lines = ["some output", "processing data..."]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is False

    def test_no_compact_with_tool_call(self):
        """Tool call has higher priority than compact."""
        lines = ["Compacting conversation...", "● Edit file.py"]
        result = OutputParser.detect_state(lines)
        assert result.state == "TOOL_CALL"
        assert result.is_compacting is False

    def test_no_compact_with_idle_prompt(self):
        """IDLE prompt has higher priority than compact."""
        lines = ["Compacting done", "❯ "]
        result = OutputParser.detect_state(lines)
        assert result.state == "IDLE"
        assert result.is_compacting is False

    def test_no_compact_with_error(self):
        """ERROR has highest priority, no compact flag."""
        lines = ["Compacting conversation", "Error: compact failed"]
        result = OutputParser.detect_state(lines)
        assert result.state == "ERROR"
        assert result.is_compacting is False

    def test_compact_result_field_default(self):
        """Default ParseResult should have is_compacting=False."""
        pr = ParseResult(state="THINKING")
        assert pr.is_compacting is False

    def test_compact_case_insensitive(self):
        """Compact detection should be case-insensitive."""
        lines = ["COMPACTING CONVERSATION HISTORY"]
        result = OutputParser.detect_state(lines)
        assert result.state == "THINKING"
        assert result.is_compacting is True


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


class TestDetectUserPrompt:
    """Tests for detect_user_prompt() scene detection."""

    def test_ask_user_basic(self):
        """Numbered options with ❯ on option 2."""
        lines = [
            "Which file should I modify?",
            "❯ 2. src/auth.py",
            "  3. src/utils.py",
            "  4. src/main.py",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is not None
        assert result.prompt_type == "ask_user"
        assert result.selected_index == 0  # 0-based, first option is selected
        assert "src/auth.py" in result.options
        assert "src/utils.py" in result.options
        assert "src/main.py" in result.options
        assert result.has_other is False

    def test_ask_user_with_type_something(self):
        """4 options, last is 'Type something.'."""
        lines = [
            "How would you like to proceed?",
            "  1. Refactor the code",
            "  2. Add tests",
            "❯ 3. Update documentation",
            "  4. Type something.",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is not None
        assert result.prompt_type == "ask_user"
        assert len(result.options) == 4
        assert result.has_other is True
        assert result.selected_index == 2  # option 3 selected (0-based)

    def test_ask_user_selector_at_top(self):
        """❯ on first option."""
        lines = [
            "Pick a framework:",
            "❯ 1. React",
            "  2. Vue",
            "  3. Svelte",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is not None
        assert result.prompt_type == "ask_user"
        assert result.selected_index == 0
        assert result.options == ["React", "Vue", "Svelte"]

    def test_ask_user_single_option(self):
        """Just 1 option."""
        lines = [
            "Continue?",
            "❯ 1. Yes",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is not None
        assert result.prompt_type == "ask_user"
        assert result.selected_index == 0
        assert result.options == ["Yes"]

    def test_permission_with_allow_deny(self):
        """'❯ Allow' / 'Deny' pattern."""
        lines = [
            "Allow Edit to src/auth.py?",
            "❯ Allow",
            "  Deny",
        ]
        result = OutputParser.detect_user_prompt(lines, "PERMISSION")
        assert result is not None
        assert result.prompt_type == "permission"
        assert "Allow" in result.options
        assert "Deny" in result.options
        assert result.selected_index == 0

    def test_permission_with_numbered_options(self):
        """Numbered permission options detected by ask_user (runs first)."""
        lines = [
            "Allow Bash?",
            "❯ 1. Yes, allow this time",
            "  2. Yes, and don't ask again",
            "  3. No",
        ]
        result = OutputParser.detect_user_prompt(lines, "PERMISSION")
        assert result is not None
        # Numbered options match ask_user detector first
        assert result.prompt_type == "ask_user"
        assert len(result.options) == 3
        assert result.selected_index == 0

    def test_confirmation_yes_no(self):
        """'Do you want to proceed?' with Yes/No options."""
        lines = [
            "Do you want to proceed?",
            "❯ Yes",
            "  No",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is not None
        assert result.prompt_type == "confirmation"
        assert "Yes" in result.options
        assert "No" in result.options
        assert result.selected_index == 0

    def test_free_text_with_done_marker(self):
        """Question + done marker + empty ❯."""
        lines = [
            "What would you like me to do next?",
            "✻ Crunched for 6m 36s",
            "❯ ",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is not None
        assert result.prompt_type == "free_text"
        assert "What would you like" in result.question
        assert result.options == []
        assert result.selected_index == -1

    def test_free_text_without_done_marker(self):
        """Question close to ❯ (within 5 lines)."""
        lines = [
            "How should I handle this?",
            "❯ ",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is not None
        assert result.prompt_type == "free_text"
        assert "How should" in result.question

    def test_no_prompt_thinking_state(self):
        """THINKING state returns None."""
        lines = [
            "Which option?",
            "❯ 1. First",
        ]
        result = OutputParser.detect_user_prompt(lines, "THINKING")
        assert result is None

    def test_no_prompt_normal_idle(self):
        """No question returns None."""
        lines = [
            "some output",
            "❯ ",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        # No question mark found near the prompt — should not detect
        assert result is None

    def test_no_prompt_empty_lines(self):
        """Empty lines return None."""
        lines = []
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is None

    def test_ask_user_in_permission_state(self):
        """Numbered options detected as ask_user even in PERMISSION state."""
        lines = [
            "Which approach?",
            "❯ 1. Use existing library",
            "  2. Write from scratch",
        ]
        result = OutputParser.detect_user_prompt(lines, "PERMISSION")
        assert result is not None
        assert result.prompt_type == "ask_user"
        assert result.selected_index == 0

    def test_ask_user_takes_priority_over_permission(self):
        """'Allow Bash?' with numbered options = ask_user (not permission)."""
        lines = [
            "Allow Bash?",
            "❯ 1. Yes, allow this time",
            "  2. Yes, and don't ask again",
            "  3. No",
        ]
        result = OutputParser.detect_user_prompt(lines, "PERMISSION")
        assert result is not None
        # ask_user runs first and matches numbered options
        assert result.prompt_type == "ask_user"

    def test_raw_context_includes_surrounding_lines(self):
        """Verify raw_context includes surrounding lines."""
        lines = [
            "Previous output line",
            "Pick an option:",
            "❯ 1. Option A",
            "  2. Option B",
            "Following output line",
        ]
        result = OutputParser.detect_user_prompt(lines, "IDLE")
        assert result is not None
        assert result.prompt_type == "ask_user"
        # raw_context should include lines around the options
        assert "Pick an option:" in result.raw_context
        assert "Option A" in result.raw_context
        assert "Option B" in result.raw_context
