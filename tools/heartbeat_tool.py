"""Heartbeat tool — lets the agent proactively request workflow hints.

During a conversation turn, the agent can call this tool to check whether any
heartbeat-* skill has guidance for the current context.

The tool reuses the same decide() functions as the reference hook, so guidance
logic stays in one place.  The agent passes the current session context and
gets back structured hints it should act on.
"""
import logging
from typing import Any, Dict, List, Optional

from tools.heartbeat_shared import discover_heartbeat_skills

logger = logging.getLogger(__name__)

HEARTBEAT_GUIDE_SCHEMA = {
    "name": "heartbeat_tool",
    "description": (
        "Check heartbeat skills for proactive guidance based on the current "
        "conversation context. Call this when you sense the user might need "
        "workflow guidance (e.g. after a greeting, a vague request, or an "
        "unanswered question). Returns hints that tell you what to proactively "
        "suggest or follow up on."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": (
                    "What the user seems to want. Free-form text — e.g. "
                    "'greeting', 'temu refund', 'erp inbound', 'casual chat'. "
                    "Helps skills filter their guidance."
                ),
            },
        },
        "required": [],
    },
}


def heartbeat_tool(
    intent: str = "",
    session_id: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Check heartbeat skills for guidance. Returns hints or empty result."""
    from hermes_state import SessionDB

    sid = session_id or ""
    skills = discover_heartbeat_skills()
    if not skills:
        return {"has_guidance": False, "hints": [], "message": "No heartbeat skills found."}

    # Build ctx for decide()
    messages: List[Dict[str, Any]] = []
    if sid:
        try:
            _db = SessionDB()
            messages = _db.get_messages(sid) or []
        except Exception:
            pass

    ctx = {
        "session_id": sid,
        "messages": messages,
        "intent": intent,
        "source": "tool",  # let decide() know this is an active tool call
    }

    hints = []
    for (name, mod, hb, state_md, skill_cfg) in skills:
        try:
            if skill_cfg:
                ctx_skill = {**ctx, "config": skill_cfg}
            else:
                ctx_skill = ctx
            result = mod.decide(ctx_skill, state_md)
            if not result or not isinstance(result, dict):
                continue
            if not result.get("has_followup"):
                continue
            text = (result.get("text") or "").strip()
            if not text:
                continue
            hints.append({"skill": name, "hint": text})
        except Exception as e:
            logger.debug("heartbeat_tool: skill %s failed: %s", name, e)

    if not hints:
        return {"has_guidance": False, "hints": [], "message": "No guidance for current context."}

    return {
        "has_guidance": True,
        "hints": hints,
        "message": "Follow the hints above to proactively help the user.",
    }


def _check_heartbeat_tool_requirements(**kwargs) -> bool:
    """Always available — heartbeat skills are optional, tool works without them."""
    return True


# --- registration ---
try:
    from tools.registry import registry, tool_error
    registry.register(
        name="heartbeat_tool",
        toolset="heartbeat",
        schema=HEARTBEAT_GUIDE_SCHEMA,
        handler=lambda args, **kw: heartbeat_tool(
            intent=args.get("intent", ""),
            session_id=kw.get("session_id"),
        ),
        check_fn=_check_heartbeat_tool_requirements,
        emoji="💓",
    )
except ImportError:
    pass
