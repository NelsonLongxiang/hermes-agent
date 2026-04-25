"""tools/claude_session_tool.py — Hermes tool for Claude Code session management."""

import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from typing import Optional

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session Registry（支持并行运行多个独立会话 + gateway session 隔离）
# ---------------------------------------------------------------------------
_sessions: dict = {}   # session_id → ClaudeSessionManager 实例
_workdir_index: dict = {}  # (gateway_key, workdir) → session_id 反向索引
_sessions_lock = threading.Lock()

# Per-gateway-session status observers — bridges session status to Telegram.
# Keyed by gateway_session_key so concurrent sessions route to the correct chat.
from typing import Callable
_status_observers: dict[str, Callable[[str, dict], None]] = {}  # gw_key → callback(session_id, info)
_status_observers_lock = threading.Lock()


def register_status_observer(callback, gateway_session_key: str = ""):
    """Register a status observer for a specific gateway session.

    Called by gateway/run.py to bridge ClaudeSessionManager status updates
    to Telegram status messages. The callback receives (session_id, status_info).

    Uses per-gateway-session-key isolation so concurrent sessions (e.g. a DM
    and a group chat running in parallel) each route status updates to the
    correct chat instead of overwriting each other.
    """
    with _status_observers_lock:
        _status_observers[gateway_session_key] = callback


def unregister_status_observer(gateway_session_key: str = ""):
    """Remove the status observer for a specific gateway session."""
    with _status_observers_lock:
        _status_observers.pop(gateway_session_key, None)


def _get_gateway_session_key() -> str:
    """读取当前 gateway session_key（并发安全）。

    优先从 contextvars 读取（gateway 模式，每个 Telegram 群聊独立），
    回退到 os.environ（CLI/cron 模式），都为空则返回空串（无隔离）。
    """
    try:
        from gateway.session_context import get_session_env
        key = get_session_env("HERMES_SESSION_KEY", "")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("HERMES_SESSION_KEY", "")


def _safe_call_observer(observer: Callable[[str, dict], None], session_id: str, status_info: dict) -> None:
    """Safely call an observer with exception handling.

    Wraps observer callbacks to prevent crashes when the underlying resources
    (e.g., gateway session, event loop) have been cleaned up. Silently logs
    errors rather than propagating them to the Claude Code session manager.

    Args:
        observer: The observer callback to call
        session_id: Claude session ID
        status_info: Status information dictionary
    """
    try:
        observer(session_id, status_info)
    except Exception as e:
        logger.debug(
            "Observer callback error (session=%s, gateway_key=%s): %s",
            session_id,
            _get_gateway_session_key(),
            e,
        )


def _derive_session_name(workdir: str, gateway_session_key: str = "") -> str:
    """基于 workdir + gateway session_key 生成确定性 tmux session 名。

    gateway 模式下，同一 workdir 的不同 Telegram 群聊会得到不同的 tmux 名。
    CLI/cron 模式下（gateway_session_key 为空），退化为纯 workdir 哈希。
    格式：hermes-{sha256前8位}
    """
    abs_path = os.path.abspath(workdir)
    if gateway_session_key:
        combined = f"{abs_path}:{gateway_session_key}"
    else:
        combined = abs_path
    h = hashlib.sha256(combined.encode()).hexdigest()[:8]
    return f"hermes-{h}"


def _get_session(session_id: str = None, gateway_session_key: str = "", strict: bool = False):
    """获取指定会话，无 session_id 时返回当前 gateway session 的最近会话。

    Args:
        session_id: 目标会话 ID。None 时按 gateway_session_key 过滤后返回最近的会话。
        gateway_session_key: 当前 gateway session key，用于隔离不同 Telegram 群聊。
        strict: 为 True 时，指定了 session_id 但找不到则返回 None（不回退），
                用于 stop/操作类 action 防止操作错误会话。
    """
    with _sessions_lock:
        if session_id:
            if session_id in _sessions:
                return _sessions[session_id]
            # session_id 已明确指定但找不到
            if strict:
                logger.warning(
                    "session_id=%s not found in registry (known: %s). "
                    "Possible gateway restart lost in-memory state.",
                    session_id, list(_sessions.keys()),
                )
                return None
        # 按 gateway session_key 过滤，返回该 gateway 下最近创建的会话
        if gateway_session_key:
            sessions_for_gateway = [
                mgr for mgr in _sessions.values()
                if getattr(mgr, "_gateway_session_key", "") == gateway_session_key
            ]
            if sessions_for_gateway:
                return sessions_for_gateway[-1]
        # CLI/cron 模式（无 gateway session_key）返回全局最后一个
        if _sessions:
            return list(_sessions.values())[-1]
    return None


def _get_session_by_workdir(workdir: str, gateway_session_key: str = ""):
    """通过 (gateway_session_key, workdir) 查找已注册的会话。

    无锁，调用方需持有 _sessions_lock。
    """
    abs_path = os.path.abspath(workdir)
    idx_key = (gateway_session_key, abs_path)
    sid = _workdir_index.get(idx_key)
    if sid and sid != "__starting__" and sid in _sessions:
        return _sessions[sid]
    return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CLAUDE_SESSION_SCHEMA = {
    "name": "claude_session",
    "description": (
        "Interactive Claude Code session via tmux — PREFERRED way to delegate coding tasks to Claude Code.\n"
        "Actions: start|send|type|submit|status|wait_for_idle|output|respond_permission|stop|history|events|diagnose|... (see parameters for full list)\n\n"
        "WHEN TO USE claude_session (preferred over delegate_task/terminal for Claude Code):\n"
        "- Complex multi-file coding tasks (refactoring, feature implementation)\n"
        "- Tasks requiring real-time monitoring and mid-task intervention\n"
        "- Long-running Claude Code sessions with state tracking\n"
        "- Any task where you need to see and control Claude's progress\n\n"
        "WHEN NOT TO USE:\n"
        "- Simple shell commands -> use terminal\n"
        "- Non-Claude reasoning tasks -> use delegate_task\n"
        "- One-shot quick questions -> use terminal with 'claude -p'\n\n"
        "Provides real-time state awareness (IDLE/THINKING/TOOL_CALL/PERMISSION), "
        "turn-level tracking, atomic send, and permission handling.\n"
        "Load 'claude-session' skill for detailed workflows and troubleshooting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "start", "send", "type", "submit", "cancel_input",
                    "status", "wait_for_idle", "wait_for_state",
                    "output", "respond_permission", "stop", "history", "events",
                    "diagnose",
                ],
                "description": "Action to perform on the Claude session",
            },
            # 多会话路由
            "session_id": {
                "type": "string",
                "description": "目标会话ID（可选，默认最近活跃的会话）",
            },
            # start
            "workdir": {
                "type": "string",
                "description": "Working directory for 'start' action",
            },
            "session_name": {
                "type": "string",
                "description": "tmux session name (default: hermes-{sha256[:8]} based on workdir)",
            },
            "model": {
                "type": "string",
                "description": "Claude model to use (e.g. 'sonnet', 'opus')",
            },
            "permission_mode": {
                "type": "string",
                "enum": ["normal", "skip"],
                "description": "Permission mode: 'normal' (Claude asks) or 'skip' (auto-approve)",
            },
            "on_event": {
                "type": "string",
                "enum": ["notify", "queue", "none"],
                "description": "Event delivery mode (default: 'notify')",
            },
            "resume_uuid": {
                "type": "string",
                "description": "Claude Code session UUID to resume (optional). If provided, starts with --resume to restore history.",
            },
            # send / type
            "message": {
                "type": "string",
                "description": "Message text for 'send' action",
            },
            "text": {
                "type": "string",
                "description": "Text for 'type' action (no Enter)",
            },
            # wait_for_idle / wait_for_state
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait (default: 900 for wait_for_idle, 60 for wait_for_state). Claude Code tasks typically take 3-30 minutes. Use 900 for normal tasks, 1800 for heavy analysis.",
                "minimum": 1,
            },
            "target_state": {
                "type": "string",
                "description": "Target state for 'wait_for_state' action",
            },
            # output
            "offset": {
                "type": "integer",
                "description": "Line offset for 'output' action",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines for 'output' action",
                "minimum": 1,
            },
            # respond_permission
            "response": {
                "type": "string",
                "enum": ["allow", "deny"],
                "description": "Permission response for 'respond_permission' action",
            },
            # events
            "since_turn": {
                "type": "integer",
                "description": "Filter events since turn ID for 'events' action",
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _handle_claude_session(args, **kw):
    """Dispatch claude_session tool calls (支持多会话路由 + gateway session 隔离)."""
    action = args.get("action", "")
    gw_key = _get_gateway_session_key()

    # ── start：创建新实例并注册（gateway session + workdir 联合隔离）──
    if action == "start":
        from tools.claude_session.manager import ClaudeSessionManager

        workdir = args.get("workdir", ".")
        abs_workdir = os.path.abspath(workdir)
        idx_key = (gw_key, abs_workdir)

        # 基于 (gateway_session_key, workdir) 生成确定性 tmux session 名（除非显式指定）
        sn = args.get("session_name")
        if not sn:
            sn = _derive_session_name(abs_workdir, gw_key)

        with _sessions_lock:
            # 检查 (gateway_key, workdir) 索引：同一 gateway session 下已有活跃会话则复用
            existing = _get_session_by_workdir(abs_workdir, gw_key)
            if existing and existing._session_active:
                return json.dumps({
                    "session_id": existing._session_id,
                    "tmux_session": existing._tmux.session_name if existing._tmux else None,
                    "state": existing._sm.current_state,
                    "permission_mode": existing._permission_mode,
                    "claude_session_uuid": existing._claude_session_uuid,
                    "note": "Session already active for this workdir",
                }, ensure_ascii=False)

            # 预占槽位，防止并发 start 时双重创建
            _workdir_index[idx_key] = "__starting__"

        try:
            mgr = ClaudeSessionManager()
            mgr._gateway_session_key = gw_key
            result = mgr.start(
                workdir=abs_workdir,
                session_name=sn,
                model=args.get("model"),
                permission_mode=args.get("permission_mode", "normal"),
                on_event=args.get("on_event", "notify"),
                completion_queue=kw.get("completion_queue"),
                resume_uuid=args.get("resume_uuid"),
            )
        except Exception as e:
            # 启动异常时清理占位
            with _sessions_lock:
                _workdir_index.pop(idx_key, None)
            return json.dumps({"error": f"Failed to create session: {e}"}, ensure_ascii=False)

        # 仅启动成功时注册到会话表和索引
        if "error" not in result:
            sid = result.get("session_id")
            if sid:
                with _sessions_lock:
                    _sessions[sid] = mgr
                    _workdir_index[idx_key] = sid
                # Attach status observer for this gateway session (per-key isolation).
                # Bind the observer via default parameter so the lambda captures the
                # correct callback at creation time — NOT the global dict at call time.
                # Hold lock during observer read to prevent TOCTOU race.
                with _status_observers_lock:
                    _observer = _status_observers.get(gw_key)
                if _observer:
                    mgr._status_callback = (
                        lambda info, _sid=sid, _obs=_observer: _safe_call_observer(_obs, _sid, info)
                    )
        else:
            # 启动失败时清理占位
            with _sessions_lock:
                _workdir_index.pop(idx_key, None)
        return json.dumps(result, ensure_ascii=False)

    # ── stop：停止并从注册表和索引移除 ──
    if action == "stop":
        specified_id = args.get("session_id")
        mgr = _get_session(specified_id, gateway_session_key=gw_key, strict=bool(specified_id))
        if mgr is None:
            return tool_error(
                f"Session '{specified_id}' not found in registry. "
                "It may have been lost after a gateway restart. "
                "Use tmux directly to clean up orphaned sessions."
            )
        result = mgr.stop()
        if result.get("stopped"):
            with _sessions_lock:
                _sessions.pop(result.get("session_id"), None)
                # 清理索引
                stale_keys = [k for k, v in _workdir_index.items() if v == result.get("session_id")]
                for k in stale_keys:
                    _workdir_index.pop(k, None)
        return json.dumps(result, ensure_ascii=False)

    # ── diagnose：不需要会话实例 ──
    if action == "diagnose":
        result = _diagnose_claude_session()
        return json.dumps(result, ensure_ascii=False)

    # ── 其他动作：通过 _get_session 路由到对应实例（按 gateway session 隔离）──
    mgr = _get_session(args.get("session_id"), gateway_session_key=gw_key)
    if mgr is None:
        # 只读查询 action：无会话时返回优雅默认值
        if action == "status":
            return json.dumps({"state": "DISCONNECTED"}, ensure_ascii=False)
        if action == "output":
            return json.dumps({"lines": [], "offset": 0, "total": 0}, ensure_ascii=False)
        if action == "events":
            return json.dumps({"events": []}, ensure_ascii=False)
        if action == "history":
            return json.dumps({"total_turns": 0, "turns": []}, ensure_ascii=False)
        return tool_error("No active session. Use 'start' first.")

    if action == "send":
        message = args.get("message")
        if not message:
            return tool_error("message is required for send action")
        result = mgr.send(message)
    elif action == "type":
        text = args.get("text")
        if not text:
            return tool_error("text is required for type action")
        result = mgr.type_text(text)
    elif action == "submit":
        result = mgr.submit()
    elif action == "cancel_input":
        result = mgr.cancel_input()
    elif action == "status":
        result = mgr.status()
    elif action == "wait_for_idle":
        result = mgr.wait_for_idle(timeout=args.get("timeout", 900))
    elif action == "wait_for_state":
        target = args.get("target_state")
        if not target:
            return tool_error("target_state is required for wait_for_state action")
        result = mgr.wait_for_state(target_state=target, timeout=args.get("timeout", 60))
    elif action == "output":
        result = mgr.output(
            offset=args.get("offset", 0),
            limit=args.get("limit", 50),
        )
    elif action == "respond_permission":
        response = args.get("response")
        if not response:
            return tool_error("response is required for respond_permission action")
        result = mgr.respond_permission(response)
    elif action == "history":
        result = mgr.history()
    elif action == "events":
        result = mgr.events(since_turn=args.get("since_turn", 0))
    else:
        return tool_error(
            f"Unknown action: {action}. "
            "Valid: start, send, type, submit, cancel_input, status, "
            "wait_for_idle, wait_for_state, output, respond_permission, "
            "stop, history, events, diagnose"
        )

    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_claude_session():
    """Check if tmux (hard dep) and claude CLI (soft dep) are available.
    
    Only tmux is required for the tool to register. Claude CLI availability
    is logged as a warning but does not prevent registration, because the
    user might install it later.
    """
    tmux_ok = shutil.which("tmux") is not None
    claude_ok = shutil.which("claude") is not None
    
    if not claude_ok:
        logger.warning(
            "claude_session: Claude Code CLI not found in PATH. "
            "Install with: npm install -g @anthropic-ai/claude-code"
        )
    
    if not tmux_ok:
        logger.warning(
            "claude_session: tmux not found in PATH. "
            "Install with: apt install tmux / brew install tmux"
        )
    
    return tmux_ok


def _diagnose_claude_session() -> dict:
    """Diagnose claude_session dependencies and configuration.
    
    Returns a structured report of all dependencies, their status,
    and remediation hints. Used by the 'diagnose' action.
    """
    import os
    
    checks = []
    all_ok = True
    
    # 1. tmux
    tmux_path = shutil.which("tmux")
    checks.append({
        "dependency": "tmux",
        "status": "ok" if tmux_path else "missing",
        "path": tmux_path,
        "hint": "Install: apt install tmux / brew install tmux" if not tmux_path else None,
        "required": True,
    })
    if not tmux_path:
        all_ok = False
    
    # 2. claude CLI
    claude_path = shutil.which("claude")
    checks.append({
        "dependency": "Claude Code CLI",
        "status": "ok" if claude_path else "missing",
        "path": claude_path,
        "hint": "Install: npm install -g @anthropic-ai/claude-code" if not claude_path else None,
        "required": True,
    })
    if not claude_path:
        all_ok = False
    
    # 3. HERMES_STREAM_STALE_TIMEOUT
    timeout_val = os.environ.get("HERMES_STREAM_STALE_TIMEOUT", "")
    timeout_ok = timeout_val.isdigit() and int(timeout_val) >= 300
    checks.append({
        "dependency": "HERMES_STREAM_STALE_TIMEOUT",
        "status": "ok" if timeout_ok else ("not_set" if not timeout_val else "too_low"),
        "value": timeout_val or "(not set)",
        "hint": (
            "Set to >= 300 in ~/.hermes/.env to prevent Stream Stalled errors"
            if not timeout_ok else None
        ),
        "required": False,
    })
    
    # 4. tmux version
    tmux_version = ""
    if tmux_path:
        try:
            import subprocess
            result = subprocess.run(
                [tmux_path, "-V"], capture_output=True, text=True, timeout=5
            )
            tmux_version = result.stdout.strip()
        except Exception:
            tmux_version = "unknown"
    checks.append({
        "dependency": "tmux version",
        "status": "ok" if tmux_version else "unknown",
        "value": tmux_version or "unknown",
        "required": False,
    })
    
    # 5. Claude Code version
    claude_version = ""
    if claude_path:
        try:
            import subprocess
            result = subprocess.run(
                [claude_path, "--version"], capture_output=True, text=True, timeout=10
            )
            claude_version = result.stdout.strip()
        except Exception:
            claude_version = "unknown"
    checks.append({
        "dependency": "Claude Code version",
        "status": "ok" if claude_version else "unknown",
        "value": claude_version or "unknown",
        "required": False,
    })
    
    return {
        "status": "ready" if all_ok else "missing_deps",
        "checks": checks,
        "summary": (
            "All dependencies met — claude_session is ready to use."
            if all_ok
            else "Missing required dependencies. See hints above."
        ),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="claude_session",
    toolset="claude_session",
    schema=CLAUDE_SESSION_SCHEMA,
    handler=_handle_claude_session,
    check_fn=_check_claude_session,
    emoji="🤖",
    max_result_size_chars=200_000,
)
