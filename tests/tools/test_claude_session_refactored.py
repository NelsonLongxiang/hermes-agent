"""Tests for refactored claude_session modules: task_context, idle, session."""

import pytest
from tools.claude_session.task_context import TaskContext, FileContext
from tools.claude_session.idle import (
    SessionState, clean_lines, detect_state, detect_activity,
    detect_startup_scene, strip_ansi, is_permission_in_text,
)
from tools.claude_session.session import ClaudeSession, _StateView


# ---------------------------------------------------------------------------
# TaskContext tests
# ---------------------------------------------------------------------------

class TestTaskContext:
    def test_basic_prompt(self):
        tc = TaskContext(task_description="Fix login bug")
        prompt = tc.to_prompt()
        assert "Fix login bug" in prompt
        assert "## Task" in prompt

    def test_full_prompt(self):
        tc = TaskContext(
            task_description="Implement auth",
            file_contexts=[
                FileContext(path="auth.py", content="def login(): pass", description="Auth module"),
            ],
            constraints=["Must not break existing tests"],
            acceptance_criteria=["Login works with valid credentials"],
            project_conventions="Use snake_case for functions",
        )
        prompt = tc.to_prompt()
        assert "## Task" in prompt
        assert "## Relevant Files" in prompt
        assert "auth.py" in prompt
        assert "def login(): pass" in prompt
        assert "## Constraints" in prompt
        assert "## Acceptance Criteria" in prompt
        assert "## Project Conventions" in prompt

    def test_empty_optional_fields(self):
        tc = TaskContext(task_description="Simple task")
        prompt = tc.to_prompt()
        assert "Relevant Files" not in prompt
        assert "Constraints" not in prompt
        assert "Acceptance Criteria" not in prompt
        assert "Project Conventions" not in prompt


# ---------------------------------------------------------------------------
# idle.py tests
# ---------------------------------------------------------------------------

class TestIdleDetection:
    def test_empty_input(self):
        result = detect_state([])
        assert result.state == SessionState.THINKING

    def test_idle_with_welcome_screen(self):
        lines = ["Welcome to Claude", "Tips for getting started", "❯"]
        result = detect_state(lines)
        assert result.state == SessionState.IDLE

    def test_idle_with_done_marker(self):
        lines = ["✻ Churned for 2m 57s", "─────────", "❯", "─────────", "⏵⏵"]
        result = detect_state(lines)
        assert result.state == SessionState.IDLE

    def test_tool_call(self):
        lines = ["● Read file.py"]
        result = detect_state(lines)
        assert result.state == SessionState.TOOL_CALL
        assert result.tool_name == "Read"
        assert result.tool_target == "file.py"

    def test_tool_call_paren(self):
        lines = ["● Bash(npm test)"]
        result = detect_state(lines)
        assert result.state == SessionState.TOOL_CALL
        assert result.tool_name == "Bash"

    def test_permission(self):
        lines = ["Allow Bash command?", "❯ 1. Yes", "  2. No"]
        result = detect_state(lines)
        assert result.state == SessionState.PERMISSION

    def test_shell_prompt_is_exited(self):
        lines = ["some output", "❯"]
        result = detect_state(lines)
        assert result.state == SessionState.EXITED

    def test_phantom_prompt_is_thinking(self):
        lines = ["─────────", "❯", "─────────", "⏵⏵ bypass permissions on"]
        result = detect_state(lines)
        assert result.state == SessionState.THINKING

    def test_compacting(self):
        lines = ["Compacting conversation..."]
        result = detect_state(lines)
        assert result.state == SessionState.THINKING
        assert result.is_compacting

    def test_stale_tool_marker(self):
        lines = ["● Read old.py", "✻ Churned for 1m", "Welcome to Claude", "❯"]
        result = detect_state(lines)
        assert result.state == SessionState.IDLE


class TestActivityDetection:
    def test_reading(self):
        result = detect_activity(["● Read file.py"])
        assert result["activity"] == "reading"

    def test_writing(self):
        result = detect_activity(["● Edit file.py"])
        assert result["activity"] == "writing"

    def test_executing(self):
        result = detect_activity(["● Bash(npm test)"])
        assert result["activity"] == "executing"

    def test_searching(self):
        result = detect_activity(["● Grep pattern"])
        assert result["activity"] == "searching"

    def test_idle_no_markers(self):
        result = detect_activity(["some text", "more text"])
        assert result["activity"] == "idle"

    def test_empty(self):
        result = detect_activity([])
        assert result["activity"] == "idle"


class TestCleanLines:
    def test_strips_ansi(self):
        raw = "\x1b[32mgreen text\x1b[0m\nnormal"
        lines = clean_lines(raw)
        assert lines == ["green text", "normal"]

    def test_removes_empty(self):
        lines = clean_lines("hello\n\nworld\n")
        assert lines == ["hello", "world"]


class TestPermissionDetection:
    def test_real_permission(self):
        assert is_permission_in_text("Allow Bash command?\n❯ 1. Yes")

    def test_status_bar_not_permission(self):
        assert not is_permission_in_text("⏵⏵ bypass permissions on\nshift+tab to cycle")


class TestStartupScene:
    def test_workspace_trust(self):
        lines = ["Quick safety check", "Enter to confirm"]
        scene = detect_startup_scene(lines)
        assert scene is not None
        assert scene.scene_type == "workspace_trust"

    def test_no_scene(self):
        assert detect_startup_scene(["normal output"]) is None


# ---------------------------------------------------------------------------
# ClaudeSession tests (unit tests, no tmux required)
# ---------------------------------------------------------------------------

class TestClaudeSessionBasic:
    def test_initial_state(self):
        s = ClaudeSession()
        assert s._state == SessionState.DISCONNECTED
        assert s._session_active is False

    def test_sm_compat(self):
        s = ClaudeSession()
        sv = s._sm
        assert sv.current_state == SessionState.DISCONNECTED
        assert sv.state_duration() > 0

    def test_backward_compat_alias(self):
        from tools.claude_session import ClaudeSessionManager
        assert ClaudeSessionManager is ClaudeSession

    def test_status_no_session(self):
        s = ClaudeSession()
        result = s.status()
        assert result["state"] == SessionState.DISCONNECTED

    def test_send_no_session(self):
        s = ClaudeSession()
        result = s.send("hello")
        assert "error" in result

    def test_send_task_context_no_session(self):
        s = ClaudeSession()
        tc = TaskContext(task_description="test")
        result = s.send(tc)
        assert "error" in result

    def test_stop_no_session(self):
        s = ClaudeSession()
        result = s.stop()
        assert "error" in result

    def test_output_no_session(self):
        s = ClaudeSession()
        result = s.output()
        assert result["lines"] == []

    def test_history_simplified(self):
        s = ClaudeSession()
        result = s.history()
        assert result["total_turns"] == 0
