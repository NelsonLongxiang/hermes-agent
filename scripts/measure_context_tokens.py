#!/usr/bin/env python3
"""Measure the token composition of a Hermes API call payload.

Uses tiktoken (cl100k_base) to count tokens in system prompt layers,
skills index, tool definitions, and simulated conversation history.

Usage:
    python scripts/measure_context_tokens.py [--toolsets hermes-cli]
"""

import json
import os
import sys
import argparse
from pathlib import Path
from dataclasses import dataclass, field

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

import tiktoken

enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(enc.encode(text))


def fmt(n: int) -> str:
    if n >= 1000:
        return f"{n:,}"
    return str(n)


# ── Mock agent ──────────────────────────────────────────────────────────────

@dataclass
class MockAgent:
    valid_tool_names: set = field(default_factory=set)
    model: str = "claude-sonnet-4.7"
    provider: str = "anthropic"
    platform: str = "cli"
    load_soul_identity: bool = True
    skip_context_files: bool = False
    _tool_use_enforcement: str = "auto"
    _memory_store: object = None
    _memory_enabled: bool = False
    _user_profile_enabled: bool = False
    _memory_manager: object = None
    pass_session_id: bool = False
    session_id: str = None
    _kanban_worker_guidance: str = None


# ── Data collection ─────────────────────────────────────────────────────────

def collect_system_prompt_parts(agent, tool_names_set):
    """Get system prompt layers via Hermes internals."""
    from agent.system_prompt import build_system_prompt_parts, build_system_prompt
    parts = build_system_prompt_parts(agent)
    full = build_system_prompt(agent)
    return parts, full


def collect_tools(enabled_toolsets):
    """Get tool definitions and per-tool token counts."""
    from model_tools import get_tool_definitions
    tools = get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        quiet_mode=True,
    )
    return tools


def collect_skills_prompt(agent):
    """Get the skills system prompt text."""
    # build_skills_system_prompt is called inside build_system_prompt_parts
    # but we also want it measured separately.
    # Re-extract it from the stable part by looking for the skills marker.
    from agent.system_prompt import build_system_prompt_parts
    parts = build_system_prompt_parts(agent)
    stable = parts.get("stable", "")
    # Skills prompt starts with "## Skills (mandatory)"
    marker = "## Skills (mandatory)"
    idx = stable.find(marker)
    if idx >= 0:
        # Find end — look for next "\n\n" section or end of string
        # Skills block ends before environment hints typically
        # Just take from marker to the end of the skills block
        end_marker = "\n\nOnly proceed without loading a skill"
        end_idx = stable.find(end_marker, idx)
        if end_idx >= 0:
            return stable[idx:end_idx + len("\n\nOnly proceed without loading a skill if genuinely none are relevant to the task.")]
    return ""


def simulate_conversation_history(turns, avg_user_tokens=50, avg_assistant_tokens=200, avg_tool_tokens=300):
    """Simulate token counts for N-turn conversation history.

    Each turn = 1 user message + 1 assistant response (may include tool calls).
    Real pattern: some turns have tool calls, some don't.
    """
    per_turn = []
    for i in range(turns):
        user_t = avg_user_tokens
        has_tools = (i % 3 != 2)  # ~2/3 of turns have tool calls
        if has_tools:
            # assistant text + tool_call + tool_result
            assistant_t = avg_assistant_tokens + avg_tool_tokens * 2
        else:
            assistant_t = avg_assistant_tokens
        per_turn.append(user_t + assistant_t)
    return per_turn


# ── Main measurement ────────────────────────────────────────────────────────

def measure(enabled_toolsets=None):
    if enabled_toolsets is None:
        enabled_toolsets = ["hermes-cli"]

    print(f"Initializing Hermes modules (toolsets: {enabled_toolsets})...")

    # 1. Get tool definitions
    tools = collect_tools(enabled_toolsets)
    tool_names = {t["function"]["name"] for t in tools}

    # 2. Build mock agent
    agent = MockAgent(valid_tool_names=tool_names)

    # 3. System prompt parts
    parts, full_prompt = collect_system_prompt_parts(agent, tool_names)

    # 4. Skills prompt (extracted separately)
    skills_text = collect_skills_prompt(agent)

    # 5. Per-tool token counts
    tool_token_list = []
    for t in tools:
        name = t["function"]["name"]
        t_json = json.dumps(t, ensure_ascii=False)
        tool_token_list.append((name, count_tokens(t_json)))

    tool_token_list.sort(key=lambda x: -x[1])
    total_tool_tokens = sum(n for _, n in tool_token_list)

    # 6. Tool JSON serialization overhead
    tools_json = json.dumps(tools, ensure_ascii=False)
    tools_json_tokens = count_tokens(tools_json)

    # 7. System prompt layers
    stable_tokens = count_tokens(parts.get("stable", ""))
    context_tokens = count_tokens(parts.get("context", ""))
    volatile_tokens = count_tokens(parts.get("volatile", ""))

    # claude_session tool tokens
    claude_session_tokens = 0
    for name, n in tool_token_list:
        if name == "claude_session":
            claude_session_tokens = n
            break

    # 8. Conversation history simulation
    history_turns = [1, 5, 10, 15]
    history_data = {}
    per_turn_tokens = simulate_conversation_history(15)
    cumulative = 0
    for i, t_count in enumerate(per_turn_tokens, 1):
        cumulative += t_count
        if i in history_turns:
            history_data[i] = cumulative

    return {
        "enabled_toolsets": enabled_toolsets,
        "num_tools": len(tools),
        "tool_names": sorted(tool_names),
        # System prompt
        "system_prompt": {
            "stable_tokens": stable_tokens,
            "context_tokens": context_tokens,
            "volatile_tokens": volatile_tokens,
            "total_tokens": count_tokens(full_prompt),
            "stable_cacheable": True,
            "context_cacheable": True,
            "volatile_cacheable": False,
        },
        # Skills
        "skills_prompt_tokens": count_tokens(skills_text) if skills_text else 0,
        # Tools
        "tools_total_tokens": tools_json_tokens,
        "tools_per_tool": tool_token_list,
        "claude_session_tokens": claude_session_tokens,
        "claude_session_pct": round(claude_session_tokens / total_tool_tokens * 100, 1) if total_tool_tokens else 0,
        # Conversation history
        "history_simulated": history_data,
        # Per-turn breakdown
        "per_turn_avg_tokens": sum(per_turn_tokens) / len(per_turn_tokens),
        # Grand totals
        "grand_total_no_history": count_tokens(full_prompt) + tools_json_tokens,
        "grand_totals_with_history": {
            f"{turns}_turns": count_tokens(full_prompt) + tools_json_tokens + hist_tokens
            for turns, hist_tokens in history_data.items()
        },
    }


def print_report(data):
    # ── System Prompt Layers ────────────────────────────────────────────────
    sp = data["system_prompt"]
    print("\n" + "=" * 72)
    print("  SYSTEM PROMPT LAYERS")
    print("=" * 72)
    print(f"  {'Layer':<24} {'Tokens':>8}   {'Cacheable':<10}   {'Notes'}")
    print(f"  {'-'*24} {'-'*8}   {'-'*10}   {'-'*20}")
    rows = [
        ("stable", sp["stable_tokens"], "YES", "identity, guidance, skills, hints"),
        ("context", sp["context_tokens"], "YES", "AGENTS.md, project files"),
        ("volatile", sp["volatile_tokens"], "NO", "memory, timestamp, session"),
        ("TOTAL system prompt", sp["total_tokens"], "partial", ""),
    ]
    for name, tokens, cache, notes in rows:
        marker = " " if name.startswith("TOTAL") else " "
        print(f"  {marker}{name:<24} {fmt(tokens):>8}   {cache:<10}   {notes}")

    # ── Skills Index ────────────────────────────────────────────────────────
    print(f"\n  Skills prompt:          {fmt(data['skills_prompt_tokens']):>8} tokens  (inside stable)")

    # ── Tools ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"  TOOLS ({data['num_tools']} tools, total JSON: {fmt(data['tools_total_tokens'])} tokens)")
    print("=" * 72)
    print(f"  {'Tool Name':<30} {'Tokens':>8}   {'% of Total':>10}")
    print(f"  {'-'*30} {'-'*8}   {'-'*10}")
    total = sum(n for _, n in data["tools_per_tool"])
    for name, tokens in data["tools_per_tool"]:
        pct = tokens / total * 100 if total else 0
        highlight = " <-- claude_session" if name == "claude_session" else ""
        print(f"  {name:<30} {fmt(tokens):>8}   {pct:>9.1f}%{highlight}")

    print(f"\n  claude_session 占 tools 总量: {data['claude_session_pct']}%")

    # ── Conversation History ────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  SIMULATED CONVERSATION CONTEXT GROWTH")
    print("=" * 72)
    base = data["grand_total_no_history"]
    avg_per_turn = data["per_turn_avg_tokens"]
    print(f"  Base (system + tools):  {fmt(base):>8} tokens")
    print(f"  Avg per turn:           {fmt(int(avg_per_turn)):>8} tokens (user+assistant+tools)")
    print(f"  {'-'*50}")
    for turns in [1, 5, 10, 15]:
        total_t = data["grand_totals_with_history"][f"{turns}_turns"]
        pct_used = total_t / 200000 * 100  # 200k context window
        print(f"  {turns:>3} turns:  {fmt(total_t):>8} tokens  ({pct_used:.1f}% of 200k)")

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  SUMMARY")
    print("=" * 72)
    print(f"  Toolset:             {data['enabled_toolsets']}")
    print(f"  Tools count:         {data['num_tools']}")
    print(f"  System prompt:       {fmt(sp['total_tokens'])} tokens")
    print(f"  Tools JSON:          {fmt(data['tools_total_tokens'])} tokens")
    print(f"  Base per API call:   {fmt(data['grand_total_no_history'])} tokens")
    print(f"  claude_session:      {fmt(data['claude_session_tokens'])} tokens ({data['claude_session_pct']}% of tools)")


def save_report(data, path):
    # Strip non-serializable data
    report = {
        "enabled_toolsets": data["enabled_toolsets"],
        "num_tools": data["num_tools"],
        "system_prompt": data["system_prompt"],
        "skills_prompt_tokens": data["skills_prompt_tokens"],
        "tools_total_tokens": data["tools_total_tokens"],
        "tools_per_tool": data["tools_per_tool"],
        "claude_session_tokens": data["claude_session_tokens"],
        "claude_session_pct": data["claude_session_pct"],
        "per_turn_avg_tokens": data["per_turn_avg_tokens"],
        "grand_total_no_history": data["grand_total_no_history"],
        "grand_totals_with_history": data["grand_totals_with_history"],
    }
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"\nJSON report saved to {path}")


# ── Runtime Token Simulation ────────────────────────────────────────────────

def _mock_start_return():
    return json.dumps({
        "session_id": "cs_a1b2c3d4",
        "tmux_session": "hermes-fix-bug-session",
        "state": "IDLE",
        "permission_mode": "normal",
        "claude_session_uuid": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "name": "fix-bug-session",
    }, ensure_ascii=False)


def _mock_stop_return():
    return json.dumps({
        "stopped": True,
        "session_id": "cs_a1b2c3d4",
        "claude_session_uuid": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    }, ensure_ascii=False)


def _mock_status_return():
    return json.dumps({
        "state": "IDLE",
        "state_duration_seconds": 45.2,
        "output_tail": (
            "  248│    def _send_text(self, text: str) -> dict:\n"
            "  249│        \"\"\"Internal: send text to tmux.\"\"\"\n"
            "  250│        with self._lock:\n"
            "  251│            if not self._session_active:\n"
            "  252│                raise SessionNotActiveError()\n"
            "  253│            self._send_marker = self._buf.total_count()\n"
        ),
        "current_activity": "idle",
        "activity_detail": "",
        "session_ready": True,
    }, ensure_ascii=False)


def _mock_send_return():
    return json.dumps({
        "sent": True,
        "state": "THINKING",
        "send_seq": 1,
        "echo_status": "echo_detected",
    }, ensure_ascii=False)


def _mock_output_return(n_lines: int, avg_line_chars: int = 80) -> str:
    """Mock output with realistic code lines."""
    lines = []
    for i in range(n_lines):
        indent = "    " * (i % 4)
        content = f"def process_data(item: dict) -> Optional[str]:" if i % 7 == 0 else \
                  f"result = self._handler.execute(query)" if i % 5 == 0 else \
                  f"{indent}return value if condition else default"
        lines.append({"text": f"  {i:>4}│ {content}", "index": i})
    total = n_lines + 200  # simulate more lines than requested
    return json.dumps({
        "lines": lines,
        "total": total,
        "has_more": total > n_lines,
    }, ensure_ascii=False)


def _mock_wait_for_idle_return(output_chars: int = 3000) -> str:
    """Mock wait_for_idle return with realistic output_since_send."""
    # Generate realistic Claude Code output
    code_lines = [
        "I'll fix the bug in the authentication module.",
        "",
        "Let me first read the file to understand the issue:",
        "",
        "● Read(file_path=\"src/auth/handler.py\")",
        "",
        "  1│ import hashlib",
        "  2│ import hmac",
        "  3│ from typing import Optional",
        " 4│",
        "  5│ class AuthHandler:",
        "  6│     def __init__(self, secret_key: str):",
        "  7│         self._secret = secret_key",
        "  8│",
        "  9│     def verify_token(self, token: str) -> bool:",
        "10│         \"\"\"Verify HMAC token.\"\"\"",
        "11│         parts = token.split(':')",
        "12│         if len(parts) != 2:",
        "13│             return False",
        "14│         payload, signature = parts",
        "15│         expected = hmac.new(",
        "16│             self._secret.encode(),",
        "17│             payload.encode(),",
        "18│             hashlib.sha256",
        "19│         ).hexdigest()",
        "20│         return hmac.compare_digest(signature, expected)",
        "",
        "I found the bug — `hmac.new` should be `hmac.new`... wait, actually it's",
        "`hmac.new` → should be `hmac.new()`. Let me fix this:",
        "",
        "● Edit(file_path=\"src/auth/handler.py\")",
        "",
        "  15│         expected = hmac.new(",
        "  16│             self._secret.encode(),",
        "  17│             payload.encode(),",
        "  18│             hashlib.sha256",
        "  19│         ).hexdigest()",
        "     │ → 15│         expected = hmac.new(",
        "     │ → 16│             self._secret.encode(),",
        "     │ → 17│             payload.encode(),",
        "     │ → 18│             hashlib.sha256",
        "     │ → 19│         ).hexdigest()",
        "",
        "The fix replaces `hmac.new` with the correct `hmac.new` constructor.",
        "",
        "Let me verify the tests pass:",
        "",
        "● Bash(command=\"cd /project && python -m pytest tests/test_auth.py -v\")",
        "",
        "  tests/test_auth.py::test_verify_valid_token PASSED",
        "  tests/test_auth.py::test_verify_invalid_token PASSED",
        "  tests/test_auth.py::test_verify_malformed_token PASSED",
        "  ================================ 3 passed in 0.04s ================================",
        "",
        "The bug is fixed. The issue was a typo in the HMAC call.",
    ]

    # Build output to approximately match output_chars
    output_parts = []
    current_len = 0
    while current_len < output_chars:
        for line in code_lines:
            output_parts.append(line)
            current_len += len(line) + 1
            if current_len >= output_chars:
                break

    output_since_send = "\n".join(output_parts)

    return json.dumps({
        "status": "idle",
        "state": "IDLE",
        "output_since_send": output_since_send,
    }, ensure_ascii=False)


def _generate_mock_code_line(idx: int) -> str:
    templates = [
        "def process_{name}(data: Dict[str, Any]) -> Optional[List[Result]]:",
        "    result = self._client.query(sql, params)",
        "    if not result or len(result) == 0:",
        "        logger.warning('No data found for query: %s', sql)",
        "        return None",
        "    return [self._transform(row) for row in result]",
        "",
        "class DataProcessor:",
        "    def __init__(self, config: Config):",
        "        self._config = config",
        "        self._cache: Dict[str, Any] = {}",
        "        self._lock = threading.RLock()",
        "",
        "    def process(self, items: List[Item]) -> List[Result]:",
        "        with self._lock:",
        "            results = []",
        "            for item in items:",
        "                key = self._cache_key(item)",
        "                if key in self._cache:",
        "                    results.append(self._cache[key])",
        "                    continue",
        "                result = self._compute(item)",
        "                self._cache[key] = result",
        "                results.append(result)",
        "            return results",
    ]
    template = templates[idx % len(templates)]
    return template.format(name=f"item_{idx}")


def measure_runtime_tokens():
    """Simulate a full claude-session task flow and measure token cost per action."""
    CONTEXT_WINDOW = 200000

    # Define the simple scenario steps
    simple_steps = [
        ("start", _mock_start_return),
        ("wait_for_idle(60)", lambda: _mock_wait_for_idle_return(output_chars=2000)),
        ('send("fix a simple bug")', _mock_send_return),
        ("wait_for_idle(300)", lambda: _mock_wait_for_idle_return(output_chars=3000)),
        ("output(limit=50)", lambda: _mock_output_return(50)),
        ("output(limit=500)", lambda: _mock_output_return(500)),
        ("status", _mock_status_return),
        ("stop", _mock_stop_return),
    ]

    # Measure each action
    step_data = []
    for label, mock_fn in simple_steps:
        ret = mock_fn()
        chars = len(ret)
        tokens = count_tokens(ret)
        step_data.append((label, tokens, chars))

    # Complex scenario: 15 turns of send/wait/output cycles
    complex_steps = []
    for i in range(5):
        task_descs = [
            "implement user auth",
            "add database migration",
            "write API endpoint",
            "refactor error handling",
            "add integration tests",
        ]
        task = task_descs[i]
        complex_steps.append((f'send("{task}")', _mock_send_return))
        # Varying output sizes: 3k, 5k, 8k, 4k, 6k
        output_sizes = [3000, 5000, 8000, 4000, 6000]
        complex_steps.append(
            (f"wait_for_idle(300) #{i+1}", lambda s=output_sizes[i]: _mock_wait_for_idle_return(output_chars=s))
        )
        complex_steps.append((f"output(limit=100) #{i+1}", lambda: _mock_output_return(100)))

    complex_data = []
    for label, mock_fn in complex_steps:
        ret = mock_fn()
        chars = len(ret)
        tokens = count_tokens(ret)
        complex_data.append((label, tokens, chars))

    return {
        "simple_steps": step_data,
        "complex_steps": complex_data,
        "context_window": CONTEXT_WINDOW,
    }


def measure_cumulative_context(enabled_toolsets=None):
    """Measure cumulative context including system prompt + tools + history."""
    if enabled_toolsets is None:
        enabled_toolsets = ["hermes-cli"]

    # Get base measurements
    tools = collect_tools(enabled_toolsets)
    tool_names = {t["function"]["name"] for t in tools}
    agent = MockAgent(valid_tool_names=tool_names)
    _, full_prompt = collect_system_prompt_parts(agent, tool_names)

    base_tokens = count_tokens(full_prompt)
    tools_json = json.dumps(tools, ensure_ascii=False)
    tools_tokens = count_tokens(tools_json)

    return {
        "system_prompt_tokens": base_tokens,
        "tools_tokens": tools_tokens,
        "fixed_overhead": base_tokens + tools_tokens,
    }


def print_runtime_report(data, base_ctx):
    CONTEXT_WINDOW = data["context_window"]
    fixed = base_ctx["fixed_overhead"]

    # ── Simple Scenario ─────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  CLAUDE-SESSION RUNTIME TOKEN COST — Simple Scenario")
    print("=" * 72)
    print(f"  {'Action':<32} {'Tokens':>8}   {'Chars':>8}")
    print(f"  {'-'*32} {'-'*8}   {'-'*8}")

    simple_total_t = 0
    simple_total_c = 0
    for label, tokens, chars in data["simple_steps"]:
        print(f"  {label:<32} {tokens:>6}t   {chars:>8}")
        simple_total_t += tokens
        simple_total_c += chars

    print(f"  {'─' * 50}")
    print(f"  {'TOTAL (simple 8-step)':<32} {simple_total_t:>6}t   {simple_total_c:>8}")

    # ── Complex Scenario ────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  CLAUDE-SESSION RUNTIME TOKEN COST — Complex Scenario (15 turns)")
    print("=" * 72)
    print(f"  {'Action':<32} {'Tokens':>8}   {'Chars':>8}")
    print(f"  {'-'*32} {'-'*8} {'-'*8}")

    complex_total_t = 0
    complex_total_c = 0
    for label, tokens, chars in data["complex_steps"]:
        print(f"  {label:<32} {tokens:>6}t   {chars:>8}")
        complex_total_t += tokens
        complex_total_c += chars

    print(f"  {'─' * 50}")
    print(f"  {'TOTAL (complex 15-step)':<32} {complex_total_t:>6}t   {complex_total_c:>8}")

    # ── Cumulative Context Analysis ─────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  CUMULATIVE CONTEXT GROWTH (per API call)")
    print("=" * 72)
    print(f"  Fixed overhead (system prompt + tools): {fmt(fixed):>8} tokens")

    # Each tool call result becomes part of conversation history for subsequent calls.
    # Structure: user_turn (tool_call) + assistant_turn (tool_result)
    # Each adds: ~30t overhead per message boundary + tool_result tokens

    msg_boundary_overhead = 30  # role markers, whitespace, formatting

    print(f"\n  {'Step':<28} {'Step Ret':>7}   {'Cumulative':>10}   {'% of 200k':>9}")
    print(f"  {'-'*28} {'-'*7}   {'-'*10}   {'-'*9}")

    cumulative = fixed
    print(f"  {'(initial context)':<28} {'—':>7}   {fmt(cumulative):>10}   {cumulative/CONTEXT_WINDOW*100:>8.1f}%")

    all_steps = data["simple_steps"] + [("", 0, 0)] + data["complex_steps"]
    for label, tokens, chars in all_steps:
        if not label:
            print(f"  {'':>28}")
            continue
        # Each step adds: tool_call (~20t) + tool_result (tokens) + message overhead (msg_boundary_overhead)
        step_context = 20 + tokens + msg_boundary_overhead
        cumulative += step_context
        pct = cumulative / CONTEXT_WINDOW * 100
        print(f"  {label:<28} {tokens:>5}t   {fmt(cumulative):>10}   {pct:>8.1f}%")

    # ── Key Insights ────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  OPTIMIZATION INSIGHTS")
    print("=" * 72)

    # Analyze wait_for_idle as the biggest cost
    wfi_small = data["simple_steps"][1]  # wait_for_idle(60)
    wfi_large = data["simple_steps"][3]  # wait_for_idle(300)
    output_small = data["simple_steps"][4]  # output(limit=50)
    output_large = data["simple_steps"][5]  # output(limit=500)

    print(f"\n  wait_for_idle is the dominant cost driver:")
    print(f"    Short output (~2k chars):  {wfi_small[1]:>5}t / {wfi_small[2]:>6} chars")
    print(f"    Long output  (~3k chars):  {wfi_large[1]:>5}t / {wfi_large[2]:>6} chars")
    print(f"    output(50 lines):          {output_small[1]:>5}t / {output_small[2]:>6} chars")
    print(f"    output(500 lines):         {output_large[1]:>5}t / {output_large[2]:>6} chars")

    # Truncation scenario
    truncation_chars = 500
    # Estimate tokens for truncated output_since_send
    truncation_text = "x" * truncation_chars
    truncation_tokens = count_tokens(truncation_text)

    wfi_savings = wfi_large[1] - truncation_tokens
    print(f"\n  Truncation analysis (output_since_send → {truncation_chars} chars):")
    print(f"    Original tokens:  {wfi_large[1]:>5}t")
    print(f"    Truncated tokens: {truncation_tokens:>5}t")
    print(f"    Savings per call: {wfi_savings:>5}t ({wfi_savings/wfi_large[1]*100:.0f}%)")

    # Per-turn savings in complex scenario
    complex_wfi_total = sum(t for l, t, c in data["complex_steps"] if "wait_for_idle" in l)
    complex_wfi_count = sum(1 for l, t, c in data["complex_steps"] if "wait_for_idle" in l)
    complex_wfi_savings = (complex_wfi_total - complex_wfi_count * truncation_tokens)
    print(f"\n    Complex scenario wait_for_idle total: {complex_wfi_total}t")
    print(f"    With truncation:                       {complex_wfi_count * truncation_tokens}t")
    print(f"    Total savings:                         {complex_wfi_savings}t ({complex_wfi_savings/complex_wfi_total*100:.0f}%)")

    # output truncation analysis
    print(f"\n  output() truncation analysis:")
    print(f"    output(500 lines) is {output_large[1]}t — consider limiting to 50-100 lines for routine checks")
    print(f"    output(50 lines)  is {output_small[1]}t  — acceptable for incremental reads")

    print(f"\n  Recommendations:")
    print(f"    1. Truncate output_since_send to ~{truncation_chars} chars → save ~{wfi_savings}t per wait_for_idle")
    print(f"    2. Use output(limit=50) by default; only request more when needed")
    pct_used = cumulative / CONTEXT_WINDOW * 100
    print(f"    3. After {pct_used:.0f}% context usage, consider compacting or restarting the session")
    print(f"    4. Complex tasks (>10 send/wait cycles) risk hitting 200k window")


def main():
    parser = argparse.ArgumentParser(description="Measure Hermes API token composition")
    parser.add_argument(
        "--toolsets", nargs="+", default=["hermes-cli"],
        help="Enabled toolsets to measure (default: hermes-cli)",
    )
    parser.add_argument(
        "--runtime", action="store_true",
        help="Also measure runtime token simulation for claude-session flow",
    )
    args = parser.parse_args()

    data = measure(enabled_toolsets=args.toolsets)
    print_report(data)

    report_path = Path(__file__).resolve().parent / "token_report.json"
    save_report(data, report_path)

    if args.runtime:
        print("\n\n" + "#" * 72)
        print("# RUNTIME TOKEN SIMULATION")
        print("#" * 72)
        base_ctx = measure_cumulative_context(enabled_toolsets=args.toolsets)
        runtime_data = measure_runtime_tokens()
        print_runtime_report(runtime_data, base_ctx)


if __name__ == "__main__":
    main()
