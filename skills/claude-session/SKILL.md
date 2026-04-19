---
name: claude-session
description: Guide for using claude_session tool to delegate coding tasks to Claude Code via tmux
version: 2.1
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

## When to Use

Use this skill when you need to delegate a coding task to Claude Code:
- Complex multi-file refactoring
- Running long test suites
- Tasks requiring deep codebase understanding
- Parallel work (you handle strategy, Claude handles implementation)

## Quick Start

0. **Diagnose dependencies** (首次使用或排查问题时):
   ```
   claude_session(action="diagnose")
   ```
   返回 5 项检查：tmux、Claude Code CLI、HERMES_STREAM_STALE_TIMEOUT、版本信息。
   如果用户反馈 claude_session 不可用，先调用 diagnose 排查。

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

## 快速诊断：diagnose action

当 claude_session 行为异常或需要排查环境问题时，使用内置诊断：

```
claude_session(action="diagnose")
```

返回结构化 JSON，包含 5 项检查：
1. **tmux** — 是否安装、路径、版本
2. **Claude Code CLI** — 是否安装、路径、版本
3. **HERMES_STREAM_STALE_TIMEOUT** — 是否配置、值是否 ≥ 300
4. **tmux version** — 版本号
5. **Claude Code version** — 版本号

每项包含 `status`（ok/missing/not_set/too_low）、`hint`（修复建议）、`required`（是否必须）。
最终 `status` 为 `ready` 或 `missing_deps`。

**优先用 diagnose 排查，不要手动 tmux/which 检查。**

## 快速诊断：diagnose action

运行 `claude_session(action="diagnose")` 可获取结构化依赖报告，包含 5 项检查：

| 检查项 | 内容 | required |
|--------|------|----------|
| tmux | 路径 + 版本 | ✅ 硬依赖 |
| Claude Code CLI | 路径 + 版本 | ✅ 硬依赖 |
| HERMES_STREAM_STALE_TIMEOUT | 当前值是否 ≥ 300 | 可选 |
| tmux version | 版本号 | 信息 |
| Claude Code version | 版本号 | 信息 |

返回结构：`{"status": "ready"|"missing_deps", "checks": [...], "summary": "..."}`

**使用场景：**
- 用户报告 claude_session 不工作时，先调 diagnose 排查
- 新机器部署后验证环境
- 排查流程的第一步，不要跳过

## 工具不可用时的排查流程

如果 `claude_session` 不在当前可用工具列表中，按以下步骤排查：

1. **先运行 diagnose**：`claude_session(action="diagnose")` — 获取结构化依赖报告
2. **确认工具是否注册**：`hermes tools --summary` 查看是否有 Claude Code Session
3. **确认 toolset 配置**：运行 `hermes tools` 确认已勾选
4. **检查 gateway 日志**：启动时日志会显示 `claude_session: Claude Code CLI not found` 等 warning
5. **可能的原因**：平台工具过滤、工具加载时 import 失败（静默跳过）、模型 provider 兼容性问题
6. **如果确认不可用**：如实告知用户排查结果，**不要偷偷换成 delegate_task 或 terminal**

## Troubleshooting: claude_session 工具不可用

如果 `claude_session` 工具没有出现在可用工具列表中：

### 根因分析链路
1. `tools/claude_session_tool.py` — 工具注册（`registry.register()`），`toolset="claude_session"`，`check_fn` 检查 tmux（硬依赖）+ claude CLI（软依赖，仅日志警告）
2. `toolsets.py` — `_HERMES_CORE_TOOLS` 包含 `"claude_session"`，`TOOLSETS["claude_session"]` 定义工具集
3. **`hermes_cli/tools_config.py`** — `CONFIGURABLE_TOOLSETS` 列表定义所有可配置工具集；`_post_setup_claude_session()` 在用户启用时自动检查依赖和配置环境变量
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

## 实战经验与陷阱（2026-04-19 补充）

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

### 陷阱5：启动时需要多次 Permission 响应
**现象**：Claude Code 启动后显示"Bypass permissions"确认界面，需要手动确认。
**应对**：循环调用 `respond_permission(action="allow")` 2-3 次，然后用 `wait_for_idle` 等待初始化完成。

### 陷阱7："y" 污染对话（2026-04-19 新增）
**现象**：`respond_permission("allow")` 向 tmux 发送 "y"，但 Claude Code 在某些状态下将其解释为用户消息，而非权限确认。Claude 随后回复 "It looks like you sent 'y' but there's no prior question"。
**原因**：权限确认界面和正常输入框共用同一终端，状态切换有时序竞争。
**应对**：
- 启动后等待 Claude 完全初始化（显示 `❯` 提示符）再发送任务
- 如果 "y" 已污染对话，在 send 任务时忽略它（Claude 会自动上下文切换）
- 污染不影响最终输出质量，但会增加 token 消耗

### 陷阱8：permission_mode="skip" 不跳过 Bash 权限（2026-04-19 新增）
**现象**：即使设置了 `permission_mode="skip"`，Claude Code 执行 Bash 命令时仍会弹出权限确认。
**原因**：`skip` 模式主要跳过文件读写权限，但 Bash 命令（特别是网络请求如 `curl`）默认仍需确认。
**应对**：
- 在任务执行过程中，持续监控 PERMISSION 状态并 `respond_permission("allow")`
- 或者在启动后预先配置：`/permissions add Bash:* allow`
- 预计每个复杂任务需要 3-5 次 permission 响应

### 自动安装配置（首次启用时）

当用户在 `hermes setup` 或 `hermes tools` 中勾选 `🤖 Claude Code Session` 时，Hermes 自动执行 `_post_setup_claude_session()`：

1. **检测 tmux** — 缺失时输出安装命令
2. **检测 Claude Code CLI** — 缺失时输出安装命令
3. **自动配置 `HERMES_STREAM_STALE_TIMEOUT=300`** — 防止 Stream Stalled（幂等，不会覆盖已有配置）
4. **输出状态报告** — 所有依赖 ✅ 或缺失 ⚠️

**check_fn 注册门控：**
- `tmux` 是硬依赖 — 缺失则工具不注册
- `claude` CLI 是软依赖 — 缺失仅输出 warning 日志，工具仍注册（用户可能稍后安装）
- 缺失时日志格式：`claude_session: Claude Code CLI not found in PATH. Install with: npm install -g @anthropic-ai/claude-code`

## 提取长报告的最佳实践
当 Claude 生成了很长的报告/代码时：
1. 用 `tmux capture-pane -t claude-work -p -S -5000` 捕获完整 pane 历史
2. 保存到 `/tmp/claude_raw_output.txt`
3. 用 `grep -n` 定位报告起止行号
4. 用 `sed` 提取并清理内容
5. 用 `read_file` 最终读取干净内容

### Ctrl+O 展开折叠输出（2026-04-19 新增）
**现象**：Claude Code TUI 对长输出折叠显示 `… +292 lines (ctrl+o to expand)`，tmux capture-pane 只能捕获折叠后的内容。
**解决**：
```bash
tmux send-keys -t claude-work C-o    # 展开/折叠切换
sleep 1
tmux capture-pane -t claude-work -p 2>/dev/null
```
**技巧**：先 capture 获取全局视图（折叠），定位到需要展开的区域后再 C-O 展开，避免一次展开所有内容。

## Error Recovery

1. Check `status` for ERROR state
2. Read `output` to understand the error
3. Send corrective instructions (max 2 retries)
4. If still failing, report to user
5. **绝对不要因为"等得久"就放弃当前方案切换到别的模式**

### 陷阱6：Hermes 层面 "Stream stalled mid tool-call" 中断
**现象**：Claude Session 正在正常工作，但 Hermes 自己的 API 流式响应突然中断，返回：
`⚠ Stream stalled mid tool-call (read_file); the action was not executed.`
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

## Permission Handling (normal mode)

When `wait_for_idle` returns `PERMISSION` state:
1. Read `permission_request` to see what Claude wants to do
2. **Safe operations** (Read, Grep, Glob) → auto-allow
3. **Modification** (Edit, Write) → auto-allow
4. **Delete operations** (rm, del, git rm) → report to user
5. **Destructive operations** → auto-deny

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

### diagnose
```
claude_session(action="diagnose")
```
Returns structured JSON with dependency checks (tmux, claude CLI, timeout, versions).
Use this FIRST when troubleshooting claude_session issues.

### stop
```
claude_session(action="stop")
```

## 自动安装配置（hermes setup 时触发）

当用户在 `hermes setup` → tools checklist 中启用 claude_session 时，`_post_setup_claude_session()` 自动执行：

1. **检测依赖**：tmux（硬）、Claude Code CLI（软，仅警告不阻断）
2. **自动配置环境变量**：`HERMES_STREAM_STALE_TIMEOUT=300`（如未设置）
3. **输出安装引导**：缺失依赖时给出安装命令

此逻辑在 `hermes_cli/tools_config.py` 中，对首次安装和回访用户（`hermes tools`）两条路径都生效。

## 改动 claude_session 代码后的测试流程

修改 `tools/claude_session_tool.py` 或相关文件后，按以下顺序验证：

1. **单元测试**（秒级）：
   ```bash
   .venv/bin/python -m pytest tests/tools/test_claude_session_tool.py -v
   ```

2. **Python 直接调用**（秒级，不需要重启 Gateway）：
   ```bash
   venv/bin/python -c "
   from tools.claude_session_tool import _diagnose_claude_session
   import json; print(json.dumps(_diagnose_claude_session(), indent=2))
   "
   ```

3. **Shell 脚本验证**（秒级）：
   ```bash
   bash skills/claude-session/scripts/configure.sh
   ```

4. **Gateway 线上验证**（需重启）：
   - 重启 gateway：`hermes gateway run`
   - 通过 Telegram 调用：`claude_session(action="diagnose")`

注意：Gateway 代码从项目目录直接加载（editable install），重启即可加载新代码，不需要重新安装。
