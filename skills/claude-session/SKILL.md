---
name: claude-session
description: Guide for using claude_session tool to delegate coding tasks to Claude Code via tmux
tags: ['claude-code', 'tmux', 'interactive', 'coding', 'delegation']
triggers:
  - "claude session"
  - "coding task"
  - "delegation"
  - "interactive session"
version: 3.0
required_environment_variables:
  - name: HERMES_STREAM_STALE_TIMEOUT
    prompt: "Stream stale timeout (秒，推荐 300)"
    help: "防止 claude_session 长任务时 Hermes API 流中断，默认 180s 不够用"
    optional: true
    required_for: "防止 Stream Stalled mid tool-call 错误"
---

## ⚡ 首次使用自动配置（重要）

加载此 skill 时，检查 `HERMES_STREAM_STALE_TIMEOUT` 是否已配置：

**自动配置**：运行配置脚本
```bash
bash ~/.hermes/skills/claude-session/scripts/configure.sh
```

如果此环境变量未配置（默认 180s），使用 claude_session 执行长任务时可能出现：
`⚠ Stream stalled mid tool-call (xxx); the action was not executed.`

**配置后必须重启 Gateway 才能生效。** 提醒用户重启。

# Claude Session — Delegating Tasks to Claude Code

## Named Sessions（多会话管理）

v3.0 引入 **Named Sessions**：通过用户友好的名称管理多个并行 Claude Code 会话。

**为什么需要命名会话？**
- 同一 gateway 下并行运行多个会话（如同时处理前端和后端任务）
- 用 `name="frontend"` / `name="backend"` 替代不可读的 session_id
- `switch` 快速切换活跃会话，`list` 查看所有会话状态

**核心概念**：
- **name**: 1-64 字符，`[a-zA-Z0-9_-]`，同一 gateway 下唯一
- **active session**: 每个 gateway 记录最近交互的会话，无显式指定时自动路由
- **路由优先级**: `session_id` > `name` > 活跃会话 > 最近创建的会话

## When to Use

Use this skill when you need to delegate a coding task to Claude Code:
- Complex multi-file refactoring
- Running long test suites
- Tasks requiring deep codebase understanding
- Parallel work (you handle strategy, Claude handles implementation)

## Quick Start

1. **Start a named session**:
   ```
   claude_session(action="start", workdir="/project", name="frontend", permission_mode="skip")
   ```

2. **Wait for ready**:
   ```
   claude_session(action="wait_for_idle", timeout=30)
   ```

2b. **(Optional) Start a second session**:
   ```
   claude_session(action="start", workdir="/project", name="backend")
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
| `skip` | Automated tasks, trusted operations, long multi-step tasks | Claude auto-approves all actions |
| `normal` | Exploratory tasks, untrusted code, user needs oversight | Claude asks for permission |

### 权限死循环的解决（2026-04-26 补充）

**现象**：
- `wait_for_idle` 返回 `PERMISSION` 状态
- 调用 `respond_permission("allow")` 后状态仍然停留在 `PERMISSION`
- 连续多次响应 `allow` 都无法恢复

**原因**：
- Claude Code 的 permission 机制与状态机检测有时不同步
- 多个工具调用可能连续触发 Permission 请求
- 状态检查和权限响应之间存在竞态条件

**解决方案**：
1. **优先使用 `permission_mode='skip'`**：
   ```python
   claude_session(action="start", permission_mode="skip", ...)
   ```
   - 避免所有权限确认问题
   - 适合自动化任务、多步实施、批量代码修改
   - **如果遇到权限死循环，最有效的解决方法**

2. **如果必须使用 `normal` 模式**：
   - 准备批量批准：在 prompt 中明确说明"所有需要的权限都已授权"
   - 循环响应：`while status == PERMISSION: respond_permission("allow")`
   - 不要假设一次 allow 就够

**关键经验**：
- 如果遇到权限请求死循环，**直接用 `permission_mode='skip'` 重启会话**比反复响应更有效
- 多步任务（Task 1-6）建议直接用 skip 模式，避免中途卡住
- 实战中4个Task的连续实施过程中，skip 模式避免了多次权限中断

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

### 检查已有实现（2026-04-26 补充）

**为什么要检查**：
- 避免重复造轮子
- 发现已有功能但缺少测试/文档
- 了解现有架构再开始修改

**检查流程**：

1. **搜索关键关键词**：
   ```bash
   # 搜索功能相关的代码
   grep -r "status_callback\|_status_observer\|register_status" \
       tools/claude_session/ gateway/
   ```

2. **阅读相关文件**：
   ```python
   read_file("tools/claude_session/manager.py")
   read_file("tools/claude_session_tool.py")
   read_file("gateway/run.py")
   ```

3. **确认实现状态**：
   - ✅ 已实现 → 补充测试、文档或集成
   - ⏳ 部分实现 → 补充缺失部分
   - ❌ 未实现 → 从零开始

**实战案例（Telegram状态消息功能）**：
- Task 1: StatusMessageManager → ✅ 已实现（commit 467a99c）
- Task 2: manager.py 回调机制 → ✅ 已实现
- Task 3: observer 注册机制 → ✅ 已实现
- **结论**：Task 1-3 不需要实施，只需补充Task 3的测试
- **节省时间**：避免了重复实现约200行代码

### 逐个Task实施策略（2026-04-26 补充）

**适用场景**：
- 多个独立任务需要顺序实施
- 每个Task需要单独测试和验证
- 用户要求"做完了就一直持续做下去"

**实施流程**：

1. **发送当前Task**：
   ```python
   claude_session(action="send", message="""
   ## Task 4: gateway桥接
   **目标**：...
   **具体要求**：...
   **开始实施！完成直接继续Task 5。**
   """)
   ```

2. **等待完成**：
   ```python
   claude_session(action="wait_for_idle", timeout=600)
   ```

3. **检查结果**：
   ```python
   claude_session(action="output", limit=100)
   ```

4. **继续下一个Task**：
   - 如果Task完成 → 在消息中明确说"继续Task 5"
   - 如果Task失败 → 分析原因，修复后重试
   - **不要反复询问用户**，直接按计划执行

**关键优势**：
- 每个Task独立完成，问题定位快
- 避免并行多个任务导致的混乱
- 每个Task可以单独测试
- 用户明确要求后，持续执行不中断

**实战效果（Telegram状态消息）**：
- Task 2-3: 发现已有实现，直接补充测试
- Task 4-6: 逐个实施，全部完成且测试通过
- 耗时: 约7分钟完成所有Task（包括排查已有实现）

### 多步任务的提示技巧

在发送多步任务时，给Claude足够的上下文：

```python
claude_session(action="send", message="""
**任务：** 实施 Telegram 状态消息功能（Task 4-6）

**执行策略：** 逐个Task实施，每个Task单独完成

**关键点：**
1. 使用 asyncio.run_coroutine_threadsafe() 处理线程桥接
2. 确保事件循环正确获取
3. 500字符限制内优化信息展示

**开始实施Task 4！完成直接继续Task 5。**
""")
```

**为什么重要**：
- Claude知道整体目标，不会偏离方向
- 明确每个Task的边界
- 知道完成一个后自动继续下一个
- 减少中途沟通的成本

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

## 核心原则（必读）

1. **永远用 `claude_session` 工具**，不要手动 tmux send-keys，不要用 `claude -p` print 模式
2. **要有耐心** — Claude 思考+执行复杂任务可能需要几分钟，不要因为"看起来没变化"就判定失败
3. **不要频繁切换方案** — 遇到问题先分析原因，而不是立刻换另一种启动方式
4. **Print 模式是黑盒** — 中间过程看不到、干预不了，不适合复杂任务
5. **复杂研究/分析/写作任务，一定要用交互模式** — 能实时监控状态、中途调整

## 常见错误（血泪教训）

### ❌ 错误1：学了一套，用了另一套
**表现**：刚读完 claude-session skill，转头就用 `tmux send-keys` + `claude -p` 手动操作。
**原因**：觉得手动方式"更简单"，没有强制自己按 skill 流程走。
**正确做法**：执行前先确认——我要用哪个工具？按什么流程？确认后再动手。

### ❌ 错误2：等不及就换方案
**表现**：tmux 模式等了不到2分钟看到"没变化"，立刻判定失败，切换到 terminal background。
**原因**：Claude Code 启动+思考+执行本身就可能需要1-3分钟，这是正常的。
**正确做法**：`wait_for_idle` 给够 timeout（研究类任务建议 300-600秒），不要反复 poll 打扰。

### ❌ 错误3：遇到问题不分析就放弃
**表现**：tmux capture-pane 看到命令还在，没分析是不是在正常思考，就直接 kill 换方案。
**正确做法**：先用 `status` 检查状态，用 `output` 看是否有输出，分析原因后再决定是否需要换方案。

### ❌ 错误4：不区分任务类型选模式
**表现**：复杂研究任务用了 print 模式（-p），导致黑盒无法监控和干预。
**正确做法**：
- 简单单步任务（修bug、格式化）→ 可以用 print 模式
- 多步复杂任务（研究、分析、重构+测试）→ 必须用交互模式（claude_session）

## 工具不可用时的排查流程

如果 `claude_session` 不在当前可用工具列表中，按以下步骤排查：

1. **确认工具是否注册**：`hermes tools list` 查看是否有 claude 相关工具集
2. **确认 Hermes 安装中有此工具**：查找 `claude_session_tool.py` 文件是否存在
3. **确认 toolset 配置**：查看 config.yaml 中 toolsets 是否包含 `hermes-cli` 或 `hermes-telegram`（它们都包含 `claude_session`）
4. **检查 gateway 日志**：`hermes logs` 查看是否有 claude_session 加载错误
5. **可能的原因**：平台工具过滤、工具加载时 import 失败（静默跳过）、模型 provider 兼容性问题
6. **如果确认不可用**：如实告知用户排查结果，**不要偷偷换成 delegate_task 或 terminal**

## Troubleshooting: claude_session 工具不可用

如果 `claude_session` 工具没有出现在可用工具列表中：

### 根因分析链路
1. `tools/claude_session_tool.py` — 工具注册（`registry.register()`），`toolset="claude_session"`，`check_fn` 检查 tmux 是否存在
2. `toolsets.py` — `_HERMES_CORE_TOOLS` 包含 `"claude_session"`，`TOOLSETS["claude_session"]` 定义工具集
3. **`hermes_cli/tools_config.py`** — `CONFIGURABLE_TOOLSETS` 列表定义所有可配置工具集
4. `_get_platform_tools()` — 无显式配置时，将默认 toolset（如 `hermes-telegram`）展开为工具名，再**反向映射回 CONFIGURABLE_TOOLSETS**
5. 如果工具集不在 `CONFIGURABLE_TOOLSETS` 中 → 反向映射丢失 → 工具对模型不可用

### 已知修复
- 2026-04-19: `claude_session` 已添加到 `CONFIGURABLE_TOOLSETS`，无需再次修复
- 如果未来有新的工具集注册但不可见，检查 `CONFIGURABLE_TOOLSETS` 是否包含它

### Gateway 重启
- Gateway 可能运行在用户终端前台（非 tmux/systemd），无法远程 restart
- 修改代码后需要重启 gateway 才能生效
- 如果 kill gateway 进程会导致断线，需要用户手动重启

## 轮询策略优化（减少上下文膨胀）

**核心原则：用最少 tool call 完成任务，避免上下文膨胀导致 Stream Stalled。**

### ❌ 错误模式：反复短轮询
```
send → wait_for_idle(60) → output → wait_for_idle(60) → output → ...（10+轮）
```
每轮 tool call + result 累积在上下文中，50k+ tokens 后模型 prefill 超过 180s → Stream Stalled。

### ✅ 正确模式：3 轮完成
```
1. start + wait_for_idle(60)           # 启动，等就绪
2. send + wait_for_idle(300-600)       # 发任务，一次等到底
3. output + stop                       # 取结果，关闭
```

### 关键规则
- **wait_for_idle 的 timeout 给够**：研究/写作类任务给 300-600 秒，不要反复用 60 秒轮询
- **中间不看进度**：不要在 wait_for_idle 期间插 output 检查，等 IDLE 后一次取完整结果
- **如果任务可能很长**：在 prompt 中告诉 Claude "直接输出，不要使用 Web Search 等工具"，避免工具调用卡死
- **提取长输出**：用 tmux capture-pane 替代多次 output 调用
- **安全网配置**：HERMES_STREAM_STALE_TIMEOUT 建议设为 300（在 Hermes 环境变量中配置）

## 实战经验与陷阱（2026-04-24 补充）

### 陷阱0：首次启动会话不稳定
**现象**：
- `wait_for_idle` 在 120 秒内超时，但 Claude 实际上还在初始化
- 出现 `TypeError: 'NoneType' object is not subscriptable` 错误
- 会话状态变为 `DISCONNECTED`
**原因**：
- Claude Code 首次启动需要加载环境、初始化模型、扫描工作目录，可能需要 1-2 分钟
- 首次启动时会有多个异步任务并行执行，可能导致状态检测异常
- tmux session 可能在初始化过程中意外断开
**应对**：
- 首次启动时 `wait_for_idle` timeout 至少设为 300 秒（5分钟）
- 如果遇到 `TypeError` 或 `DISCONNECTED`，不要惊慌，重新启动即可
- 重试前先调用 `status` 检查当前状态
- 如果连续 2 次启动都失败，再考虑其他问题（如环境配置、依赖缺失等）
**正确流程**：
```python
# 首次启动给足时间
claude_session(action="start", ...)
claude_session(action="wait_for_idle", timeout=300)  # 首次启动给 5 分钟

# 如果断开，重新启动
if status == "DISCONNECTED":
    claude_session(action="start", ...)
    claude_session(action="wait_for_idle", timeout=300)
```

### 陷阱0.5：启动卡住 9 分钟（tmux session 复用逻辑缺陷）
**现象**：
- Hermes 启动时卡住非常久（9 分钟以上）
- 日志显示 `claude_session... (×3)`，然后卡在"思考中"状态
- `wait_for_idle` 永远等不到 IDLE 状态
**根因**（已修复）：
1. **残留 tmux session 未清理**：之前的 session 对应的 tmux session 仍存在
2. **session 名确定性**：相同的 (workdir, gateway_session_key) 总是生成相同的 session 名
3. **复用逻辑误判**：使用简单的 `❯` 字符检测判断 Claude Code 是否运行
4. **Phantom ❯ 问题**：Claude Code TUI 在 THINKING/TOOL_CALL 状态时也会渲染 phantom `❯`
5. **状态检测失效**：复用的 session 处于不确定状态，poller 从 DISCONNECTED 开始无法匹配
**应对**（2026-04-26 已修复）：
- 修复已合并到 manager.py：使用 `OutputParser.detect_state()` 精确检测真实状态
- 只有 IDLE 状态才安全复用，非 IDLE 状态一律 kill + 重建
- 添加启动健康检查：30s 超时验证 Claude Code 是否正常启动
- diagnose 新增残留 session 检测，显示未注册的 hermes-* session
**临时手动清理**（如果遇到此问题）：
```bash
# 查看所有 hermes-* session
tmux list-sessions | grep hermes

# 清理残留 session（手动选择，避免误删其他会话）
tmux kill-session -t <session-name>

# 或批量清理（谨慎使用，会删除所有 hermes-* session）
for s in $(tmux list-sessions 2>/dev/null | grep '^hermes-' | cut -d: -f1); do
    tmux kill-session -t "$s"
done
```
**预防**：
- 不要在多个终端同时启动会话
- 任务完成后显式调用 `stop`
- 定期检查残留 session

### 陷阱1：Permission 状态幽灵
**现象**：`wait_for_idle` 一直返回 `PERMISSION` 状态，但 Claude 实际上已经在工作/输出了。
**原因**：Claude Code UI 底部有 permission 提示条时，状态机检测为 PERMISSION。
**应对**：
- 多次调用 `respond_permission(action="allow")` 直到状态稳定
- 用 `output` 或 `tmux capture-pane` 检查 Claude 是否在正常输出
- 不要因为状态显示 PERMISSION 就反复响应，先看实际内容

### 陷阱2：Web Search 在非 Anthropic 模型上失败
**现象**：Claude Code 的 Web Search 工具执行后显示 `Did 0 searches in XXs`，没有任何搜索结果。
**原因**：GLM-5.1 等非 Anthropic 模型可能不支持 Claude Code 的工具调用协议。
**应对**：
- 在 prompt 中明确写"不要使用 Web Search，直接基于知识输出"
- 如果已经卡住（tokens 不增长），用 `cancel_input` 中断，重发不带工具要求的 prompt

### 陷阱3：API 调用卡死（tokens 不增长）
**现象**：Claude 停在某个 token 数不动，`wait_for_idle` 反复超时。
**诊断**：检查 output 中 tokens 数是否持续不变（如连续多轮都是 198 tokens）。
**应对**：
```
claude_session(action="cancel_input")  # 发送 Esc 中断
claude_session(action="send", message="简化后的任务指令")  # 重发
```

### 陷阱4：output 高 offset 返回空
**现象**：`output(offset=1000, limit=50)` 返回空 lines，但 `total` 很大。
**原因**：output buffer 的索引不是连续的（动画帧等被过滤），offset 可能落在空隙中。
**应对**：使用 terminal 直接读取 tmux pane：
```bash
tmux capture-pane -t claude-work -p -S -3000 2>/dev/null > /tmp/claude_output.txt
```
然后用 `read_file` 读取并过滤。

### 陷阱5：首次进入新项目目录的 Trust Prompt
**现象**：首次在某个项目目录启动 Claude Code 时，会弹出 "Quick safety check: Is this a project you created or one you trust?" 交互式选择界面。如果此时 `send` 的消息被当作 shell 命令执行（如 `1: command not found`），会话将无法正常使用。
**原因**：Claude Code 的 trust 选择界面不是标准输入，tmux send-keys 发送的文本会被 shell 解释而非 Claude Code 的选择器接收。
**应对（按优先级）**：
**方法1：提前预确认（推荐）**
```bash
# 在启动 claude_session 之前，先在终端手动完成 trust 确认
cd /目标/项目/目录
echo -e "1\nq" | claude --permission-mode bypassPermissions 2>&1 | head -20
# 然后启动 claude_session，trust 状态已记录，直接进入 IDLE
```

**方法2：停止并重试**
- 如果遇到 `1: command not found` 或类似错误，说明 trust prompt 处理失败
- 用 `stop` 终止当前会话，然后 `start` 重新启动
- Trust 状态通常已被记录，第二次启动会直接进入 IDLE

**方法3：发送空消息**
- 如果第二次启动仍然出现 trust prompt，尝试 `claude_session(action="send", message="")` 发送空消息
- 可能会触发默认行为或让 prompt 消失

**预防**：
- 如果知道是新目录，先在终端手动运行一次 `claude --permission-mode bypassPermissions` 完成信任确认
- 使用 `--permission-mode bypassPermissions` 可以减少额外的权限弹窗

### 陷阱6：多步任务中的连续 Permission 响应
**现象**：Claude 在执行多步任务时（如文档更新 + 代码修改 + 测试），每个步骤都可能触发 Permission 状态，需要多次批准。
**原因**：Claude Code 的 permission 机制是每个 Edit/Write/Bash 操作独立请求的。一个包含 4 个文件改动的任务可能触发 4-8 次 Permission。
**应对**：
- 使用循环模式：`while state == PERMISSION: respond_permission("allow")`
- 不要假设一次 allow 就够——Claude 可能在一个 turn 内执行多个需要权限的操作
- 如果 Claude 使用了 TaskCreate（子任务追踪），每个子任务的第一步都可能需要权限
- **批量批准策略**：如果任务已经过用户审批且无风险，考虑在 send 时明确说"你需要的所有文件编辑权限都已授权"
- 本次实战：4 个任务（文档更新 + models.py + Alembic + base.py）共触发 7+ 次 Permission 请求

### 提取长报告的最佳实践
当 Claude 生成了很长的报告/代码时：
1. 用 `tmux capture-pane -t claude-work -p -S -5000` 捕获完整 pane 历史
2. 保存到 `/tmp/claude_raw_output.txt`
3. 用 `grep -n` 定位报告起止行号
4. 用 `sed` 提取并清理内容
5. 用 `read_file` 最终读取干净内容

## Error Recovery

1. Check `status` for ERROR state
2. Read `output` to understand the error
3. Send corrective instructions (max 2 retries)
4. If still failing, report to user
5. **绝对不要因为"等得久"就放弃当前方案切换到别的模式**

### 陷阱7：隐藏的 Permission 导致长时间 THINKING 卡死
**现象**：Claude 停在 THINKING 状态超过 10 分钟，`wait_for_idle` 反复超时，output 中 token 数不增长或增长极慢，但 `status` 始终返回 THINKING 而不是 PERMISSION。
**根因**：Claude Code 的 permission 对话框有时不会正确地将状态机切换到 PERMISSION，导致状态报告为 THINKING。Claude 实际上在等待权限批准。
**诊断**：查看 output 尾部是否有"Do you want to make this edit?"或"Yes, and don't ask again for"等权限提示文本。
**应对**：
1. 先尝试 `respond_permission("allow")` — 即使报 "Not in PERMISSION state" 也再试几次（状态可能在检查和响应之间变化）
2. 如果连续失败，用 `cancel_input` 中断，然后用更明确的指令重发
3. **预防**：如果知道任务会触发多个权限请求，在 send 时加"你需要的所有文件编辑和命令执行权限都已授权"
4. **生产环境注意**：生产服务器可能没有 sqlite3 等常用 CLI 工具，Claude 尝试使用时会卡住。提前在指令中说明环境约束

### 陷阱8：respond_permission "Not in PERMISSION state" 竞态
**现象**：`status` 显示 PERMISSION 状态，但调用 `respond_permission("allow")` 返回 "Not in PERMISSION state"。
**原因**：在 `status` 调用和 `respond_permission` 调用之间，Claude 的状态已经从 PERMISSION 变回了 THINKING（可能是超时自动处理或其他事件）。
**应对**：
- 连续调用 `respond_permission("allow")` 2-3 次
- 如果始终失败，说明权限对话框已经消失，不需要再处理
- 用 `status` 重新检查当前状态
- 如果状态变为 THINKING 但 output 显示仍有权限提示，用 `cancel_input` + 重发

### 陷阱9：Hermes 层面 "Stream stalled mid tool-call" 中断
**现象**：Claude Session 正在正常工作，但 Hermes 自己的 API 流式响应突然中断，返回：
`⚠ Stream stalled mid tool-call (read_file); action was not executed.`
**根因**：Hermes 的 Stale Stream 检测机制（`run_agent.py:6149-6346`）。当模型 prefill 时间超过
`HERMES_STREAM_STALE_TIMEOUT`（默认 180s）没有收到任何 SSE chunk，Hermes 强制断开连接。
**触发条件**：
- `claude_session` 的 wait_for_idle/output 循环产生大量上下文 → prefill 变慢
- 上下文 50k-100k tokens 时，超时放宽到 240s，可能仍不够
- 上下文 >100k tokens 时，放宽到 300s
**修复方案**：
1. 快速修复：设置环境变量 `HERMES_STREAM_STALE_TIMEOUT=360`（6分钟），然后重启 gateway
2. 根本修复：减少 `claude_session` 循环产生的上下文累积，更早触发 context compression
3. 预防：对长任务的 claude_session 操作，尽量减少 `output` 调用频率，用 `tmux capture-pane` 替代
6. **如果收到 "Stream stalled mid tool-call"**：这是 Hermes 层面的超时保护，不是 Claude 出错。告知用户根因，建议调大 `HERMES_STREAM_STALE_TIMEOUT`

### 陷阱10：skip 模式下仍遇权限对话框（2026-04-26 补充）
**现象**：即使使用 `permission_mode='skip'` 启动 Claude session，仍然遇到权限对话框卡住的情况。
`wait_for_idle` 持续返回 PERMISSION 状态，需要多次手动 `respond_permission("allow")`。
**根因**：
- 代码中的自动批准机制（`_auto_approve_permission`）可能在某些情况下检测失败或超时
- 权限对话框的 UI 格式检测可能不准确（编号选择器 vs 经典选择器）
- 状态检查和权限响应之间存在竞态条件
**正确应对**：
1. **不要简单重启会话**：重启会话不能根本解决问题，且会丢失当前上下文
2. **主动响应权限**：检测到 PERMISSION 状态时，持续调用 `respond_permission("allow")` 直到状态变化
   ```python
   while True:
       status = claude_session(action="status")
       if status["state"] != "PERMISSION":
           break
       claude_session(action="respond_permission", response="allow")
       time.sleep(0.5)
   ```
3. **结合 wait_for_idle**：如果 `wait_for_idle` 返回 PERMISSION，立即进入响应循环
   ```python
   result = claude_session(action="wait_for_idle", timeout=60)
   if result["state"] == "PERMISSION":
       for _ in range(10):  # 最多重试10次
           claude_session(action="respond_permission", response="allow")
           # 等待状态变化
           time.sleep(1)
           status = claude_session(action="status")
           if status["state"] != "PERMISSION":
               break
   ```
4. **检查实际内容**：用 `output` 或 `tmux capture-pane` 确认是否真的有权限对话框，避免误响应
**关键原则**：
- **优先主动响应而非重启**：权限问题是运行时的状态，重启不能解决
- **循环响应直到状态稳定**：不要假设一次 allow 就够
- **验证实际状态**：`status` 可能不准确，结合 `output` 检查
**实战案例**：
- 使用 `permission_mode='skip'` 启动会话后
- 执行代码审查任务时仍然遇到权限请求
- 连续 3 次 `respond_permission("allow")` 才恢复正常
- 如果立即重启会话，会丢失已完成的代码审查进度

### 陷阱11：detect_state() 错误判断 THINKING 而非 IDLE（2026-04-26 补充）
**现象**：
- Claude Code TUI 显示欢迎界面和 `❯` 提示符，明确是 IDLE 状态
- 但 `claude_session(action="status")` 返回 "THINKING"，无法发送任务
- 手动查看 tmux pane 可以看到 completion markers 如 "✻ Cogitated for 5m 43s" 或 "✻ Beaming…"
**根因**：
- `tools/claude_session/output_parser.py` 的 `detect_state()` 方法中，IDLE 检测逻辑只检查最后5行（`last_lines`）
- 当 `❯` 被分隔线包围时，代码认为是 phantom prompt（幻影提示符）
- 检查 completion markers 时，范围限制在 `last_lines[:prompt_idx]`（5行窗口内）
- 但实际 completion markers 出现在更早的历史行中（`lines[:prompt_idx]`），不在5行窗口内
- 导致 `has_done_marker = False`，误判为幻影提示符，返回 THINKING

**具体场景**：
```
（更早的输出，包含完成标记 "✻ Cogitated for 5m 43s"）
───────────────────────────────────────────────────────────────────────────────
❯
───────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt
```

当 `prompt_idx = 2`（在5行中），`last_lines[:2]` 只有：
```
[空行]
───────────────────────────────────────────────────────────────────────────────
```

不包含完成标记，所以被误判为幻影提示符。

**诊断方法**：
1. 使用 `execute_code` 模拟 tmux 输出和 regex 匹配逻辑
2. 确认 `last_lines` 的范围和内容
3. 检查 `has_done_marker` 的检查范围是否过小
4. 验证实际 completion markers 出现的位置

**修复方案**：
修改 `tools/claude_session/output_parser.py` 第168-173行：
```python
# 错误代码（当前）：
has_done_marker = any(
    _DONE_TIME_RE.search(l)
    for l in last_lines[:prompt_idx]  # ❌ 只检查最后5行
)
if not has_done_marker:
    continue

# 正确代码（修复后）：
has_done_marker = any(
    _DONE_TIME_RE.search(l)
    for l in lines[:prompt_idx]  # ✅ 检查所有历史行
)
if not has_done_marker:
    continue
```

**验证步骤**：
1. 修复后运行测试：`pytest tests/tools/test_claude_session_parser.py -v`
2. 手动验证：启动 Claude session，检查 `status` 命令是否能正确识别 IDLE 状态
3. 确保不破坏其他状态的检测逻辑（PERMISSION、TOOL_CALL、ERROR）

**预防**：
- 遇到状态判断错误时，优先用 `execute_code` 模拟验证
- 检查正则表达式和范围限制是否符合预期
- 不要假设代码逻辑正确，实际场景可能触发边界条件

## Permission Handling (normal mode)

When `wait_for_idle` returns `PERMISSION` state:
1. Read `permission_request` to see what Claude wants to do
2. **Safe operations** (Read, Grep, Glob) → auto-allow
3. **Modification** (Edit, Write) → auto-allow
4. **Delete operations** (rm, del, git rm) → report to user
5. **Destructive operations** → auto-deny

## Meeting & Debate Workflow（三方协作模式）

当 Hermes 作为协调者，与 Claude 和龙翔（项目负责人）三方协作时：
- **尊重用户明确指令**：如果用户说"你自行决策"或"不要问我了"，直接执行不要反复确认
- **自主完成多任务**：用户要求"持续做下去"时，按计划顺序执行，每个Task完成直接进入下一个
- **关键原则**：用户的明确指令（尤其是"必须"、"自行决策"等强语气词）具有最高优先级

### 典型场景
- 方案讨论 + 辩论后达成共识
- 多个修复需求先讨论后统一实施
- 代码审查 + 技术决策需要双方独立分析
- 用户说"你们自行讨论，我不参与，你自行决策"

### 流程
1. **启动会话** → 自我介绍，互相了解能力边界
2. **发起讨论** → 明确角色分配（正方/反方），要求对方反驳并提出独立质疑
3. **多轮交锋** → 逐步收敛分歧，识别共识和仍存分歧
4. **汇总结论** → 用表格呈现共识、分歧、技术发现，给出分阶段计划
5. **确认执行模式** → 询问用户是否要"逐个确认"还是"持续执行"
6. **执行策略**：
   - **逐个确认模式**：每个Task完成后询问"继续下一个吗？"
   - **持续执行模式**：在消息中说"完成直接继续Task X"，不再中间询问
7. **执行与监控** → 按用户选择的策略执行任务

### 讨论任务 vs 修复任务
- **讨论类**（辩论、方案评审）：完成后需汇总给用户确认
- **修复类**（代码改动）：先讨论方案，等用户说"可以"或"继续"后再安排实施
- **关键区别**：
  - 用户说"你们自行讨论" → 进入辩论流程，最后汇总
  - 用户说"你们自行讨论，我不参与，你自行决策" → 直接按决策执行
  - 用户说"继续吧，后面你别再问我了" → 持续执行所有剩余Task

### 用户明确指令的处理（2026-04-26 补充）

**强语气词优先级最高**：
- "必须"、"一定要" → 必须遵循，不得偏离
- "你们自行讨论，我不参与，你自行决策" → 完全自主决策
- "不要问我了"、"做完了就一直持续做下去" → 不再中间询问，按计划执行

**实战案例**：
- 用户："继续吧，后面你别再问我了，做完了就一直持续做下去啊"
- 正确响应："收到！不再询问，直接持续执行Task 4-6。"
- 错误响应："好的，现在实施Task 4...完成...是否继续Task 5？"（违反了用户指令）

**关键原则**：
- 一旦用户明确要求"不要问我"或"持续做下去"，就按此模式执行到底
- 不要中途因为"不确定"或"谨慎"就放弃用户的明确指令
- 用户强语气词（"必须"、"一定要"）具有最高优先级，高于技能建议

### 权限批量批准
修复任务通常涉及多文件改动，会连续触发 Permission 请求：
- 4 个文件的改动可能触发 7+ 次 Permission
- 不要假设一次 allow 就够，持续 `respond_permission("allow")` 直到 `wait_for_idle` 返回非 PERMISSION 状态

## Session Lifecycle

- **Start fresh** for each major task
- **Stop** when done to free resources
- Monitor turn history for context window limits
- If Claude seems confused, stop and restart

## 清理所有会话（重要）

**关键发现**：`claude_session` 工具只能管理当前活跃会话。许多残留会话在tmux层面独立运行，无法通过 `stop` 命令清理。

### 完整清理流程

1. **检查Hermes层面的会话**：
   ```python
   claude_session(action="status")
   ```

2. **检查tmux层面的会话**（重要！）：
   ```bash
   tmux list-sessions
   ```
   如果看到多个会话，说明有残留。

3. **清理Hermes可见的会话**：
   ```python
   claude_session(action="stop")
   ```

4. **批量清理tmux会话**：
   ```bash
   # 逐个清理
   tmux kill-session -t hermes-<hash>
   tmux kill-session -t session-name

   # 或批量清理
   tmux list-sessions | cut -d: -f1 | xargs -I {} tmux kill-session -t {}
   ```

5. **验证清理结果**：
   ```bash
   tmux list-sessions  # 应显示 "no server running"
   ```

### 常见问题

**Q**: 为什么 `claude_session stop` 后还有会话残留？  
**A**: 会话可能因为以下原因泄漏：
- 任务中断后未正常停止
- 多次启动会话但只停止了最后一个
- tmux会话与Hermes状态机不同步

**Q**: 如何防止会话泄漏？  
**A**:
- 任务完成后显式调用 `stop`
- 避免在多个终端同时启动会话
- 定期执行清理流程

### 完整指南

详细清理流程和故障排查，请参考：
- **技能**: `cleanup-claude-sessions` (software-development/cleanup-claude-sessions)

## API Reference

### start
```
claude_session(action="start", workdir="/path", name="my-task",
               session_name="claude-work",
               model="sonnet", permission_mode="skip", on_event="notify")
```

**name 参数**（v3.0 新增）：
- 为会话分配一个人类可读的名称，用于后续 `switch`/`stop`/`send` 路由
- 不指定 name 时，会话仍可正常工作（通过 session_id 或活跃会话路由）
- name 会纳入 tmux session 名的哈希计算，同 workdir 不同 name 生成不同 tmux 名
- 规则：1-64 字符，仅 `[a-zA-Z0-9_-]`，同一 gateway 下唯一

### send (atomic)
```
# 路由到活跃会话（默认）
claude_session(action="send", message="Fix the auth bug")

# 路由到命名会话
claude_session(action="send", name="frontend", message="Fix the auth bug")
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
# 停止活跃会话
claude_session(action="stop")

# 停止命名会话
claude_session(action="stop", name="frontend")

# 通过 session_id 停止
claude_session(action="stop", session_id="abc123...")
```

### list（v3.0 新增）
```
claude_session(action="list")
```

列出当前 gateway 下的所有会话。返回：
```json
{
  "sessions": [
    {
      "session_id": "abc123...",
      "name": "frontend",
      "workdir": "/project",
      "state": "IDLE",
      "active": true
    },
    {
      "session_id": "def456...",
      "name": null,
      "workdir": "/project",
      "state": "THINKING",
      "active": true
    }
  ],
  "active_session_id": "abc123...",
  "total": 2
}
```

### switch（v3.0 新增）
```
claude_session(action="switch", name="frontend")
```

切换活跃会话到指定 name。后续不带 `session_id`/`name` 的操作（send/status/output 等）将路由到切换后的会话。

返回：
```json
{
  "switched_to": "frontend",
  "session_id": "abc123...",
  "state": "IDLE"
}
```

### 会话路由优先级

所有需要定位会话的 action（send/status/output/wait_for_idle 等）按以下顺序解析目标：

| 优先级 | 参数 | 说明 |
|--------|------|------|
| 1（最高） | `session_id` | 精确指定，找不到直接报错 |
| 2 | `name` | 从 name 索引查找，找不到报错 |
| 3 | 活跃会话 | 该 gateway 最近交互的会话 |
| 4（最低） | 最近创建 | 回退到该 gateway 下最后创建的会话 |

**交互类 action**（send/type/submit/respond_permission/cancel_input）会自动更新活跃会话记录。

### diagnose
```
claude_session(action="diagnose")
```
检查 tmux、Claude CLI、环境变量、残留 session 等依赖状态。

### doctor_fix

**两阶段操作**：先分析（默认），再执行修复。

```
# 第一步：分析（不执行任何修改）
claude_session(action="doctor_fix")

# 第二步：根据分析结果执行修复
claude_session(action="doctor_fix", apply=True)
claude_session(action="doctor_fix", apply=True, strategy="user")   # 保留用户修改
claude_session(action="doctor_fix", apply=True, strategy="merge")  # 合并
```

技能文件存在于两个位置：
- **用户目录**: `~/.hermes/skills/claude-session`
- **项目目录**: `<project>/skills/claude-session`

最佳实践是通过软链接让用户目录指向项目目录，项目更新时技能自动同步。

**参数**：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `apply` | boolean | false | false=仅分析，true=执行修复 |
| `strategy` | string | "project" | 合并策略：project/user/merge |

**strategy 说明**：
| 策略 | 适用场景 | 行为 |
|------|----------|------|
| `project` | 项目更新或无差异 | 备份用户目录 → 创建软链接 |
| `user` | 用户有修改 | 将用户修改复制到项目 → 创建软链接 |
| `merge` | 双方都有修改 | 项目独有文件从项目复制，其余保留用户版本 → 创建软链接 |

**返回状态**：
| status | 含义 |
|--------|------|
| `ok` | 软链接正确，无需操作 |
| `needs_fix` | apply=false 检测到问题，查看 actions_available |
| `needs_user_decision` | 有冲突需选择 strategy，查看 actions_available |
| `fixed` | apply=true 修复完成 |
| `error` | 执行失败 |

## Named Sessions 使用示例

### 场景1：并行处理前端和后端任务

```python
# 1. 创建两个命名会话
claude_session(action="start", name="frontend", workdir="/project/web", permission_mode="skip")
claude_session(action="start", name="backend",  workdir="/project/api", permission_mode="skip")

# 2. 向 frontend 会话发任务
claude_session(action="send", name="frontend", message="Refactor the login page to use the new auth API")
claude_session(action="wait_for_idle", name="frontend", timeout=600)

# 3. 同时向 backend 会话发任务
claude_session(action="send", name="backend", message="Add JWT refresh token endpoint")
claude_session(action="wait_for_idle", name="backend", timeout=600)

# 4. 检查所有会话状态
claude_session(action="list")

# 5. 切换到 frontend 查看结果
claude_session(action="switch", name="frontend")
claude_session(action="output", limit=50)

# 6. 清理
claude_session(action="stop", name="frontend")
claude_session(action="stop", name="backend")
```

### 场景2：研究 + 实施 分离

```python
# 研究会话：只读分析
claude_session(action="start", name="research", workdir="/project")
claude_session(action="send", name="research", message="Analyze the auth module and list all security issues")

# 实施会话：写代码
claude_session(action="start", name="impl", workdir="/project", permission_mode="skip")

# 研究完成后，将结果发给实施会话
claude_session(action="wait_for_idle", name="research", timeout=300)
research_result = claude_session(action="output", name="research", limit=100)

claude_session(action="send", name="impl",
    message=f"Based on this analysis, fix all security issues:\n{research_result}")
```

### 场景3：无 name 的传统用法（完全兼容）

```python
# 不用 name，行为与 v2.0 完全一致
claude_session(action="start", workdir="/project")
claude_session(action="send", message="Fix the bug")
claude_session(action="wait_for_idle", timeout=300)
claude_session(action="stop")
```

## Named Sessions 最佳实践

### 何时使用命名会话

| 场景 | 建议 | 原因 |
|------|------|------|
| 单一任务 | 不用 name | 减少复杂度，默认路由够用 |
| 并行 2+ 任务 | 用 name | 避免路由混淆，明确指定目标 |
| 研究 + 实施 | 用 name | 职责分离，研究和实施互不干扰 |
| 长时间多步任务 | 可选 | name 让 debug 更容易（list 能看到有意义的名字） |

### 命名规范

```
# 推荐：简洁、有语义
name="frontend"
name="auth-fix"
name="refactor"
name="test-suite"

# 避免：过长、特殊字符、无意义
name="this-is-my-very-long-session-name-for-frontend-work"  # 太长
name="frontend work"  # 空格不允许
name="测试"           # 非 ASCII 不允许
name="session-1"      # 无语义，不好记
```

### 避免名称冲突

- name 在同一 gateway 下唯一（不同 Telegram 群聊隔离，不冲突）
- 如果 name 已被使用，`start` 会返回错误
- `stop` 后 name 自动释放，可重新使用

### 资源管理

```python
# 任务完成后及时 stop 释放 tmux 资源
claude_session(action="stop", name="frontend")

# 批量检查
result = claude_session(action="list")
for s in result["sessions"]:
    if not s["active"]:
        claude_session(action="stop", session_id=s["session_id"])
```

### 注意事项

1. **gateway 重启后 name 丢失**：name 索引存储在内存中，gateway 重启后所有 name 需重新分配
2. **同 workdir 多 name**：同一路径可以创建多个 name 不同的会话（tmux session 名不同）
3. **name 不是 session_name**：`name` 是路由标识，`session_name` 是 tmux 层面的名称，两者独立
