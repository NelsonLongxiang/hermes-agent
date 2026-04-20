"""tools/claude_session/manager.py — Main ClaudeSessionManager class."""

import logging
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from tools.claude_session.state_machine import ClaudeState, StateMachine, StateTransition
from tools.claude_session.output_buffer import OutputBuffer
from tools.claude_session.output_parser import OutputParser
from tools.claude_session.tmux_interface import TmuxInterface
from tools.claude_session.adaptive_poller import AdaptivePoller

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """A single tool call within a Turn."""
    tool_name: str
    target: Optional[str]
    start_time: float
    end_time: Optional[float] = None
    duration: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool_name,
            "target": self.target,
            "duration": self.duration,
        }


@dataclass
class Turn:
    """Tracks a full Hermes→Claude interaction cycle."""
    turn_id: int
    start_time: float
    end_time: Optional[float] = None
    state: str = ClaudeState.THINKING
    user_message: str = ""
    tool_calls: list = field(default_factory=list)
    thinking_cycles: int = 0
    total_duration: Optional[float] = None
    output_marker: int = 0  # OutputBuffer marker at send time

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "user_message": self.user_message,
            "total_duration": self.total_duration,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "thinking_cycles": self.thinking_cycles,
            "elapsed_seconds": (
                (self.end_time or time.monotonic()) - self.start_time
            ),
        }

    def finalize(self) -> None:
        """Mark turn as completed."""
        self.end_time = time.monotonic()
        self.total_duration = self.end_time - self.start_time
        for tc in self.tool_calls:
            if tc.end_time is None:
                tc.end_time = self.end_time
                tc.duration = tc.end_time - tc.start_time


class ClaudeSessionManager:
    """Manager for a single Claude Code tmux session.

    所有状态均为实例变量，天然支持多实例并行。
    Provides the full API: start, send, status, wait_for_idle, output, etc.
    """

    def __init__(self):
        self._session_id: Optional[str] = None
        self._tmux: Optional[TmuxInterface] = None
        self._sm = StateMachine()
        self._buf = OutputBuffer(max_lines=1000)
        self._poller: Optional[AdaptivePoller] = None
        self._session_active = False
        self._permission_mode = "normal"
        self._claude_session_uuid: Optional[str] = None  # Claude Code 内部的 session UUID

        # Turn tracking
        self._turn_counter = 0
        self._current_turn: Optional[Turn] = None
        self._turn_history: list = []

        # Event system
        self._event_queue: queue.Queue = queue.Queue()
        self._event_mode = "notify"  # "notify" | "queue" | "none"
        self._on_event = None

        # Threading
        self._lock = threading.Lock()
        self._state_event = threading.Event()

        # wait_for_idle 自适应轮询追踪状态（跨调用持久化，send 时重置）
        self._wait_state: Optional[dict] = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        workdir: str,
        session_name: str = "claude-work",
        model: Optional[str] = None,
        permission_mode: str = "normal",
        on_event: str = "notify",
        completion_queue: Optional[queue.Queue] = None,
        resume_uuid: Optional[str] = None,
    ) -> dict:
        """Start a Claude Code session in tmux.

        Args:
            resume_uuid: 如果提供，用 --resume 恢复指定的 Claude Code 会话。
                         要求对应的 .jsonl 历史文件存在，否则降级为新建会话。
        """
        with self._lock:
            if self._session_active and self._tmux and self._tmux.session_exists():
                return {
                    "session_id": self._session_id,
                    "tmux_session": session_name,
                    "state": self._sm.current_state,
                    "permission_mode": self._permission_mode,
                    "claude_session_uuid": self._claude_session_uuid,
                    "note": "Session already active",
                }

            if permission_mode not in ("normal", "skip"):
                return {"error": f"Invalid permission_mode: {permission_mode}"}

            # 生成新的 Claude Code session UUID
            self._claude_session_uuid = str(uuid.uuid4())

            # 检查 resume_uuid 的历史文件是否存在
            actually_resuming = False
            if resume_uuid:
                jsonl_path = self._find_session_jsonl(workdir, resume_uuid)
                if jsonl_path:
                    actually_resuming = True
                else:
                    logger.warning(
                        "resume_uuid=%s 的历史文件不存在，降级为新建会话",
                        resume_uuid,
                    )

            # resume 时重置 Hermes 层状态（与 Claude Code 的 JSONL 历史无关）
            if actually_resuming:
                self._sm.transition(ClaudeState.DISCONNECTED)
                self._buf.clear()
                self._turn_counter = 0
                self._current_turn = None
                self._turn_history = []
                self._wait_state = None

            self._session_id = f"cs_{uuid.uuid4().hex[:8]}"
            self._permission_mode = permission_mode
            self._event_mode = on_event

            self._tmux = TmuxInterface(session_name)

            try:
                if not self._tmux.session_exists():
                    self._tmux.create_session(workdir=workdir)

                    # If running as root, switch to non-root user first
                    # Claude Code refuses --permission-mode bypassPermissions when run as root
                    current_uid = os.getuid()
                    if current_uid == 0:
                        non_root_user = os.environ.get("SUDO_USER") or "longxiang"
                        self._tmux.send_keys(f"su - {non_root_user}", enter=True)
                        time.sleep(1.5)
                        # Set workdir and PATH for the non-root user
                        self._tmux.send_keys(f"cd {workdir}", enter=True)
                        time.sleep(0.5)
                        user_bin = f"/home/{non_root_user}/bin"
                        self._tmux.send_keys(f"export PATH={user_bin}:$PATH", enter=True)
                        time.sleep(0.5)

                    # 构建 Claude Code 启动命令
                    claude_cmd = "claude"
                    if actually_resuming:
                        claude_cmd += f" --resume {resume_uuid}"
                    claude_cmd += f" --session-id {self._claude_session_uuid}"
                    if permission_mode == "skip":
                        claude_cmd += " --permission-mode bypassPermissions"
                    if model:
                        claude_cmd += f" --model {model}"
                    self._tmux.send_keys(claude_cmd, enter=True)
                    # Wait briefly for Claude Code to start
                    time.sleep(2)

                    # Handle skip permission confirmation if any
                    if permission_mode == "skip":
                        time.sleep(1)
                        # --permission-mode bypassPermissions may show a confirmation
                        # dialog; try to dismiss it with Down+Enter
                        pane = self._tmux.capture_pane()
                        if "permission" in pane.lower() or "bypass" in pane.lower() or "yes" in pane.lower():
                            self._tmux.send_special_key("Down")
                            time.sleep(0.3)
                            self._tmux.send_special_key("Enter")
                            time.sleep(1)

            except Exception as e:
                return {"error": f"Failed to start session: {e}"}

            # Start polling
            self._poller = AdaptivePoller(
                state_machine=self._sm,
                output_buffer=self._buf,
                tmux=self._tmux,
                on_state_change=self._handle_state_change,
            )
            self._poller.start()
            self._session_active = True

            # Store completion_queue for event injection
            if completion_queue:
                self._on_event = completion_queue

            result = {
                "session_id": self._session_id,
                "tmux_session": session_name,
                "state": self._sm.current_state,
                "permission_mode": self._permission_mode,
                "claude_session_uuid": self._claude_session_uuid,
            }
            if actually_resuming:
                result["resumed_from"] = resume_uuid
            return result

    def stop(self) -> dict:
        """Stop the session and clean up."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}

            # Stop polling first
            if self._poller:
                self._poller.stop()

            # Finalize current turn
            if self._current_turn and self._current_turn.end_time is None:
                self._current_turn.finalize()
                self._turn_history.append(self._current_turn)
                self._current_turn = None

            # Kill tmux session
            if self._tmux:
                try:
                    self._tmux.kill_session()
                except Exception:
                    pass

            sid = self._session_id
            self._session_active = False
            self._session_id = None
            self._sm.transition(ClaudeState.DISCONNECTED)

            return {
                "stopped": True,
                "session_id": sid,
                "claude_session_uuid": self._claude_session_uuid,
            }

    # ------------------------------------------------------------------
    # Send operations
    # ------------------------------------------------------------------

    def send(self, message: str) -> dict:
        """Send a message atomically (type + Enter in one command)."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if not self._tmux:
                return {"error": "No tmux interface"}

            # Create a new Turn
            self._turn_counter += 1
            marker = self._buf.total_count()
            turn = Turn(
                turn_id=self._turn_counter,
                start_time=time.monotonic(),
                user_message=message,
                output_marker=marker,
            )
            self._current_turn = turn
            # 新 Turn 开始，重置等待进度追踪
            self._wait_state = None

            # Atomic send
            self._tmux.send_keys(message, enter=True)

        # Poll outside lock to avoid deadlock
        if self._poller:
            self._poller.poll_now()

        return {"sent": True, "state": self._sm.current_state}

    def type_text(self, text: str) -> dict:
        """Type text without pressing Enter (for multi-line input)."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            self._tmux.send_keys(text)
            self._sm.transition(ClaudeState.INPUTTING)
            return {"typed": True, "state": self._sm.current_state}

    def submit(self) -> dict:
        """Submit typed text by pressing Enter."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            self._tmux.send_special_key("Enter")

            # Create Turn if not exists
            if self._current_turn is None:
                self._turn_counter += 1
                self._current_turn = Turn(
                    turn_id=self._turn_counter,
                    start_time=time.monotonic(),
                    user_message="(typed input)",
                    output_marker=self._buf.total_count(),
                )

        # Poll outside lock to avoid deadlock
        if self._poller:
            self._poller.poll_now()
        return {"submitted": True, "state": self._sm.current_state}

    def cancel_input(self) -> dict:
        """Cancel current input (Ctrl+C)."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            self._tmux.send_special_key("C-c")
            self._sm.transition(ClaudeState.IDLE)
            return {"cancelled": True, "state": self._sm.current_state}

    # ------------------------------------------------------------------
    # Status and wait
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return current session state."""
        if not self._session_active:
            return {"state": ClaudeState.DISCONNECTED}

        result = {
            "state": self._sm.current_state,
            "state_duration_seconds": round(self._sm.state_duration(), 1),
            "output_tail": self._buf.last_n_chars(200),
        }

        if self._current_turn:
            result["current_turn"] = self._current_turn.to_dict()

        return result

    def wait_for_idle(self, timeout: int = 300) -> dict:
        """自适应轮询等待 Claude 回到 IDLE 状态。

        相比固定轮询的改进：
        1. 自适应间隔：token 持续增长保持 5s，停滞时放慢到 10s
        2. 巡检保底：每 600s 返回进度摘要，外层决定继续或终止
        3. 连续无进展检测：3 次巡检（30分钟）token 无增长 → stalled

        Returns:
            dict 含 status 字段：
                "idle"        — 任务完成，Claude 已回到 IDLE
                "permission"  — 需要权限审批，处理后可再次调用
                "error"       — 发生错误
                "disconnected"— 会话断开
                "progress"    — 巡检返回，外层可决定继续等或终止
                "stalled"     — 连续无进展，建议外层 cancel
                "timeout"     — 超时
        """
        if not self._session_active:
            return {"error": "No active session", "status": "disconnected"}

        # ── 常量配置 ──
        PATROL_INTERVAL = 600        # 巡检间隔：600s（10 分钟）
        MAX_STALLED_PATROLS = 3      # 连续 3 次巡检无增长 → stalled
        ACTIVE_INTERVAL = 5.0        # 正常工作时的轮询间隔
        STALL_SLOW_INTERVAL = 10.0   # 停滞时放慢轮询间隔
        STALL_THRESHOLD = 30.0       # 30s 无 token 增长视为停滞

        # ── 初始化或恢复进度追踪（跨调用持久化） ──
        now = time.monotonic()
        current_tokens = self._buf.total_count()

        if self._wait_state is None:
            # 首次进入或 send 后重置
            self._wait_state = {
                "start_time": now,
                "start_tokens": current_tokens,
                "last_patrol_time": now,
                "last_patrol_tokens": current_tokens,
                "last_growth_time": now,
                "last_check_tokens": current_tokens,
                "stalled_patrols": 0,
            }

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            state = self._sm.current_state

            # ── 终态：立即返回 ──
            if state == ClaudeState.IDLE:
                self._wait_state = None
                return {**self._build_idle_result(), "status": "idle"}

            if state == ClaudeState.PERMISSION:
                # 保持 _wait_state，外层处理权限后会再次调用 wait_for_idle
                return {**self._build_permission_result(), "status": "permission"}

            if state == ClaudeState.ERROR:
                self._wait_state = None
                return {**self._build_error_result(), "status": "error"}

            if state == ClaudeState.DISCONNECTED:
                self._wait_state = None
                return {
                    "error": "Session disconnected",
                    "state": state,
                    "status": "disconnected",
                }

            now = time.monotonic()
            current_tokens = self._buf.total_count()

            # ── 更新 token 增长追踪 ──
            if current_tokens > self._wait_state["last_check_tokens"]:
                self._wait_state["last_growth_time"] = now
            self._wait_state["last_check_tokens"] = current_tokens

            # ── 巡检检测：每 PATROL_INTERVAL 秒做一次 ──
            time_since_patrol = now - self._wait_state["last_patrol_time"]
            if time_since_patrol >= PATROL_INTERVAL:
                patrol_delta = current_tokens - self._wait_state["last_patrol_tokens"]

                if patrol_delta == 0:
                    self._wait_state["stalled_patrols"] += 1
                else:
                    self._wait_state["stalled_patrols"] = 0

                # 更新巡检基准时间戳和 token 数
                self._wait_state["last_patrol_time"] = now
                self._wait_state["last_patrol_tokens"] = current_tokens

                # 连续无进展 → stalled
                if self._wait_state["stalled_patrols"] >= MAX_STALLED_PATROLS:
                    result = {
                        "status": "stalled",
                        "state": state,
                        "progress_info": self._check_progress(),
                    }
                    self._wait_state = None
                    return result

                # 正常巡检 → 仅打日志，不中断等待（避免外层模型误判为失败）
                logger.info(
                    "wait_for_idle patrol: elapsed=%.0fs, state=%s, token_delta=%d",
                    now - self._wait_state["start_time"], state, patrol_delta,
                )

            # ── 自适应轮询间隔 ──
            stall_duration = now - self._wait_state["last_growth_time"]
            if stall_duration > STALL_THRESHOLD:
                # token 停止增长超过 30s → 放慢轮询
                interval = STALL_SLOW_INTERVAL
            else:
                # token 持续增长或刚停止 → 保持快速轮询
                interval = ACTIVE_INTERVAL

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._state_event.clear()
            self._state_event.wait(timeout=min(interval, remaining))

        # ── 超时 ──
        result = {
            "status": "timeout",
            "state": self._sm.current_state,
            "timeout_reached": True,
            "progress_info": self._check_progress(),
        }
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        result["output_since_send"] = self._get_output_since_send()
        self._wait_state = None
        return result

    def wait_for_state(self, target_state: str, timeout: int = 60) -> dict:
        """Wait for a specific state."""
        if not self._session_active:
            return {"error": "No active session"}

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sm.current_state == target_state:
                return {
                    "state": target_state,
                    "duration_seconds": round(self._sm.state_duration(), 1),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._state_event.clear()
            self._state_event.wait(timeout=min(0.3, remaining))

        return {"state": self._sm.current_state, "timeout_reached": True}

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def output(self, offset: int = 0, limit: int = 50) -> dict:
        """Get output lines with pagination."""
        lines = self._buf.read(offset=offset, limit=limit)
        return {
            "lines": [{"text": l.text, "index": l.index} for l in lines],
            "total": self._buf.total_count(),
            "has_more": (offset + limit) < self._buf.total_count(),
        }

    # ------------------------------------------------------------------
    # Permission handling
    # ------------------------------------------------------------------

    def respond_permission(self, response: str) -> dict:
        """Respond to a permission request."""
        if self._sm.current_state != ClaudeState.PERMISSION:
            return {"error": "Not in PERMISSION state"}

        if response == "allow":
            self._tmux.send_keys("y", enter=True)
        elif response == "deny":
            self._tmux.send_keys("n", enter=True)
        else:
            return {"error": f"Invalid response: {response}"}

        if self._poller:
            self._poller.poll_now()
        return {"responded": True, "state": self._sm.current_state}

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def history(self) -> dict:
        """Return turn history from OutputBuffer."""
        turns = []
        all_turns = list(self._turn_history)
        if self._current_turn and self._current_turn.end_time is not None:
            all_turns.append(self._current_turn)

        for t in all_turns:
            turns.append({
                "role": "user",
                "message": t.user_message,
                "turn_id": t.turn_id,
                "tools": [tc.to_dict() for tc in t.tool_calls],
                "duration": t.total_duration,
            })

        all_tools = sum(len(t.tool_calls) for t in all_turns)
        return {
            "turns": turns,
            "total_turns": len(turns),
            "total_tools_called": all_tools,
        }

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def events(self, since_turn: int = 0) -> dict:
        """Get queued events since a given turn."""
        collected = []
        while not self._event_queue.empty():
            try:
                evt = self._event_queue.get_nowait()
                if evt.get("turn_id", 0) >= since_turn:
                    collected.append(evt)
            except queue.Empty:
                break
        return {"events": collected}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_state_change(self, transition: StateTransition) -> None:
        """Called by poller on state changes. Updates turns and fires events.

        Runs in the poller background thread. All shared state mutations
        are protected by self._lock.

        When permission_mode is 'skip' and state transitions to PERMISSION,
        automatically approves the permission request without requiring
        external respond_permission calls.
        """
        now = time.monotonic()

        with self._lock:
            # Update current turn
            if self._current_turn:
                if transition.to_state == ClaudeState.TOOL_CALL:
                    tc = ToolCall(
                        tool_name=getattr(transition, "tool_name", "Unknown") or "Unknown",
                        target=getattr(transition, "tool_target", None),
                        start_time=now,
                    )
                    self._current_turn.tool_calls.append(tc)
                elif transition.to_state == ClaudeState.THINKING:
                    self._current_turn.thinking_cycles += 1
                elif transition.to_state == ClaudeState.IDLE:
                    self._current_turn.finalize()
                    self._turn_history.append(self._current_turn)
                    self._fire_event("turn_completed", {
                        "turn_id": self._current_turn.turn_id,
                        "duration": self._current_turn.total_duration,
                        "tool_calls": [tc.to_dict() for tc in self._current_turn.tool_calls],
                    })
                    self._current_turn = None

            # Fire state_changed event
            self._fire_event("state_changed", {
                "from_state": transition.from_state,
                "to_state": transition.to_state,
            })

        # Signal waiters outside lock
        self._state_event.set()

        # Auto-approve permissions when in skip mode
        if (
            transition.to_state == ClaudeState.PERMISSION
            and self._permission_mode == "skip"
        ):
            self._auto_approve_permission()

    def _auto_approve_permission(self) -> None:
        """Auto-approve a permission request when in skip mode.

        First checks if this is a real permission prompt (not the bottom
        status bar "bypass permissions on" line). For real prompts, sends
        'y' + Enter. Runs in the poller background thread.
        """
        max_retries = 3
        for attempt in range(max_retries):
            # Small delay to let the permission prompt fully render
            time.sleep(0.3)

            if self._sm.current_state != ClaudeState.PERMISSION:
                # Already transitioned away, nothing to do
                return

            # Verify this is a REAL permission prompt, not the status bar
            # by capturing and checking the pane content directly
            try:
                pane = self._tmux.capture_pane()
                # If the only "permission" text is the status bar, skip
                from tools.claude_session.output_parser import OutputParser, _STATUS_BAR_RE
                lines = OutputParser.clean_lines(pane)
                last_lines = lines[-5:] if len(lines) >= 5 else lines
                non_status = [l for l in last_lines if not _STATUS_BAR_RE.search(l)]
                non_status_text = "\n".join(non_status)

                # Check for real permission keywords (Allow?, Yes/No, etc.)
                # but NOT status bar "bypass permissions on"
                has_real_prompt = bool(
                    re.search(r"(Allow\?|Yes.*No|permission to|wants to)", non_status_text, re.IGNORECASE)
                )

                if not has_real_prompt:
                    logger.info("Skipping auto-approve: no real permission prompt found (status bar only)")
                    # Force state back to THINKING — the poller will correct it next cycle
                    self._sm.transition(ClaudeState.THINKING)
                    return
            except Exception as e:
                logger.warning("Auto-approve pane check failed: %s", e)

            # Send 'y' + Enter to approve the real permission
            logger.info(
                "Auto-approving permission (skip mode, attempt %d/%d)",
                attempt + 1, max_retries,
            )
            self._tmux.send_keys("y", enter=True)

            # Wait for state to change (up to 2 seconds)
            for _ in range(10):
                time.sleep(0.2)
                if self._sm.current_state != ClaudeState.PERMISSION:
                    logger.info("Permission auto-approved successfully")
                    return

        logger.warning(
            "Auto-approve: still in PERMISSION after %d attempts", max_retries,
        )

    def _fire_event(self, event_type: str, data: dict) -> None:
        """Push event to queue and optionally to completion_queue."""
        if self._event_mode == "none":
            return

        evt = {"session_id": self._session_id, "type": event_type, "timestamp": time.time(), **data}
        self._event_queue.put(evt)

        if self._event_mode == "notify" and self._on_event:
            try:
                self._on_event.put(evt)
            except Exception:
                pass

    def _build_idle_result(self) -> dict:
        """Build result for wait_for_idle when state is IDLE."""
        result = {"state": ClaudeState.IDLE}
        # Check turn_history for the latest completed turn
        if self._turn_history:
            last_turn = self._turn_history[-1]
            result["turn"] = last_turn.to_dict()
        result["output_since_send"] = self._get_output_since_send()
        return result

    def _build_permission_result(self) -> dict:
        """Build result for wait_for_idle when state is PERMISSION."""
        result = {"state": ClaudeState.PERMISSION}
        # Single read call, then slice for last 10 lines
        all_lines = self._buf.read()
        last_lines = all_lines[-10:] if len(all_lines) > 10 else all_lines
        for line in reversed(last_lines):
            if "allow" in line.text.lower() or "permission" in line.text.lower():
                result["permission_request"] = line.text
                break
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        return result

    def _build_error_result(self) -> dict:
        """Build result for wait_for_idle when state is ERROR."""
        result = {"state": ClaudeState.ERROR}
        tail = self._buf.last_n_chars(500)
        result["error_output"] = tail
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        return result

    def _check_progress(self) -> dict:
        """检测当前 Claude 工作进度。

        基于 OutputBuffer 行数（作为 token 代理指标）判断工作是否在推进。
        依赖 self._wait_state 中的追踪数据，由 wait_for_idle 维护。

        Returns:
            dict: 进度信息，包含：
                - elapsed_seconds: 自首次等待以来的总时长
                - token_count: 当前输出总行数
                - token_delta: 自首次等待以来的行数增量
                - token_delta_since_patrol: 自上次巡检以来的行数增量
                - stalled_patrols: 连续无进展的巡检次数
                - current_state: 当前 Claude 状态
                - state_duration_seconds: 当前状态已持续时间
        """
        ws = self._wait_state
        if ws is None:
            return {}

        now = time.monotonic()
        current_tokens = self._buf.total_count()

        return {
            "elapsed_seconds": round(now - ws["start_time"], 1),
            "token_count": current_tokens,
            "token_delta": current_tokens - ws["start_tokens"],
            "token_delta_since_patrol": current_tokens - ws["last_patrol_tokens"],
            "stalled_patrols": ws["stalled_patrols"],
            "current_state": self._sm.current_state,
            "state_duration_seconds": round(self._sm.state_duration(), 1),
        }

    def _get_output_since_send(self) -> str:
        """Get all output since last send operation."""
        if self._current_turn:
            lines = self._buf.since(self._current_turn.output_marker)
        elif self._turn_history:
            last = self._turn_history[-1]
            lines = self._buf.since(last.output_marker)
        else:
            lines = self._buf.read()
        return "\n".join(l.text for l in lines)

    @staticmethod
    def _find_session_jsonl(workdir: str, session_uuid: str) -> Optional[str]:
        """查找 Claude Code 会话历史文件。

        在 ~/.claude/projects/<cwd-path>/<uuid>.jsonl 中查找对应的历史文件。
        workdir 中的路径分隔符会被替换为 - 来构造目录名。

        Returns:
            .jsonl 文件的完整路径，如果不存在则返回 None。
        """
        claude_dir = os.path.expanduser("~/.claude/projects")
        # Claude Code 用 / 替换为 - 来构造项目目录名（保留前导 -）
        dir_name = workdir.replace("/", "-")
        jsonl_path = os.path.join(claude_dir, dir_name, f"{session_uuid}.jsonl")
        if os.path.exists(jsonl_path):
            return jsonl_path
        return None


# === wait_for_idle v2 改造完成 ===
