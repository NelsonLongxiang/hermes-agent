#!/usr/bin/env python3
"""claude_status_card.py — Monitor Claude session via jsonl and update Telegram message.

Usage:
    python claude_status_card.py <session_id> <chat_id> [telegram|weixin]

Reads ~/.claude/projects/-mnt-f-Projects-hermes-agent/<session_id>.jsonl
and sends/edits a Telegram message with the latest session state.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def get_jsonl_path(session_id: str) -> Path:
    home = Path.home()
    return home / ".claude" / "projects" / "-mnt-f-Projects-hermes-agent" / f"{session_id}.jsonl"


def parse_jsonl(jsonl_path: Path) -> dict:
    """Parse jsonl and return latest state."""
    if not jsonl_path.exists():
        return {"status": "no_session", "lines": []}

    lines = jsonl_path.read_text().strip().split("\n")
    if not lines:
        return {"status": "empty", "lines": []}

    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        return {"status": "empty", "lines": []}

    # Get last entry
    last = entries[-1]
    entry_type = last.get("type", "unknown")

    result = {
        "status": "active",
        "entry_type": entry_type,
        "timestamp": last.get("timestamp", ""),
        "lines": entries,
    }

    if entry_type == "assistant":
        msg = last.get("message", {})
        content = msg.get("content", [])
        for item in content:
            if item.get("type") == "text":
                result["text"] = item.get("text", "")[:200]
            elif item.get("type") == "tool_use":
                result["tool"] = item.get("name", "")
                result["tool_input"] = item.get("input", {})
        result["model"] = msg.get("model", "")

    elif entry_type == "user":
        msg = last.get("message", {})
        content = msg.get("content", [])
        if content and isinstance(content[0], dict):
            if content[0].get("type") == "text":
                result["text"] = content[0].get("text", "")[:200]
            elif content[0].get("type") == "tool_result":
                result["tool_result"] = content[0].get("content", "")[:200]

    # Count totals
    result["total_entries"] = len(entries)
    assistant_count = sum(1 for e in entries if e.get("type") == "assistant")
    user_count = sum(1 for e in entries if e.get("type") == "user")
    result["assistant_count"] = assistant_count
    result["user_count"] = user_count

    return result


def format_status(state: dict) -> str:
    """Format state as a readable status card."""
    if state["status"] == "no_session":
        return "❌ Session not found"

    if state["status"] == "empty":
        return "⏳ Waiting for activity..."

    lines = ["📊 Claude Session Status\n"]

    # Entry type and timestamp
    entry_type = state.get("entry_type", "?")
    ts = state.get("timestamp", "")
    if ts:
        # Format timestamp
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_str = dt.strftime("%H:%M:%S")
        except:
            ts_str = ts[11:19] if len(ts) > 11 else ts
        lines.append(f"🕐 {ts_str}")

    # Counts
    lines.append(f"💬 {state.get('assistant_count', 0)} assistant / {state.get('user_count', 0)} user")

    # Current content
    if "tool" in state:
        tool = state["tool"]
        tool_input = state.get("tool_input", {})
        if tool == "Write":
            path = tool_input.get("file_path", "?")
            lines.append(f"✏️  Write: `{path}`")
        elif tool == "Read":
            path = tool_input.get("file_path", "?")
            lines.append(f"📖 Read: `{path}`")
        elif tool == "Bash":
            cmd = tool_input.get("command", "?")
            if len(cmd) > 50:
                cmd = cmd[:50] + "..."
            lines.append(f"⚡ Bash: `{cmd}`")
        elif tool == "TaskUpdate":
            status = tool_input.get("status", "?")
            task_id = tool_input.get("taskId", "?")
            lines.append(f"📋 Task #{task_id}: {status}")
        else:
            lines.append(f"🔧 {tool}")

    if "text" in state and state["text"]:
        text = state["text"]
        if len(text) > 100:
            text = text[:100] + "..."
        lines.append(f"\n💭 {text}")

    if "tool_result" in state:
        result = state["tool_result"]
        if len(result) > 80:
            result = result[:80] + "..."
        lines.append(f"✅ {result}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Monitor Claude session and update Telegram message")
    parser.add_argument("session_id", help="Claude session ID")
    parser.add_argument("chat_id", help="Telegram chat ID to send/edit message")
    parser.add_argument("--platform", default="telegram", choices=["telegram", "weixin"])
    parser.add_argument("--interval", type=float, default=3.0, help="Polling interval in seconds")
    parser.add_argument("--message-id", help="Existing message ID to edit (for updates)")
    args = parser.parse_args()

    jsonl_path = get_jsonl_path(args.session_id)
    message_id = args.message_id

    print(f"Monitoring: {jsonl_path}")
    print(f"Target: {args.platform}:{args.chat_id}")
    print(f"Interval: {args.interval}s")
    print("---")

    last_state = None

    while True:
        try:
            state = parse_jsonl(jsonl_path)
            status_text = format_status(state)

            # Only send if state changed
            if status_text != last_state:
                print(f"[{time.strftime('%H:%M:%S')}] {state.get('entry_type', '?')} - {state.get('tool', state.get('text', '')[:50] if 'text' in state else '')}")

                if message_id:
                    # Edit existing message
                    from gateway.platform_adapter import PlatformAdapter
                    # This is simplified - actual implementation needs proper adapter init
                    print(f"Would edit message {message_id}: {status_text[:50]}...")
                else:
                    # Send new message
                    print(f"Would send: {status_text[:50]}...")

                last_state = status_text

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
