"""heartbeat-unanswered decide() — detects user questions the agent missed.

Algorithm (heuristic, deliberately simple — no NLU/embedding):

  1. Walk the last `window_size` messages (default 6).
  2. Collect user-role messages that contain '?' or '？' and have at
     least `min_question_length` chars of substantive text.
  3. For each user question, search subsequent assistant messages in the
     same window for keyword overlap (>= 1 shared token of length >= 3
     after stopword removal). If found, treat as answered.
  4. Remaining = unanswered. If empty, return has_followup=False.
  5. If unanswered set is non-empty AND a recent dedup marker is NOT
     present in SKILL.md (within `dedup_ttl_turns` most recent log
     entries), emit a hint and (if write_back) append a log entry.

Design choices:
  - Keyword overlap, not semantic similarity. The orchestrator's hint
    guidance already tells the agent to use judgment — we just surface
    "you were asked X, Y, Z that you didn't visibly address".
  - State file (skill-local) is intentionally absent; we rely on
    SKILL.md log + dedup_ttl_turns so the skill is stateless across
    reloads and survives profile reinstalls.
  - First-token extraction handles CJK without jieba/etc — split on
    punctuation and whitespace, drop stopwords, take up to 4 longest
    tokens.
"""
from __future__ import annotations

import re
import time
from typing import Any

# Stopwords — kept tiny on purpose; this is recall, not precision.
_STOPWORDS = frozenset(
    """
    a an the is are was were be been being do does did have has had
    i you he she it we they me him her us them my your his their our
    this that these those and or but if then else so as at by for
    from in into of on to with what when where which who whom how why
    can could may might shall should will would
    什么 怎么 怎么 怎样 为啥 为什么 哪里 哪个 哪些 多少 几 时 何时
    是 的 了 在 和 与 或 但 如果 那么 就 也 都 还 只 被 把 让 请
    """.split()
)

_QUESTION_RE = re.compile(r"[?？]")


def _tokens(text: str) -> list[str]:
    """Crude tokenizer — split on non-letter/CJK, drop stopwords + short tokens."""
    raw = re.findall(r"[A-Za-z0-9一-鿿]+", text or "")
    return [t for t in raw if len(t) >= 3 and t.lower() not in _STOPWORDS]


def _answered(question: str, later_assistant_texts: list[str]) -> bool:
    """Return True if any later assistant turn shows keyword overlap."""
    q_tokens = set(_tokens(question))
    if not q_tokens:
        # Too generic to track — treat as answered (avoid spam).
        return True
    for ans in later_assistant_texts:
        a_tokens = set(_tokens(ans))
        if q_tokens & a_tokens:
            return True
    return False


def _existing_recent_log(skill_md: str, ttl: int) -> set[str]:
    """Return set of questions logged in the most recent `ttl` entries."""
    if not skill_md:
        return set()
    # Each write_back block starts with the HTML comment marker; split on it.
    # After split, block[0] is whatever came before the first marker;
    # block[1:] are the actual entries, where the first line is the
    # closing half of the HTML comment (" 1234 -->") and the rest are
    # the Q: lines (possibly prefixed with "- " markdown bullets).
    blocks = re.split(r"<!--\s*heartbeat write_back heartbeat-unanswered\s*@", skill_md)
    entries = blocks[1:]  # drop preamble
    recent = entries[-ttl:] if len(entries) >= ttl else entries
    seen: set[str] = set()
    for blk in recent:
        for ln in blk.splitlines():
            ln = ln.strip().lstrip("-").strip()
            if ln.startswith("Q: "):
                seen.add(ln[3:].strip())
                break
    return seen


def decide(ctx, state_md):  # sync signature
    cfg = (ctx or {}).get("config") if isinstance(ctx, dict) else None
    if not isinstance(cfg, dict):
        cfg = {}
    window_size = int(cfg.get("window_size", 6))
    min_q_len = int(cfg.get("min_question_length", 6))
    dedup_ttl = int(cfg.get("dedup_ttl_turns", 3))

    messages = (ctx or {}).get("messages") or []
    if not isinstance(messages, list):
        return {"has_followup": False, "text": ""}
    window = messages[-window_size:]

    # Group user-question → later-assistant-texts.
    unanswered: list[str] = []
    for i, m in enumerate(window):
        if not isinstance(m, dict):
            continue
        if m.get("role") != "user":
            continue
        content = (m.get("content") or "").strip()
        if len(content) < min_q_len or not _QUESTION_RE.search(content):
            continue
        # Collect assistant text after this point in the same window.
        later = [
            (mm.get("content") or "")
            for mm in window[i + 1:]
            if isinstance(mm, dict) and mm.get("role") == "assistant"
        ]
        if not _answered(content, later):
            unanswered.append(content)

    if not unanswered:
        return {"has_followup": False, "text": ""}

    # Dedup vs recent log entries.  TTL=0 means "no dedup" (always inject).
    if dedup_ttl <= 0:
        recent_asked = set()
    else:
        recent_asked = _existing_recent_log(state_md or "", dedup_ttl)
    fresh = []
    for q in unanswered:
        # Truncate to 80 chars for dedup key.
        key = q.strip()[:80]
        if key in recent_asked:
            continue
        fresh.append(key)

    if not fresh:
        return {"has_followup": False, "text": ""}

    if len(fresh) == 1:
        hint = f"你上一轮没回答这条用户问题：{fresh[0]}"
    else:
        items = "\n".join(f"  {i + 1}. {q}" for i, q in enumerate(fresh))
        hint = f"你上一轮有 {len(fresh)} 个用户问题没回答：\n{items}"

    append_lines = [f"  - Q: {q}" for q in fresh]
    append_block = "\n".join(append_lines)
    write_back_md = (
        f"\n<!-- heartbeat write_back heartbeat-unanswered @ {int(time.time())} -->\n"
        f"{append_block}\n"
    )

    return {
        "has_followup": True,
        "text": hint,
        "write_back": {"append_md": write_back_md},
    }
