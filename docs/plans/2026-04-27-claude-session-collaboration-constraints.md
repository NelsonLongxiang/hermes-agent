# Claude Session 协作约束实施计划

> **For Claude:** 使用 subagent-driven-development 技能逐任务实施。

**目标：** 在 claude_session 工具描述中增加协作约束，减少 Hermes 的微观管理行为。

**架构：**
1. 在 `CLAUDE_SESSION_SCHEMA["description"]` 中增加协作原则
2. 在 `manager.py` 的 `cancel_input()` 方法中增加审计日志

**涉及文件：**
- `tools/claude_session_tool.py` — 工具描述
- `tools/claude_session/manager.py` — cancel 审计

---

## Task 1: 更新工具描述（协作约束）

**Objective:** 在 CLAUDE_SESSION_SCHEMA description 中增加协作原则

**Files:**
- Modify: `tools/claude_session_tool.py:295-312`

**Step 1: 读取当前描述**
定位 `CLAUDE_SESSION_SCHEMA` 的 description 字段（行 297-312）

**Step 2: 在 description 末尾追加协作约束段落**
在现有描述后添加：

```
\n\nCOLLABORATION PRINCIPLES (IMPORTANT):
- Claude is a COOPERATOR, not a subordinate
- After delegating a task via send(), trust Claude to complete it autonomously
- Avoid frequent checks/interrupts — this breaks collaboration efficiency
- Only cancel if Claude is truly stuck (no output for 10+ minutes)
- Before cancelling, analyze why Claude hasn't responded
- If you must cancel, note the reason — repeated cancellations indicate micromanagement
```

**Step 3: 验证修改**
```bash
grep -A 20 "COLLABORATION PRINCIPLES" tools/claude_session_tool.py
```

---

## Task 2: 增加 cancel 审计日志

**Objective:** 在 manager.py 的 cancel_input() 中记录审计日志

**Files:**
- Modify: `tools/claude_session/manager.py`

**Step 1: 找到 cancel_input 方法**
```bash
grep -n "def cancel_input" tools/claude_session/manager.py
```

**Step 2: 在 cancel_input 方法开头添加审计日志**
在方法体的第一行添加：

```python
def cancel_input(self):
    """Cancel any in-progress input in Claude Code."""
    # 审计日志：记录 cancel 操作
    logger.warning(
        "cancel_input called for session %s (state=%s). "
        "Repeated cancellations indicate micromanagement. "
        "Consider: is Claude really stuck, or am I being impatient?",
        self._session_id,
        self._sm.current_state if hasattr(self._sm, 'current_state') else 'UNKNOWN'
    )
    with self._lock:
        # ... existing code ...
```

**Step 3: 验证修改**
```bash
grep -A 5 "cancel_input called" tools/claude_session/manager.py
```

---

## Task 3: 提交更改

**Step 1: 检查更改**
```bash
git diff tools/claude_session_tool.py tools/claude_session/manager.py
```

**Step 2: 提交**
```bash
git add tools/claude_session_tool.py tools/claude_session/manager.py
git commit -m "feat(claude-session): add collaboration constraints to reduce micromanagement

- Add collaboration principles to tool description
- Add audit logging to cancel_input for repeated cancellation detection
"
```

---

## 验证步骤

1. 启动新会话：`claude_session(action="start", name="test-collab", workdir="/mnt/f/Projects/hermes-agent")`
2. 发送任务：`claude_session(action="send", message="echo test")`
3. 等待完成：`claude_session(action="wait_for_idle", timeout=30)`
4. 检查日志中是否有 "collaboration" 相关提示

---

## 注意事项

- 不要修改状态机（暂不采用 DELEGATED 状态）
- 不要修改核心接口（暂不采用 collaboration_score）
- 只做最小化改动，先看效果再迭代
