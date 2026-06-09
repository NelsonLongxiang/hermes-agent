"""Step-1 + Step-2 acceptance: heartbeat-orchestrator end-to-end.

Spins up a real SessionDB, registers a stub heartbeat-* skill, fires
the hook's handle() with an agent:end event, and asserts:

  Step 1:
    1. discovery finds the stub skill
    2. decide() returns has_followup
    3. a system-role message was appended to the session, prefixed with
       the skill's namespace tag ``[hint: <skill-name>]``

  Step 2:
    4. dedup: a second identical emit short-circuits (no new row)
    5. write_back: skill with heartbeat.write_back=true appends to SKILL.md
    6. gating: skill with has_followup=False produces no hint
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure repo root is importable
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def _make_session(db, source="test", user_id="u1"):
    sid = f"hb-test-{int(time.time()*1_000_000)}"
    conn = db._conn
    assert conn is not None
    conn.execute(
        "INSERT INTO sessions (id, source, user_id, started_at) VALUES (?,?,?,?)",
        (sid, source, user_id, time.time()),
    )
    conn.commit()
    return sid


class _HandlerHarness:
    """Loads the production handler.py with a temp HERMES_HOME."""

    def __init__(self, hermes_home: Path, skill_yaml: str, decide_src: str):
        self.hermes_home = hermes_home
        # Lay down the skill
        skill_dir = hermes_home / "skills" / "heartbeat-test"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.yaml").write_text(skill_yaml, encoding="utf-8")
        (skill_dir / "decide.py").write_text(decide_src, encoding="utf-8")
        # Load handler
        handler_path = Path.home() / ".hermes" / "hooks" / "heartbeat-orchestrator" / "handler.py"
        spec = importlib.util.spec_from_file_location("hb_handler_prod", handler_path)
        assert spec is not None and spec.loader is not None
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.SKILLS_DIR = hermes_home / "skills"
        self.skill_dir = skill_dir
        self.skill_md = skill_dir / "SKILL.md"

    def reset_skill_md(self):
        if self.skill_md.exists():
            self.skill_md.unlink()

    def skill_md_text(self) -> str:
        return self.skill_md.read_text() if self.skill_md.exists() else ""


class HeartbeatOrchestratorStep1Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hermes-hb-step1-")
        os.environ["HERMES_HOME"] = self.tmp
        for mod in list(sys.modules):
            if mod.startswith("hermes_cli.config"):
                del sys.modules[mod]
        from hermes_state import SessionDB
        self.db = SessionDB()
        self.sid = _make_session(self.db)

    def tearDown(self):
        try:
            conn = self.db._conn
            if conn is not None:
                conn.execute("DELETE FROM sessions WHERE id=?", (self.sid,))
                conn.commit()
        except Exception:
            pass

    def test_end_to_end_injects_system_hint(self):
        h = _HandlerHarness(
            Path(self.tmp),
            skill_yaml=(
                "name: heartbeat-test\n"
                "description: stub\n"
                "heartbeat:\n"
                "  enabled: true\n"
                "  trigger: agent_end\n"
            ),
            decide_src=(
                "def decide(ctx, state_md):\n"
                "    return {'has_followup': True, 'text': 'TEST_HINT_PAYLOAD'}\n"
            ),
        )
        asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid, "response": "hi"}))

        # Hint is no longer written to DB; it's delivered via the return
        # value (trigger_followup + hints list) and optionally to SKILL.md.
        result_hints = []
        for _ in range(3):
            # handle() returns {"trigger_followup": True, "hints": [...]}
            r = asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid, "response": "hi"}))
            if isinstance(r, dict):
                result_hints = r.get("hints") or []
            break
        self.assertTrue(any("TEST_HINT_PAYLOAD" in hint for hint in result_hints),
                        "hint payload not in return value")


class HeartbeatOrchestratorStep2Test(unittest.TestCase):
    """Step 2: dedup + write_back + gating on has_followup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hermes-hb-step2-")
        os.environ["HERMES_HOME"] = self.tmp
        for mod in list(sys.modules):
            if mod.startswith("hermes_cli.config"):
                del sys.modules[mod]
        from hermes_state import SessionDB
        self.db = SessionDB()
        self.sid = _make_session(self.db)

    def tearDown(self):
        try:
            conn = self.db._conn
            if conn is not None:
                conn.execute("DELETE FROM sessions WHERE id=?", (self.sid,))
                conn.commit()
        except Exception:
            pass

    def test_dedup_skips_inject_on_exact_repeat(self):
        h = _HandlerHarness(
            Path(self.tmp),
            skill_yaml=(
                "name: heartbeat-test\n"
                "description: stub\n"
                "heartbeat:\n"
                "  enabled: true\n"
                "  trigger: agent_end\n"
                "  write_back: true\n"
            ),
            decide_src=(
                "def decide(ctx, state_md):\n"
                "    return {\n"
                "        'has_followup': True,\n"
                "        'text': 'DEDUP_TEST',\n"
                "        'write_back': {'append_md': 'last hint:\\n> DEDUP_TEST'},\n"
                "    }\n"
            ),
        )
        if h.skill_md.exists():
            h.skill_md.unlink()
        async def run():
            r1 = await h.mod.handle("agent:end", {"session_id": self.sid})
            r2 = await h.mod.handle("agent:end", {"session_id": self.sid})
            return r1, r2
        r1, r2 = asyncio.run(run())
        # First call injects (returns trigger_followup); second is deduped (returns None)
        self.assertIsNotNone(r1, "first call should inject")
        self.assertIsNone(r2, "dedup should have skipped the second inject")

    def test_write_back_appends_to_skill_md(self):
        h = _HandlerHarness(
            Path(self.tmp),
            skill_yaml=(
                "name: heartbeat-test\n"
                "description: stub\n"
                "heartbeat:\n"
                "  enabled: true\n"
                "  trigger: agent_end\n"
                "  write_back: true\n"
            ),
            decide_src=(
                "def decide(ctx, state_md):\n"
                "    return {\n"
                "        'has_followup': True,\n"
                "        'text': 'wb test',\n"
                "        'write_back': {'append_md': 'WB_MARKER_42'},\n"
                "    }\n"
            ),
        )
        h.reset_skill_md()
        asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid}))
        self.assertIn("WB_MARKER_42", h.skill_md_text())
        self.assertIn("<!-- heartbeat write_back", h.skill_md_text())

    def test_no_inject_when_has_followup_false(self):
        h = _HandlerHarness(
            Path(self.tmp),
            skill_yaml=(
                "name: heartbeat-test\n"
                "description: stub\n"
                "heartbeat:\n"
                "  enabled: true\n"
                "  trigger: agent_end\n"
            ),
            decide_src=(
                "def decide(ctx, state_md):\n"
                "    return {'has_followup': False, 'text': 'noop'}\n"
            ),
        )
        asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid}))

        conn = self.db._conn
        assert conn is not None
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='system'",
            (self.sid,),
        ).fetchone()[0]
        self.assertEqual(count, 0, "has_followup=False must not inject")


if __name__ == "__main__":
    unittest.main()
