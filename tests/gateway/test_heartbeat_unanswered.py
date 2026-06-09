"""Step 3 acceptance: heartbeat-unanswered skill decides correctly.

This test loads the production `~/.hermes/skills/heartbeat-unanswered/decide.py`
(loaded from $HOME, NOT from the repo — that skill is a deployable artifact)
and asserts:

  1. answered question → no hint (no spam)
  2. unanswered single question → hint emitted
  3. unanswered multi-question → numbered hint + write_back block
  4. dedup vs recent SKILL.md log → skip if already logged
  5. TTL=0 disables dedup
  6. fail-soft on malformed ctx (None / dict-without-messages)
  7. CJK-only question is tokenized and detected
  8. short question (under min_question_length) is filtered
  9. write_back block contains the expected HTML comment + Q: bullets
 10. window cutoff: question outside the recent window is ignored
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


def _load_decide():
    """Load decide.py from $HOME/.hermes/skills/heartbeat-unanswered/."""
    skill_dir = Path.home() / ".hermes" / "skills" / "heartbeat-unanswered"
    spec = importlib.util.spec_from_file_location(
        "heartbeat_unanswered_decide", skill_dir / "decide.py"
    )
    assert spec is not None and spec.loader is not None, (
        f"Could not load decide.py from {skill_dir}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HeartbeatUnansweredTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.mod = _load_decide()
        except (FileNotFoundError, AssertionError) as e:
            raise unittest.SkipTest(f"heartbeat-unanswered skill not deployed: {e}")

    # ── 1. answered → no hint ──────────────────────────────────────────
    def test_answered_question_emits_no_hint(self):
        ctx = {"config": {}, "messages": [
            {"role": "user", "content": "什么是 heartbeat 机制？"},
            {"role": "assistant", "content": "heartbeat 是 agent:end 触发的 hint 注入。"},
        ]}
        r = self.mod.decide(ctx, "")
        self.assertFalse(r["has_followup"])

    # ── 2. unanswered single → hint ────────────────────────────────────
    def test_unanswered_single_question_emits_hint(self):
        ctx = {"config": {}, "messages": [
            {"role": "user", "content": "为什么 vault 路径要双写呢？"},
            {"role": "assistant", "content": "好的，已写入 commit 951d59e1a 并打 tag。"},
        ]}
        r = self.mod.decide(ctx, "")
        self.assertTrue(r["has_followup"])
        self.assertIn("vault", r["text"])

    # ── 3. multi-unanswered → numbered hint + write_back ───────────────
    def test_multi_unanswered_numbered_with_write_back(self):
        ctx = {"config": {}, "messages": [
            {"role": "user", "content": "dev-flow 第一个 skill 选哪个？"},
            {"role": "assistant", "content": "A7.1 未回答问题检测。"},
            {"role": "user", "content": "心跳触发位点选哪里？"},
            {"role": "assistant", "content": "commit push 一下看看。"},
        ]}
        r = self.mod.decide(ctx, "")
        self.assertTrue(r["has_followup"])
        self.assertIn("1.", r["text"])
        self.assertIn("2.", r["text"])
        self.assertIn("write_back", r)
        self.assertIn("<!-- heartbeat write_back heartbeat-unanswered @", r["write_back"]["append_md"])
        self.assertIn("  - Q: dev-flow 第一个 skill 选哪个？", r["write_back"]["append_md"])

    # ── 4. dedup vs recent log ─────────────────────────────────────────
    def test_dedup_skips_question_in_recent_log(self):
        md = "<!-- heartbeat write_back heartbeat-unanswered @ 1000 -->\n  - Q: 为什么 vault 路径要双写呢？\n"
        ctx = {"config": {"dedup_ttl_turns": 3}, "messages": [
            {"role": "user", "content": "为什么 vault 路径要双写呢？"},
            {"role": "assistant", "content": "好的已写入。"},
        ]}
        r = self.mod.decide(ctx, md)
        self.assertFalse(r["has_followup"])

    def test_dedup_respects_ttl_window(self):
        # Two log entries, TTL=1 → only the most recent counts; earlier one
        # is forgotten and would re-fire.
        md = (
            "<!-- heartbeat write_back heartbeat-unanswered @ 1000 -->\n"
            "  - Q: 旧问题在这里问？\n"
            "<!-- heartbeat write_back heartbeat-unanswered @ 2000 -->\n"
            "  - Q: 新问题在这里问？\n"
        )
        ctx = {"config": {"dedup_ttl_turns": 1}, "messages": [
            {"role": "user", "content": "旧问题在这里问？"},  # outside TTL window → should re-fire
            {"role": "assistant", "content": "嗯。"},
        ]}
        r = self.mod.decide(ctx, md)
        self.assertTrue(r["has_followup"])

    # ── 5. TTL=0 disables dedup ────────────────────────────────────────
    def test_ttl_zero_disables_dedup(self):
        md = "<!-- heartbeat write_back heartbeat-unanswered @ 1000 -->\n  - Q: 为什么 vault 路径要双写呢？\n"
        ctx = {"config": {"dedup_ttl_turns": 0}, "messages": [
            {"role": "user", "content": "为什么 vault 路径要双写呢？"},
            {"role": "assistant", "content": "好的已写入。"},
        ]}
        r = self.mod.decide(ctx, md)
        self.assertTrue(r["has_followup"])

    # ── 6. fail-soft on malformed ctx ──────────────────────────────────
    def test_fail_soft_on_empty_ctx(self):
        self.assertFalse(self.mod.decide({}, "")["has_followup"])

    def test_fail_soft_on_none_ctx(self):
        self.assertFalse(self.mod.decide(None, "")["has_followup"])

    def test_fail_soft_on_non_list_messages(self):
        self.assertFalse(self.mod.decide({"messages": "not a list"}, "")["has_followup"])

    # ── 7. CJK-only question is detected ───────────────────────────────
    def test_cjk_only_question_detected(self):
        ctx = {"config": {}, "messages": [
            {"role": "user", "content": "为什么没回答呢？"},
            {"role": "assistant", "content": "嗯嗯。"},
        ]}
        r = self.mod.decide(ctx, "")
        self.assertTrue(r["has_followup"])

    # ── 8. short question filtered ─────────────────────────────────────
    def test_short_question_below_min_length_filtered(self):
        ctx = {"config": {"min_question_length": 6}, "messages": [
            {"role": "user", "content": "啥?"},  # 2 chars
            {"role": "assistant", "content": "我不知道。"},
        ]}
        r = self.mod.decide(ctx, "")
        self.assertFalse(r["has_followup"])

    # ── 9. write_back block format ─────────────────────────────────────
    def test_write_back_block_has_comment_and_bullets(self):
        ctx = {"config": {}, "messages": [
            {"role": "user", "content": "测试问题长一点？"},
            {"role": "assistant", "content": "答非所问。"},
        ]}
        r = self.mod.decide(ctx, "")
        block = r["write_back"]["append_md"]
        self.assertIn("<!-- heartbeat write_back heartbeat-unanswered @", block)
        self.assertIn("  - Q: 测试问题长一点？", block)

    # ── 10. window cutoff: Q outside window ignored ────────────────────
    def test_question_outside_window_ignored(self):
        ctx = {"config": {"window_size": 2}, "messages": [
            {"role": "user", "content": "窗外问题？"},
            {"role": "assistant", "content": "随便答一下。"},
            {"role": "user", "content": "短问？"},  # filtered by min length
            {"role": "assistant", "content": "答非所问。"},
        ]}
        r = self.mod.decide(ctx, "")
        # The only question in the last 2 messages is too short → no hint.
        self.assertFalse(r["has_followup"])

    def test_question_inside_window_unanswered(self):
        ctx = {"config": {"window_size": 2}, "messages": [
            {"role": "user", "content": "窗外问题测试？"},
            {"role": "assistant", "content": "随便答一下。"},
            {"role": "user", "content": "窗内问题测试？"},
            {"role": "assistant", "content": "随便答一下。"},
        ]}
        r = self.mod.decide(ctx, "")
        self.assertTrue(r["has_followup"])


if __name__ == "__main__":
    unittest.main()
