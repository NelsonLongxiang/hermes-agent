"""tools/claude_session_tool.py — Hermes tool for Claude Code session management."""

import hashlib
import json
import logging
import shutil
import threading
import uuid
from typing import Optional

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session Registry（支持并行运行多个独立会话 + workdir 隔离）
# ---------------------------------------------------------------------------
_sessions: dict = {}   # session_id → ClaudeSessionManager 实例
_workdir_index: dict = {}  # workdir(绝对路径) → session_id 反向索引
_sessions_lock = threading.Lock()


def _derive_session_name(workdir: str) -> str:
    """基于 workdir 绝对路径生成确定性 tmux session 名。

    同一 workdir 永远映射到同一 tmux session，防止重复创建。
    格式：hermes-{sha256前8位}
    """
    import os
    abs_path = os.path.abspath(workdir)
    h = hashlib.sha256(abs_path.encode()).hexdigest()[:8]
    return f"hermes-{h}"


def _get_session(session_id: str = None):
    """获取指定会话，无 session_id 时返回最近创建的会话。"""
    with _sessions_lock:
        if session_id and session_id in _sessions:
            return _sessions[session_id]
        # 无指定时返回最后添加的（最近创建的）会话
        if _sessions:
            return list(_sessions.values())[-1]
    return None


def _get_session_by_workdir(workdir: str):
    """通过 workdir 查找已注册的会话（无锁，调用方需持有 _sessions_lock）。"""
    import os
    abs_path = os.path.abspath(workdir)
    sid = _workdir_index.get(abs_path)
    if sid and sid in _sessions:
        return _sessions[sid]
    return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CLAUDE_SESSION_SCHEMA = {
    "name": "claude_session",
    "description": (
        "Manage an interactive Claude Code session via tmux. "
        "Provides real-time state awareness, turn-level tracking, and atomic send. "
        "Actions: 'start' (launch session), 'send' (send message atomically), "
        "'type' (type without sending), 'submit' (press Enter), "
        "'cancel_input' (Ctrl+C), 'status' (query state), "
        "'wait_for_idle' (block until Claude finishes), "
        "'wait_for_state' (block until specific state), "
        "'output' (get output with pagination), "
        "'respond_permission' (handle permission dialog), "
        "'stop' (terminate session), 'history' (turn history), "
        "'events' (get queued events), 'diagnose' (check dependencies)."
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
                "description": "Max seconds to wait (default: 600 for wait_for_idle, 60 for wait_for_state)",
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
    """Dispatch claude_session tool calls (支持多会话路由)."""
    action = args.get("action", "")

    # ── start：创建新实例并注册（workdir 隔离：同 workdir 复用已有会话）──
    if action == "start":
        from tools.claude_session.manager import ClaudeSessionManager
        import os

        workdir = args.get("workdir", ".")
        abs_workdir = os.path.abspath(workdir)

        with _sessions_lock:
            # 检查 workdir 索引：已有活跃会话则复用
            existing = _get_session_by_workdir(abs_workdir)
            if existing and existing._session_active:
                return json.dumps({
                    "session_id": existing._session_id,
                    "tmux_session": existing._tmux.session_name if existing._tmux else None,
                    "state": existing._sm.current_state,
                    "permission_mode": existing._permission_mode,
                    "claude_session_uuid": existing._claude_session_uuid,
                    "note": "Session already active for this workdir",
                }, ensure_ascii=False)

        # 基于 workdir 生成确定性 tmux session 名（除非显式指定）
        sn = args.get("session_name")
        if not sn:
            sn = _derive_session_name(abs_workdir)

        mgr = ClaudeSessionManager()
        result = mgr.start(
            workdir=abs_workdir,
            session_name=sn,
            model=args.get("model"),
            permission_mode=args.get("permission_mode", "normal"),
            on_event=args.get("on_event", "notify"),
            completion_queue=kw.get("completion_queue"),
            resume_uuid=args.get("resume_uuid"),
        )
        # 仅启动成功时注册到会话表和 workdir 索引
        if "error" not in result:
            sid = result.get("session_id")
            if sid:
                with _sessions_lock:
                    _sessions[sid] = mgr
                    _workdir_index[abs_workdir] = sid
        return json.dumps(result, ensure_ascii=False)

    # ── stop：停止并从注册表和 workdir 索引移除 ──
    if action == "stop":
        mgr = _get_session(args.get("session_id"))
        if mgr is None:
            return tool_error("No active session. Use 'start' first.")
        result = mgr.stop()
        if result.get("stopped"):
            with _sessions_lock:
                _sessions.pop(result.get("session_id"), None)
                # 清理 workdir 索引
                stale_keys = [k for k, v in _workdir_index.items() if v == result.get("session_id")]
                for k in stale_keys:
                    _workdir_index.pop(k, None)
        return json.dumps(result, ensure_ascii=False)

    # ── diagnose：不需要会话实例 ──
    if action == "diagnose":
        result = _diagnose_claude_session()
        return json.dumps(result, ensure_ascii=False)

    # ── 其他动作：通过 _get_session 路由到对应实例 ──
    mgr = _get_session(args.get("session_id"))
    if mgr is None:
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
        result = mgr.wait_for_idle(timeout=args.get("timeout", 600))
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
