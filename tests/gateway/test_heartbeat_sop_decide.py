"""Tests for heartbeat-sop decide() logic.

Covers:
  1. heartbeat-sop decide() — greeting scenario emits menu hint
  2. heartbeat-sop decide() — TEMU command + lazy agent emits reminder
  3. heartbeat-sop decide() — TEMU command + diligent agent → silent
  4. heartbeat-sop decide() — casual chat → silent
  5. heartbeat-sop decide() — ERP inbound + lazy agent emits reminder

decide() is called by both:
  - heartbeat_guide tool (active, agent-initiated)
  - heartbeat-orchestrator hook (passive, reference impl in optional-skills/)
"""
import importlib.util
import unittest
from pathlib import Path


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
