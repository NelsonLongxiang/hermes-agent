"""tools/claude_session/output_parser.py — Parse Claude Code TUI output."""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParseResult:
    """Result of parsing captured tmux output."""
    state: str
    tool_name: Optional[str] = None
    tool_target: Optional[str] = None
    permission_text: Optional[str] = None
    error_text: Optional[str] = None


# Regex patterns
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?m")
_TOOL_CALL_RE = re.compile(r"^●\s+(\w+)(?:\s+(.+))?$")
_TOOL_CALL_PAREN_RE = re.compile(r"^●\s+(\w+)\((.+)\)$")
_PROMPT_RE = re.compile(r"^❯")
_PERMISSION_RE = re.compile(r"(Allow\?.*|.*permission.*|.*Yes.*No.*)", re.IGNORECASE)
# Bottom status bar patterns — these are NOT real permission prompts
_DECORATION_RE = re.compile(r"^[─━]{5,}$")  # thin or thick separator lines
_STATUS_BAR_RE = re.compile(
    r"(bypass permissions (on|off)|shift\+tab to cycle|esc to interrupt|"
    r"⏵⏵|/model|/mcp|/ide for Visual Studio Code|"
    r"[─━]{5,})",  # horizontal separator lines (thin ─ or thick ━)
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"(Error:.*|Failed:.*|error:.*)", re.IGNORECASE)


class OutputParser:
    """Static methods for parsing Claude Code TUI output from tmux capture-pane."""

    @staticmethod
    def strip_ansi(text: str) -> str:
        """Remove all ANSI escape sequences from text."""
        return _ANSI_RE.sub("", text)

    @staticmethod
    def clean_lines(raw_output: str) -> list:
        """Split raw tmux output into cleaned, non-empty lines."""
        text = OutputParser.strip_ansi(raw_output)
        return [line for line in text.splitlines() if line.strip()]

    @staticmethod
    def detect_state(lines: list) -> ParseResult:
        """Detect the current Claude Code state from cleaned output lines.

        Priority order: ERROR > PERMISSION > TOOL_CALL > IDLE > THINKING
        """
        if not lines:
            return ParseResult(state="THINKING")

        last_lines = lines[-5:] if len(lines) >= 5 else lines
        all_text = "\n".join(last_lines)

        # Check ERROR first (highest priority)
        error_match = _ERROR_RE.search(all_text)
        if error_match:
            return ParseResult(state="ERROR", error_text=error_match.group(0))

        # Check PERMISSION — but exclude bottom status bar lines
        # Status bar contains "bypass permissions on" etc. which falsely match
        non_status_lines = [l for l in last_lines if not _STATUS_BAR_RE.search(l)]
        if non_status_lines:
            non_status_text = "\n".join(non_status_lines)
            perm_match = _PERMISSION_RE.search(non_status_text)
            if perm_match:
                return ParseResult(state="PERMISSION", permission_text=perm_match.group(0))

        # Check IDLE before TOOL_CALL — if there's a prompt anywhere in
        # the recent non-decorative lines, the tool call is from a completed turn.
        idle_check_lines = [l for l in last_lines if not _STATUS_BAR_RE.search(l)]
        for line in reversed(idle_check_lines):
            if _PROMPT_RE.search(line):
                return ParseResult(state="IDLE")

        # Check TOOL_CALL (scan recent lines only — last 10, not all)
        # Old ● lines from completed turns should not trigger this.
        recent_lines = lines[-10:] if len(lines) >= 10 else lines
        for line in reversed(recent_lines):
            tool_info = OutputParser._parse_tool_line(line)
            if tool_info:
                return ParseResult(
                    state="TOOL_CALL",
                    tool_name=tool_info["tool_name"],
                    tool_target=tool_info["target"],
                )

        # Default: THINKING
        return ParseResult(state="THINKING")

    @staticmethod
    def _parse_tool_line(line: str) -> Optional[dict]:
        """Parse a single tool call line like '● Edit src/auth.py'."""
        # Try parenthesized form: ● Bash(cmd)
        m = _TOOL_CALL_PAREN_RE.match(line.strip())
        if m:
            return {"tool_name": m.group(1), "target": m.group(2)}
        # Try standard form: ● Edit file
        m = _TOOL_CALL_RE.match(line.strip())
        if m:
            return {"tool_name": m.group(1), "target": m.group(2) or ""}
        return None

    @staticmethod
    def extract_tool_calls(lines: list) -> list:
        """Extract all tool call entries from output lines."""
        calls = []
        for line in lines:
            info = OutputParser._parse_tool_line(line)
            if info:
                calls.append(info)
        return calls
