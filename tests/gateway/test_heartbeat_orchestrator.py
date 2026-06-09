"""Step-1 acceptance: heartbeat-orchestrator end-to-end.

Spins up a real SessionDB, registers a stub heartbeat-* skill, fires
the hook's handle() with an agent:end event, and asserts:

  1. discovery finds the stub skill
  2. decide() returns has_followup
  3. a system-role message was appended to the session, prefixed with
     the skill's namespace tag ``[hint: <skill-name>]``
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure repo root is importable
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


class HeartbeatOrchestratorStep1Test(unittest.TestCase):
    def setUp(self):
        # Use a temp HERMES_HOME so the test never touches the real
        # ~/.hermes/skills/ tree (or its real SessionDB).
        self.tmp = tempfile.mkdtemp(prefix="hermes-hb-step1-")
        os.environ["HERMES_HOME"] = self.tmp
        # Reload config so get_hermes_home() picks up the env var.
        for mod in list(sys.modules):
            if mod.startswith("hermes_cli.config"):
                del sys.modules[mod]

        from hermes_cli.config import get_hermes_home  # noqa: F401
        self.hermes_home = Path(self.tmp)

        # Lay down a stub skill: skills/heartbeat-test/SKILL.yaml + decide.py
        skill_dir = self.hermes_home / "skills" / "heartbeat-test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.yaml").write_text(
            "name: heartbeat-test\n"
            "description: stub\n"
            "heartbeat:\n"
            "  enabled: true\n"
            "  trigger: agent_end\n",
            encoding="utf-8",
        )
        (skill_dir / "decide.py").write_text(
            "def decide(ctx, state_md):\n"
            "    return {'has_followup': True, 'text': 'TEST_HINT_PAYLOAD'}\n",
            encoding="utf-8",
        )

        # Create a real session in the real SessionDB (it respects HERMES_HOME).
        from hermes_state import SessionDB
        self.db = SessionDB()
        import time
        self.sid = f"hb-test-{int(time.time()*1000)}"
        conn = self.db._conn
        assert conn is not None
        conn.execute(
            "INSERT INTO sessions (id, source, user_id, started_at) VALUES (?,?,?,?)",
            (self.sid, "test", "u1", time.time()),
        )
        conn.commit()

        # Load the orchestrator handler from the production install path
        # so this test mirrors the real hook layout.
        handler_path = Path.home() / ".hermes" / "hooks" / "heartbeat-orchestrator" / "handler.py"
        if not handler_path.exists():
            self.skipTest(f"production hook not installed at {handler_path}")
        spec = importlib.util.spec_from_file_location("hb_handler_prod", handler_path)
        assert spec is not None and spec.loader is not None
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def tearDown(self):
        try:
            conn = self.db._conn
            if conn is not None:
                conn.execute("DELETE FROM sessions WHERE id=?", (self.sid,))
                conn.commit()
        except Exception:
            pass

    def test_end_to_end_injects_system_hint(self):
        # 1. discovery finds the stub skill (in this temp HERMES_HOME)
        # Force the handler's module-level SKILLS_DIR to match our temp.
        self.mod.SKILLS_DIR = self.hermes_home / "skills"
        found = self.mod._discover_heartbeat_skills()
        names = [n for n, _, _, _ in found]
        self.assertIn("heartbeat-test", names)

        # 2. handle() with agent:end -> injects system-role message
        async def run():
            await self.mod.handle("agent:end", {"session_id": self.sid, "response": "hi"})

        asyncio.run(run())

        # 3. assert the hint landed in messages
        conn = self.db._conn
        assert conn is not None
        row = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? AND role='system'",
            (self.sid,),
        ).fetchone()
        self.assertIsNotNone(row, "no system-role message appended")
        self.assertIn("[hint: heartbeat-test]", row[1])
        self.assertIn("TEST_HINT_PAYLOAD", row[1])


if __name__ == "__main__":
    unittest.main()
