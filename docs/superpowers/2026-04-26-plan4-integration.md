# Plan 4: 集成修改 — AdaptivePoller + ClaudeSessionManager

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Plan 1-3 的模块集成到现有架构中：修改 AdaptivePoller 增加场景检测和回调签名变更，修改 ClaudeSessionManager 初始化和管理 AutoResponder 生命周期。

**Architecture:** 在 AdaptivePoller._poll_once() 中增加 detect_user_prompt() 调用，通过向后兼容的回调签名传递给 ClaudeSessionManager。ClaudeSessionManager 在 start() 中创建 AutoResponder，在 send() 中重置 turn 计数器，在 stop() 中清理。

**Tech Stack:** Python 3.10+, unittest.mock

**Depends on:** Plan 1（OutputParser）、Plan 2（DecisionEngine）、Plan 3（AutoResponder）

---

### Task 1: 修改 AdaptivePoller — 场景检测 + 回调签名扩展

**Files:**
- Modify: `tools/claude_session/adaptive_poller.py`
- Modify: `tests/tools/test_claude_session_poller.py`

- [ ] **Step 1: 编写 AdaptivePoller 场景检测的失败测试**

在 `tests/tools/test_claude_session_poller.py` 末尾添加：

```python
class TestAdaptivePollerPromptDetection:
    """Tests for prompt detection in AdaptivePoller._poll_once()."""

    def test_callback_receives_prompt_info_on_idle(self):
        """When state is IDLE and ask_user prompt is detected, callback gets prompt_info."""
        sm = StateMachine()
        buf = OutputBuffer(max_lines=100)
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = (
            "选择方案\n\n❯ 1. 方案 A\n  2. 方案 B\n  3. 方案 C\n"
        )

        received = []
        def callback(transition, prompt_info=None):
            received.append((transition, prompt_info))

        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=callback,
        )
        poller._poll_once()

        assert len(received) >= 1
        _, prompt_info = received[0]
        assert prompt_info is not None
        assert prompt_info.prompt_type == "ask_user"

    def test_callback_receives_none_when_no_prompt(self):
        """When IDLE but no user prompt detected, prompt_info is None."""
        sm = StateMachine()
        buf = OutputBuffer(max_lines=100)
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = "some output\n❯ "

        received = []
        def callback(transition, prompt_info=None):
            received.append((transition, prompt_info))

        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=callback,
        )
        poller._poll_once()

        assert len(received) >= 1
        _, prompt_info = received[0]
        assert prompt_info is None

    def test_backward_compat_callback_without_prompt_info(self):
        """Old-style callback accepting only transition still works."""
        sm = StateMachine()
        buf = OutputBuffer(max_lines=100)
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        tmux_mock.capture_pane.return_value = "❯ "

        transitions = []
        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=lambda t: transitions.append(t),
        )
        poller._poll_once()

        # Should not raise, and transition should be captured
        assert len(transitions) >= 1
        assert transitions[0].to_state == "IDLE"

    def test_no_prompt_detection_in_thinking_state(self):
        """Prompt detection skipped when state is THINKING."""
        sm = StateMachine()
        buf = OutputBuffer(max_lines=100)
        tmux_mock = MagicMock()
        tmux_mock.session_exists.return_value = True
        # Output with question but in THINKING state (no ❯ prompt)
        tmux_mock.capture_pane.return_value = "thinking...\n"

        received = []
        def callback(transition, prompt_info=None):
            received.append((transition, prompt_info))

        poller = AdaptivePoller(
            state_machine=sm, output_buffer=buf, tmux=tmux_mock,
            on_state_change=callback,
        )
        poller._poll_once()

        # State should be THINKING, no prompt detection attempted
        assert sm.current_state == "THINKING"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_poller.py::TestAdaptivePollerPromptDetection -v`
Expected: FAIL — callback signature mismatch或 prompt_info 为 None

- [ ] **Step 3: 修改 AdaptivePoller._poll_once() 增加场景检测**

修改 `tools/claude_session/adaptive_poller.py`：

1. 在文件顶部 import 中添加：
```python
import time
from typing import Optional
```

2. 在 `__init__` 中修改 `on_state_change` 的类型提示：
```python
on_state_change: Optional[Callable] = None,
```

3. 替换整个 `_poll_once` 方法为：

```python
def _poll_once(self) -> None:
    """Perform a single poll cycle."""
    if not self._tmux.session_exists():
        transition = self._sm.transition(ClaudeState.DISCONNECTED)
        if transition and self._on_state_change:
            self._fire_callback(transition, None)
        return

    raw = self._tmux.capture_pane()
    lines = OutputParser.clean_lines(raw)

    # Update buffer with new lines
    if lines:
        self._buf.append_batch(lines)

    # Detect and update state
    result = OutputParser.detect_state(lines)
    transition = self._sm.transition(result.state)

    # Scene detection: for IDLE and PERMISSION states
    # Must happen BOTH on transition AND on repeated same-state polls,
    # because a prompt may appear while already in IDLE (no state change)
    prompt_info = None
    if result.state in (ClaudeState.IDLE, ClaudeState.PERMISSION):
        prompt_info = OutputParser.detect_user_prompt(lines, current_state=result.state)

    if transition and self._on_state_change:
        transition.tool_name = result.tool_name
        transition.tool_target = result.tool_target
        self._fire_callback(transition, prompt_info)
    elif prompt_info and self._on_state_change:
        # No state transition, but prompt detected — fire a synthetic callback
        # so AutoResponder can still respond
        synthetic = StateTransition(
            from_state=result.state,
            to_state=result.state,
            timestamp=time.monotonic(),
        )
        self._fire_callback(synthetic, prompt_info)

def _fire_callback(self, transition, prompt_info) -> None:
    """Fire state change callback with backward compatibility."""
    import inspect
    if not self._on_state_change:
        return
    try:
        sig = inspect.signature(self._on_state_change)
        params = list(sig.parameters.values())
        if len(params) >= 2:
            self._on_state_change(transition, prompt_info)
        else:
            self._on_state_change(transition)
    except TypeError:
        # Fallback: try old-style call
        try:
            self._on_state_change(transition)
        except Exception:
            pass
```

- [ ] **Step 4: 运行所有 poller 测试**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_poller.py -v`
Expected: 所有测试 PASS（原有 6 个 + 新增 4 个）

- [ ] **Step 5: 运行全量 claude_session 测试确认无回归**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_*.py -v`
Expected: 所有测试 PASS

- [ ] **Step 6: Commit**

```bash
git add tools/claude_session/adaptive_poller.py tests/tools/test_claude_session_poller.py
git commit -m "feat(claude-session): add prompt detection to AdaptivePoller with backward-compatible callback"
```

---

### Task 2: 修改 ClaudeSessionManager — 集成 AutoResponder 生命周期

**Files:**
- Modify: `tools/claude_session/manager.py`
- Modify: `tests/tools/test_claude_session_manager.py`

- [ ] **Step 1: 编写集成测试**

在 `tests/tools/test_claude_session_manager.py` 末尾添加：

```python
class TestAutoResponderIntegration:
    """Tests for AutoResponder integration in ClaudeSessionManager."""

    def test_auto_responder_disabled_by_default(self):
        """AutoResponder is None when not enabled."""
        mgr = ClaudeSessionManager()
        assert mgr._auto_responder is None

    def test_auto_responder_created_on_start(self):
        """AutoResponder is created when start() is called with auto_responder=True."""
        mgr = ClaudeSessionManager()
        with patch.object(TmuxInterface, 'session_exists', return_value=False), \
             patch.object(TmuxInterface, 'create_session'), \
             patch.object(TmuxInterface, 'capture_pane', return_value='❯ '), \
             patch('tools.claude_session.manager.subprocess.run'):
            mgr.start(
                workdir="/tmp/test",
                session_name="test-auto-responder",
                auto_responder=True,
            )
        assert mgr._auto_responder is not None

    def test_auto_responder_not_created_without_flag(self):
        """AutoResponder is not created when auto_responder is not set."""
        mgr = ClaudeSessionManager()
        with patch.object(TmuxInterface, 'session_exists', return_value=False), \
             patch.object(TmuxInterface, 'create_session'), \
             patch.object(TmuxInterface, 'capture_pane', return_value='❯ '), \
             patch('tools.claude_session.manager.subprocess.run'):
            mgr.start(
                workdir="/tmp/test",
                session_name="test-no-auto",
            )
        assert mgr._auto_responder is None

    def test_send_resets_auto_responder_turn(self):
        """send() calls auto_responder.reset_turn() when responder exists."""
        mgr = ClaudeSessionManager()
        mgr._session_active = True
        mgr._tmux = MagicMock()
        mgr._poller = MagicMock()
        mgr._auto_responder = MagicMock()

        mgr.send("test message")

        mgr._auto_responder.reset_turn.assert_called_once()

    def test_stop_clears_auto_responder(self):
        """stop() clears auto_responder reference."""
        mgr = ClaudeSessionManager()
        mgr._session_active = True
        mgr._tmux = MagicMock()
        mgr._auto_responder = MagicMock()

        mgr.stop()

        assert mgr._auto_responder is None

    def test_state_change_callback_routes_to_auto_responder(self):
        """_handle_state_change routes prompt_info to auto_responder.handle_prompt()."""
        mgr = ClaudeSessionManager()
        mgr._auto_responder = MagicMock()
        mgr._conversation_context = {"current_message": "test", "conversation_history": []}

        # Simulate state change with prompt_info via _handle_state_change
        prompt_info = MagicMock()
        prompt_info.prompt_type = "ask_user"
        transition = StateTransition(
            from_state="THINKING",
            to_state="IDLE",
            timestamp=time.monotonic(),
        )

        mgr._handle_state_change(transition, prompt_info)

        mgr._auto_responder.handle_prompt.assert_called_once_with(
            prompt_info,
            mgr._conversation_context,
        )

    def test_state_change_callback_ignores_none_prompt(self):
        """_handle_state_change does not call auto_responder when prompt_info is None."""
        mgr = ClaudeSessionManager()
        mgr._auto_responder = MagicMock()

        transition = StateTransition(
            from_state="THINKING",
            to_state="IDLE",
            timestamp=time.monotonic(),
        )

        mgr._handle_state_change(transition, None)

        mgr._auto_responder.handle_prompt.assert_not_called()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_manager.py::TestAutoResponderIntegration -v`
Expected: FAIL — `TypeError: start() got an unexpected keyword argument 'auto_responder'`

- [ ] **Step 3: 修改 ClaudeSessionManager**

修改 `tools/claude_session/manager.py`：

1. 在文件顶部 import 中添加：
```python
from tools.claude_session.auto_responder import AutoResponder, AutoResponderConfig
```

2. 在 `__init__` 方法中添加（在 `self._wait_state = None` 之后）：
```python
self._auto_responder: Optional[AutoResponder] = None
self._conversation_context: dict = {}
```

3. 在 `start()` 方法签名中添加参数：
```python
def start(
    self,
    workdir: str,
    session_name: str = "hermes-default",
    model: Optional[str] = None,
    permission_mode: str = "normal",
    on_event: str = "notify",
    completion_queue: Optional[queue.Queue] = None,
    resume_uuid: Optional[str] = None,
    auto_responder: bool = False,       # 新增
    auto_responder_config: Optional[dict] = None,  # 新增
) -> dict:
```

4. 在 `start()` 方法中，poller 创建之后（找到 `self._poller = AdaptivePoller(...)` 之后的位置），添加 AutoResponder 初始化：
```python
# Auto-Responder setup
if auto_responder:
    from tools.claude_session.decision_engine import DecisionEngine
    self._auto_responder = AutoResponder(
        decision_engine=DecisionEngine(),
        tmux=self._tmux,
        state_machine=self._sm,
        config=AutoResponderConfig(**(auto_responder_config or {})),
    )
```

5. **关键：不要替换 `_handle_state_change`**。现有的 `_handle_state_change(self, transition)` 方法（manager.py:724）负责 turn tracking、event firing、auto-approve permissions，共 40+ 行逻辑。新增的 AutoResponder 路由必须在这之上叠加，而不是替换。

   在 `_handle_state_change` 方法的**末尾**（`self._auto_approve_permission()` 调用之后）添加 AutoResponder 路由：

```python
# AutoResponder routing (added for auto-decision feature)
if prompt_info and self._auto_responder:
    self._auto_responder.handle_prompt(prompt_info, self._conversation_context)
```

   同时修改 `_handle_state_change` 的签名以接收可选的 `prompt_info` 参数：

```python
def _handle_state_change(self, transition: StateTransition, prompt_info=None) -> None:
```

   poller 创建代码**保持不变**，仍然指向 `self._handle_state_change`：
```python
# 不修改这行 —— poller 回调仍然指向 _handle_state_change
self._poller = AdaptivePoller(
    state_machine=self._sm,
    output_buffer=self._buf,
    tmux=self._tmux,
    on_state_change=self._handle_state_change,
)
```

   AdaptivePoller 的 `_fire_callback` 方法会自动处理 `_handle_state_change` 的新签名（它接受 2 个参数）。

7. 在 `send()` 方法中，`self._wait_state = None` 之后添加：
```python
# Reset auto-responder turn counter for new user message
if self._auto_responder:
    self._auto_responder.reset_turn()
# Update conversation context for decision-making
self._conversation_context["current_message"] = message
```

8. 在 `stop()` 方法中，`self._session_active = False` 之前添加：
```python
self._auto_responder = None
```

9. 在 `status()` 方法中，`result` dict 构建之后添加：
```python
if self._auto_responder:
    result["auto_responder"] = {
        "enabled": True,
        "response_count": self._auto_responder._response_count,
        "response_log_count": len(self._auto_responder.response_log),
    }
```

- [ ] **Step 4: 运行集成测试**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_manager.py::TestAutoResponderIntegration -v`
Expected: 7 tests PASS

- [ ] **Step 5: 运行全量 manager 测试确认无回归**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_manager.py -v`
Expected: 所有测试 PASS

- [ ] **Step 6: Commit**

```bash
git add tools/claude_session/manager.py tests/tools/test_claude_session_manager.py
git commit -m "feat(claude-session): integrate AutoResponder into ClaudeSessionManager lifecycle"
```

---

### Task 3: 全量回归测试

**Files:** 无修改

- [ ] **Step 1: 运行所有 claude_session 相关测试**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/test_claude_session_*.py tests/tools/test_decision_engine.py tests/tools/test_auto_responder.py -v`
Expected: 所有测试 PASS

- [ ] **Step 2: 运行全量测试套件（如果时间允许）**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/tools/ -v --timeout=60`
Expected: 无回归（重点关注 test_claude_session_*.py 相关测试）

- [ ] **Step 3: 最终 commit（如有修复）**

```bash
git add -A
git commit -m "fix: regression fixes from auto-decision integration"
```
