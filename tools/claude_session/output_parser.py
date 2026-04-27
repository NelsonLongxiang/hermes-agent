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
    is_compacting: bool = False


@dataclass
class UserPromptInfo:
    """Detected user-input prompt from Claude Code TUI output."""
    prompt_type: str       # "ask_user" | "permission" | "confirmation" | "free_text"
    question: str          # Question text presented to user
    options: list          # Option labels
    selected_index: int    # Current ❯ selected index (0-based), -1 if unknown
    has_other: bool        # Whether last option is "Type something." / "Other"
    raw_context: str       # Raw TUI text around the prompt


@dataclass
class StartupScene:
    """Detected startup scene requiring special handling before Claude Code is fully initialized."""
    scene_type: str   # "workspace_trust" | "bypass_confirm"
    description: str
    action: str       # "press_enter" | "press_down_enter"


# Regex patterns
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?m")
_TOOL_CALL_RE = re.compile(r"^●\s+(\w+)(?:\s+(.+))?$")
_TOOL_CALL_PAREN_RE = re.compile(r"^●\s+(\w+)\((.+)\)$")
_PROMPT_RE = re.compile(r"^❯")
# Pasted text indicator: Claude Code TUI shows "[Pasted text #N +M lines]" when
# multi-line content is pasted. The text is in the input buffer but NOT sent yet.
_PASTED_TEXT_RE = re.compile(r"^\❯\s*\[Pasted text")
_PERMISSION_RE = re.compile(
    r"(Allow\s+.*\?"
    r"|.*permission\s+to.*"
    r"|❯\s*(Allow|Yes)\b"
    r"|❯\s*\d+\.\s*(Yes|Allow|Deny|No)\b"
    r"|Do you want to proceed\?"
    r"|.*Yes.*No\b)",
    re.IGNORECASE,
)
# Bottom status bar patterns — these are NOT real permission prompts
_DECORATION_RE = re.compile(r"^[─━]{5,}$")  # thin or thick separator lines
_STATUS_BAR_RE = re.compile(
    r"(bypass permissions (on|off)|shift\+tab to cycle|esc to interrupt|"
    r"⏵⏵|/model|/mcp|/ide for Visual Studio Code|"
    r"[─━]{5,})",  # horizontal separator lines (thin ─ or thick ━)
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"(Error:.*|Failed:.*|error:.*)", re.IGNORECASE)
# Claude Code completion time indicator: "✻ Churned for 2m 57s", "✻ Sautéed for 6m 28s"
_DONE_TIME_RE = re.compile(r"^✻\s+\S+.*\bfor\s+\d+[hms]", re.IGNORECASE)
_COMPACT_RE = re.compile(
    r"(Compacting|compressing\s+conversation|context\s+compression|"
    r"condensing|summarizing\s+conversation|✓.*compact|"
    r"concise.*summary|compact.*history)",
    re.IGNORECASE,
)
# User prompt detection patterns
_SELECTED_OPTION_RE = re.compile(r"^❯\s*(\d+)\.\s*(.+)$")
_UNSELECTED_OPTION_RE = re.compile(r"^\s*(\d+)\.\s+(.+)$")
_TYPE_SOMETHING_RE = re.compile(r"Type something", re.IGNORECASE)
_QUESTION_END_RE = re.compile(r"[?？]$")

# Startup scene detection patterns
# "Quick safety check" is unique to Claude Code's workspace trust prompt.
_TRUST_WORKSPACE_RE = re.compile(
    r"quick\s+safety\s+check",
    re.IGNORECASE,
)
_ENTER_TO_CONFIRM_RE = re.compile(
    r"enter\s+to\s+confirm",
    re.IGNORECASE,
)

# Claude Code CLI welcome screen detection
_WELCOME_SCREEN_RE = re.compile(
    r"(welcome to claude|welcome back|tips for getting started|recent activity)",
    re.IGNORECASE,
)

# Claude Code TUI signature patterns — present when Claude is running
_CLAUDE_TUI_SIGNATURE_RE = re.compile(
    r"(claude|thinking|compacting|bypass permissions|"
    r"shift\+tab to cycle|esc to interrupt|/model|/mcp|"
    r"⏵⏵|welcome to claude)",
    re.IGNORECASE,
)


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

        Key insight: Claude Code's permission UI uses ❯ as a selector arrow
        (e.g. "❯ Allow"), which must not be confused with the IDLE prompt ❯.
        We detect permission prompts BEFORE checking for IDLE to avoid this.
        """
        if not lines:
            return ParseResult(state="THINKING")

        last_lines = lines[-5:] if len(lines) >= 5 else lines
        all_text = "\n".join(last_lines)

        # Pre-compute recent_lines for ERROR suppression and TOOL_CALL detection
        recent_lines = lines[-10:] if len(lines) >= 10 else lines

        # Check ERROR first (highest priority), but suppress when tool markers (●)
        # are present — those errors are tool output (e.g. Bash stderr), not
        # Claude Code state errors.  Without this, any "Error:" in tool output
        # would cause wait_for_idle to immediately return error, killing the task.
        has_active_tool = any(OutputParser._parse_tool_line(l) for l in recent_lines)
        error_match = _ERROR_RE.search(all_text)
        if error_match and not has_active_tool:
            return ParseResult(state="ERROR", error_text=error_match.group(0))

        # Check PERMISSION — exclude bottom status bar lines
        # Status bar contains "bypass permissions on" etc. which falsely match
        non_status_lines = [l for l in last_lines if not _STATUS_BAR_RE.search(l)]
        if non_status_lines:
            non_status_text = "\n".join(non_status_lines)
            perm_match = _PERMISSION_RE.search(non_status_text)
            if perm_match:
                return ParseResult(state="PERMISSION", permission_text=perm_match.group(0))

        # Check TOOL_CALL (scan recent_lines — already computed above)
        # Must check BEFORE IDLE because Claude Code TUI renders a phantom ❯
        # at the bottom of the pane while still executing tool calls.
        #
        # Stale marker fix: if a ❯ (IDLE prompt) appears BELOW the ● marker,
        # the tool call has already completed — skip it and let IDLE detection
        # handle the state.
        rev = list(reversed(recent_lines))  # last line first
        for i, line in enumerate(rev):
            tool_info = OutputParser._parse_tool_line(line)
            if tool_info:
                # Lines "below" the ● (later in original order) = rev[0:i]
                has_prompt_below = any(_PROMPT_RE.search(l) for l in rev[:i])
                if has_prompt_below:
                    break  # Stale marker — fall through to IDLE detection
                return ParseResult(
                    state="TOOL_CALL",
                    tool_name=tool_info["tool_name"],
                    tool_target=tool_info["target"],
                )

        # Check IDLE — but ONLY if the ❯ appears on a line by itself
        # (the bare prompt) or followed only by whitespace.
        # Claude Code's permission selector uses "❯ Allow" or "❯ 1. Yes"
        # which are NOT idle prompts.
        #
        # IMPORTANT: Also check that ❯ is NOT sandwiched between separator
        # lines (────). Claude Code TUI renders a phantom ❯ at the bottom
        # of the pane while actively working (thinking/tool_call). The real
        # idle prompt appears WITHOUT surrounding separator lines.
        idle_check_lines = [l for l in last_lines if not _STATUS_BAR_RE.search(l)]
        for line in reversed(idle_check_lines):
            stripped = line.strip()
            # IDLE prompt is "❯" alone or "❯ " followed by typed user text,
            # but NOT "❯ Allow" or "❯ 1. Yes" (permission selector).
            if _PROMPT_RE.search(line):
                # Exclude permission-selector patterns
                if re.match(r"^❯\s*(Allow|Yes|Deny|No|\d+\.)", stripped, re.IGNORECASE):
                    continue  # This is a permission selector, not IDLE

                # Pasted text is in input buffer but NOT sent yet — treat as INPUTTING
                if _PASTED_TEXT_RE.search(stripped):
                    return ParseResult(state="INPUTTING")

                # Check if ❯ is surrounded by separator lines (phantom prompt)
                # The TUI bottom area looks like:
                #   ────────
                #   ❯
                #   ────────
                #   ⏵⏵ bypass permissions on...
                # If separator lines appear within 3 lines of ❯, it's phantom.
                prompt_idx = None
                for i, raw_line in enumerate(last_lines):
                    if _PROMPT_RE.search(raw_line):
                        prompt_idx = i
                        break
                if prompt_idx is not None:
                    # Check 1-2 lines above and below for separator lines
                    nearby_separators = 0
                    for j in range(max(0, prompt_idx - 2), min(len(last_lines), prompt_idx + 3)):
                        if j == prompt_idx:
                            continue
                        if _DECORATION_RE.search(last_lines[j].strip()):
                            nearby_separators += 1
                    if nearby_separators >= 2:
                        # Possible phantom — but two cases still mean real IDLE:
                        # 1. Welcome screen is visible (Claude is waiting for first input)
                        # 2. Completion marker above separators shows Claude finished.
                        global_prompt_idx = len(lines) - len(last_lines) + prompt_idx
                        has_welcome_screen = any(_WELCOME_SCREEN_RE.search(l) for l in lines)
                        has_done_marker = any(
                            _DONE_TIME_RE.search(l)
                            for l in lines[:global_prompt_idx]
                        )
                        if not (has_welcome_screen or has_done_marker):
                            continue

                # Final check: distinguish shell prompt from Claude IDLE.
                # If ❯ has NO Claude Code TUI decorations around it, it's a
                # shell prompt (Claude has exited), not Claude's IDLE prompt.
                if OutputParser._is_shell_prompt(lines):
                    return ParseResult(state="EXITED")

                return ParseResult(state="IDLE")

        # Check COMPACT — compact 操作期间状态通常是 THINKING
        if _COMPACT_RE.search(all_text):
            return ParseResult(state="THINKING", is_compacting=True)

        # Default: THINKING
        return ParseResult(state="THINKING")

    @staticmethod
    def _is_shell_prompt(lines: list) -> bool:
        """Check if output is a bare shell prompt (Claude Code has exited).

        When Claude Code exits, the tmux pane returns to the shell. If the
        shell prompt happens to be ❯ (common with zsh/starship themes), it
        could be confused with Claude's IDLE prompt.

        Key distinction: Claude Code's TUI always renders decorations near
        the ❯ prompt (separator lines, status bar, tool markers). A bare
        shell prompt has NONE of these in its immediate vicinity.

        IMPORTANT: Only checks the last ~5 lines, not the entire scrollback.
        After Claude exits, old TUI output may persist in tmux scrollback
        history. Checking the entire buffer would produce false negatives.
        The shell prompt is always at the bottom; if its immediate context
        has no TUI elements, Claude has exited regardless of old history.
        """
        if not lines:
            return False

        # Only examine recent lines near the ❯ prompt
        window = lines[-5:] if len(lines) >= 5 else lines

        # Must have ❯ in the recent window
        if not any("❯" in l for l in window):
            return False

        window_text = "\n".join(window)

        # Check for Claude Code TUI signatures in the recent window only
        has_separator = any(_DECORATION_RE.search(l.strip()) for l in window)
        has_status_bar = any(_STATUS_BAR_RE.search(l) for l in window)
        has_tool_markers = any("●" in l for l in window)
        has_done_marker = any(_DONE_TIME_RE.search(l) for l in window)
        has_claude_signature = bool(_CLAUDE_TUI_SIGNATURE_RE.search(window_text))
        has_welcome_screen = bool(_WELCOME_SCREEN_RE.search(window_text))

        if has_separator or has_status_bar or has_tool_markers or has_done_marker or has_claude_signature:
            return False

        # Welcome screen has ❯ at bottom but it's Claude Code's input prompt, not shell
        if has_welcome_screen:
            return False

        return True

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

    @staticmethod
    def detect_user_prompt(
        lines: list, current_state: str
    ) -> Optional["UserPromptInfo"]:
        """Detect user-input prompt from TUI output lines.

        Only processes IDLE and PERMISSION states — other states return None.
        Tries detectors in order: ask_user, permission, confirmation, free_text.
        """
        if current_state not in ("IDLE", "PERMISSION"):
            return None
        if not lines:
            return None

        result = OutputParser._detect_ask_user(lines)
        if result:
            return result
        result = OutputParser._detect_permission_prompt(lines)
        if result:
            return result
        result = OutputParser._detect_confirmation(lines)
        if result:
            return result
        result = OutputParser._detect_free_text(lines)
        if result:
            return result
        return None

    @staticmethod
    def _detect_ask_user(lines: list) -> Optional["UserPromptInfo"]:
        """Detect AskUserQuestion-style numbered options with ❯ selector."""
        # Find all selected (❯) and unselected option lines
        selected_indices = []  # (line_index, option_number, label)
        unselected = []  # (line_index, option_number, label)
        for i, line in enumerate(lines):
            m = _SELECTED_OPTION_RE.match(line.strip())
            if m:
                selected_indices.append((i, int(m.group(1)), m.group(2)))
                continue
            m = _UNSELECTED_OPTION_RE.match(line.strip())
            if m:
                unselected.append((i, int(m.group(1)), m.group(2)))

        if not selected_indices:
            return None

        # Must have exactly one selected option
        if len(selected_indices) != 1:
            return None

        sel_line_idx, sel_num, sel_label = selected_indices[0]

        # Collect all options (selected + unselected), sorted by line index
        all_options_raw = selected_indices + unselected
        all_options_raw.sort(key=lambda x: x[0])

        # Verify options have consecutive numbers
        nums = [opt[1] for opt in all_options_raw]
        expected = list(range(nums[0], nums[0] + len(nums)))
        if nums != expected:
            return None

        # Extract labels in order
        options = [opt[2] for opt in all_options_raw]

        # Determine selected_index (0-based position in the ordered list)
        selected_index = nums.index(sel_num)

        # Check if last option is "Type something."
        has_other = bool(options and _TYPE_SOMETHING_RE.search(options[-1]))

        # Find question text from lines above the first option
        first_opt_line = all_options_raw[0][0]
        question = ""
        for j in range(first_opt_line - 1, -1, -1):
            text = lines[j].strip()
            if text:
                question = text
                break

        # Build raw_context from surrounding lines
        ctx_start = max(0, first_opt_line - 2)
        ctx_end = min(len(lines), all_options_raw[-1][0] + 2)
        raw_context = "\n".join(lines[ctx_start:ctx_end])

        return UserPromptInfo(
            prompt_type="ask_user",
            question=question,
            options=options,
            selected_index=selected_index,
            has_other=has_other,
            raw_context=raw_context,
        )

    @staticmethod
    def _detect_permission_prompt(lines: list) -> Optional["UserPromptInfo"]:
        """Detect permission prompt (Allow/Deny, Yes/No).

        Handles unnumbered options like '❯ Allow' / 'Deny'.
        Numbered options are handled by _detect_ask_user (which runs first).
        """
        # Find permission question line via _PERMISSION_RE
        perm_line_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Look for "Allow ... ?" pattern (the question part)
            if re.match(r"^Allow\s+.*\?$", stripped, re.IGNORECASE):
                perm_line_idx = i
                break
        if perm_line_idx is None:
            return None

        # Look for unnumbered options below the question
        options = []
        selected_index = -1
        for i in range(perm_line_idx + 1, min(len(lines), perm_line_idx + 6)):
            stripped = lines[i].strip()
            if not stripped:
                continue
            # Check for selected option: "❯ Allow", "❯ Deny", "❯ Yes", "❯ No"
            if stripped.startswith("❯"):
                label = stripped.lstrip("❯").strip()
                if label:
                    first_word = label.split()[0].lower()
                    if first_word in ("allow", "deny", "yes", "no"):
                        selected_index = len(options)
                        options.append(label)
                        continue
            # Check for unselected option: "Allow", "Deny", "Yes", "No"
            first_word = stripped.split()[0].lower() if stripped else ""
            if first_word in ("allow", "deny", "yes", "no"):
                options.append(stripped)

        if len(options) < 2:
            return None
        if len(options) > 5:
            return None

        question = lines[perm_line_idx].strip()
        ctx_start = max(0, perm_line_idx - 1)
        ctx_end = min(len(lines), perm_line_idx + len(options) + 2)
        raw_context = "\n".join(lines[ctx_start:ctx_end])

        return UserPromptInfo(
            prompt_type="permission",
            question=question,
            options=options,
            selected_index=selected_index,
            has_other=False,
            raw_context=raw_context,
        )

    @staticmethod
    def _detect_confirmation(lines: list) -> Optional["UserPromptInfo"]:
        """Detect confirmation prompt (Yes/No questions).

        Looks for question ending with ? containing confirmation keywords,
        followed by Yes/No options (may include ❯ selector).
        """
        _CONFIRM_KEYWORDS = re.compile(
            r"(do you want|are you sure|proceed\?|confirm|continue\?)",
            re.IGNORECASE,
        )

        # Find question lines ending with ?
        for i, line in enumerate(lines):
            stripped = line.strip()
            if _QUESTION_END_RE.search(stripped) and _CONFIRM_KEYWORDS.search(stripped):
                # Look for Yes/No options below
                options = []
                selected_index = -1
                for j in range(i + 1, min(len(lines), i + 6)):
                    opt_stripped = lines[j].strip()
                    if not opt_stripped:
                        continue
                    # Selected: "❯ Yes", "❯ No"
                    if opt_stripped.startswith("❯"):
                        label = opt_stripped.lstrip("❯").strip()
                        if label:
                            first_word = label.split()[0].lower()
                            if first_word in ("yes", "no"):
                                selected_index = len(options)
                                options.append(label)
                                continue
                    # Unselected: "Yes", "No"
                    first_word = opt_stripped.split()[0].lower() if opt_stripped else ""
                    if first_word in ("yes", "no"):
                        options.append(opt_stripped)

                if len(options) >= 2:
                    ctx_start = max(0, i - 1)
                    ctx_end = min(len(lines), i + len(options) + 2)
                    raw_context = "\n".join(lines[ctx_start:ctx_end])
                    return UserPromptInfo(
                        prompt_type="confirmation",
                        question=stripped,
                        options=options,
                        selected_index=selected_index,
                        has_other=False,
                        raw_context=raw_context,
                    )
        return None

    @staticmethod
    def _detect_free_text(lines: list) -> Optional["UserPromptInfo"]:
        """Detect free-text input prompt.

        Must have empty ❯ or ❯ (with optional whitespace) in last 3 lines.
        Search backwards for a question ending with ? or ？.
        Conservative: require done marker (✻ ...) OR question within 5 lines of ❯.
        """
        if not lines:
            return None

        # Find empty ❯ or ❯ with only whitespace in last 3 lines
        prompt_idx = None
        check_range = lines[-3:] if len(lines) >= 3 else lines
        for k, line in enumerate(check_range):
            stripped = line.strip()
            if stripped == "❯" or stripped == "❯ ":
                prompt_idx = len(lines) - len(check_range) + k
                break
        if prompt_idx is None:
            return None

        # Search backwards for question ending with ?
        question = ""
        question_idx = None
        for j in range(prompt_idx - 1, -1, -1):
            stripped = lines[j].strip()
            # Skip status bar, decoration, done-time lines
            if _STATUS_BAR_RE.search(stripped):
                continue
            if _DECORATION_RE.search(stripped):
                continue
            if _DONE_TIME_RE.search(stripped):
                continue
            if not stripped:
                continue
            if _QUESTION_END_RE.search(stripped):
                question = stripped
                question_idx = j
                break
            # Stop if we hit a non-question line that isn't skippable
            # (only look at immediate nearby lines for close questions)
            break

        if not question:
            return None

        # Conservative: require done marker OR question within 5 lines of ❯
        has_done_marker = any(
            _DONE_TIME_RE.search(lines[j])
            for j in range(0, prompt_idx)
        )
        distance = prompt_idx - question_idx if question_idx is not None else 999
        if not has_done_marker and distance > 5:
            return None

        ctx_start = max(0, question_idx - 1 if question_idx is not None else prompt_idx - 2)
        ctx_end = min(len(lines), prompt_idx + 2)
        raw_context = "\n".join(lines[ctx_start:ctx_end])

        return UserPromptInfo(
            prompt_type="free_text",
            question=question,
            options=[],
            selected_index=-1,
            has_other=False,
            raw_context=raw_context,
        )

    @staticmethod
    def detect_startup_scene(lines: list) -> Optional["StartupScene"]:
        """Detect startup scenes that require special handling.

        Only applies during Claude Code initialization, before the normal
        IDLE/THINKING states are reached. Detects interactive prompts that
        block startup, such as workspace trust confirmation.

        Returns None if no startup scene is detected.
        """
        if not lines:
            return None

        all_text = "\n".join(lines)

        # Workspace trust prompt: "Quick safety check" + "Enter to confirm"
        if _TRUST_WORKSPACE_RE.search(all_text):
            if _ENTER_TO_CONFIRM_RE.search(all_text):
                return StartupScene(
                    scene_type="workspace_trust",
                    description="Workspace trust confirmation prompt",
                    action="press_enter",
                )

        return None
