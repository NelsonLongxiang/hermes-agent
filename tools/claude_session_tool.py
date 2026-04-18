"""tools/claude_session_tool.py — Hermes tool for Claude Code session management."""

import json
import logging
import shutil
import threading
from typing import Optional

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton manager
# ---------------------------------------------------------------------------
_manager = None
_manager_lock = threading.Lock()


def _get_manager():
    """Lazy-init the singleton manager (thread-safe)."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                from tools.claude_session.manager import ClaudeSessionManager
                _manager = ClaudeSessionManager()
    return _manager


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
        "'events' (get queued events)."
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
                ],
                "description": "Action to perform on the Claude session",
            },
            # start
            "workdir": {
                "type": "string",
                "description": "Working directory for 'start' action",
            },
            "session_name": {
                "type": "string",
                "description": "tmux session name (default: 'claude-work')",
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
                "description": "Max seconds to wait (default: 300 for wait_for_idle, 60 for wait_for_state)",
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
    """Dispatch claude_session tool calls."""
    mgr = _get_manager()
    action = args.get("action", "")
    task_id = kw.get("task_id")

    if action == "start":
        result = mgr.start(
            workdir=args.get("workdir", "."),
            session_name=args.get("session_name", "claude-work"),
            model=args.get("model"),
            permission_mode=args.get("permission_mode", "normal"),
            on_event=args.get("on_event", "notify"),
            completion_queue=kw.get("completion_queue"),
        )
    elif action == "send":
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
        result = mgr.wait_for_idle(timeout=args.get("timeout", 300))
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
    elif action == "stop":
        result = mgr.stop()
    elif action == "history":
        result = mgr.history()
    elif action == "events":
        result = mgr.events(since_turn=args.get("since_turn", 0))
    else:
        return tool_error(
            f"Unknown action: {action}. "
            "Valid: start, send, type, submit, cancel_input, status, "
            "wait_for_idle, wait_for_state, output, respond_permission, "
            "stop, history, events"
        )

    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_claude_session():
    """Check if tmux and claude are available."""
    return shutil.which("tmux") is not None


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
