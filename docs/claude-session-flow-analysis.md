# Claude Session 流程图与数据流转分析

> 生成日期: 2026-04-27
> 分析范围: `tools/claude_session/` 全部模块
> 目的: 系统性排查运行异常，定位根因

---

## 1. 模块依赖关系

```
claude_session_tool.py          # 外部 API 入口，路由/注册/多会话管理
    │
    └── manager.py              # 核心编排层 (ClaudeSessionManager)
         │
         ├── state_machine.py   # 7 状态有限状态机
         ├── output_buffer.py   # 去重环形缓冲区
         ├── output_parser.py   # TUI 输出解析 (状态检测 + 用户提示检测)
         ├── tmux_interface.py  # 底层 tmux CLI 操作
         ├── adaptive_poller.py # 自适应轮询引擎 (后台线程)
         ├── auto_responder.py  # 自动响应器
         └── decision_engine.py # LLM 决策引擎
```

---

## 2. 完整数据流转图

```
┌─────────────────────────────────────────────────────────────────┐
│  外部调用 (Gateway / Telegram / API)                              │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  claude_session_tool.py                                         │
│  - 会话注册表: {gateway_key: {name: ClaudeSessionManager}}       │
│  - 路由优先级: session_id > name > active_session > latest       │
│  - 暴露 actions: start, stop, send, type_text, submit,          │
│    send_text, status, wait_for_idle, output, respond_permission │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  ClaudeSessionManager (manager.py)                              │
│                                                                 │
│  实例状态:                                                       │
│    _session_id, _tmux, _sm(StateMachine), _buf(OutputBuffer),  │
│    _poller(AdaptivePoller), _auto_responder, _current_turn,    │
│    _wait_state, _permission_mode, _conversation_context         │
│                                                                 │
│  核心方法:                                                       │
│    start()        → 创建 tmux session + 启动 Claude Code 进程    │
│    send()         → 原子发送消息 (type + Enter)                   │
│    send_text()    → 同上，别名                                    │
│    type_text()    → 仅打字不回车                                  │
│    submit()       → 按回车提交                                    │
│    wait_for_idle()→ 自适应轮询等待 Claude 完成                    │
│    respond_permission() → 响应权限请求                            │
│    status()       → 返回当前状态                                  │
└───────────────────────────┬─────────────────────────────────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
│ TmuxInterface│  │AdaptivePoller│  │  OutputBuffer     │
│ (tmux CLI)   │  │ (后台线程)    │  │  (环形缓冲区)     │
│              │  │              │  │                    │
│ create_session│  │ _poll_loop() │  │ append_batch()    │
│ capture_pane │◄─┤   ↓          │  │   ↓ 去重(MD5)     │
│ send_keys    │  │ capture_pane │  │ since(marker)     │
│ send_special │  │   ↓          │  │ last_n_chars()    │
│ kill_session │  │ OutputParser │  │ read(offset/limit)│
└──────────────┘  │   ↓          │  └──────────────────┘
                  │ StateMachine │
                  │   ↓          │
                  │ _handle_     │
                  │ state_change │
                  └──────┬───────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  _handle_state_change() 回调 (在 poller 后台线程中执行)          │
│                                                                 │
│  1. 更新 Turn (tool_calls / thinking_cycles / finalize)          │
│  2. 触发 event_queue 事件                                       │
│  3. 构建 status_info → status_callback (Gateway 状态桥接)        │
│  4. 自动审批: skip 模式下 PERMISSION → _auto_approve_permission  │
│  5. 粘贴自动提交: INPUTTING + pasted → sleep(10) → Enter         │
│  6. AutoResponder: prompt_info → DecisionEngine → LLM → tmux    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 状态机详解

### 3.1 状态定义

| 状态 | 含义 | 轮询间隔 |
|------|------|----------|
| `DISCONNECTED` | tmux session 不存在或未连接 | 5.0s |
| `IDLE` | Claude Code 等待输入 (`❯` 空提示符) | 3.0s |
| `INPUTTING` | 正在输入 / 粘贴文本未提交 | 0.5s |
| `THINKING` | Claude 正在思考 (默认/回退状态) | 1.0s |
| `TOOL_CALL` | Claude 正在执行工具 (`● ToolName`) | 0.5s |
| `PERMISSION` | 等待权限审批 | 0.3s |
| `ERROR` | 检测到错误输出 | 1.0s |
| `EXITED` | Claude Code 已退出 (shell 提示符) | 5.0s |

### 3.2 合法状态转换

```
DISCONNECTED → {IDLE, ERROR, DISCONNECTED, EXITED}
IDLE         → {THINKING, INPUTTING, DISCONNECTED, ERROR, EXITED}
INPUTTING    → {THINKING, IDLE, DISCONNECTED, ERROR, EXITED}
THINKING     → {TOOL_CALL, PERMISSION, IDLE, ERROR, DISCONNECTED, EXITED}
TOOL_CALL    → {THINKING, PERMISSION, IDLE, ERROR, DISCONNECTED, EXITED}
PERMISSION   → {THINKING, IDLE, ERROR, DISCONNECTED, EXITED}
ERROR        → {THINKING, IDLE, DISCONNECTED, ERROR, EXITED}
EXITED       → {IDLE, DISCONNECTED, EXITED}
```

> 注: 非预期转换不会阻止，仅打 warning 日志。

### 3.3 状态检测优先级

```
OutputParser.detect_state() 判定顺序:

ERROR > PERMISSION > TOOL_CALL > IDLE > COMPACT > THINKING(默认)
```

具体逻辑:
1. **ERROR**: `Error:` / `Failed:` 匹配，但当有 `●` tool marker 时抑制（避免 tool 输出的 stderr 误判）
2. **PERMISSION**: `Allow ... ?` / `permission to` / `❯ 1. Yes` 匹配，排除状态栏行
3. **TOOL_CALL**: `● ToolName target` 匹配，但当 `❯` 出现在 `●` 之下时视为过期标记
4. **IDLE**: 独立 `❯` 匹配，排除: permission selector、pasted text、phantom prompt（被分隔线包围）
5. **COMPACT**: `Compacting` / `compressing conversation` 等关键词 → THINKING
6. **THINKING**: 默认回退状态

---

## 4. 核心流程详解

### 4.1 Session 启动流程 (start)

```
start(workdir, name, model, permission_mode, ...)
    │
    ├── Phase 0: 持锁验证 (无 I/O)
    │   ├── 检查 session_active / initializing
    │   ├── 生成 claude_session_uuid
    │   ├── 检查 resume_uuid 的 .jsonl 是否存在
    │   └── 创建 TmuxInterface 实例
    │
    ├── Phase 1: tmux I/O 操作 (无锁)
    │   ├── tmux session 不存在? → create_session()
    │   ├── tmux session 已存在?
    │   │   ├── 验证 workdir 匹配 (tmux display-message)
    │   │   ├── 验证 Claude Code 归属
    │   │   └── OutputParser.detect_state() 精确检测
    │   │       ├── IDLE → 安全复用
    │   │       ├── EXITED → kill + 重建
    │   │       └── 其他 → kill + 重建 (避免卡住的 session)
    │   │
    │   ├── needs_init → 启动 Claude Code 进程
    │   │   ├── root 用户? → su 切换非 root
    │   │   ├── 构建 claude 命令 (--session-id / --resume / --permission-mode)
    │   │   ├── send_keys(claude_cmd, enter=True)
    │   │   ├── skip 模式? → 处理 bypass 确认
    │   │   └── _wait_for_claude_startup(30s)
    │   │       ├── 检测 startup scene (workspace trust)
    │   │       ├── 检测 IDLE → 成功
    │   │       ├── 检测 THINKING/TOOL_CALL/PERMISSION + Claude 签名 → 成功
    │   │       ├── 检测 ERROR → 失败
    │   │       └── 检测 EXITED → 失败
    │   │
    │   └── 异常 → kill tmux + 返回 error
    │
    └── Phase 2: 持锁状态更新
        ├── _initializing = False
        ├── AdaptivePoller.start() → 启动后台轮询线程
        ├── _session_active = True
        ├── AutoResponder 初始化 (如启用)
        └── 返回 session 信息
```

### 4.2 消息发送流程 (send)

```
send(message)
    │
    ├── 持锁
    │   ├── 验证 session_active / tmux / state(非 EXITED/DISCONNECTED)
    │   ├── 创建新 Turn (_turn_counter++, output_marker)
    │   ├── 重置 _wait_state
    │   ├── 重置 auto_responder
    │   ├── 更新 _conversation_context
    │   ├── 构建 status_info (如有 status_callback)
    │   └── tmux.send_keys(message, enter=True)
    │
    ├── 锁外: status_callback(status_info)
    │
    └── 锁外: poller.poll_now() → 触发即时轮询

TmuxInterface.send_keys() 多行处理:
    ├── send-keys -l <text>      # 字面量发送
    └── 如果 enter=True 且含 "\n":
        ├── sleep(10.0)          # 等待 bracketed paste 完成
        └── send-special Enter
```

### 4.3 自适应等待流程 (wait_for_idle)

```
wait_for_idle(timeout=900)
    │
    ├── 初始化 _wait_state (首次/重置后)
    │   ├── start_time, start_tokens
    │   ├── last_patrol_time, last_patrol_tokens
    │   └── stalled_patrols, compact_detected
    │
    └── while 循环:
        │
        ├── 终态检测 (立即返回):
        │   ├── IDLE      → "idle"     (清理 _wait_state)
        │   ├── PERMISSION → "permission" (保持 _wait_state)
        │   ├── ERROR     → "error"    (清理 _wait_state)
        │   ├── DISCONNECTED → "disconnected"
        │   └── EXITED    → "exited"
        │
        ├── compact 检测:
        │   └── detect_state().is_compacting → 延长 deadline
        │
        ├── token 增长追踪:
        │   └── current_tokens > last_check_tokens → 更新 last_growth_time
        │
        ├── 巡检 (每 600s):
        │   ├── patrol_delta == 0 → stalled_patrols++
        │   ├── stalled_patrols >= 3 且非 compact → 返回 "stalled"
        │   └── 否则仅打日志
        │
        ├── 自适应间隔:
        │   ├── 停滞 > 30s → 10.0s 间隔
        │   └── 正常增长  → 5.0s 间隔
        │
        └── timeout:
            ├── compact 活跃 → 延长 deadline (最多 15min)
            └── 否则 → 返回 "timeout"
```

### 4.4 后台轮询流程 (AdaptivePoller._poll_loop)

```
_poll_loop() [daemon thread]
    │
    └── while !stop_event:
        │
        ├── _poll_once():
        │   │
        │   ├── tmux.session_exists()?
        │   │   └── No → transition(DISCONNECTED) + fire_callback
        │   │
        │   ├── raw = tmux.capture_pane()
        │   ├── lines = OutputParser.clean_lines(raw)
        │   ├── buf.append_batch(lines)    # 去重写入缓冲区
        │   │
        │   ├── result = OutputParser.detect_state(lines)
        │   ├── transition = sm.transition(result.state)
        │   │
        │   ├── if state in (IDLE, PERMISSION):
        │   │   └── prompt_info = detect_user_prompt(lines, state)
        │   │
        │   └── if transition or prompt_info:
        │       └── _fire_callback(transition, prompt_info)
        │           └── _handle_state_change() [见 4.5]
        │
        └── sleep(interval)  # 按当前状态自适应间隔

_fire_callback():
    ├── 检查 callback 参数数量 (兼容旧接口)
    └── on_state_change(transition, prompt_info)
```

### 4.5 状态变更回调 (_handle_state_change)

```
_handle_state_change(transition, prompt_info)
    │
    ├── [持锁] 更新 Turn:
    │   ├── → TOOL_CALL: 追加 ToolCall 记录
    │   ├── → THINKING: thinking_cycles++
    │   ├── → IDLE: finalize Turn + fire "turn_completed"
    │   └── → EXITED: finalize Turn + fire "session_exited"
    │
    ├── [持锁] fire "state_changed" event
    │
    ├── [持锁] 构建 status_info (如有 status_callback)
    │
    ├── [持锁] 提取 permission_details (PERMISSION 状态时)
    │
    ├── [锁外] _state_event.set() → 唤醒 wait_for_idle
    │
    ├── [锁外] 自动审批 (skip 模式):
    │   └── _auto_approve_permission()
    │       ├── 验证是真正的 permission (非状态栏噪声)
    │       ├── 检测编号选择器 UI vs 经典 UI
    │       ├── 发送 Enter 或 "y" + Enter
    │       └── 最多 3 次重试
    │
    ├── [锁外] 粘贴自动提交:
    │   └── INPUTTING + from IDLE/INPUTTING:
    │       ├── sleep(10.0)          ← ⚠️ 阻塞 poller 线程
    │       └── send_special_key("Enter")
    │
    ├── [锁外] AutoResponder:
    │   └── prompt_info → DecisionEngine.decide()
    │       ├── 构建 LLM prompt (场景类型 + 选项 + 上下文)
    │       ├── LLM 返回 {"action": "...", "value": ...}
    │       └── _execute_decision():
    │           ├── select: navigate + Enter
    │           ├── select_and_type: navigate to Other + type
    │           ├── text: send_keys(value)
    │           ├── confirm/permission: find Yes/Allow option
    │           └── 安全限制: max 5次/turn, 2s cooldown, fingerprint 去重
    │
    └── [锁外] status_callback(status_info) → Gateway 状态桥接
```

---

## 5. 线程模型与锁分析

### 5.1 线程分布

| 线程 | 职责 | 操作的共享状态 |
|------|------|---------------|
| **主线程** | API 调用处理 | `_lock` 保护的所有状态 |
| **poller 线程** | 后台轮询 + 状态检测 | 通过 `_handle_state_change` 获取 `_lock` |
| **Gateway 线程** | status_callback / event_queue | 读取 status_info (不可变 snapshot) |

### 5.2 锁的作用域

```
ClaudeSessionManager._lock 保护:
  ├── _session_active, _initializing
  ├── _session_id, _claude_session_uuid
  ├── StateMachine._state (自身也有 _lock)
  ├── _current_turn, _turn_history
  ├── _wait_state
  ├── _auto_responder 状态
  └── _conversation_context

持锁的操作:
  ├── start() Phase 0 + Phase 2
  ├── send() / send_text() / type_text() / submit()
  ├── _handle_state_change() 前半段 (状态更新)
  ├── respond_permission()
  └── status()

刻意锁外的操作 (避免死锁):
  ├── tmux I/O (create_session, capture_pane, send_keys)
  ├── _state_event.set()
  ├── _auto_approve_permission()
  ├── status_callback() 调用
  └── poller.poll_now()
```

### 5.3 潜在死锁/竞态场景

```
场景 1: send() vs _handle_state_change()
  send() 持锁 → send_keys() → poll_now()
    poll_now() → _handle_state_change() → 尝试获取 _lock
  ✅ 已规避: send() 在 poll_now() 前释放锁

场景 2: 粘贴自动提交阻塞 poller
  poller 线程 → _handle_state_change() → sleep(10.0)
  → poller 线程被阻塞 10 秒，期间无状态更新
  ⚠️ 风险: wait_for_idle 的状态检测依赖 poller 更新

场景 3: wait_for_idle vs _state_event
  wait_for_idle 在 _state_event.wait() 上等待
  _handle_state_change 调用 _state_event.set()
  ✅ 正确: 锁外 set，避免死锁

场景 4: _auto_approve_permission 竞态
  poller 回调 → _auto_approve_permission()
    → capture_pane + send_keys
    → 与主线程的 respond_permission() 可能冲突
  ⚠️ 风险: 两个线程可能同时发送按键
```

---

## 6. 关键风险点清单

### R1: 状态检测误判 (output_parser.py:119-244)

**根因**: Claude Code TUI 渲染的 `❯` 有三种含义，正则难以 100% 区分:
1. 真正的 IDLE 提示符 (等待用户输入)
2. 权限选择器 (`❯ Allow`, `❯ 1. Yes`)
3. Phantom 提示符 (TUI 底部装饰，Claude 还在工作)

**当前缓解措施**:
- 分隔线包围检测 (`_DECORATION_RE`)
- 完成时间指示器 (`✻ ... for Xm Xs`)
- Shell 签名检测 (`_is_shell_prompt`)

**残余风险**: 边界情况下 (如 compact 操作完成瞬间、网络延迟导致 TUI 渲染不完整) 仍可能误判。

### R2: 粘贴文本 10s sleep 阻塞 (manager.py:1098)

**根因**: 在 poller 回调线程中执行 `time.sleep(10.0)`，阻塞了整个 poller 线程。

**影响**:
- 10 秒内无状态轮询，状态机不更新
- 如果粘贴实际在 5 秒内完成，浪费 5 秒
- 如果粘贴需要超过 10 秒，Enter 过早导致消息截断

**理想方案**: 用独立线程/定时器处理粘贴提交，不阻塞 poller。

### R3: Permission 误判 (output_parser.py:46-53)

**根因**: `_PERMISSION_RE` 覆盖面广，状态栏 "bypass permissions on" 虽然通过 `_STATUS_BAR_RE` 过滤，但过滤列表可能不完整。

**误判路径**:
```
状态栏: "bypass permissions on" → _STATUS_BAR_RE 过滤 ✅
工具输出: "Error: permission denied" → has_active_tool 抑制 ✅
Claude 输出: "I need permission to..." → 可能触发 ❌
```

### R4: ERROR 抑制过度 (output_parser.py:141-144)

**根因**: 当检测到 `●` tool marker 时，所有 ERROR 匹配被抑制。这正确避免了 tool stderr 的误判，但如果 Claude Code 自身真的在 TOOL_CALL 期间崩溃，这个错误也会被吞掉。

### R5: Shell 提示符 vs Claude 提示符 (output_parser.py:247-291)

**根因**: `_is_shell_prompt()` 仅检查最近 5 行是否有 Claude TUI 签名。Claude Code 刚退出时，旧的 TUI 输出可能还在 scrollback 中，但最近 5 行只有 shell prompt + 空 `❯`。

**边界情况**:
- zsh/starship 主题的 `❯` 提示符 → 需要完整依赖 `_is_shell_prompt`
- Claude Code 正在启动 (welcome screen) → `has_welcome_screen` 需优先检测

### R6: Turn 跟踪不准确

**根因**: Turn 的生命周期依赖 `_handle_state_change` 回调，但:
- 首个 Turn 的 `start_time` 使用 `_session_start_time` (可能在 start() 完成后很久才 send)
- `send_text()` 和 `send()` 创建 Turn 的逻辑重复
- Turn finalize 时机仅依赖 `→ IDLE` 转换，如果状态跳过 IDLE (如 ERROR → THINKING)，Turn 可能不会被 finalize

### R7: _wait_state 跨调用持久化

**设计**: `_wait_state` 在 send() 时重置，在 wait_for_idle() 中持久化。

**风险**: 如果外部调用 wait_for_idle() → timeout → 再次 wait_for_idle()，`_wait_state` 被正确恢复。但如果中间有其他操作 (如 respond_permission + wait_for_idle)，`_wait_state` 可能包含过时的 `last_patrol_tokens`。

---

## 7. 异常排查清单

根据以上分析，排查异常时应按以下顺序检查:

### 第一步: 日志确认

```bash
# 查看状态转换日志 (unexpected transition 是重要信号)
grep "Unexpected state transition" hermes.log

# 查看自动审批日志
grep "Auto-approve" hermes.log

# 查看粘贴文本处理
grep "Pasted text detected" hermes.log

# 查看启动失败
grep "failed to start\|startup.*failed\|startup.*timeout" hermes.log
```

### 第二步: 状态检测准确性

```bash
# 手动 capture + 解析
tmux capture-pane -t <session> -p -S -50 | python3 -c "
import sys, re
text = sys.stdin.read()
# 去 ANSI
text = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?m', '', text)
lines = [l for l in text.splitlines() if l.strip()]
for i, l in enumerate(lines[-15:]):
    print(f'{i:3d}: {repr(l)}')"
```

### 第三步: 线程状态

```python
# 在运行时检查 poller 是否存活
import threading
print([t.name for t in threading.enumerate()])
# 预期: ['MainThread', 'claude-session-poller', ...]
```

### 第四步: 缓冲区状态

```python
# 检查 output buffer 的去重是否过度
manager = get_active_manager()
print(f"buffer total: {manager._buf.total_count()}")
print(f"buffer current: {len(manager._buf._lines)}")
print(f"last 5 lines: {[l.text for l in manager._buf.read(limit=5)]}")
```

---

## 8. 改进建议 (按优先级)

| 优先级 | 建议 | 模块 | 原因 |
|--------|------|------|------|
| P0 | 粘贴提交不阻塞 poller | manager.py:1098 | 10s sleep 导致状态盲区 |
| P0 | 添加可观测性: 关键路径打点日志 | 全局 | 排查异常依赖日志，当前日志不够 |
| P1 | 状态检测结果持久化 (最近 N 次) | output_parser.py | 排查时能回溯 "为什么判定为 X" |
| P1 | Turn finalize 补偿: 超时/错误时也 finalize | manager.py | 避免泄漏未完成的 Turn |
| P2 | Permission 检测增加上下文验证 | output_parser.py | 减少 "bypass permissions" 误判 |
| P2 | AdaptivePoller 健康检查: 检测 poller 卡死 | adaptive_poller.py | poller 线程异常不会被发现 |
| P3 | send_text/send 去重 (统一入口) | manager.py | 逻辑重复，维护成本高 |
