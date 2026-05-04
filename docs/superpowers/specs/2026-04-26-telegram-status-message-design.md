# Telegram Status Message Design

**Date:** 2026-04-26
**Scope:** Claude Code tmux mode — show real-time Claude activity status on Telegram
**Author:** Claude + NelsonLongxiang

## Problem

When Claude Code is working via tmux, the Telegram user sees no progress feedback — only the final result after minutes of silence. This is disorienting for long-running tasks.

## Goal

Display a single continuously-edited Telegram message showing Claude Code's current activity (state + tool calls + elapsed time), replaced with a summary when done.

## Constraints

- **No thinking content**: Claude Code TUI does not expose internal reasoning
- **500 char limit**: Telegram message display must fit within 500 chars
- **Rate limits**: Telegram throttles message edits (~30/min); need 2s minimum interval
- **Thread bridge**: Poller runs in sync thread; Telegram adapter is async

## Data Sources

| Source | Component | Data |
|--------|-----------|------|
| State | `StateMachine` | THINKING / TOOL_CALL / PERMISSION / IDLE |
| Tool info | `OutputParser` | `tool_name` + `tool_target` (e.g. `Read src/main.py`) |
| Recent output | `OutputBuffer.last_n_chars(200)` | Claude's intermediate text |
| Turn info | `Turn` | `turn_id`, elapsed seconds, tool call count |

## Architecture

```
AdaptivePoller (bg thread)
    └─ _poll_once() → StateMachine.transition()
        └─ _handle_state_change(transition)
            └─ _status_callback(status_dict)
                └─ bridge: asyncio.run_coroutine_threadsafe()
                    └─ StatusMessageManager.update()
                        └─ TelegramAdapter.edit_message()
```

## Components

### 1. StatusMessageManager (new: `gateway/status_message.py`)

Manages a single Telegram message that is continuously edited.

```python
class StatusMessageManager:
    def __init__(self, adapter, chat_id, metadata=None)
    async def update(self, status_info: dict) -> None
    async def finish(self, summary: str) -> None
    async def cancel(self) -> None  # remove status message on error
```

**Throttling rules:**
- Min 2.0s between edits
- Max 30 edits per turn
- 3 consecutive flood-control failures → permanently stop editing
- Only edit when state changes or new tool call detected

### 2. ClaudeSessionManager changes (`tools/claude_session/manager.py`)

Add a status callback field, called from `_handle_state_change`:

```python
# New field
self._status_callback: Optional[Callable[[dict], None]] = None

# In _handle_state_change, after existing logic:
if self._status_callback:
    self._status_callback(self._build_status_info())

# New helper
def _build_status_info(self) -> dict:
    return {
        "state": self._sm.current_state,
        "tool_name": ...,
        "tool_target": ...,
        "turn_id": ...,
        "elapsed_seconds": ...,
        "tool_calls": [...],
        "recent_output": self._buf.last_n_chars(200),
    }
```

### 3. claude_session_tool.py changes

Expose a global status observer registration:

```python
_status_observer: Optional[Callable[[str, dict], None]] = None

def register_status_observer(callback):
    global _status_observer
    _status_observer = callback

def unregister_status_observer():
    global _status_observer
    _status_observer = None
```

When creating a session, attach the observer as the session's `_status_callback`.

### 4. gateway/run.py integration

In `_run_agent`, before the agent starts, register the bridge:

```python
# Create StatusMessageManager for this turn
_status_msg_mgr = StatusMessageManager(adapter, chat_id, metadata)

# Bridge: sync poller callback → async Telegram edit
def _session_status_bridge(session_id: str, status_info: dict):
    if not _run_still_current():
        return
    asyncio.run_coroutine_threadsafe(
        _status_msg_mgr.update(status_info),
        _loop_for_step,
    )

from tools.claude_session_tool import register_status_observer
register_status_observer(_session_status_bridge)

# ... agent runs ...

# On completion:
await _status_msg_mgr.finish(summary_text)
unregister_status_observer()
```

## Message Formats

### THINKING
```
🤔 思考中...
分析项目结构，准备修改方案
⏱ 15s | turn 2
```

### TOOL_CALL
```
🔧 执行工具...
● Read src/main.py
● Edit auth.py — 修改权限
⏱ 25s | turn 2
```

### PERMISSION
```
⚠️ 等待授权...
Allow Bash: npm run build
⏱ 30s | turn 2
```

### Completion Summary
```
✅ 完成
● Read x2  ● Edit x1  ● Bash x1
⏱ 45s | 3 turns
```

## Text Formatting Rules

1. **State icon** (🤔/🔧/⚠️/✅) + state label — always first line
2. **Tool calls** — list recent tool names + targets (max 3)
3. **Recent output** — last 1-2 lines of meaningful text (if space allows)
4. **Footer** — `⏱ Xs | turn N` always last line
5. **Total ≤ 500 chars** — truncate from the middle if needed

## Error Handling

- Flood control: adaptive backoff (double interval, max 10s), then fallback
- Edit failure after creation: stop updating, let final message deliver normally
- StatusMessageManager never blocks the agent — all edits are fire-and-forget via run_coroutine_threadsafe

## Files Changed

| File | Change |
|------|--------|
| `gateway/status_message.py` | **New** — StatusMessageManager class |
| `tools/claude_session/manager.py` | Add `_status_callback` + `_build_status_info()` |
| `tools/claude_session_tool.py` | Add `register_status_observer()` / `unregister_status_observer()` |
| `gateway/run.py` | Register bridge in `_run_agent`, cleanup on completion |

## Testing Strategy

1. **Unit tests**: `StatusMessageManager` throttling, formatting, flood handling
2. **Unit tests**: `ClaudeSessionManager._build_status_info()` data extraction
3. **Unit tests**: `claude_session_tool.py` observer registration
4. **Integration test**: end-to-end flow with mock adapter

## Out of Scope

- Thinking content extraction (not available from TUI)
- AIAgent API mode status messages (separate feature)
- Other platforms (Discord, Slack) — can be added later with same StatusMessageManager
