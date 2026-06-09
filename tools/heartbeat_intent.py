"""Heartbeat intent recognition via auxiliary_client.

Analyzes recent conversation messages and returns a structured intent
that decide() can match against rules — no keyword guessing.
"""
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an intent classifier for a business operations assistant.
Analyze the conversation and return a JSON object with:

- intent: one of "greeting", "refund_appeal", "inbound_order", "command", "question", "other"
- entities: extracted key-value pairs (e.g. {"shop": "XX", "po": "PO-123"})
- workflow_state: "none", "awaiting_data", "rehearsal_pending", "confirmation_pending", "completed"
- next_step: suggested next action in Chinese (e.g. "show_menu", "run_rehearsal", "preview_data", "ask_confirmation")
- confidence: 0.0-1.0

Return ONLY the JSON, no explanation."""


def classify_intent(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Call LLM to classify intent from conversation messages.

    Uses hermes auxiliary_client — proper provider resolution, timeout,
    and connection pooling. Falls back to default on failure.
    """
    if not messages:
        return _default_intent()

    # Build conversation summary (last 6 messages)
    window = messages[-6:]
    conv_text = ""
    for msg in window:
        role = msg.get("role", "")
        content = (msg.get("content") or "")[:200]
        if role in ("user", "assistant"):
            conv_text += f"{role}: {content}\n"

    if not conv_text.strip():
        return _default_intent()

    try:
        from agent.auxiliary_client import call_llm

        resp = call_llm(
            task="heartbeat_intent",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": conv_text},
            ],
            temperature=0.1,
            max_tokens=200,
            timeout=5,
        )
        text = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        logger.info("[heartbeat] Intent: %s (confidence=%.2f) next=%s",
                    result.get("intent"), result.get("confidence", 0),
                    result.get("next_step"))
        return result
    except Exception as e:
        logger.debug("[heartbeat] Intent classification failed: %s", e)
        return _default_intent()


def _default_intent() -> Dict[str, Any]:
    return {
        "intent": "other",
        "entities": {},
        "workflow_state": "none",
        "next_step": "",
        "confidence": 0.0,
    }
