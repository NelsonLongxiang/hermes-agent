"""Tests for heartbeat followup trigger and SOUL-aware SOP skill.

Covers:
  1. handler returns {"trigger_followup": True} when hint is injected
  2. handler returns None when dedup skips (no followup)
  3. handler returns None when no hints (all skills has_followup=False)
  4. heartbeat-sop decide() — greeting scenario emits menu hint
  5. heartbeat-sop decide() — TEMU command + lazy agent emits reminder
  6. heartbeat-sop decide() — TEMU command + diligent agent → silent
  7. heartbeat-sop decide() — casual chat → silent
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _make_session(db, source="test", user_id="u1"):
    sid = f"hb-followup-{int(time.time()*1_000_000)}"
    conn = db._conn
    conn.execute(
        "INSERT INTO sessions (id, source, user_id, started_at) VALUES (?,?,?,?)",
        (sid, source, user_id, time.time()),
    )
    conn.commit()
    return sid


class _HandlerHarness:
    def __init__(self, hermes_home: Path, skill_yaml: str, decide_src: str):
        self.hermes_home = hermes_home
        skill_dir = hermes_home / "skills" / "heartbeat-test"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.yaml").write_text(skill_yaml, encoding="utf-8")
        (skill_dir / "decide.py").write_text(decide_src, encoding="utf-8")
        handler_path = Path.home() / ".hermes" / "hooks" / "heartbeat-orchestrator" / "handler.py"
        spec = importlib.util.spec_from_file_location("hb_handler_followup", handler_path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.SKILLS_DIR = hermes_home / "skills"
        self.skill_dir = skill_dir
        self.skill_md = skill_dir / "SKILL.md"


class TriggerFollowupTest(unittest.TestCase):
    """Verify handler return value drives run.py followup logic."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hb-followup-")
        os.environ["HERMES_HOME"] = self.tmp
        for mod_name in list(sys.modules):
            if mod_name.startswith("hermes_cli.config"):
                del sys.modules[mod_name]
        from hermes_state import SessionDB
        self.db = SessionDB()
        self.sid = _make_session(self.db)

    def test_followup_returned_on_inject(self):
        """handler returns {'trigger_followup': True} when a hint is injected."""
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
                "    return {'has_followup': True, 'text': 'FOLLOWUP_TEST'}\n"
            ),
        )
        result = asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid}))
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("trigger_followup"))

    def test_no_followup_on_dedup(self):
        """handler returns None (not followup) when dedup skips inject."""
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
                "        'text': 'DEDUP_FOLLOWUP',\n"
                "        'write_back': {'append_md': 'last hint:\\n> DEDUP_FOLLOWUP'},\n"
                "    }\n"
            ),
        )
        if h.skill_md.exists():
            h.skill_md.unlink()
        asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid}))
        result2 = asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid}))
        self.assertIsNone(result2)

    def test_no_followup_when_silent(self):
        """handler returns None when skill has_followup=False."""
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
                "    return {'has_followup': False, 'text': ''}\n"
            ),
        )
        result = asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid}))
        self.assertIsNone(result)

    def test_followup_coexists_with_write_back(self):
        """write_back must still execute when trigger_followup is True."""
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
                "        'text': 'wb + followup',\n"
                "        'write_back': {'append_md': 'WB_AND_FU'},\n"
                "    }\n"
            ),
        )
        if h.skill_md.exists():
            h.skill_md.unlink()
        result = asyncio.run(h.mod.handle("agent:end", {"session_id": self.sid}))
        self.assertTrue(result.get("trigger_followup"))
        self.assertIn("WB_AND_FU", h.skill_md.read_text())


class HeartbeatSopDecideTest(unittest.TestCase):
    """Unit tests for heartbeat-sop decide() logic."""

    @classmethod
    def setUpClass(cls):
        sop_dir = Path.home() / ".hermes" / "skills" / "heartbeat-sop"
        spec = importlib.util.spec_from_file_location(
            "hb_sop_decide", str(sop_dir / "decide.py")
        )
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        import yaml
        meta = yaml.safe_load((sop_dir / "SKILL.yaml").read_text())
        cls.cfg = meta.get("config", {})

    def _decide(self, messages):
        ctx = {"session_id": "sop-test", "messages": messages, "config": self.cfg}
        return self.mod.decide(ctx, None)

    def test_greeting_emits_menu(self):
        r = self._decide([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "你好"},
        ])
        self.assertTrue(r["has_followup"])
        self.assertIn("TEMU", r["text"])

    def test_temu_lazy_agent_emits_reminder(self):
        r = self._decide([
            {"role": "user", "content": "帮 环球跨境 申诉 PO-12345"},
            {"role": "assistant", "content": "好的我来看看"},
        ])
        self.assertTrue(r["has_followup"])
        self.assertIn("预演", r["text"])

    def test_temu_diligent_agent_silent(self):
        r = self._decide([
            {"role": "user", "content": "帮 环球跨境 申诉 PO-12345"},
            {"role": "assistant", "content": "skill_view 正在加载工作流，先预演..."},
        ])
        self.assertFalse(r["has_followup"])

    def test_casual_chat_silent(self):
        r = self._decide([
            {"role": "user", "content": "Raft共识算法的leader election超时怎么设"},
            {"role": "assistant", "content": "150-300ms随机化..."},
        ])
        self.assertFalse(r["has_followup"])

    def test_inbound_lazy_agent_emits_reminder(self):
        r = self._decide([
            {"role": "user", "content": "这是物流表格数据，帮忙入库"},
            {"role": "assistant", "content": "好的收到"},
        ])
        self.assertTrue(r["has_followup"])
        self.assertIn("dry-run", r["text"])


if __name__ == "__main__":
    unittest.main()
