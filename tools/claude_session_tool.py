"""tools/claude_session_tool.py — Hermes tool for Claude Code session management."""

import filecmp
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime
from typing import Optional

from tools.registry import registry, tool_error, tool_result

# Module-level import of gateway session context — avoids repeated import on hot path.
try:
    from gateway.session_context import get_session_env
except ImportError:
    get_session_env = None

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
    if get_session_env is not None:
        try:
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
                    "diagnose", "doctor_fix",
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
            # doctor_fix
            "apply": {
                "type": "boolean",
                "description": "For 'doctor_fix': False=analyze only (default), True=execute fixes",
            },
            "strategy": {
                "type": "string",
                "enum": ["project", "user", "merge"],
                "description": "For 'doctor_fix': merge strategy — 'project' (default), 'user', or 'merge'",
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
        # Clear callback before stop to prevent late callbacks to cleaned-up resources.
        mgr._status_callback = None
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

    # ── doctor_fix：诊断并修复技能文件同步 ──
    if action == "doctor_fix":
        result = _doctor_fix_skills(
            apply=args.get("apply", False),
            strategy=args.get("strategy", "project"),
        )
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
            "stop, history, events, diagnose, doctor_fix"
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

    # 6. 残留 tmux session 检测
    orphaned_sessions = []
    if tmux_path:
        try:
            import subprocess
            result = subprocess.run(
                ["tmux", "list-sessions"], capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                with _sessions_lock:
                    known_tmux_names = set()
                    for mgr in _sessions.values():
                        if mgr._tmux:
                            known_tmux_names.add(mgr._tmux.session_name)

                for line in result.stdout.strip().splitlines():
                    name = line.split(":")[0].strip()
                    if name.startswith("hermes-") and name not in known_tmux_names:
                        orphaned_sessions.append(name)

            orphan_count = len(orphaned_sessions)
            checks.append({
                "dependency": "orphaned tmux sessions",
                "status": "ok" if orphan_count == 0 else "warning",
                "value": f"{orphan_count} orphaned session(s)" if orphan_count else "none",
                "sessions": orphaned_sessions[:10],  # 最多显示 10 个
                "hint": (
                    "Orphaned hermes-* sessions detected. These may cause startup hangs. "
                    "Clean up with: tmux kill-session -t <name>  "
                    "or kill all: for s in $(tmux list-sessions 2>/dev/null | grep '^hermes-' | cut -d: -f1); do tmux kill-session -t \"$s\"; done"
                    if orphan_count > 0 else None
                ),
                "required": False,
            })
        except Exception:
            pass

    return {
        "status": "ready" if all_ok else "missing_deps",
        "checks": checks,
        "summary": (
            "All dependencies met — claude_session is ready to use."
            if all_ok
            else "Missing required dependencies. See hints above."
        ),
    }


def _doctor_fix_skills(apply: bool = False, strategy: str = "project") -> dict:
    """诊断并修复 claude-session 技能文件同步问题。

    两阶段操作：
      - apply=False（默认）：仅分析，不执行任何修改
      - apply=True：根据 strategy 执行修复

    Args:
        apply: False=仅分析返回报告，True=执行修复操作
        strategy: 合并策略，仅在 apply=True 且有差异时生效
            - "project": 优先项目版本（备份用户目录后创建软链接）
            - "user": 保留用户版本（将用户修改复制到项目目录）
            - "merge": 逐文件合并，项目独有的文件从项目复制，其余保留用户版本
    """
    # 定位两个目录
    user_skill_dir = os.path.expanduser("~/.hermes/skills/claude-session")
    # 项目目录：基于当前文件位置推导
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(_this_dir)  # tools/ → project root
    project_skill_dir = os.path.join(project_root, "skills", "claude-session")

    report = {
        "user_dir": user_skill_dir,
        "project_dir": project_skill_dir,
        "apply": apply,
        "strategy": strategy,
        "steps": [],
        "actions_taken": [],
        "status": "ok",
    }

    # ── Step 1: 项目目录必须存在 ──
    if not os.path.isdir(project_skill_dir):
        report["status"] = "error"
        report["steps"].append({
            "step": "check_project_dir",
            "result": "missing",
            "message": f"Project skill directory not found: {project_skill_dir}",
        })
        logger.error("doctor_fix: project skill dir missing: %s", project_skill_dir)
        return report

    report["steps"].append({
        "step": "check_project_dir",
        "result": "ok",
        "path": project_skill_dir,
        "files": _list_skill_files(project_skill_dir),
    })

    # ── Step 2: 用户目录状态检测 ──
    if not os.path.exists(user_skill_dir):
        report["steps"].append({"step": "check_user_dir", "result": "missing"})
        if not apply:
            report["status"] = "needs_fix"
            report["actions_available"] = [_action_create_symlink()]
            return report
        action_result = _create_symlink(user_skill_dir, project_skill_dir)
        report["actions_taken"].append(action_result)
        report["status"] = "fixed" if action_result["success"] else "error"
        logger.info("doctor_fix: created symlink %s -> %s", user_skill_dir, project_skill_dir)
        return report

    # 用户目录存在，判断类型
    is_link = os.path.islink(user_skill_dir)
    if is_link:
        link_target = os.readlink(user_skill_dir)
        resolved = os.path.realpath(user_skill_dir)
        project_resolved = os.path.realpath(project_skill_dir)

        # 断链检测
        if not os.path.exists(resolved):
            report["steps"].append({
                "step": "check_user_dir",
                "result": "symlink_broken",
                "target": link_target,
                "resolved": resolved,
            })
            logger.warning("Broken symlink: %s -> %s", user_skill_dir, link_target)
            if not apply:
                report["status"] = "needs_fix"
                report["actions_available"] = [_action_fix_broken_symlink(link_target)]
                return report
            return _do_fix_broken_symlink(report, user_skill_dir, project_skill_dir)

        if resolved == project_resolved:
            report["steps"].append({
                "step": "check_user_dir",
                "result": "symlink_ok",
                "target": link_target,
                "resolved": resolved,
            })
            report["status"] = "ok"
            return report
        else:
            report["steps"].append({
                "step": "check_user_dir",
                "result": "symlink_wrong",
                "current_target": link_target,
                "resolved": resolved,
                "expected": project_resolved,
            })
            if not apply:
                report["status"] = "needs_fix"
                report["actions_available"] = [_action_fix_wrong_symlink(link_target, project_resolved)]
                return report
            return _do_fix_wrong_symlink(report, user_skill_dir, project_skill_dir)

    # ── Step 3: 硬拷贝 — 比较差异 ──
    report["steps"].append({"step": "check_user_dir", "result": "hardcopy"})

    diff_result = _compare_skill_dirs(user_skill_dir, project_skill_dir)
    report["steps"].append({
        "step": "compare_content",
        "result": diff_result["summary"],
        "details": diff_result["details"],
        "summary_human": _format_diff_summary_human(diff_result),
    })

    # 顶层 diff_summary
    user_files = _list_skill_files(user_skill_dir)
    project_files = _list_skill_files(project_skill_dir)
    report["diff_summary"] = {
        "total_files": len(set(user_files) | set(project_files)),
        "differing_files": len(diff_result["details"]),
        "newer_in_project": [d["file"] for d in diff_result["details"]
                             if d["status"] in ("project_newer", "missing_in_user")],
        "newer_in_user": [d["file"] for d in diff_result["details"]
                          if d["status"] in ("user_newer", "missing_in_project")],
    }

    if diff_result["identical"]:
        if not apply:
            report["status"] = "needs_fix"
            report["actions_available"] = [_action_replace_hardcopy()]
            return report
        return _do_replace_hardcopy(report, user_skill_dir, project_skill_dir)

    # 有差异 → 根据分类和 strategy 决定
    diff_class = _classify_diff_status(diff_result["details"])

    # project_newer 且 strategy=project → 自动修复（安全操作）
    if diff_class == "project_newer" and strategy in ("project", "merge"):
        if not apply:
            report["status"] = "needs_fix"
            report["actions_available"] = [_action_backup_and_symlink()]
            return report
        return _do_backup_and_symlink(report, user_skill_dir, project_skill_dir)

    # user_newer 且 strategy=user → 同步用户修改到项目
    if diff_class == "user_newer" and strategy == "user":
        if not apply:
            report["status"] = "needs_fix"
            report["actions_available"] = [_action_sync_user_to_project(diff_result["details"])]
            return report
        return _do_sync_user_to_project(report, user_skill_dir, project_skill_dir, diff_result["details"])

    # merge 策略：逐文件合并
    if strategy == "merge":
        if not apply:
            report["status"] = "needs_fix"
            report["actions_available"] = [_action_merge_files(diff_result["details"])]
            return report
        return _do_merge_files(report, user_skill_dir, project_skill_dir, diff_result["details"])

    # 所有其他情况（user_newer+project / both_modified / 策略不匹配）
    report["status"] = "needs_user_decision"
    logger.warning("doctor_fix: diff_class=%s, needs user decision", diff_class)
    report["actions_available"] = _build_actions_for_decision(diff_class, diff_result["details"])
    return report


# ---------------------------------------------------------------------------
# doctor_fix: execute helpers (only run when apply=True)
# ---------------------------------------------------------------------------

def _do_fix_broken_symlink(report, user_dir, project_dir):
    try:
        os.remove(user_dir)
    except OSError as e:
        report["actions_taken"].append({"action": "remove_broken_symlink", "success": False, "error": str(e)})
        report["status"] = "error"
        return report
    r = _create_symlink(user_dir, project_dir)
    r["action"] = "fix_broken_symlink"
    report["actions_taken"].append(r)
    report["status"] = "fixed" if r["success"] else "error"
    return report


def _do_fix_wrong_symlink(report, user_dir, project_dir):
    try:
        os.remove(user_dir)
    except OSError as e:
        report["actions_taken"].append({"action": "remove_wrong_symlink", "success": False, "error": str(e)})
        report["status"] = "error"
        return report
    r = _create_symlink(user_dir, project_dir)
    r["action"] = "fix_symlink"
    report["actions_taken"].append(r)
    report["status"] = "fixed" if r["success"] else "error"
    return report


def _do_replace_hardcopy(report, user_dir, project_dir):
    try:
        shutil.rmtree(user_dir)
    except OSError as e:
        report["actions_taken"].append({"action": "remove_hardcopy", "success": False, "error": str(e)})
        report["status"] = "error"
        return report
    r = _create_symlink(user_dir, project_dir)
    r["action"] = "replace_hardcopy_with_symlink"
    r["reason"] = "files_identical"
    report["actions_taken"].append(r)
    report["status"] = "fixed" if r["success"] else "error"
    logger.info("doctor_fix: replaced identical hardcopy with symlink: %s", user_dir)
    return report


def _do_backup_and_symlink(report, user_dir, project_dir):
    backup_dir = user_dir + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        os.rename(user_dir, backup_dir)
    except OSError as e:
        report["actions_taken"].append({"action": "backup_user_dir", "success": False, "error": str(e)})
        report["status"] = "error"
        return report
    report["actions_taken"].append({"action": "backup_user_dir", "success": True, "backup_path": backup_dir})
    r = _create_symlink(user_dir, project_dir)
    r["action"] = "replace_with_symlink_after_backup"
    report["actions_taken"].append(r)
    report["status"] = "fixed" if r["success"] else "error"
    logger.info("doctor_fix: backed up to %s, created symlink", backup_dir)
    return report


def _do_sync_user_to_project(report, user_dir, project_dir, details):
    """将用户修改的文件复制到项目目录，然后替换用户目录为软链接。"""
    for d in details:
        if d["status"] in ("user_newer", "missing_in_project"):
            src = os.path.join(user_dir, d["file"])
            dst = os.path.join(project_dir, d["file"])
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                report["actions_taken"].append({"action": "sync_file_to_project", "file": d["file"], "success": True})
            except OSError as e:
                report["actions_taken"].append({"action": "sync_file_to_project", "file": d["file"], "success": False, "error": str(e)})
                report["status"] = "error"
                return report
    # 同步完成后替换用户目录为软链接
    try:
        shutil.rmtree(user_dir)
    except OSError as e:
        report["actions_taken"].append({"action": "remove_user_dir", "success": False, "error": str(e)})
        report["status"] = "error"
        return report
    r = _create_symlink(user_dir, project_dir)
    r["action"] = "sync_user_to_project_then_symlink"
    report["actions_taken"].append(r)
    report["status"] = "fixed" if r["success"] else "error"
    logger.info("doctor_fix: synced user changes to project, created symlink")
    return report


def _do_merge_files(report, user_dir, project_dir, details):
    """逐文件合并：项目独有的从项目复制，其余保留用户版本，然后创建软链接。"""
    for d in details:
        if d["status"] in ("missing_in_user",):
            # 项目独有的文件 → 复制到用户目录
            src = os.path.join(project_dir, d["file"])
            dst = os.path.join(user_dir, d["file"])
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                report["actions_taken"].append({"action": "copy_project_file", "file": d["file"], "success": True})
            except OSError as e:
                report["actions_taken"].append({"action": "copy_project_file", "file": d["file"], "success": False, "error": str(e)})
        # user_newer / missing_in_project → 保留用户版本（不动）
        # project_newer / both_modified → 保留用户版本（用户优先）
    # 合并完成后替换用户目录为软链接
    try:
        shutil.rmtree(user_dir)
    except OSError as e:
        report["actions_taken"].append({"action": "remove_user_dir", "success": False, "error": str(e)})
        report["status"] = "error"
        return report
    r = _create_symlink(user_dir, project_dir)
    r["action"] = "merge_then_symlink"
    report["actions_taken"].append(r)
    report["status"] = "fixed" if r["success"] else "error"
    logger.info("doctor_fix: merged files, created symlink")
    return report


# ---------------------------------------------------------------------------
# doctor_fix: actions_available builders (for apply=False reports)
# ---------------------------------------------------------------------------

def _action_create_symlink():
    return {
        "action": "create_symlink",
        "description": "Create symlink to project directory",
        "command": "claude_session(action='doctor_fix', apply=True)",
    }


def _action_fix_broken_symlink(broken_target):
    return {
        "action": "fix_broken_symlink",
        "description": f"Remove broken symlink (points to {broken_target}) and recreate",
        "command": "claude_session(action='doctor_fix', apply=True)",
    }


def _action_fix_wrong_symlink(current, expected):
    return {
        "action": "fix_wrong_symlink",
        "description": f"Redirect symlink from {current} to {expected}",
        "command": "claude_session(action='doctor_fix', apply=True)",
    }


def _action_replace_hardcopy():
    return {
        "action": "replace_hardcopy_with_symlink",
        "description": "User directory is identical to project — replace with symlink",
        "command": "claude_session(action='doctor_fix', apply=True)",
    }


def _action_backup_and_symlink():
    return {
        "action": "backup_and_symlink",
        "description": "Backup user directory, then create symlink to project version",
        "command": "claude_session(action='doctor_fix', apply=True)",
    }


def _action_sync_user_to_project(details):
    files = [d["file"] for d in details if d["status"] in ("user_newer", "missing_in_project")]
    return {
        "action": "sync_user_to_project",
        "description": f"Copy {len(files)} user-modified file(s) to project, then create symlink",
        "files": files,
        "command": "claude_session(action='doctor_fix', apply=True, strategy='user')",
    }


def _action_merge_files(details):
    project_only = [d["file"] for d in details if d["status"] == "missing_in_user"]
    return {
        "action": "merge_files",
        "description": (
            f"Copy {len(project_only)} project-only file(s) to user dir, "
            "keep user versions for rest, then create symlink"
        ),
        "project_only_files": project_only,
        "command": "claude_session(action='doctor_fix', apply=True, strategy='merge')",
    }


def _build_actions_for_decision(diff_class, details):
    """构建 needs_user_decision 状态下的可用操作列表。"""
    actions = []

    # 始终提供"使用项目版本"选项
    actions.append({
        "action": "use_project_version",
        "description": "Backup user directory and create symlink to project version",
        "command": "claude_session(action='doctor_fix', apply=True, strategy='project')",
    })

    # 如果用户有更新，提供"同步用户修改"选项
    user_files = [d["file"] for d in details if d["status"] in ("user_newer", "missing_in_project")]
    if user_files:
        actions.append({
            "action": "sync_user_changes",
            "description": f"Sync {len(user_files)} user-modified file(s) to project, then symlink",
            "files": user_files,
            "command": "claude_session(action='doctor_fix', apply=True, strategy='user')",
        })

    # merge 选项
    actions.append({
        "action": "merge",
        "description": "Copy project-only files to user dir, keep user versions for rest, then symlink",
        "command": "claude_session(action='doctor_fix', apply=True, strategy='merge')",
    })

    return actions


# ---------------------------------------------------------------------------
# doctor_fix: pure utility helpers
# ---------------------------------------------------------------------------

def _list_skill_files(directory: str) -> list:
    """列出技能目录中的所有文件（相对路径）。"""
    files = []
    for root, _dirs, filenames in os.walk(directory):
        for fn in filenames:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, directory)
            files.append(rel)
    return sorted(files)


def _create_symlink(link_path: str, target_path: str) -> dict:
    """创建软链接，确保父目录存在。"""
    parent = os.path.dirname(link_path)
    try:
        os.makedirs(parent, exist_ok=True)
        os.symlink(target_path, link_path)
        return {
            "action": "create_symlink",
            "success": True,
            "link": link_path,
            "target": target_path,
        }
    except OSError as e:
        return {
            "action": "create_symlink",
            "success": False,
            "error": str(e),
        }


def _compare_skill_dirs(user_dir: str, project_dir: str) -> dict:
    """比较两个技能目录的内容差异。

    Returns:
        dict with keys:
            identical: bool
            summary: str  ("identical" | "project_newer" | "user_newer" | "both_modified")
            details: list of per-file comparison dicts
    """
    user_files = set(_list_skill_files(user_dir))
    project_files = set(_list_skill_files(project_dir))

    all_files = sorted(user_files | project_files)
    details = []
    project_newer_count = 0
    user_newer_count = 0
    both_modified_count = 0

    for rel in all_files:
        user_path = os.path.join(user_dir, rel)
        proj_path = os.path.join(project_dir, rel)

        entry = {"file": rel}

        if not os.path.exists(user_path):
            entry["status"] = "missing_in_user"
            project_newer_count += 1
        elif not os.path.exists(proj_path):
            entry["status"] = "missing_in_project"
            user_newer_count += 1
        else:
            # 比较内容
            if filecmp.cmp(user_path, proj_path, shallow=False):
                entry["status"] = "identical"
            else:
                user_mtime = os.path.getmtime(user_path)
                proj_mtime = os.path.getmtime(proj_path)
                entry["user_mtime"] = datetime.fromtimestamp(user_mtime).isoformat()
                entry["project_mtime"] = datetime.fromtimestamp(proj_mtime).isoformat()

                if proj_mtime > user_mtime:
                    entry["status"] = "project_newer"
                    project_newer_count += 1
                elif user_mtime > proj_mtime:
                    entry["status"] = "user_newer"
                    user_newer_count += 1
                else:
                    # 同一秒修改但内容不同
                    entry["status"] = "both_modified"
                    both_modified_count += 1

                # 生成 diff 摘要
                entry["diff_summary"] = _diff_summary(user_path, proj_path)

        if entry["status"] != "identical":
            details.append(entry)

    identical = len(details) == 0
    if identical:
        summary = "identical"
    elif both_modified_count > 0 or (user_newer_count > 0 and project_newer_count > 0):
        summary = "both_modified"
    elif user_newer_count == 0 and project_newer_count > 0:
        summary = "project_newer"
    elif project_newer_count == 0 and user_newer_count > 0:
        summary = "user_newer"
    else:
        summary = "both_modified"

    return {"identical": identical, "summary": summary, "details": details}


def _classify_diff_status(details: list) -> str:
    """根据差异详情分类状态。"""
    has_user_newer = any(
        d["status"] in ("user_newer", "missing_in_project") for d in details
    )
    has_project_newer = any(
        d["status"] in ("project_newer", "missing_in_user") for d in details
    )
    has_both_modified = any(d["status"] == "both_modified" for d in details)

    if has_both_modified or (has_user_newer and has_project_newer):
        return "both_modified"
    if has_project_newer:
        return "project_newer"
    if has_user_newer:
        return "user_newer"
    return "identical"


def _diff_summary(file_a: str, file_b: str) -> str:
    """生成两个文件的简要 diff 摘要。"""
    if not shutil.which("diff"):
        return "files differ (diff not available)"

    try:
        result_stat = subprocess.run(
            ["diff", file_a, file_b],
            capture_output=True, text=True, timeout=5,
        )
        diff_lines = [l for l in result_stat.stdout.splitlines()
                      if l.startswith(("<", ">"))]
        return f"{len(diff_lines)} lines differ"
    except (subprocess.TimeoutExpired, FileNotFoundError,
            subprocess.SubprocessError):
        return "files differ"


def _format_diff_summary_human(diff_result: dict) -> str:
    """生成人类可读的差异摘要。"""
    if diff_result["identical"]:
        return "All files identical"

    parts = []
    for d in diff_result["details"]:
        if d["status"] == "project_newer":
            parts.append(f"[project_newer] {d['file']} ({d['project_mtime']}, {d.get('diff_summary', '')})")
        elif d["status"] == "user_newer":
            parts.append(f"[user_newer] {d['file']} ({d['user_mtime']}, {d.get('diff_summary', '')})")
        elif d["status"] == "missing_in_user":
            parts.append(f"[project_only] {d['file']}")
        elif d["status"] == "missing_in_project":
            parts.append(f"[user_only] {d['file']}")

    return "\n".join(parts)


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
