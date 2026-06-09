"""heartbeat-sop decide() — SOUL-aware workflow guidance.

Reads SKILL.yaml config (keywords) and inspects the recent message window
to decide whether the agent should proactively advance the SOUL workflow.

Output hint is always phrased as a gentle nudge — the agent decides whether
to act on it.
"""
from __future__ import annotations

import re

GREETING_HINT = (
    "用户刚打招呼。请主动告知可用的运营指令：\n"
    "- TEMU 退款申诉：`帮 XX店铺 申诉 PO-xxxxx`\n"
    "- 领星 ERP 入库单：发物流表格数据\n"
    "简短引导即可，不要长篇。"
)

TEMU_REMINDER = (
    "TEMU 申诉工作流进行中。如果还没有预演（rehearsal-refund-appeal --auto），"
    "请先预演并报告结果给用户确认；如果预演已完成，请明确询问用户是否确认提交。"
)

INBOUND_REMINDER = (
    "入库单工作流进行中。请确保已执行 --dry-run 或 --show-data 预览数据，"
    "并等用户确认后再提交。"
)

COMMAND_REMINDER = (
    "用户的消息看起来像运营指令，但上一轮回复未提及加载对应技能。"
    "请确认任务类型并 skill_view 加载正确的技能工作流。"
)


def _matches(text: str, keywords: list) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def _is_greeting(text: str, keywords: list) -> bool:
    stripped = text.strip().lower()
    if len(stripped) < 10:
        return any(kw in stripped for kw in keywords)
    return False


def _agent_did_confirm(agent_text: str) -> bool:
    """Check if agent's last reply already asked for confirmation."""
    patterns = ["确认", "是否提交", "请确认", "可以提交", "继续吗", "确认提交"]
    lower = agent_text.lower()
    return any(p in lower for p in patterns)


def _agent_loaded_skill(agent_text: str) -> bool:
    """Check if agent mentioned loading a skill or showed workflow steps."""
    patterns = ["skill_view", "技能", "预演", "rehearsal", "dry-run", "预览", "工作流"]
    lower = agent_text.lower()
    return any(p in lower for p in patterns)


def decide(ctx, state_md):
    cfg = (ctx or {}).get("config") if isinstance(ctx, dict) else None
    if not isinstance(cfg, dict):
        cfg = {}

    temu_kw = cfg.get("temu_keywords", [])
    inbound_kw = cfg.get("inbound_keywords", [])
    greeting_kw = cfg.get("greeting_keywords", [])
    window_size = int(cfg.get("window_size", 6))

    messages = (ctx or {}).get("messages") or []
    if not isinstance(messages, list) or len(messages) < 2:
        return {"has_followup": False, "text": ""}

    window = messages[-window_size:]

    # Find the last user message and the assistant reply that followed.
    last_user = ""
    last_agent = ""
    for msg in reversed(window):
        role = (msg.get("role") or "") if isinstance(msg, dict) else ""
        content = (msg.get("content") or "") if isinstance(msg, dict) else str(msg)
        if role == "user" and not last_user:
            last_user = content
        elif role == "assistant" and not last_agent:
            last_agent = content

    if not last_user:
        return {"has_followup": False, "text": ""}

    # Strip reply-to prefix
    last_user_clean = re.sub(r'\[Replying to:.*?\]', '', last_user).strip()

    # Rule 1: greeting
    if _is_greeting(last_user_clean, greeting_kw):
        return {
            "has_followup": True,
            "text": GREETING_HINT,
            "write_back": {"append_md": f"last hint:\n> {GREETING_HINT}"},
        }

    # Rule 2: TEMU workflow
    if _matches(last_user_clean, temu_kw):
        if not _agent_did_confirm(last_agent) and not _agent_loaded_skill(last_agent):
            return {
                "has_followup": True,
                "text": TEMU_REMINDER,
                "write_back": {"append_md": f"last hint:\n> {TEMU_REMINDER}"},
            }

    # Rule 3: inbound workflow
    if _matches(last_user_clean, inbound_kw):
        if not _agent_did_confirm(last_agent) and not _agent_loaded_skill(last_agent):
            return {
                "has_followup": True,
                "text": INBOUND_REMINDER,
                "write_back": {"append_md": f"last hint:\n> {INBOUND_REMINDER}"},
            }

    # Rule 4: looks like a command but agent didn't load skill
    if len(last_user_clean) > 5 and not _agent_loaded_skill(last_agent):
        has_command_marker = any(c in last_user_clean for c in ["帮", "申诉", "入库", "提交", "po-", "PO-"])
        if has_command_marker:
            return {
                "has_followup": True,
                "text": COMMAND_REMINDER,
                "write_back": {"append_md": f"last hint:\n> {COMMAND_REMINDER}"},
            }

    # Rule 5: stay silent
    return {"has_followup": False, "text": ""}
