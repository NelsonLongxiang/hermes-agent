"""Tests for heartbeat-sop decide() — intent-driven mode.

decide() now receives structured intent_result from heartbeat_tool's
LLM classifier, not raw messages for keyword matching.
"""
import importlib.util
import unittest
from pathlib import Path


class HeartbeatSopDecideTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sop_dir = Path.home() / ".hermes" / "skills" / "heartbeat-sop"
        spec = importlib.util.spec_from_file_location(
            "hb_sop_decide", str(sop_dir / "decide.py")
        )
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _decide(self, intent_result):
        ctx = {"session_id": "test", "intent_result": intent_result}
        return self.mod.decide(ctx, None)

    def test_greeting_emits_menu(self):
        r = self._decide({"intent": "greeting", "confidence": 0.95, "next_step": "show_menu"})
        self.assertTrue(r["has_followup"])
        self.assertIn("TEMU", r["text"])

    def test_temu_reminder(self):
        r = self._decide({"intent": "refund_appeal", "confidence": 0.9, "next_step": "run_rehearsal"})
        self.assertTrue(r["has_followup"])
        self.assertIn("预演", r["text"])

    def test_inbound_reminder(self):
        r = self._decide({"intent": "inbound_order", "confidence": 0.9, "next_step": "preview_data"})
        self.assertTrue(r["has_followup"])
        self.assertIn("dry-run", r["text"])

    def test_low_confidence_silent(self):
        r = self._decide({"intent": "greeting", "confidence": 0.2, "next_step": "show_menu"})
        self.assertFalse(r["has_followup"])

    def test_other_intent_silent(self):
        r = self._decide({"intent": "other", "confidence": 0.9, "next_step": ""})
        self.assertFalse(r["has_followup"])


if __name__ == "__main__":
    unittest.main()
