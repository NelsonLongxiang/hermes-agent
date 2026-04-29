"""session.py — Simplified Claude Code session as a context pipeline.

Replaces the former manager.py. Key simplifications:
- No state machine object — state detected fresh each poll
- No turn/tool-call tracking — just "output since last send"
- No auto-responder — that's a separate concern
- wait_for_idle uses inline polling

Core flow:
    TaskContext → format prompt → write to tmux → wait for idle → return result
"""

import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from typing import Optional

from tools.claude_session.idle import (
    SessionState, clean_lines, detect_state, detect_startup_scene,
    detect_activity, is_permission_in_text,
    _STATUS_BAR_RE, _PERMISSION_RE, _DONE_TIME_RE,
)
from tools.claude_session.output_buffer import OutputBuffer
from tools.claude_session.tmux_interface import TmuxInterface
from tools.claude_session.task_context import TaskContext
from tools.claude_session.observer import SessionObserver
from tools.claude_session.status_card import StatusCard

logger = logging.getLogger(__name__)

PASTE_SUBMIT_DELAY_SECONDS = 10.0


class _StateView:
    """Lightweight shim for backward compatibility with mgr._sm.current_state."""

    __slots__ = ("_session",)

    def __init__(self, session: "ClaudeSession"):
        self._session = session

    @property
    def current_state(self) -> str:
        return self._session._state

    def state_duration(self) -> float:
        return time.monotonic() - self._session._state_entered


class ClaudeSession:
    """Context pipeline: TaskContext → Claude Code → Result.

    All instance state, naturally supports parallel sessions.
    """

    _PERMISSION_PROMPT_RE = re.compile(
        r"(Allow\?|Yes.*No|permission to|wants to|proceed\?|"
        r"❯\s*\d+\.\s*(Yes|Allow))",
        re.IGNORECASE,
    )

    def __init__(self):
        self._session_id: Optional[str] = None
        self._tmux: Optional[TmuxInterface] = None
        self._buf = OutputBuffer(max_lines=1000)
        self._session_active = False
        self._permission_mode = "normal"
        self._claude_session_uuid: Optional[str] = None
        self._session_start_time: Optional[float] = None
        self._workdir: Optional[str] = None

        # Simplified state tracking (no StateMachine object)
        self._state: str = SessionState.DISCONNECTED
        self._state_entered: float = time.monotonic()

        # Output tracking (replaces Turn tracking)
        self._send_marker: int = 0

        # Threading
        self._lock = threading.Lock()
        self._state_event = threading.Event()
        self._initializing = False

        # Gateway session isolation
        self._gateway_session_key: str = ""
        self._session_name: Optional[str] = None

        # Status callback
        self._status_callback = None

        # Observer (optional side-channel)
        self._observer: Optional[SessionObserver] = None
        # Observer poll interval (default 5s — tight enough to catch short tool calls
        # like Bash/Read/Write which complete in 1-10s; 180s would miss them entirely)
        self._observer_poll_interval: float = float(os.environ.get("HERMES_CLAUDE_SESSION_OBSERVER_POLL_INTERVAL", "5"))

        # Status card (optional Telegram real-time status)
        self._status_card: Optional[StatusCard] = None

    # ------------------------------------------------------------------
    # Backward compatibility shim
    # ------------------------------------------------------------------

    @property
    def _sm(self) -> _StateView:
        return _StateView(self)

    def _update_state(self, new_state: str) -> None:
        if new_state != self._state:
            old = self._state
            self._state = new_state
            self._state_entered = time.monotonic()
            self._state_event.set()
            logger.debug("State: %s → %s", old, new_state)

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
        completion_queue=None,
        resume_uuid: Optional[str] = None,
        auto_responder: bool = False,  # Accepted for API compat, no-op in pipeline architecture
        auto_responder_config: Optional[dict] = None,  # Accepted for API compat, no-op
        status_card_config: Optional[dict] = None,
    ) -> dict:
        """Start a Claude Code session in tmux."""
        # Phase 0: fast validation (under lock, no I/O)
        with self._lock:
            if self._session_active or self._initializing:
                return {
                    "session_id": self._session_id,
                    "tmux_session": session_name,
                    "state": self._state,
                    "permission_mode": self._permission_mode,
                    "claude_session_uuid": self._claude_session_uuid,
                    "note": "Session already active",
                }

            if permission_mode not in ("normal", "skip"):
                return {"error": f"Invalid permission_mode: {permission_mode}"}

            self._claude_session_uuid = str(uuid.uuid4())

            actually_resuming = False
            if resume_uuid:
                jsonl_path = self._find_session_jsonl(workdir, resume_uuid)
                if jsonl_path:
                    actually_resuming = True
                else:
                    logger.warning("resume_uuid=%s history not found, starting new", resume_uuid)

            if actually_resuming:
                self._state = SessionState.DISCONNECTED
                self._buf.clear()
                self._send_marker = 0

            self._session_id = f"cs_{uuid.uuid4().hex[:8]}"
            self._permission_mode = permission_mode
            self._workdir = os.path.abspath(workdir)
            self._tmux = TmuxInterface(session_name)
            self._initializing = True

        # Phase 1: tmux I/O (no lock, don't block other threads)
        try:
            needs_init = False

            if not self._tmux.session_exists():
                self._tmux.create_session(workdir=workdir)
                needs_init = True
            else:
                pane = self._tmux.capture_pane(lines=50)
                pane_lower = pane.lower()

                cwd_check = subprocess.run(
                    ["tmux", "display-message", "-t", session_name,
                     "-p", "#{pane_current_path}"],
                    capture_output=True, text=True, timeout=5,
                )
                tmux_cwd = cwd_check.stdout.strip() if cwd_check.returncode == 0 else ""

                needs_rebuild = False
                if tmux_cwd and workdir not in tmux_cwd and tmux_cwd not in workdir:
                    needs_rebuild = True
                elif "claude code" in pane_lower or "claude-" in pane_lower:
                    if workdir.lower() not in pane_lower:
                        needs_rebuild = True

                if needs_rebuild:
                    logger.warning("tmux session %s needs rebuild", session_name)
                    self._tmux.kill_session()
                    time.sleep(0.5)
                    self._tmux.create_session(workdir=workdir)
                    needs_init = True
                else:
                    pane_lines = clean_lines(pane)
                    result = detect_state(pane_lines)
                    if result.state == SessionState.IDLE:
                        logger.info("Reusing existing IDLE session %s", session_name)
                    else:
                        logger.warning("Session %s in %s state, rebuilding", session_name, result.state)
                        self._tmux.kill_session()
                        time.sleep(0.5)
                        self._tmux.create_session(workdir=workdir)
                        needs_init = True

            if needs_init:
                current_uid = os.getuid()
                if current_uid == 0:
                    non_root_user = os.environ.get("SUDO_USER") or "longxiang"
                    self._tmux.send_keys(f"su - {shlex.quote(non_root_user)}", enter=True)
                    time.sleep(1.5)
                    self._tmux.send_keys(f"cd {shlex.quote(workdir)}", enter=True)
                    time.sleep(0.5)
                    user_bin = f"/home/{non_root_user}/bin"
                    self._tmux.send_keys(f"export PATH={shlex.quote(user_bin)}:$PATH", enter=True)
                    time.sleep(0.5)

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
                time.sleep(2)

                if permission_mode == "skip":
                    time.sleep(1)
                    pane = self._tmux.capture_pane()
                    if "permission" in pane.lower() or "bypass" in pane.lower():
                        self._tmux.send_special_key("Down")
                        time.sleep(0.3)
                        self._tmux.send_special_key("Enter")
                        time.sleep(1)

                STARTUP_HEALTH_TIMEOUT = 30
                if not self._wait_for_claude_startup(STARTUP_HEALTH_TIMEOUT):
                    logger.error("Claude Code failed to start in %s", session_name)
                    try:
                        self._tmux.kill_session()
                    except Exception:
                        pass
                    with self._lock:
                        self._initializing = False
                    return {
                        "error": f"Claude Code did not start within {STARTUP_HEALTH_TIMEOUT}s.",
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

        # Phase 2: finalize (under lock)
        with self._lock:
            self._initializing = False
            self._session_active = True
            self._session_start_time = time.monotonic()
            self._update_state(SessionState.IDLE)

            # Start status card first (sets self._status_callback for observer)
            if status_card_config:
                self._start_status_card(status_card_config)

            # Start observer (uses self._status_callback set above)
            self._observer = SessionObserver(
                tmux=self._tmux,
                buffer=self._buf,
                on_update=self._on_observer_update if self._status_callback else None,
                poll_interval=self._observer_poll_interval,
            )
            self._observer.start()

        # Synchronous initial poll
        try:
            if self._observer:
                self._observer.poll_now()
                # Refresh state from buffer
                pane = self._tmux.capture_pane()
                lines = clean_lines(pane)
                result = detect_state(lines)
                self._update_state(result.state)
        except Exception as e:
            logger.warning("Initial poll failed: %s", e)

        with self._lock:
            result = {
                "session_id": self._session_id,
                "tmux_session": session_name,
                "state": self._state,
                "permission_mode": self._permission_mode,
                "claude_session_uuid": self._claude_session_uuid,
            }
            if actually_resuming:
                result["resumed_from"] = resume_uuid
            elif resume_uuid:
                result["fallback_note"] = f"resume_uuid={resume_uuid} history not found"
            return result

    def stop(self) -> dict:
        """Stop the session and clean up."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}

            if self._observer:
                self._observer.stop()

            if self._status_card:
                self._status_card.stop()
                self._status_card = None

            if self._tmux:
                try:
                    self._tmux.kill_session()
                except Exception as e:
                    logger.warning("Failed to kill tmux: %s", e)

            sid = self._session_id
            uuid_to_return = self._claude_session_uuid
            self._session_active = False
            self._session_id = None
            self._claude_session_uuid = None
            self._session_start_time = None
            self._observer = None
            self._update_state(SessionState.DISCONNECTED)

            return {
                "stopped": True,
                "session_id": sid,
                "claude_session_uuid": uuid_to_return,
            }

    # ------------------------------------------------------------------
    # Send operations
    # ------------------------------------------------------------------

    def send(self, message_or_task) -> dict:
        """Send a message or TaskContext to Claude.

        Args:
            message_or_task: str (raw message) or TaskContext (structured)
        """
        if isinstance(message_or_task, TaskContext):
            message = message_or_task.to_prompt()
        else:
            message = str(message_or_task)

        return self._send_text(message)

    def send_text(self, text: str) -> dict:
        """Type text and submit atomically."""
        return self._send_text(text)

    def type_text(self, text: str) -> dict:
        """Type text without pressing Enter."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if self._state in (SessionState.EXITED, SessionState.DISCONNECTED):
                return {"error": "Claude Code has exited."}
            self._tmux.send_keys(text)
            return {"typed": True, "state": self._state}

    def submit(self) -> dict:
        """Submit typed text by pressing Enter."""
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if self._state in (SessionState.EXITED, SessionState.DISCONNECTED):
                return {"error": "Claude Code has exited."}
            self._tmux.send_special_key("Enter")
        return {"submitted": True, "state": self._state}

    def cancel_input(self) -> dict:
        """Cancel current input (Ctrl+C)."""
        logger.warning("cancel_input for session %s", self._session_id)
        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            self._tmux.send_special_key("C-c")
            return {"cancelled": True, "state": self._state}

    def _send_text(self, text: str) -> dict:
        """Internal: send text to tmux, handling multi-line."""
        is_multiline = "\n" in text

        with self._lock:
            if not self._session_active:
                return {"error": "No active session"}
            if not self._tmux:
                return {"error": "No tmux interface"}
            if self._state in (SessionState.EXITED, SessionState.DISCONNECTED):
                return {"error": "Claude Code has exited."}

            self._send_marker = self._buf.total_count()

            if is_multiline:
                self._tmux.send_keys(text, enter=False)
            else:
                self._tmux.send_keys(text, enter=True)

        # Multi-line: wait for bracketed paste, then submit
        if is_multiline:
            logger.info("Multi-line send: waiting %.1fs for paste...", PASTE_SUBMIT_DELAY_SECONDS)
            time.sleep(PASTE_SUBMIT_DELAY_SECONDS)
            self._tmux.send_special_key("Enter")

        # Refresh state
        self._refresh_state()
        return {"sent": True, "state": self._state}

    # ------------------------------------------------------------------
    # Status and wait
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return current session state."""
        if not self._session_active:
            return {"state": SessionState.DISCONNECTED}

        self._refresh_state()
        # Detect current activity from buffer
        buf_lines = self._buf.read()
        activity = detect_activity([l.text for l in buf_lines]) if buf_lines else {"activity": "idle", "detail": ""}

        return {
            "state": self._state,
            "state_duration_seconds": round(time.monotonic() - self._state_entered, 1),
            "output_tail": self._buf.last_n_chars(200),
            "current_activity": activity.get("activity", "idle"),
            "activity_detail": activity.get("detail", ""),
        }

    def wait_for_idle(self, timeout: int = 1800) -> dict:
        """Wait for Claude to return to IDLE state.

        Simplified from the original: inline polling, no adaptive intervals,
        no turn tracking. Just poll → check state → return when done.

        Returns dict with status: "idle" | "permission" | "error" |
        "disconnected" | "exited" | "timeout"
        """
        if not self._session_active:
            return {"error": "No active session", "status": "disconnected"}

        # Timeout & patrol intervals (all in seconds)
        # PATROL_INTERVAL: how often to check for output growth when idle (default 300s = 5 min)
        PATROL_INTERVAL = int(os.environ.get("HERMES_CLAUDE_SESSION_PATROL_INTERVAL", "300"))
        # STALL_THRESHOLD: consecutive seconds with no buffer growth → consider stalled
        STALL_THRESHOLD = float(os.environ.get("HERMES_CLAUDE_SESSION_STALL_THRESHOLD", "1800"))
        COMPACT_MIN_WAIT = 3600  # 1 hour minimum for compaction
        COMPACT_MAX_WAIT = 7200  # 2 hours maximum for compaction
        POLL_INTERVAL = 180  # 3 minutes - poll for state changes when thinking/calling

        deadline = time.monotonic() + timeout
        last_patrol_tokens = self._buf.total_count()
        last_growth_time = time.monotonic()
        compact_detected = False
        compact_start = None

        while time.monotonic() < deadline:
            pane = self._tmux.capture_pane()
            lines = clean_lines(pane)
            if lines:
                self._buf.append_batch(lines)

            result = detect_state(lines)
            self._update_state(result.state)
            state = self._state

            # Terminal states
            if state == SessionState.IDLE:
                # Guard against animation ghost: when IDLE is first detected,
                # wait a short moment and confirm the prompt is still stable
                # (animations like "Forming…" can produce a transient ❯ before clearing)
                time.sleep(0.5)
                confirm_pane = self._tmux.capture_pane()
                confirm_lines = clean_lines(confirm_pane)
                confirm_result = detect_state(confirm_lines)
                if confirm_result.state != SessionState.IDLE:
                    # Still transitioning, continue waiting
                    self._update_state(confirm_result.state)
                    continue
                return {**self._build_idle_result(), "status": "idle"}

            if state == SessionState.PERMISSION:
                return {**self._build_permission_result(lines), "status": "permission"}

            if state == SessionState.ERROR:
                return {"state": state, "error_output": self._buf.last_n_chars(500), "status": "error"}

            if state == SessionState.DISCONNECTED:
                return {"error": "Session disconnected", "state": state, "status": "disconnected"}

            if state == SessionState.EXITED:
                return {"error": "Claude Code has exited", "state": state, "status": "exited"}

            # Compact detection
            if result.is_compacting and not compact_detected:
                compact_detected = True
                compact_start = time.monotonic()
                logger.info("Compact detected, extending wait")

            now = time.monotonic()

            # Update growth tracking
            current_tokens = self._buf.total_count()
            if current_tokens > last_patrol_tokens:
                last_growth_time = now
                last_patrol_tokens = current_tokens

            # Fast poll: just check state and wait
            remaining = deadline - now
            if remaining <= 0:
                if compact_detected and compact_start and (now - compact_start) < COMPACT_MAX_WAIT:
                    deadline = now + COMPACT_MIN_WAIT
                    continue
                break

            # Stall detection: no output growth for STALL_THRESHOLD seconds
            compact_active = (
                compact_detected and compact_start is not None
                and (now - compact_start) < COMPACT_MAX_WAIT
            )
            if not compact_active and (now - last_growth_time) > STALL_THRESHOLD:
                return {
                    "status": "stalled",
                    "state": state,
                    "stalled_seconds": round(now - last_growth_time, 1),
                    "progress_info": self._check_progress(deadline - timeout),
                }

            # State-aware wait: fast poll when active, respect patrol interval when idle/waiting
            if state in (SessionState.THINKING, SessionState.TOOL_CALL):
                wait_time = min(POLL_INTERVAL, remaining)
            else:
                wait_time = min(PATROL_INTERVAL, remaining)
            self._state_event.clear()
            self._state_event.wait(timeout=wait_time)

        # Timeout
        elapsed = time.monotonic() - (deadline - timeout)
        return {
            "status": "timeout",
            "state": self._state,
            "timeout_reached": True,
            "elapsed_seconds": round(elapsed, 1),
            "hint": "Timeout is normal for long tasks. Call wait_for_idle again with a larger timeout.",
            "output_since_send": self._get_output_since_send(),
        }

    def wait_for_state(self, target_state: str, timeout: int = 60) -> dict:
        """Wait for a specific state."""
        if not self._session_active:
            return {"error": "No active session"}

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._refresh_state()
            if self._state == target_state:
                return {"state": target_state}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._state_event.clear()
            self._state_event.wait(timeout=min(0.3, remaining))

        return {"state": self._state, "timeout_reached": True}

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
        if response not in ("allow", "deny"):
            return {"error": f"Invalid response: {response}"}

        max_retries = 3
        for attempt in range(max_retries):
            with self._lock:
                pane = self._tmux.capture_pane()
                lines = clean_lines(pane)
                result = detect_state(lines)

                if result.state != SessionState.PERMISSION:
                    if is_permission_in_text(pane):
                        self._update_state(SessionState.PERMISSION)
                    else:
                        if attempt < max_retries - 1:
                            continue
                        return {
                            "error": "Not in PERMISSION state",
                            "hint": "Permission may have been auto-handled.",
                        }

                is_numbered = self._detect_numbered_selector(pane)
                if response == "allow":
                    if is_numbered:
                        self._tmux.send_special_key("Enter")
                    else:
                        self._tmux.send_keys("y", enter=True)
                else:
                    if is_numbered:
                        deny_num = self._find_deny_option_number()
                        if deny_num:
                            self._tmux.send_keys(str(deny_num), enter=True)
                        else:
                            self._tmux.send_special_key("Enter")
                    else:
                        self._tmux.send_keys("n", enter=True)

            self._refresh_state()
            return {"responded": True, "state": self._state}

        return {"error": "Not in PERMISSION state after retries"}

    # ------------------------------------------------------------------
    # History / Events (simplified — no turn tracking)
    # ------------------------------------------------------------------

    def history(self) -> dict:
        """Return simplified session history."""
        return {
            "turns": [],
            "total_turns": 0,
            "total_tools_called": 0,
            "deprecated": True,
            "note": "Turn tracking removed in context-pipeline refactor",
        }

    def events(self, since_turn: int = 0) -> dict:
        """Return queued events (no-op in simplified version)."""
        return {"events": [], "deprecated": True}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_state(self) -> None:
        """Poll tmux and update internal state."""
        if not self._tmux or not self._session_active:
            return
        try:
            pane = self._tmux.capture_pane()
            lines = clean_lines(pane)
            if lines:
                self._buf.append_batch(lines)
            result = detect_state(lines)
            self._update_state(result.state)

            # Auto-approve in skip mode
            if result.state == SessionState.PERMISSION and self._permission_mode == "skip":
                self._auto_approve_permission()
        except Exception as e:
            logger.warning("State refresh failed: %s", e)
            # Detect tmux session loss and update state
            err_msg = str(e).lower()
            if "session not found" in err_msg or "can't find session" in err_msg or "no session" in err_msg:
                logger.warning("Tmux session lost, updating state to DISCONNECTED")
                self._update_state(SessionState.DISCONNECTED)

    def _on_observer_update(self, info: dict) -> None:
        """Called by observer thread with activity updates."""
        if self._status_callback:
            try:
                now = time.monotonic()
                status_info = {
                    "state": info.get("state", self._state),
                    "turn_id": None,
                    "elapsed_seconds": round(now - self._state_entered, 1),
                    "tool_calls": [],
                    "recent_output": self._buf.last_n_chars(200),
                    "tool_name": info.get("tool_name"),
                    "tool_target": info.get("tool_target"),
                }
                current_activity = info.get("current_activity", "idle")
                if current_activity != "idle":
                    status_info["current_activity"] = current_activity
                    status_info["activity_detail"] = info.get("activity_detail", "")
                self._status_callback(status_info)
            except Exception as e:
                logger.warning("Status callback error: %s", e)

    def _start_status_card(self, config: dict) -> None:
        """Create and start a StatusCard for Telegram real-time status.

        config keys:
            chat_id (str): Telegram chat ID
            loop: asyncio event loop from Gateway
            send_func: async callable(chat_id, content) -> SendResult
            edit_func: async callable(chat_id, message_id, content) -> SendResult
            delete_func: async callable(chat_id, message_id) -> bool
            poll_interval (float, optional): polling interval in seconds (default 3.0)
            max_card_length (int, optional): max characters in status card (default 500)
            bump_threshold (int, optional): consecutive failed edits before bumping (default 3)
        """
        chat_id = config.get("chat_id")
        loop = config.get("loop")
        send_func = config.get("send_func")
        edit_func = config.get("edit_func")
        delete_func = config.get("delete_func")

        if not chat_id or not loop or not send_func or not edit_func or not delete_func:
            logger.warning(
                "Status card disabled: missing gateway adapter config "
                "(chat_id=%s, loop=%s, send=%s)",
                chat_id or "missing",
                "set" if loop else "missing",
                "set" if send_func else "missing",
            )
            return
        try:
            self._status_card = StatusCard(
                session_uuid=self._claude_session_uuid,
                loop=loop,
                send_func=send_func,
                edit_func=edit_func,
                delete_func=delete_func,
                chat_id=chat_id,
                poll_interval=config.get("poll_interval", 3.0),
                max_card_length=config.get("max_card_length", 500),
                bump_threshold=config.get("bump_threshold", 3),
                session_name=self._session_name or "",
            )
            self._status_card.start()

            # Wire observer updates to StatusCard for real-time Telegram updates
            def _observer_to_status_card(info: dict) -> None:
                if self._status_card:
                    self._status_card.update_from_observer(info)

            self._status_callback = _observer_to_status_card

            logger.info("Status card started for session %s", self._claude_session_uuid[:8])
        except Exception as e:
            logger.warning("Status card start failed: %s", e)
            self._status_card = None

    def _auto_approve_permission(self) -> None:
        """Auto-approve permission in skip mode."""
        for _ in range(3):
            time.sleep(0.3)
            pane = self._tmux.capture_pane()
            if not is_permission_in_text(pane):
                self._update_state(SessionState.THINKING)
                return

            is_numbered = self._detect_numbered_selector(pane)
            if is_numbered:
                self._tmux.send_special_key("Enter")
            else:
                self._tmux.send_keys("y", enter=True)

            time.sleep(0.5)
            self._refresh_state()
            if self._state != SessionState.PERMISSION:
                return

    def _build_idle_result(self) -> dict:
        return {
            "state": SessionState.IDLE,
            "output_since_send": self._get_output_since_send(),
        }

    def _build_permission_result(self, lines: list = None) -> dict:
        result = {"state": SessionState.PERMISSION}
        if lines:
            for line in reversed(lines[-10:]):
                lower = line.lower()
                if "allow" in lower or "permission" in lower or "proceed?" in lower:
                    result["permission_request"] = line
                    break
        return result

    def _get_output_since_send(self) -> str:
        lines = self._buf.since(self._send_marker)
        return "\n".join(l.text for l in lines)

    def _check_progress(self, start_time: float) -> dict:
        now = time.monotonic()
        current = self._buf.total_count()
        return {
            "elapsed_seconds": round(now - start_time, 1),
            "token_count": current,
            "current_state": self._state,
            "state_duration_seconds": round(now - self._state_entered, 1),
        }

    def _detect_numbered_selector(self, pane_text: Optional[str] = None) -> bool:
        try:
            if pane_text is None:
                pane_text = self._tmux.capture_pane()
            lines = clean_lines(pane_text)
            last_lines = lines[-8:] if len(lines) >= 8 else lines
            for line in last_lines:
                if re.match(r"\s*❯\s*\d+\.", line):
                    return True
        except Exception:
            pass
        return False

    def _find_deny_option_number(self) -> Optional[int]:
        try:
            pane = self._tmux.capture_pane()
            lines = clean_lines(pane)
            for line in lines[-8:]:
                m = re.match(r"\s*(?:❯\s*)?(\d+)\.\s*(No|Deny)\b", line, re.IGNORECASE)
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        return None

    def _wait_for_claude_startup(self, timeout: int = 30) -> bool:
        """Wait for Claude Code to become usable."""
        deadline = time.monotonic() + timeout
        EMPTY_THRESHOLD = 3
        startup_attempts = 0

        while time.monotonic() < deadline:
            try:
                pane = self._tmux.capture_pane(lines=100)
                lines = clean_lines(pane)

                if not lines or len(lines) < EMPTY_THRESHOLD:
                    time.sleep(1.0)
                    continue

                scene = detect_startup_scene(lines)
                if scene and startup_attempts < 3:
                    startup_attempts += 1
                    if scene.action == "press_enter":
                        self._tmux.send_special_key("Enter")
                    elif scene.action == "press_down_enter":
                        self._tmux.send_special_key("Down")
                        time.sleep(0.3)
                        self._tmux.send_special_key("Enter")
                    time.sleep(2.0)
                    continue

                result = detect_state(lines)

                if result.state == SessionState.IDLE:
                    logger.info("Claude Code startup OK: IDLE")
                    return True

                if result.state in ("THINKING", "TOOL_CALL", "PERMISSION"):
                    pane_lower = pane.lower()
                    if any(sig in pane_lower for sig in
                           ("claude", "model", "thinking", "permission", "●", "❯")):
                        logger.info("Claude Code startup OK: %s", result.state)
                        return True

                if result.state in (SessionState.ERROR, SessionState.EXITED):
                    return False

            except Exception as e:
                logger.warning("Startup poll error: %s", e)

            time.sleep(1.0)

        logger.warning("Claude Code startup timed out after %ds", timeout)
        return False

    @staticmethod
    def _find_session_jsonl(workdir: str, session_uuid: str) -> Optional[str]:
        if not re.match(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            session_uuid,
        ):
            return None

        workdir = os.path.abspath(workdir).rstrip("/")
        claude_dir = os.path.expanduser("~/.claude/projects")
        dir_name = workdir.replace("/", "-")
        jsonl_path = os.path.join(claude_dir, dir_name, f"{session_uuid}.jsonl")
        if os.path.exists(jsonl_path):
            return jsonl_path
        return None
