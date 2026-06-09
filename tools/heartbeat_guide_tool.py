"""Heartbeat guidance tool — lets the agent proactively request workflow hints.

During a conversation turn, the agent can call this tool to check whether any
heartbeat-* skill has guidance for the current context.

The tool reuses the same decide() functions as the reference hook, so guidance
logic stays in one place.  The agent passes the current session context and
gets back structured hints it should act on.
"""
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

HEARTBEAT_GUIDE_SCHEMA = {
    "name": "heartbeat_guide",
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


def _discover_heartbeat_skills() -> list:
    """Return list of (name, module, skill_yaml_dict, state_md_path, skill_cfg)."""
    skills_dir = get_hermes_home() / "skills"
    found = []
    if not skills_dir.exists():
        return found
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or not skill_dir.name.startswith("heartbeat-"):
            continue
        manifest = skill_dir / "SKILL.yaml"
        decide_py = skill_dir / "decide.py"
        if not manifest.exists() or not decide_py.exists():
            continue
        try:
            meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            hb = (meta.get("heartbeat") or {})
            if not hb.get("enabled", False):
                continue
            mod_name = f"heartbeat_tool_{skill_dir.name.replace('-', '_')}"
            if mod_name in sys.modules:
                mod = sys.modules[mod_name]
            else:
                spec = importlib.util.spec_from_file_location(mod_name, decide_py)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
            state_md = skill_dir / "SKILL.md"
            skill_cfg = meta.get("config") or {}
            found.append((skill_dir.name, mod, hb, state_md, skill_cfg))
        except Exception as e:
            logger.debug("heartbeat_guide: failed to load %s: %s", skill_dir.name, e)
    return found


def heartbeat_guide(
    intent: str = "",
    session_id: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Check heartbeat skills for guidance. Returns hints or empty result."""
    from hermes_state import SessionDB

    sid = session_id or ""
    skills = _discover_heartbeat_skills()
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
            logger.debug("heartbeat_guide: skill %s failed: %s", name, e)

    if not hints:
        return {"has_guidance": False, "hints": [], "message": "No guidance for current context."}

    return {
        "has_guidance": True,
        "hints": hints,
        "message": "Follow the hints above to proactively help the user.",
    }


def _check_heartbeat_guide_requirements(**kwargs) -> bool:
    """Always available — heartbeat skills are optional, tool works without them."""
    return True


# --- registration ---
try:
    from tools.registry import registry, tool_error
    registry.register(
        name="heartbeat_guide",
        toolset="heartbeat",
        schema=HEARTBEAT_GUIDE_SCHEMA,
        handler=lambda args, **kw: heartbeat_guide(
            intent=args.get("intent", ""),
            session_id=kw.get("session_id"),
        ),
        check_fn=_check_heartbeat_guide_requirements,
        emoji="💓",
    )
except ImportError:
    pass
