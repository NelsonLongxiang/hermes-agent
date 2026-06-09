"""heartbeat-echo decide() — returns a fixed hint + write_back payload.

Used for orchestrator validation: every agent:end emit should produce
one inject (system role) and one write_back (SKILL.md append) so we
can verify both pipelines (Q1 hint recognition + Q2 write_back schema)
are wired end-to-end.  Real skills (e.g. heartbeat-unanswered) ship in
Step 3.
"""
from __future__ import annotations

HINT_TEXT = "heartbeat orchestrator is wired end-to-end (hint + write_back)."


def decide(ctx, state_md):  # sync signature; orchestrator wraps in to_thread if needed
    return {
        "has_followup": True,
        "text": HINT_TEXT,
        "write_back": {
            "append_md": "echo: heartbeat orchestrator is wired end-to-end "
                         "(hint + write_back).",
        },
    }
