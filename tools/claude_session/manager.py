"""tools/claude_session/manager.py — Main ClaudeSessionManager class."""

import logging
import os
import queue
import re
import shlex
import subprocess
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
from tools.claude_session.auto_responder import AutoResponder, AutoResponderConfig

logger = logging.getLogger(__name__)

# Claude Code bracketed-paste needs enough time to finish accepting multi-line
# input before Enter. User explicitly requires a 10-second delay.
PASTE_SUBMIT_DELAY_SECONDS = 10.0


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

    # Shared regex for detecting real permission prompts (excludes status bar noise)
    _PERMISSION_PROMPT_RE = re.compile(
        r"(Allow\?|Yes.*No|permission to|wants to|proceed\?|"
        r"❯\s*\d+\.\s*(Yes|Allow))",
        re.IGNORECASE,
    )

    def __init__(self):
        self._session_id: Optional[str] = None
        self._tmux: Optional[TmuxInterface] = None
        self._sm = StateMachine()
        self._buf = OutputBuffer(max_lines=1000)
        self._poller: Optional[AdaptivePoller] = None
        self._session_active = False
        self._permission_mode = "normal"
        self._claude_session_uuid: Optional[str] = None  # Claude Code 内部的 session UUID

        # 会话启动时间戳（Claude Code 进程开始运行的时刻），用于首个 Turn 的 start_time
        self._session_start_time: Optional[float] = None

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
        self._initializing = False

        # wait_for_idle 自适应轮询追踪状态（跨调用持久化，send 时重置）
        self._wait_state: Optional[dict] = None

        # AutoResponder
        self._auto_responder: Optional[AutoResponder] = None
        self._conversation_context: dict = {}

        # Gateway session isolation (set by claude_session_tool.py)
        self._gateway_session_key: str = ""

        # Status callback for gateway status bridge
        self._status_callback = None  # Optional[Callable[[dict], None]]

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        workdir: str,
        session_name: str = "hermes-default",
        model: Optional[str] = None,
        permission_mode: str = "normal",
        on_event: str = "notify",
        completion_queue: Optional[queue.Queue] = None,
        resume_uuid: Optional[str] = None,
        auto_responder: bool = False,
        auto_responder_config: Optional[dict] = None,
    ) -> dict:
        """Start a Claude Code session in tmux.

        Args:
            session_name: tmux session name. Tool layer uses _derive_session_name()
                          to generate deterministic names from workdir.
            resume_uuid: 如果提供，用 --resume 恢复指定的 Claude Code 会话。
                         要求对应的 .jsonl 历史文件存在，否则降级为新建会话。
        """
        # ── Phase 0: 快速验证和状态初始化（持锁，无 I/O） ──
        with self._lock:
            if self._session_active or self._initializing:
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
            self._initializing = True

        # ── Phase 1: tmux I/O 操作（无锁，不阻塞其他线程） ──
        try:
            # ── Phase 1: 确保 tmux session 存在且属于当前 workdir ──
            needs_init = False  # 是否需要启动新的 Claude Code 进程

            if not self._tmux.session_exists():
                # 全新创建
                self._tmux.create_session(workdir=workdir)
                needs_init = True
            else:
                # tmux session 已存在 — 验证是否属于当前 workdir
                # 防止 gateway 重启后 _sessions 丢失，旧 tmux session 被错误复用
                pane = self._tmux.capture_pane(lines=50)
                pane_lower = pane.lower()

                # 检查 tmux session 的当前工作目录
                cwd_check = subprocess.run(
                    ["tmux", "display-message", "-t", session_name,
                     "-p", "#{pane_current_path}"],
                    capture_output=True, text=True, timeout=5,
                )
                tmux_cwd = cwd_check.stdout.strip() if cwd_check.returncode == 0 else ""

                # workdir 不匹配，或者有其他 Claude Code 在运行 → 杀掉重建
                needs_rebuild = False
                reason = ""
                if tmux_cwd and workdir not in tmux_cwd and tmux_cwd not in workdir:
                    needs_rebuild = True
                    reason = f"workdir mismatch (tmux={tmux_cwd}, expected={workdir})"
                elif "claude code" in pane_lower or "claude-" in pane_lower:
                    # 有 Claude Code 在运行，可能是别的项目的
                    if workdir.lower() not in pane_lower:
                        needs_rebuild = True
                        reason = "existing Claude Code belongs to different project"

                if needs_rebuild:
                    logger.warning(
                        "tmux session %s exists but %s. Killing and recreating.",
                        session_name, reason,
                    )
                    self._tmux.kill_session()
                    time.sleep(0.5)
                    self._tmux.create_session(workdir=workdir)
                    needs_init = True
                else:
                    # tmux session 存在且 workdir 匹配，检查 Claude Code
                    # 状态是否可以安全复用。
                    #
                    # 旧逻辑仅检查 "❯" 是否出现在 pane 中，但 Claude Code
                    # TUI 在 THINKING/TOOL_CALL 状态时也会在底部渲染
                    # phantom "❯"，导致误判为 Claude Code 空闲可复用。
                    # 复用一个卡在 THINKING 的 session 会导致 poller
                    # 从 DISCONNECTED 开始，状态解析无法匹配，wait_for_idle
                    # 一直等待直到超时。
                    #
                    # 修复：使用 OutputParser.detect_state() 精确检测真实状态，
                    # 仅 IDLE 状态才安全复用。其他状态（THINKING、TOOL_CALL、
                    # PERMISSION 等）一律 kill + 重建。
                    pane_lines = OutputParser.clean_lines(pane)
                    parse_result = OutputParser.detect_state(pane_lines)
                    detected_state = parse_result.state

                    if detected_state == "IDLE":
                        # 真正的 IDLE — 安全复用
                        logger.info(
                            "tmux session %s exists, Claude Code in IDLE state. Reusing.",
                            session_name,
                        )
                    elif detected_state == "EXITED":
                        # Claude Code 已退出（检测到 shell 提示符）
                        logger.warning(
                            "tmux session %s exists but Claude Code has exited (shell prompt). "
                            "Killing and recreating.",
                            session_name,
                        )
                        self._tmux.kill_session()
                        time.sleep(0.5)
                        self._tmux.create_session(workdir=workdir)
                        needs_init = True
                    else:
                        # 非 IDLE（THINKING/TOOL_CALL/PERMISSION/DISCONNECTED/ERROR）
                        # 或者没有 Claude Code 进程（空 pane → 默认 THINKING）
                        # → 一律 kill + 重建，避免复用卡住的 session
                        nonidle_reason = (
                            f"non-IDLE state: {detected_state}"
                            if detected_state != "THINKING"
                            or pane_lines
                            else "no Claude Code running"
                        )
                        logger.warning(
                            "tmux session %s exists but %s. Killing and recreating.",
                            session_name, nonidle_reason,
                        )
                        self._tmux.kill_session()
                        time.sleep(0.5)
                        self._tmux.create_session(workdir=workdir)
                        needs_init = True

            # ── Phase 2: 如果需要，启动 Claude Code 进程 ──
            if needs_init:
                # If running as root, switch to non-root user first
                # Claude Code refuses --permission-mode bypassPermissions when run as root
                current_uid = os.getuid()
                if current_uid == 0:
                    non_root_user = os.environ.get("SUDO_USER") or "longxiang"
                    self._tmux.send_keys(f"su - {shlex.quote(non_root_user)}", enter=True)
                    time.sleep(1.5)
                    # Set workdir and PATH for the non-root user
                    self._tmux.send_keys(f"cd {shlex.quote(workdir)}", enter=True)
                    time.sleep(0.5)
                    user_bin = f"/home/{non_root_user}/bin"
                    self._tmux.send_keys(f"export PATH={shlex.quote(user_bin)}:$PATH", enter=True)
                    time.sleep(0.5)

                # 构建 Claude Code 启动命令
                # C1 fix: resume 时不传 --session-id，避免覆盖原会话身份
                claude_cmd = "claude"
                if actually_resuming:
                    claude_cmd += f" --resume {shlex.quote(resume_uuid)}"
                else:
                    claude_cmd += f" --session-id {shlex.quote(self._claude_session_uuid)}"
                if permission_mode == "skip":
                    claude_cmd += " --permission-mode bypassPermissions"
                if model:
                    claude_cmd += f" --model {shlex.quote(model)}"
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

                # ── 启动健康检查 ──
                # Claude Code 启动后等待其进入可用状态（IDLE 或 THINKING）。
                # 如果超时未检测到有效状态，说明启动失败（CLI 崩溃、依赖缺失等）。
                STARTUP_HEALTH_TIMEOUT = 30  # 秒
                startup_ok = self._wait_for_claude_startup(
                    timeout=STARTUP_HEALTH_TIMEOUT,
                )
                if not startup_ok:
                    logger.error(
                        "Claude Code failed to start within %ds in session %s. "
                        "Killing tmux session.",
                        STARTUP_HEALTH_TIMEOUT, session_name,
                    )
                    try:
                        self._tmux.kill_session()
                    except Exception:
                        pass
                    with self._lock:
                        self._initializing = False
                    return {
                        "error": (
                            f"Claude Code did not start within {STARTUP_HEALTH_TIMEOUT}s. "
                            "Possible causes: CLI not installed, API key missing, "
                            "or tmux resource exhaustion. Check tmux session manually."
                        ),
                    }

        except Exception as e:
            with self._lock:
                self._initializing = False
            if self._tmux:
                try:
                    self._tmux.kill_session()
                except Exception:
                    pass
            return {"error": f"Failed to start session: {e}"}

        # ── Phase 2: 最终状态更新（持锁） ──
        with self._lock:
            self._initializing = False

            # Start polling
            self._poller = AdaptivePoller(
                state_machine=self._sm,
                output_buffer=self._buf,
                tmux=self._tmux,
                on_state_change=self._handle_state_change,
            )
            self._poller.start()
            self._session_active = True
            self._session_start_time = time.monotonic()

            # AutoResponder
            if auto_responder:
                from tools.claude_session.decision_engine import DecisionEngine
                self._auto_responder = AutoResponder(
                    decision_engine=DecisionEngine(),
                    tmux=self._tmux, state_machine=self._sm,
                    config=AutoResponderConfig(**(auto_responder_config or {})),
                )

            # Store completion_queue for event injection
            if completion_queue:
                self._on_event = completion_queue

        # Run one synchronous poll before returning so start() reports the real
        # Claude Code state instead of the initial DISCONNECTED placeholder.
        # This must happen outside self._lock because poll callbacks re-enter
        # manager methods that acquire the same lock.
        try:
            if self._poller:
                self._poller.poll_now()
        except Exception as e:
            logger.debug("Initial post-start poll failed: %s", e)

        with self._lock:
            result = {
                "session_id": self._session_id,
                "tmux_session": session_name,
                "state": self._sm.current_state,
                "permission_mode": self._permission_mode,
                "claude_session_uuid": self._claude_session_uuid,
            }
            if actually_resuming:
                result["resumed_from"] = resume_uuid
            elif resume_uuid:
                result["fallback_note"] = f"resume_uuid={resume_uuid} history not found, started new session"
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
                except Exception as e:
                    logger.warning("Failed to kill tmux session: %s", e)

            sid = self._session_id
            uuid_to_return = self._claude_session_uuid
            self._auto_responder = None
            self._session_active = False
            self._session_id = None
            self._claude_session_uuid = None
            self._session_start_time = None
            self._sm.transition(ClaudeState.DISCONNECTED)

            return {
                "stopped": True,
                "session_id": sid,
                "claude_session_uuid": uuid_to_return,
            }

    # ------------------------------------------------------------------
    # Send operations
    # ------------------------------------------------------------------

    def send(self, message: str) -> dict:
        """Send a message atomically (type + Enter in one command).

        For multi-line text (containing \\n), splits into type + delayed submit
        to avoid holding _lock during the bracketed-paste delay. The delay
        runs outside the lock so the poller thread is not blocked.
        """
        is_multiline = "\n" in message

        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if not self._tmux:
                return {"error": "No tmux interface"}
            # Guard: reject send if Claude Code has exited or disconnected
            if self._sm.current_state in (ClaudeState.EXITED, ClaudeState.DISCONNECTED):
                return {"error": "Claude Code has exited. Restart the session to continue."}

            # Create a new Turn
            self._turn_counter += 1
            marker = self._buf.total_count()
            # 首个 Turn 使用会话启动时间，后续 Turn 使用当前时间
            if self._turn_counter == 1 and self._session_start_time is not None:
                turn_start = self._session_start_time
            else:
                turn_start = time.monotonic()
            turn = Turn(
                turn_id=self._turn_counter,
                start_time=turn_start,
                user_message=message,
                output_marker=marker,
            )
            self._current_turn = turn
            # 新 Turn 开始，重置等待进度追踪
            self._wait_state = None
            if self._auto_responder:
                self._auto_responder.reset_turn()
            self._conversation_context["current_message"] = message
            self._conversation_context["history"] = [
                t.to_dict() for t in self._turn_history[-5:]
            ]

            # 在锁内构建状态快照，锁外触发回调
            new_turn_status = None
            if self._status_callback:
                new_turn_status = self._build_status_info()

            # Send text (no delay inside lock — tmux send-keys is instant)
            if is_multiline:
                # Multi-line: type first, submit after delay (outside lock)
                self._tmux.send_keys(message, enter=False)
                self._sm.transition(ClaudeState.INPUTTING)
            else:
                # Single-line: type + Enter atomically
                self._tmux.send_keys(message, enter=True)

        # 在锁外触发状态回调，避免持锁期间做 I/O
        if new_turn_status is not None:
            try:
                self._status_callback(new_turn_status)
            except Exception as e:
                logger.debug("Status callback error on new turn: %s", e)

        # Multi-line: wait for bracketed paste to complete, then submit
        # This delay is OUTSIDE the lock so poller thread is not blocked.
        if is_multiline:
            logger.info("Multi-line send: waiting %.1fs for bracketed paste...", PASTE_SUBMIT_DELAY_SECONDS)
            time.sleep(PASTE_SUBMIT_DELAY_SECONDS)
            self._tmux.send_special_key("Enter")
            logger.info("Multi-line send: Enter sent after paste delay")

        # Poll outside lock to avoid deadlock
        if self._poller:
            self._poller.poll_now()

        return {"sent": True, "state": self._sm.current_state}

    def type_text(self, text: str) -> dict:
        """Type text without pressing Enter (for multi-line input)."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if self._sm.current_state in (ClaudeState.EXITED, ClaudeState.DISCONNECTED):
                return {"error": "Claude Code has exited. Restart the session to continue."}
            self._tmux.send_keys(text)
            self._sm.transition(ClaudeState.INPUTTING)
            return {"typed": True, "state": self._sm.current_state}

    def submit(self) -> dict:
        """Submit typed text by pressing Enter."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if self._sm.current_state in (ClaudeState.EXITED, ClaudeState.DISCONNECTED):
                return {"error": "Claude Code has exited. Restart the session to continue."}
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

    def send_text(self, text: str) -> dict:
        """Type text and submit atomically (type + Enter in one command).

        Convenience method combining type_text() + submit().
        For simple single-line input, use send() directly instead.
        Handles multi-line text the same way as send() — delay outside lock.
        """
        is_multiline = "\n" in text

        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if not self._tmux:
                return {"error": "No tmux interface"}
            if self._sm.current_state in (ClaudeState.EXITED, ClaudeState.DISCONNECTED):
                return {"error": "Claude Code has exited. Restart the session to continue."}

            # Send text (no delay inside lock)
            if is_multiline:
                self._tmux.send_keys(text, enter=False)
                self._sm.transition(ClaudeState.INPUTTING)
            else:
                self._tmux.send_keys(text, enter=True)

            # Create a new Turn
            self._turn_counter += 1
            marker = self._buf.total_count()
            if self._turn_counter == 1 and self._session_start_time is not None:
                turn_start = self._session_start_time
            else:
                turn_start = time.monotonic()
            turn = Turn(
                turn_id=self._turn_counter,
                start_time=turn_start,
                user_message=text,
                output_marker=marker,
            )
            self._current_turn = turn
            self._wait_state = None
            if self._auto_responder:
                self._auto_responder.reset_turn()
            self._conversation_context["current_message"] = text
            self._conversation_context["history"] = [
                t.to_dict() for t in self._turn_history[-5:]
            ]

        # Multi-line: wait for bracketed paste to complete, then submit
        if is_multiline:
            logger.info("send_text multi-line: waiting %.1fs for bracketed paste...", PASTE_SUBMIT_DELAY_SECONDS)
            time.sleep(PASTE_SUBMIT_DELAY_SECONDS)
            self._tmux.send_special_key("Enter")
            logger.info("send_text multi-line: Enter sent after paste delay")

        # Poll outside lock to avoid deadlock
        if self._poller:
            self._poller.poll_now()
        return {"sent": True, "state": self._sm.current_state}

    def cancel_input(self) -> dict:
        """Cancel current input (Ctrl+C)."""
        logger.warning(
            "cancel_input called for session %s (state=%s). "
            "Repeated cancellations indicate micromanagement. "
            "Consider: is Claude really stuck, or am I being impatient?",
            self._session_id,
            self._sm.current_state if hasattr(self._sm, 'current_state') else 'UNKNOWN'
        )
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if self._sm.current_state in (ClaudeState.EXITED, ClaudeState.DISCONNECTED):
                return {"error": "Claude Code has exited. Restart the session to continue."}
            self._tmux.send_special_key("C-c")
            # Don't force IDLE — let poller detect actual state from pane
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

        if self._auto_responder:
            result["auto_responder"] = {
                "enabled": True,
                "response_count": self._auto_responder._response_count,
                "response_log_count": len(self._auto_responder.response_log),
            }

        return result

    def wait_for_idle(self, timeout: int = 900) -> dict:
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
        PATROL_INTERVAL = int(os.environ.get("HERMES_CLAUDE_SESSION_PATROL_INTERVAL", "60"))
        MAX_STALLED_PATROLS = 3      # 连续 3 次巡检无增长 → stalled
        # 单次 wait_for_idle 是同步 tool call，不能长时间阻塞 Hermes agent loop。
        # 巡检点返回 progress，让外层有机会查看输出/处理权限/决定是否继续。
        ACTIVE_INTERVAL = 5.0        # 正常工作时的轮询间隔
        STALL_SLOW_INTERVAL = 10.0   # 停滞时放慢轮询间隔
        STALL_THRESHOLD = 30.0       # 30s 无 token 增长视为停滞
        COMPACT_MIN_WAIT = 300       # compact 最少等 5 分钟
        COMPACT_MAX_WAIT = 900       # compact 最多等 15 分钟

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
                "compact_detected": False,
                "compact_start_time": None,
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

            if state == ClaudeState.EXITED:
                self._wait_state = None
                return {
                    "error": "Claude Code has exited (shell prompt detected)",
                    "state": state,
                    "status": "exited",
                    "hint": "Claude Code process is no longer running. Restart the session to continue.",
                }

            now = time.monotonic()
            current_tokens = self._buf.total_count()

            # ── 检测 compact 操作（通过 output_parser 的 is_compacting 标志） ──
            pane_text = self._tmux.capture_pane()
            pane_lines = OutputParser.clean_lines(pane_text)
            parse_result = OutputParser.detect_state(pane_lines)
            if getattr(parse_result, 'is_compacting', False) and not self._wait_state["compact_detected"]:
                self._wait_state["compact_detected"] = True
                self._wait_state["compact_start_time"] = now
                logger.info("Compact operation detected, extending wait time (min=%ds, max=%ds)",
                            COMPACT_MIN_WAIT, COMPACT_MAX_WAIT)

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

                # 连续无进展 → stalled（compact 期间不判定 stalled）
                compact_active = (
                    self._wait_state["compact_detected"]
                    and self._wait_state["compact_start_time"] is not None
                    and (now - self._wait_state["compact_start_time"]) < COMPACT_MAX_WAIT
                )
                if self._wait_state["stalled_patrols"] >= MAX_STALLED_PATROLS and not compact_active:
                    result = {
                        "status": "stalled",
                        "state": state,
                        "progress_info": self._check_progress(),
                    }
                    self._wait_state = None
                    return result

                elapsed = now - self._wait_state["start_time"]
                logger.info(
                    "wait_for_idle patrol: elapsed=%.0fs, state=%s, token_delta=%d",
                    elapsed, state, patrol_delta,
                )
                return {
                    "status": "progress",
                    "state": state,
                    "elapsed_seconds": round(elapsed, 1),
                    "token_delta_since_patrol": patrol_delta,
                    "stalled_patrols": self._wait_state["stalled_patrols"],
                    "progress_info": self._check_progress(),
                    "hint": (
                        "Claude Code is still working. This progress result is intentional: "
                        "call wait_for_idle again to continue waiting, or inspect output/status first."
                    ),
                }

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
                # compact 期间延长 deadline，最多等 COMPACT_MAX_WAIT
                if (self._wait_state["compact_detected"]
                        and self._wait_state["compact_start_time"] is not None
                        and (time.monotonic() - self._wait_state["compact_start_time"]) < COMPACT_MAX_WAIT):
                    deadline = time.monotonic() + COMPACT_MIN_WAIT
                    logger.info("Compact in progress, extending deadline by %ds", COMPACT_MIN_WAIT)
                    continue
                break
            self._state_event.clear()
            # 不要一次睡过巡检点；否则长 timeout 会把 Hermes agent loop 同步阻塞到结束。
            self._state_event.wait(timeout=min(interval, PATROL_INTERVAL, remaining))

        # ── 超时 ──
        # 返回信息要冷静客观，不要传递焦虑感
        elapsed = time.monotonic() - self._wait_state["start_time"]
        result = {
            "status": "timeout",
            "state": self._sm.current_state,
            "timeout_reached": True,
            "elapsed_seconds": round(elapsed, 1),
            "hint": (
                "Timeout is normal for long tasks. Claude Code may still be working. "
                "Simply call wait_for_idle again with a larger timeout (e.g. 600-900). "
                "Do NOT cancel or restart the session."
            ),
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
        """Respond to a permission request.

        Supports two Claude Code permission UI formats:
        - Old: "Allow Bash command?" → send 'y'/'n' + Enter
        - New (numbered selector): "Do you want to proceed?" with "❯ 1. Yes"
          → Enter to accept default (Yes), or type deny option number + Enter

        Includes retry logic (up to 3 attempts) to handle race conditions where
        the state machine hasn't caught up with the actual tmux pane content.
        On each retry, re-reads the pane to detect permission prompts directly.
        """
        if response not in ("allow", "deny"):
            return {"error": f"Invalid response: {response}"}

        max_retries = 3
        retry_delay = 0.3
        last_error_detail = ""

        for attempt in range(max_retries):
            should_retry = False
            permission_responded = None
            with self._lock:
                is_perm = self._sm.current_state == ClaudeState.PERMISSION

                # If state machine says non-PERMISSION, verify from tmux pane
                # directly — the poller may not have updated yet.
                if not is_perm:
                    pane_perm = self._detect_permission_from_pane()
                    if pane_perm:
                        # Force state machine to PERMISSION
                        self._sm.transition(ClaudeState.PERMISSION)
                        is_perm = True
                        logger.info(
                            "respond_permission: forced state to PERMISSION "
                            "from pane detection (attempt %d/%d)",
                            attempt + 1, max_retries,
                        )

                if not is_perm:
                    last_error_detail = (
                        f"State={self._sm.current_state}, pane showed no permission prompt"
                    )
                    should_retry = attempt < max_retries - 1
                    # Lock released here before sleep
                else:
                    is_numbered = self._detect_numbered_selector()

                    if response == "allow":
                        if is_numbered:
                            self._tmux.send_special_key("Enter")
                        else:
                            self._tmux.send_keys("y", enter=True)
                    elif response == "deny":
                        if is_numbered:
                            deny_num = self._find_deny_option_number()
                            if deny_num:
                                self._tmux.send_keys(str(deny_num), enter=True)
                            else:
                                self._tmux.send_special_key("Enter")
                        else:
                            self._tmux.send_keys("n", enter=True)

                    # Record permission_responded — poll outside lock to avoid deadlock
                    # (poller callback _handle_state_change re-acquires self._lock)
                    permission_responded = {"responded": True, "state": self._sm.current_state}

            # Outside lock — poll if permission was responded, then return
            if permission_responded is not None:
                if self._poller:
                    self._poller.poll_now()
                return permission_responded

            # Outside lock — sleep before retry
            if should_retry:
                time.sleep(retry_delay)
                continue

        return {
            "error": "Not in PERMISSION state",
            "detail": last_error_detail,
            "hint": (
                "Permission prompt may have already been handled (skip mode) "
                "or disappeared. Check session status before retrying."
            ),
        }

    def _is_real_permission_in_pane(self, pane: Optional[str] = None) -> bool:
        """Check if pane content contains a real permission prompt.

        Filters out status bar noise and checks for genuine permission keywords.
        Can accept pre-captured pane text or capture it fresh.

        Returns:
            True if a genuine permission prompt is detected.
        """
        try:
            if pane is None:
                pane = self._tmux.capture_pane()
            from tools.claude_session.output_parser import OutputParser, _STATUS_BAR_RE
            lines = OutputParser.clean_lines(pane)
            last_lines = lines[-5:] if len(lines) >= 5 else lines
            non_status = [l for l in last_lines if not _STATUS_BAR_RE.search(l)]
            non_status_text = "\n".join(non_status)
            return bool(self._PERMISSION_PROMPT_RE.search(non_status_text))
        except Exception as e:
            logger.debug("_is_real_permission_in_pane failed: %s", e)
            return False

    def _detect_permission_from_pane(self) -> bool:
        """Check if tmux pane currently shows a real permission prompt.

        Bypasses the state machine and reads the pane directly.
        Returns True if a genuine permission prompt is detected.
        Must be called with self._lock held.
        """
        return self._is_real_permission_in_pane()

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

    def _handle_state_change(self, transition: StateTransition, prompt_info=None) -> None:
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

                elif transition.to_state == ClaudeState.EXITED:
                    self._current_turn.finalize()
                    self._turn_history.append(self._current_turn)
                    self._fire_event("session_exited", {
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

            # Build status info inside lock to capture accurate state
            # (especially for IDLE transitions where _current_turn gets set to None)
            status_info = None
            if self._status_callback:
                status_info = self._build_status_info()

            # Extract permission details when entering PERMISSION state
            permission_details = None
            if transition.to_state == ClaudeState.PERMISSION:
                permission_details = self._extract_permission_details()

        # Signal waiters outside lock
        self._state_event.set()

        # Auto-approve permissions when in skip mode
        if (
            transition.to_state == ClaudeState.PERMISSION
            and self._permission_mode == "skip"
        ):
            self._auto_approve_permission()

        # Auto-submit pasted text: when multi-line content is pasted,
        # Claude Code TUI shows "[Pasted text #N +M lines]" and waits for Enter.
        # send() already handles the delayed Enter for multi-line text, so this
        # only acts as a safety net for pastes that arrive through other paths
        # (e.g. manual paste in tmux). Uses a background timer to avoid blocking
        # the poller thread.
        if (
            transition.to_state == ClaudeState.INPUTTING
            and transition.from_state in (ClaudeState.IDLE, ClaudeState.INPUTTING)
        ):
            logger.info(
                "Pasted text detected in INPUTTING state, scheduling delayed Enter"
            )
            self._schedule_paste_submit()

        # AutoResponder routing
        if prompt_info and self._auto_responder:
            self._auto_responder.handle_prompt(prompt_info, self._conversation_context)

        # Fire status callback outside lock to avoid holding lock during I/O
        if status_info is not None:
            try:
                status_info["tool_name"] = getattr(transition, "tool_name", None)
                status_info["tool_target"] = getattr(transition, "tool_target", None)
                # Enrich with permission details for PERMISSION transitions
                if permission_details:
                    status_info["needs_permission"] = True
                    status_info["permission_text"] = permission_details["text"]
                    status_info["permission_type"] = permission_details["type"]
                else:
                    status_info["needs_permission"] = False
                self._status_callback(status_info)
            except Exception as e:
                logger.debug("Status callback error: %s", e)

    def _schedule_paste_submit(self) -> None:
        """Schedule a delayed Enter for pasted text in a background thread.

        This avoids blocking the poller thread. If the paste was already
        submitted by send()'s delayed Enter (outside lock), the extra Enter
        is harmless — Claude Code ignores Enter during THINKING state.
        """
        def _submit_after_delay():
            time.sleep(PASTE_SUBMIT_DELAY_SECONDS)
            if self._sm.current_state == ClaudeState.INPUTTING:
                logger.info("Paste safety-net: sending Enter for stuck pasted text")
                self._tmux.send_special_key("Enter")
            else:
                logger.debug(
                    "Paste safety-net: skipped Enter (state=%s, paste already submitted)",
                    self._sm.current_state,
                )

        t = threading.Thread(target=_submit_after_delay, name="paste-submit", daemon=True)
        t.start()

    def _auto_approve_permission(self) -> None:
        """Auto-approve a permission request when in skip mode.

        First checks if this is a real permission prompt (not the bottom
        status bar "bypass permissions on" line). Detects old vs numbered
        selector UI format and sends the appropriate approval keystroke.
        Runs in the poller background thread.
        """
        max_retries = 3
        for attempt in range(max_retries):
            # Small delay to let the permission prompt fully render
            time.sleep(0.3)

            if self._sm.current_state != ClaudeState.PERMISSION:
                # Already transitioned away, nothing to do
                return

            # Verify this is a REAL permission prompt, not the status bar
            try:
                pane = self._tmux.capture_pane()
                if not self._is_real_permission_in_pane(pane):
                    logger.info("Skipping auto-approve: no real permission prompt found (status bar only)")
                    # Force state back to THINKING — the poller will correct it next cycle
                    self._sm.transition(ClaudeState.THINKING)
                    return
            except Exception as e:
                logger.warning("Auto-approve pane check failed: %s", e)

            # Detect UI format and send appropriate approval keystroke
            is_numbered = self._detect_numbered_selector(pane_text=pane)
            logger.info(
                "Auto-approving permission (skip mode, attempt %d/%d, format=%s)",
                attempt + 1, max_retries, "numbered" if is_numbered else "classic",
            )
            if is_numbered:
                self._tmux.send_special_key("Enter")
            else:
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

    def _build_status_info(self) -> dict:
        """Build status info dict for the status callback."""
        now = time.monotonic()
        tool_calls_dicts = []
        turn_id = None
        elapsed_seconds = 0

        if self._current_turn:
            tool_calls_dicts = [tc.to_dict() for tc in self._current_turn.tool_calls]
            turn_id = self._current_turn.turn_id
            elapsed_seconds = now - self._current_turn.start_time
        elif self._turn_history:
            # 会话完成后，从历史记录获取最后一个 turn 的信息
            last_turn = self._turn_history[-1]
            turn_id = last_turn.turn_id
            elapsed_seconds = last_turn.total_duration
            tool_calls_dicts = [tc.to_dict() for tc in last_turn.tool_calls]

        return {
            "state": self._sm.current_state,
            "tool_name": None,
            "tool_target": None,
            "turn_id": turn_id,
            "elapsed_seconds": elapsed_seconds,
            "tool_calls": tool_calls_dicts,
            "recent_output": self._buf.last_n_chars(200),
        }

    def _extract_permission_details(self) -> Optional[dict]:
        """Extract permission prompt text and classify type from output buffer.

        Scans recent output lines for permission-related text, filters out
        status bar noise, and classifies the permission type.

        If the output buffer has no recent data (poller hasn't updated yet),
        falls back to reading the tmux pane directly.

        Must be called with self._lock held.

        Returns:
            dict with 'text' and 'type' keys, or None if no permission found.
        """
        from tools.claude_session.output_parser import _STATUS_BAR_RE

        all_lines = self._buf.read()

        # Fallback: if buffer is empty, read directly from tmux pane
        if not all_lines:
            try:
                pane = self._tmux.capture_pane()
                raw_lines = OutputParser.clean_lines(pane)
                # Wrap raw strings into simple objects with .text attribute
                all_lines = [type('_', (), {'text': l})() for l in raw_lines]
            except Exception as e:
                logger.debug("_extract_permission_details pane fallback failed: %s", e)
                return None

        last_lines = all_lines[-15:] if len(all_lines) > 15 else all_lines

        # Filter out status bar lines
        non_status = [l for l in last_lines if not _STATUS_BAR_RE.search(l.text)]

        for line in reversed(non_status):
            lower = line.text.lower()
            if ("allow" in lower or "permission" in lower or "proceed?" in lower
                    or re.search(r"❯\s*\d+\.\s*(Yes|Allow)", line.text)):
                return {
                    "text": line.text,
                    "type": self._classify_permission_type(line.text),
                }
        return None

    @staticmethod
    def _classify_permission_type(text: str) -> str:
        """Classify a permission prompt into a category.

        Uses phrase-level matching to avoid false positives from short keywords
        (e.g., "view" matching "review", "exec" matching "execute the plan").

        Categories:
            - "bash": shell command execution
            - "file_write": file creation or modification
            - "file_read": file read access
            - "mcp": MCP tool access
            - "network": HTTP/network requests
            - "unknown": unrecognized permission type
        """
        lower = text.lower()
        # Bash: match specific phrases, not bare "exec" or "command"
        if any(kw in lower for kw in ("bash", "shell command", "run command", "execute command")):
            return "bash"
        if any(kw in lower for kw in ("write", "edit", "create", "delete", "remove")):
            return "file_write"
        # File read: use phrases that avoid matching "review" or "interview"
        if any(kw in lower for kw in ("read file", "cat ", "view file", "open file", "fetch ")):
            return "file_read"
        if any(kw in lower for kw in ("mcp", "tool:", "server")):
            return "mcp"
        # Network: HTTP calls, web fetch, API requests
        if any(kw in lower for kw in ("fetch(", "http", "web request", "url", "curl", "wget")):
            return "network"
        return "unknown"

    def _build_idle_result(self) -> dict:
        """Build result for wait_for_idle when state is IDLE."""
        result = {"state": ClaudeState.IDLE}
        # Check for active turn first (Turn may still be in progress)
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        elif self._turn_history:
            # Fallback to last completed turn
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
            lower = line.text.lower()
            if "allow" in lower or "permission" in lower or "proceed?" in lower:
                result["permission_request"] = line.text
                break
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        return result

    def _detect_numbered_selector(self, pane_text: Optional[str] = None) -> bool:
        """Check if current permission UI uses numbered selector format.

        Numbered selector: "❯ 1. Yes" / "2. ..." / "3. No"
        Old format: "Allow ... ?" / "❯ Allow" / "❯ Deny"

        Args:
            pane_text: Pre-captured pane content to avoid redundant capture.
                       If None, captures from tmux.
        """
        try:
            if pane_text is None:
                pane_text = self._tmux.capture_pane()
            lines = OutputParser.clean_lines(pane_text)
            last_lines = lines[-8:] if len(lines) >= 8 else lines
            for line in last_lines:
                if re.match(r"\s*❯\s*\d+\.", line):
                    return True
        except Exception:
            pass
        return False

    def _find_deny_option_number(self) -> Optional[int]:
        """Find the option number for deny/no in a numbered selector UI.

        Scans the pane for lines like "3. No" or "2. Deny" and returns
        the corresponding number. Returns None if not found.
        """
        try:
            pane = self._tmux.capture_pane()
            lines = OutputParser.clean_lines(pane)
            last_lines = lines[-8:] if len(lines) >= 8 else lines
            for line in last_lines:
                m = re.match(r"\s*(?:❯\s*)?(\d+)\.\s*(No|Deny)\b", line, re.IGNORECASE)
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        return None

    def _build_error_result(self) -> dict:
        """Build result for wait_for_idle when state is ERROR."""
        result = {"state": ClaudeState.ERROR}
        tail = self._buf.last_n_chars(500)
        result["error_output"] = tail
        if self._current_turn:
            result["turn"] = self._current_turn.to_dict()
        return result

    def _wait_for_claude_startup(self, timeout: int = 30) -> bool:
        """等待 Claude Code 进程启动并进入可用状态。

        启动后通过 OutputParser 检测 pane 输出，判断 Claude Code 是否
        成功启动。仅需要检测到 IDLE 或 THINKING 即视为成功。

        在 THINKING 状态时，需要额外验证 pane 中确实有 Claude Code
        相关的输出（而非空 pane 默认返回 THINKING）。

        启动场景检测：在状态检测之前，检查是否有需要自动处理的启动
        交互（如工作区信任确认）。检测到后自动确认，继续等待正常启动。

        Args:
            timeout: 最大等待秒数。

        Returns:
            True 表示启动成功，False 表示超时。
        """
        deadline = time.monotonic() + timeout
        EMPTY_THRESHOLD = 3  # 少于此行数视为空 pane
        startup_scene_attempts = 0
        MAX_STARTUP_SCENE_ATTEMPTS = 3

        while time.monotonic() < deadline:
            try:
                pane = self._tmux.capture_pane(lines=100)
                lines = OutputParser.clean_lines(pane)

                if not lines or len(lines) < EMPTY_THRESHOLD:
                    # 空 pane — Claude Code 还没开始输出
                    time.sleep(1.0)
                    continue

                # 启动场景检测（优先于状态检测）
                # 某些启动交互（如工作区信任确认）会阻止 Claude Code
                # 正常启动，需要自动确认后才能进入 IDLE/THINKING 状态。
                startup_scene = OutputParser.detect_startup_scene(lines)
                if startup_scene and startup_scene_attempts < MAX_STARTUP_SCENE_ATTEMPTS:
                    startup_scene_attempts += 1
                    logger.info(
                        "Startup scene detected: %s (action: %s, attempt %d/%d)",
                        startup_scene.description,
                        startup_scene.action,
                        startup_scene_attempts,
                        MAX_STARTUP_SCENE_ATTEMPTS,
                    )
                    if startup_scene.action == "press_enter":
                        self._tmux.send_special_key("Enter")
                    elif startup_scene.action == "press_down_enter":
                        self._tmux.send_special_key("Down")
                        time.sleep(0.3)
                        self._tmux.send_special_key("Enter")
                    time.sleep(2.0)
                    continue

                result = OutputParser.detect_state(lines)

                if result.state == "IDLE":
                    logger.info(
                        "Claude Code startup OK: IDLE detected after %.1fs",
                        timeout - (deadline - time.monotonic()),
                    )
                    return True

                if result.state in ("THINKING", "TOOL_CALL", "PERMISSION"):
                    # THINKING 可能是空 pane 的默认值，验证是否有 Claude Code 特征
                    pane_lower = pane.lower()
                    has_claude_signature = (
                        "claude" in pane_lower
                        or "model" in pane_lower
                        or "thinking" in pane_lower
                        or "permission" in pane_lower
                        or "●" in pane
                        or "❯" in pane
                    )
                    if has_claude_signature:
                        logger.info(
                            "Claude Code startup OK: %s detected after %.1fs",
                            result.state,
                            timeout - (deadline - time.monotonic()),
                        )
                        return True

                if result.state == "ERROR":
                    logger.error(
                        "Claude Code startup failed: ERROR detected in pane. "
                        "Output: %s",
                        pane[-200:],
                    )
                    return False

                if result.state == "EXITED":
                    logger.error(
                        "Claude Code exited immediately after launch (shell prompt detected)."
                    )
                    return False

            except Exception as e:
                logger.debug("Startup health check poll error: %s", e)

            time.sleep(1.0)

        logger.warning(
            "Claude Code startup health check timed out after %ds", timeout,
        )
        return False

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
        # C2 fix: 校验 session_uuid 为合法 UUID 格式，防止路径遍历
        if not re.match(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            session_uuid,
        ):
            return None

        # I4 fix: 规范化 workdir 路径（abspath + 去除末尾斜杠）
        workdir = os.path.abspath(workdir).rstrip("/")

        claude_dir = os.path.expanduser("~/.claude/projects")
        # Claude Code 用 / 替换为 - 来构造项目目录名（保留前导 -）
        dir_name = workdir.replace("/", "-")
        jsonl_path = os.path.join(claude_dir, dir_name, f"{session_uuid}.jsonl")
        if os.path.exists(jsonl_path):
            return jsonl_path
        return None


# === wait_for_idle v2 改造完成 ===
