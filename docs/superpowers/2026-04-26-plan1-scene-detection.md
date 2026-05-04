# Plan 1: OutputParser 场景检测

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 OutputParser 中新增 `detect_user_prompt()` 方法，能够从 Claude Code TUI 输出中识别 4 种"等待用户输入"场景（ask_user、permission、confirmation、free_text）。

**Architecture:** 扩展 `tools/claude_session/output_parser.py`，新增 `UserPromptInfo` 数据类和 `detect_user_prompt()` 静态方法。该方法在 `detect_state()` 之后调用，作为二次分析，仅处理 IDLE/PERMISSION 状态。不修改现有 `detect_state()` 逻辑。

**Tech Stack:** Python 3.10+, re (标准库), dataclasses

---

### Task 1: 新增 UserPromptInfo 数据类

**Files:**
- Modify: `tools/claude_session/output_parser.py` (第 9 行 ParseResult 之后)

- [ ] **Step 1: 在 ParseResult 之后添加 UserPromptInfo 数据类**

在 `tools/claude_session/output_parser.py` 第 16 行（`is_compacting: bool = False` 之后）插入：

```python
@dataclass
class UserPromptInfo:
    """Detected user-input prompt from Claude Code TUI output."""
    prompt_type: str       # "ask_user" | "permission" | "confirmation" | "free_text"
    question: str          # Question text presented to user
    options: list          # Option labels (for ask_user / confirmation / permission)
    selected_index: int    # Current ❯ selected index (0-based), -1 if unknown
    has_other: bool        # Whether last option is "Type something." / "Other"
    raw_context: str       # Raw TUI text around the prompt (for LLM context)
```

- [ ] **Step 2: 运行现有测试确认没有破坏**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py -v`
Expected: 所有测试 PASS（无功能变更）

- [ ] **Step 3: Commit**

```bash
git add tools/claude_session/output_parser.py
git commit -m "feat(claude-session): add UserPromptInfo dataclass for scene detection"
```

---

### Task 2: 实现 ask_user 场景检测

**Files:**
- Modify: `tools/claude_session/output_parser.py`
- Modify: `tests/tools/test_claude_session_parser.py`

- [ ] **Step 1: 编写 ask_user 检测的失败测试**

在 `tests/tools/test_claude_session_parser.py` 末尾添加：

```python
class TestDetectUserPrompt:
    """Tests for OutputParser.detect_user_prompt()."""

    def test_ask_user_basic(self):
        """Detect AskUserQuestion with numbered options and ❯ selector."""
        lines = [
            "你希望状态消息中包含哪些信息？",
            "",
            "  1. 精简模式",
            "❯ 2. 详细模式",
            "  3. 自适应模式",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is not None
        assert result.prompt_type == "ask_user"
        assert "状态消息" in result.question
        assert len(result.options) == 3
        assert "精简模式" in result.options[0]
        assert "详细模式" in result.options[1]
        assert "自适应模式" in result.options[2]
        assert result.selected_index == 1

    def test_ask_user_with_type_something(self):
        """Detect AskUserQuestion with 'Type something.' as Other option."""
        lines = [
            "当 Claude 完成处理后，这条状态消息应该怎么处理？",
            "",
            "  1. 保留最终状态",
            "  2. 完成后删除",
            "❯ 3. 替换为摘要",
            "  4. Type something.",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is not None
        assert result.prompt_type == "ask_user"
        assert result.has_other is True
        assert len(result.options) == 4
        assert result.selected_index == 2

    def test_ask_user_selector_at_top(self):
        """Handle ❯ on the first option."""
        lines = [
            "选择方案",
            "",
            "❯ 1. 方案 A",
            "  2. 方案 B",
            "  3. 方案 C",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is not None
        assert result.selected_index == 0

    def test_ask_user_single_option(self):
        """Single option still detected."""
        lines = [
            "确认继续？",
            "",
            "❯ 1. Yes, proceed",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is not None
        assert len(result.options) == 1
        assert result.selected_index == 0
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py::TestDetectUserPrompt -v`
Expected: FAIL — `AttributeError: type object 'OutputParser' has no attribute 'detect_user_prompt'`

- [ ] **Step 3: 实现 detect_user_prompt() 方法和 ask_user 检测逻辑**

在 `tools/claude_session/output_parser.py` 的 `OutputParser` 类中（`extract_tool_calls` 方法之后）添加：

```python
# ── User prompt detection patterns ──
_SELECTED_OPTION_RE = re.compile(r"^❯\s*(\d+)\.\s*(.+)$")
_UNSELECTED_OPTION_RE = re.compile(r"^\s*(\d+)\.\s+(.+)$")
_TYPE_SOMETHING_RE = re.compile(r"Type something", re.IGNORECASE)
_QUESTION_END_RE = re.compile(r"[?？]$")

@staticmethod
def detect_user_prompt(lines: list, current_state: str = "THINKING") -> Optional["UserPromptInfo"]:
    """Detect a user-input prompt in Claude Code TUI output.

    Called AFTER detect_state(), only when state is IDLE or PERMISSION.
    Returns UserPromptInfo if a prompt is detected, None otherwise.
    """
    if current_state not in (ClaudeState.IDLE, ClaudeState.PERMISSION):
        return None
    if not lines:
        return None

    # Try detection in priority order
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
    """Detect AskUserQuestion: numbered options with ❯ selector."""
    # Find all option lines and the selected one
    options = []
    selected_index = -1
    option_start = -1

    for i, line in enumerate(lines):
        sel_match = _SELECTED_OPTION_RE.match(line)
        if sel_match:
            idx = int(sel_match.group(1)) - 1
            label = sel_match.group(2).strip()
            if option_start == -1:
                option_start = i
            selected_index = idx
            # Ensure options list is long enough
            while len(options) <= idx:
                options.append("")
            options[idx] = label
            continue

        unsel_match = _UNSELECTED_OPTION_RE.match(line)
        if unsel_match:
            idx = int(unsel_match.group(1)) - 1
            label = unsel_match.group(2).strip()
            if option_start == -1:
                option_start = i
            while len(options) <= idx:
                options.append("")
            options[idx] = label
            continue

    # Need at least one selected option and one total option
    if selected_index < 0 or not options or not any(o for o in options):
        return None

    # Verify options are consecutive (1, 2, 3... not random numbers)
    for i, opt in enumerate(options):
        if not opt:
            return None  # Gap in numbering

    # Extract question text from lines above options
    question_lines = []
    if option_start > 0:
        for line in reversed(lines[:option_start]):
            stripped = line.strip()
            if not stripped:
                continue
            if _STATUS_BAR_RE.search(stripped):
                continue
            if _DECORATION_RE.search(stripped):
                continue
            # Stop at tool call markers or separators
            if _TOOL_CALL_RE.match(stripped) or _TOOL_CALL_PAREN_RE.match(stripped):
                break
            question_lines.insert(0, stripped)
            # Usually just need the last 3 non-empty lines
            if len(question_lines) >= 3:
                break

    question = " ".join(question_lines) if question_lines else ""
    has_other = bool(_TYPE_SOMETHING_RE.search(options[-1])) if options else False

    # Raw context: 5 lines around the options area
    ctx_start = max(0, option_start - 3)
    ctx_end = min(len(lines), option_start + len(options) + 3)
    raw_context = "\n".join(lines[ctx_start:ctx_end])

    return UserPromptInfo(
        prompt_type="ask_user",
        question=question,
        options=options,
        selected_index=selected_index,
        has_other=has_other,
        raw_context=raw_context,
    )
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_ask_user_basic tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_ask_user_with_type_something tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_ask_user_selector_at_top tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_ask_user_single_option -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tools/claude_session/output_parser.py tests/tools/test_claude_session_parser.py
git commit -m "feat(claude-session): add ask_user scene detection in OutputParser"
```

---

### Task 3: 实现 permission 场景检测

**Files:**
- Modify: `tools/claude_session/output_parser.py`
- Modify: `tests/tools/test_claude_session_parser.py`

- [ ] **Step 1: 编写 permission 检测的失败测试**

在 `TestDetectUserPrompt` 类中添加：

```python
    def test_permission_with_allow_deny(self):
        """Detect permission prompt with Allow/Deny selector."""
        lines = [
            "Allow Edit to src/auth.py?",
            "",
            "❯ Allow",
            "  Deny",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="PERMISSION")
        assert result is not None
        assert result.prompt_type == "permission"
        assert "Edit" in result.question
        assert result.selected_index == 0

    def test_permission_with_numbered_options(self):
        """Detect permission prompt with numbered Yes/No options."""
        lines = [
            "Allow Bash(npm test)?",
            "",
            "❯ 1. Yes, allow this time",
            "  2. Yes, and don't ask again",
            "  3. No",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="PERMISSION")
        assert result is not None
        assert result.prompt_type == "permission"
        assert len(result.options) == 3
        assert result.selected_index == 0
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_permission_with_allow_deny tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_permission_with_numbered_options -v`
Expected: FAIL — permission 检测方法不存在或返回 None

- [ ] **Step 3: 实现 _detect_permission_prompt() 方法**

在 `OutputParser` 类中 `_detect_ask_user` 之后添加：

```python
@staticmethod
def _detect_permission_prompt(lines: list) -> Optional["UserPromptInfo"]:
    """Detect permission prompt: Allow/Deny or Yes/No after 'Allow ...?' text."""
    # Find the permission question line
    perm_question = ""
    perm_line_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _PERMISSION_RE.search(stripped) and not _STATUS_BAR_RE.search(stripped):
            # Extract the actual question text (before ❯ selector)
            perm_question = stripped
            perm_line_idx = i
            break

    if perm_line_idx < 0:
        return None

    # Don't interfere with ask_user — if there are numbered options 1-N, it's ask_user
    # Permission prompts have Allow/Deny or Yes/No (2-3 options max)
    remaining = lines[perm_line_idx + 1:]
    options = []
    selected_index = -1

    for line in remaining:
        stripped = line.strip()
        if not stripped:
            continue

        # Check for ❯ selector with option text
        sel_match = _SELECTED_OPTION_RE.match(stripped)
        if sel_match:
            idx = int(sel_match.group(1)) - 1
            selected_index = idx
            options.append(sel_match.group(2).strip())
            continue

        unsel_match = _UNSELECTED_OPTION_RE.match(stripped)
        if unsel_match:
            options.append(unsel_match.group(2).strip())
            continue

        # Check for ❯ Allow / ❯ Yes pattern (non-numbered)
        if stripped.startswith("❯"):
            label = stripped[1:].strip()
            selected_index = len(options)
            options.append(label)
            continue

        # Check for unselected Allow / Deny / Yes / No
        if stripped in ("Allow", "Deny", "Yes", "No"):
            options.append(stripped)
            continue

        # Stop at non-option lines
        break

    if not options:
        return None

    # Validate: must be permission-related options (Allow/Deny or Yes/No variants)
    perm_keywords = {"allow", "deny", "yes", "no"}
    options_lower = [o.lower().split()[0] for o in options if o]
    if not all(ok in perm_keywords for ok in options_lower):
        return None

    # Skip if more than 5 options (likely ask_user, not permission)
    if len(options) > 5:
        return None

    raw_context = "\n".join(lines[max(0, perm_line_idx - 1):perm_line_idx + len(options) + 2])

    return UserPromptInfo(
        prompt_type="permission",
        question=perm_question,
        options=options,
        selected_index=selected_index if selected_index >= 0 else 0,
        has_other=False,
        raw_context=raw_context,
    )
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_permission_with_allow_deny tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_permission_with_numbered_options -v`
Expected: 2 tests PASS

- [ ] **Step 5: 运行全量 parser 测试确认无回归**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py -v`
Expected: 所有测试 PASS

- [ ] **Step 6: Commit**

```bash
git add tools/claude_session/output_parser.py tests/tools/test_claude_session_parser.py
git commit -m "feat(claude-session): add permission prompt detection in OutputParser"
```

---

### Task 4: 实现 confirmation 和 free_text 场景检测

**Files:**
- Modify: `tools/claude_session/output_parser.py`
- Modify: `tests/tools/test_claude_session_parser.py`

- [ ] **Step 1: 编写 confirmation 和 free_text 检测的失败测试**

在 `TestDetectUserPrompt` 类中添加：

```python
    def test_confirmation_yes_no(self):
        """Detect confirmation prompt with Yes/No options."""
        lines = [
            "Do you want to proceed with the changes?",
            "",
            "❯ 1. Yes",
            "  2. No",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is not None
        assert result.prompt_type == "confirmation"
        assert "proceed" in result.question
        assert result.selected_index == 0

    def test_free_text_with_done_marker(self):
        """Detect free-text prompt after Claude finishes (✻ marker)."""
        lines = [
            "我的推荐是方案 A。",
            "",
            "你觉得哪个方案更合适？或者有其他想法？",
            "",
            "✻ Crunched for 6m 36s",
            "",
            "❯ ",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is not None
        assert result.prompt_type == "free_text"
        assert "哪个方案" in result.question or "想法" in result.question

    def test_free_text_without_done_marker(self):
        """Detect free-text prompt without ✻ marker (question mark present)."""
        lines = [
            "以上是三种实现方案。",
            "请告诉我你倾向哪个方向？",
            "",
            "❯ ",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is not None
        assert result.prompt_type == "free_text"

    def test_no_prompt_thinking_state(self):
        """THINKING state should not trigger prompt detection."""
        lines = [
            "你希望选择哪个？",
            "❯ 1. 选项 A",
            "  2. 选项 B",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="THINKING")
        assert result is None

    def test_no_prompt_normal_idle(self):
        """Normal IDLE without question should return None."""
        lines = [
            "Task completed successfully.",
            "✻ Crunched for 30s",
            "",
            "❯ ",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is None

    def test_no_prompt_empty_lines(self):
        """Empty lines should return None."""
        lines = []
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is None
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_confirmation_yes_no tests/tools/test_claude_session_parser.py::TestDetectUserPrompt::test_free_text_with_done_marker -v`
Expected: FAIL

- [ ] **Step 3: 实现 _detect_confirmation() 和 _detect_free_text() 方法**

在 `OutputParser` 类中 `_detect_permission_prompt` 之后添加：

```python
_CONFIRM_KEYWORDS = re.compile(
    r"(do you want|are you sure|proceed\?|confirm|continue\?)",
    re.IGNORECASE,
)

@staticmethod
def _detect_confirmation(lines: list) -> Optional["UserPromptInfo"]:
    """Detect Yes/No confirmation prompt."""
    # Look for confirmation question followed by Yes/No options
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not _QUESTION_END_RE.search(stripped):
            continue
        if not _CONFIRM_KEYWORDS.search(stripped):
            continue
        # Found a confirmation question, look for Yes/No options below
        remaining = lines[i + 1:]
        options = []
        selected_index = -1
        for rline in remaining:
            rs = rline.strip()
            if not rs:
                continue
            sel_match = _SELECTED_OPTION_RE.match(rs)
            if sel_match:
                idx = int(sel_match.group(1)) - 1
                selected_index = idx
                options.append(sel_match.group(2).strip())
                continue
            unsel_match = _UNSELECTED_OPTION_RE.match(rs)
            if unsel_match:
                options.append(unsel_match.group(2).strip())
                continue
            break

        # Must have exactly Yes/No or similar
        yes_no = {"yes", "no", "y", "n"}
        first_words = [o.lower().split()[0] for o in options if o]
        if not first_words or not all(w in yes_no for w in first_words):
            continue

        raw_context = "\n".join(lines[max(0, i - 1):i + len(options) + 2])
        return UserPromptInfo(
            prompt_type="confirmation",
            question=stripped,
            options=options,
            selected_index=selected_index if selected_index >= 0 else 0,
            has_other=False,
            raw_context=raw_context,
        )

    return None

@staticmethod
def _detect_free_text(lines: list) -> Optional["UserPromptInfo"]:
    """Detect free-text input prompt: question + empty ❯ waiting for input."""
    if not lines:
        return None

    # Must end with empty ❯ prompt
    last_lines = lines[-3:] if len(lines) >= 3 else lines
    has_empty_prompt = any(l.strip() == "❯" or l.strip() == "❯ " for l in last_lines)
    if not has_empty_prompt:
        return None

    # Look backwards for a question (line ending with ? or ？)
    question = ""
    question_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if stripped.startswith("❯"):
            continue
        if _STATUS_BAR_RE.search(stripped) or _DECORATION_RE.search(stripped):
            continue
        if _DONE_TIME_RE.search(stripped):
            continue
        if _TOOL_CALL_RE.match(stripped) or _TOOL_CALL_PAREN_RE.match(stripped):
            break
        if _QUESTION_END_RE.search(stripped):
            question = stripped
            question_idx = i
            break
        # If we hit non-question text within 5 lines of ❯, stop
        if len(lines) - 1 - i > 5:
            break

    if not question:
        return None

    # Conservative: require ✻ done marker OR question within 5 lines of ❯
    has_done_marker = any(_DONE_TIME_RE.search(l) for l in lines)
    distance_to_prompt = len(lines) - 1 - question_idx
    if not has_done_marker and distance_to_prompt > 5:
        return None

    # Raw context: lines around the question
    ctx_start = max(0, question_idx - 2)
    ctx_end = min(len(lines), question_idx + 5)
    raw_context = "\n".join(lines[ctx_start:ctx_end])

    return UserPromptInfo(
        prompt_type="free_text",
        question=question,
        options=[],
        selected_index=-1,
        has_other=False,
        raw_context=raw_context,
    )
```

- [ ] **Step 4: 运行所有 detect_user_prompt 测试**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py::TestDetectUserPrompt -v`
Expected: 所有 10 个测试 PASS

- [ ] **Step 5: 运行全量 parser 测试确认无回归**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py -v`
Expected: 所有测试 PASS（原有测试 + 新增 10 个）

- [ ] **Step 6: Commit**

```bash
git add tools/claude_session/output_parser.py tests/tools/test_claude_session_parser.py
git commit -m "feat(claude-session): add confirmation and free_text scene detection"
```

---

### Task 5: 边界情况和回归测试

**Files:**
- Modify: `tests/tools/test_claude_session_parser.py`

- [ ] **Step 1: 编写边界情况测试**

在 `TestDetectUserPrompt` 类中添加：

```python
    def test_ask_user_skipped_for_permission_state_ask_user(self):
        """ask_user detected even in PERMISSION state if it has numbered options."""
        lines = [
            "选择权限模式",
            "",
            "❯ 1. Normal",
            "  2. Bypass",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="PERMISSION")
        assert result is not None
        assert result.prompt_type == "ask_user"

    def test_permission_takes_priority_over_confirmation(self):
        """When both permission and confirmation match, ask_user checked first."""
        lines = [
            "Allow Bash?",
            "",
            "❯ 1. Yes",
            "  2. No",
            "",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="PERMISSION")
        assert result is not None
        # This is numbered options, so ask_user should catch it first
        assert result.prompt_type == "ask_user"

    def test_raw_context_includes_surrounding_lines(self):
        """raw_context should include lines around the prompt area."""
        lines = [
            "previous output line",
            "How do you want to proceed?",
            "",
            "❯ 1. Option A",
            "  2. Option B",
            "",
            "status bar text",
        ]
        result = OutputParser.detect_user_prompt(lines, current_state="IDLE")
        assert result is not None
        assert "proceed" in result.raw_context
        assert "Option A" in result.raw_context
```

- [ ] **Step 2: 运行所有测试确认通过**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_parser.py -v`
Expected: 所有测试 PASS

- [ ] **Step 3: Commit**

```bash
git add tests/tools/test_claude_session_parser.py
git commit -m "test(claude-session): add edge case tests for scene detection"
```
