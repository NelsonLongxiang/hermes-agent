---
name: claude-session
description: Guide for using claude_session tool to delegate coding tasks to Claude Code via tmux
version: 1.0
---

# Claude Session — Delegating Tasks to Claude Code

## When to Use

Use this skill when you need to delegate a coding task to Claude Code:
- Complex multi-file refactoring
- Running long test suites
- Tasks requiring deep codebase understanding
- Parallel work (you handle strategy, Claude handles implementation)

## Quick Start

1. **Start a session**:
   ```
   claude_session(action="start", workdir="/project", permission_mode="skip")
   ```

2. **Wait for ready**:
   ```
   claude_session(action="wait_for_idle", timeout=30)
   ```

3. **Configure permissions** (delete protection):
   ```
   claude_session(action="send", message="/permissions add Bash:rm* ask")
   claude_session(action="wait_for_idle", timeout=10)
   ```

4. **Send tasks and wait**:
   ```
   claude_session(action="send", message="Refactor the auth module")
   claude_session(action="wait_for_idle", timeout=300)
   ```

5. **Review and iterate**:
   ```
   claude_session(action="status")
   claude_session(action="output", limit=50)
   ```

## Permission Modes

| Mode | When to Use | Trade-off |
|------|------------|-----------|
| `skip` | Automated tasks, trusted operations | Claude auto-approves all actions |
| `normal` | Exploratory tasks, untrusted code | Claude asks for permission |

**Always configure delete protection** after start, regardless of mode:
```
/permissions add Bash:rm* ask
/permissions add Bash:del* ask
/permissions add Bash:git rm* ask
/permissions add Bash:git clean* ask
/permissions add Bash:kill* ask
/permissions add Bash:pkill* ask
/permissions add Bash:killall* ask
/permissions add Bash:mv* ask
/permissions add Bash:shred* ask
```

## Task Decomposition

Break large tasks into Claude-manageable pieces:
- Each `send` should be a focused, completable sub-task
- Use `wait_for_idle` after each send
- Check `output` for results before sending next task
- If Claude errors, retry once with clearer instructions

## State Awareness

The tool tracks 7 states:
- **IDLE**: Claude is waiting for input (you can send)
- **INPUTTING**: Text being typed (not yet sent)
- **THINKING**: Claude is reasoning
- **TOOL_CALL**: Claude is executing a tool (Read/Edit/Bash etc.)
- **PERMISSION**: Claude needs permission approval
- **ERROR**: Something went wrong
- **DISCONNECTED**: tmux session lost

Use `status` to check current state at any time.

## Error Recovery

1. Check `status` for ERROR state
2. Read `output` to understand the error
3. Send corrective instructions (max 2 retries)
4. If still failing, report to user

## Permission Handling (normal mode)

When `wait_for_idle` returns `PERMISSION` state:
1. Read `permission_request` to see what Claude wants to do
2. **Safe operations** (Read, Grep, Glob) → auto-allow
3. **Modification** (Edit, Write) → auto-allow
4. **Delete operations** (rm, del, git rm) → report to user
5. **Dangerous** (rm -rf /, format) → auto-deny

## Session Lifecycle

- **Start fresh** for each major task
- **Stop** when done to free resources
- Monitor turn history for context window limits
- If Claude seems confused, stop and restart

## API Reference

### start
```
claude_session(action="start", workdir="/path", session_name="claude-work",
               model="sonnet", permission_mode="skip", on_event="notify")
```

### send (atomic)
```
claude_session(action="send", message="Fix the auth bug")
```

### type + submit (two-phase)
```
claude_session(action="type", text="First line")
claude_session(action="submit")
```

### cancel_input
```
claude_session(action="cancel_input")
```

### status
```
claude_session(action="status")
```

### wait_for_idle
```
claude_session(action="wait_for_idle", timeout=300)
```

### wait_for_state
```
claude_session(action="wait_for_state", target_state="TOOL_CALL", timeout=60)
```

### output
```
claude_session(action="output", offset=0, limit=50)
```

### respond_permission
```
claude_session(action="respond_permission", response="allow")
```

### history
```
claude_session(action="history")
```

### events
```
claude_session(action="events", since_turn=2)
```

### stop
```
claude_session(action="stop")
```
