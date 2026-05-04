# Claude Session 自主决策系统设计

> 日期: 2026-04-26
> 状态: Draft
> 范围: tools/claude_session/

## 问题

当 Claude Code 通过 TUI 呈现交互式选项（AskUserQuestion、权限请求、确认提示、自由文本问题）时，Hermes 无法感知这些场景，导致 session 停滞等待用户输入。

## 目标

让 Hermes 自动检测 Claude Code 的"等待用户输入"场景，基于对话上下文和 TUI 输出用 LLM 自主做出决策，并将回复注入回 Claude Code 继续执行。

## 决策记录

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 实现方案 | A: TUI 输出解析 + tmux 注入 | 与现有架构一致，改动最小 |
| 交互形式 | 自主决策，不通知用户 | 用户明确要求 Hermes 自行决策 |
| 决策范围 | 所有用户输入场景 | AskUserQuestion、权限、确认、自由文本 |
| 决策机制 | 始终 LLM 决策 | 用户明确要求 |
| 上下文来源 | TUI 输出 + Hermes 对话上下文 | 最完整的决策依据 |

## 架构概览

```
AdaptivePoller._poll_once()
    → OutputParser.detect_state()
    → OutputParser.detect_user_prompt()     # 新增
    → state_change callback
    → AutoResponder.on_state_change()       # 新增
        → DecisionEngine.decide()           # 新增
        → TmuxInterface 注入回复
```

## 模块设计

### 1. 场景检测 — 扩展 OutputParser

文件: `tools/claude_session/output_parser.py`

新增 `detect_user_prompt()` 方法和 `UserPromptInfo` 数据类：

```python
@dataclass
class UserPromptInfo:
    prompt_type: str       # "ask_user" | "permission" | "confirmation" | "free_text"
    question: str          # 问题文本
    options: list[str]     # 选项列表（ask_user 时有值）
    selected_index: int    # 当前 ❯ 选中的索引（0-based）
    has_other: bool        # 最后一项是否为 "Type something." / "Other"
    raw_context: str       # 原始 TUI 文本上下文（用于 LLM 决策）
```

#### 检测规则

**ask_user** — Claude Code 的 AskUserQuestion 工具输出：
- 特征: 连续的 `N. xxx` 编号行，其中一行以 `❯` 前缀标记选中
- 正则: `r"^❯\s*(\d+)\.\s*(.+)$"` 匹配选中行，`r"^\s*(\d+)\.\s*(.+)$"` 匹配未选中行
- 提取: 问题文本（选项上方的内容）、所有选项、当前选中索引

**permission** — 已有 `_PERMISSION_RE`，增强提取：
- 从 `❯ 1. Yes` / `❯ Allow` 模式中提取具体权限描述
- 区分文件权限（Allow Edit xxx.py）和操作权限（Allow Bash）

**confirmation** — Yes/No 确认：
- 模式: "Do you want to..." / "Are you sure" / "Proceed?" 后跟 Yes/No 选项
- 类似 ask_user 但选项固定为 Yes/No 或 Allow/Deny

**free_text** — 自由文本输入：
- 触发条件: IDLE 状态 + `❯` 空行等待 + 上方有 `?` 结尾的问题文本
- `✻` 完成标记作为辅助信号（有则更确定，无则依赖问题文本判断）
- 提取: 问题文本
- 注意: free_text 检测难度较高，初期可保守判断（宁可漏检也不误判），后续根据实际 TUI 输出优化

#### 调用时机

`detect_user_prompt()` 在 `detect_state()` 之后调用，仅当状态为 IDLE 或 PERMISSION 时尝试检测。它作为二次分析，不替换状态检测。

#### 回调签名变更

`_on_state_change` 回调签名从 `(StateTransition)` 变为 `(StateTransition, Optional[UserPromptInfo])`。向后兼容处理：如果现有回调不接受第二个参数，使用 `try/except TypeError` 或 `inspect.signature` 检查，优雅降级。

### 2. 决策引擎

文件: `tools/claude_session/decision_engine.py`（新增）

```python
@dataclass
class Decision:
    action: str     # "select" | "select_and_type" | "text" | "confirm" | "permission"
    value: Any      # select: 选项索引(int, 1-based) / select_and_type: 自定义文本(str) / text: 文本(str) / confirm: bool / permission: bool
    reasoning: str  # LLM 决策理由

class DecisionEngine:
    def __init__(self, llm_call_fn=None):
        """
        Args:
            llm_call_fn: Callable matching agent.auxiliary_client.call_llm signature。
                         如果为 None，在调用时自动导入 call_llm。
        """

    def decide(self, prompt: UserPromptInfo, context: dict) -> Optional[Decision]:
        """同步调用 LLM 做出决策。

        Args:
            prompt: 解析出的用户输入场景
            context: {
                "conversation_history": list[dict],  # Hermes 对话历史
                "current_message": str,               # 用户原始消息
                "session_state": dict,                # Claude session 状态
                "tui_output": str,                    # 完整 TUI 输出
            }
        Returns:
            Decision 或 None（LLM 调用失败时）
        """
```

#### LLM Prompt 设计

**System Prompt:**

```
你是 Hermes 的自主决策代理。Claude Code 在执行任务过程中遇到了需要用户输入的场景。
你需要根据用户的原始意图和对话上下文，做出最合理的选择。

规则：
- 始终以 JSON 格式返回：{"action": "...", "value": ..., "reasoning": "..."}
- action 必须是: "select"（选择选项）、"select_and_type"（选择 Other 并输入文本）、"text"（输入文本）、"confirm"（确认/拒绝）、"permission"（批准/拒绝）
- select 时 value 是选项编号（从 1 开始）
- select_and_type 时 value 是要输入的自定义文本字符串
- text 时 value 是要输入的文本字符串
- confirm/permission 时 value 是 true 或 false
- reasoning 简要说明决策理由
```

**User Prompt:**

```
## 场景类型: {prompt.prompt_type}
## 问题: {prompt.question}
## 可选选项:
{每个选项编号和内容}
## 当前选中: {prompt.selected_index + 1}

## 用户原始消息:
{context.current_message}

## 对话历史:
{最近 10 条对话}

## TUI 输出上下文:
{prompt.raw_context}
```

#### LLM 调用方式

复用 Hermes 已有的 `call_llm()` 同步接口（`agent/auxiliary_client.py`）。`call_llm` 已有多 provider fallback、circuit breaker、config 解析。DecisionEngine 通过依赖注入接收调用函数，测试时传入 mock。

### 3. 自动响应器

文件: `tools/claude_session/auto_responder.py`（新增）

```python
@dataclass
class AutoResponderConfig:
    max_auto_responses_per_turn: int = 5
    cooldown_seconds: float = 2.0
    enabled: bool = True

class AutoResponder:
    def __init__(
        self,
        decision_engine: DecisionEngine,
        tmux: TmuxInterface,
        state_machine: StateMachine,
        config: AutoResponderConfig = None,
    ):
        pass

    def handle_prompt(self, prompt: UserPromptInfo, context: dict) -> None:
        """主入口：检测到等待用户输入场景时由外部调用。"""

    def _execute_decision(self, decision: Decision, prompt: UserPromptInfo) -> None:
        """将决策结果转换为 tmux 操作。"""

    def reset_turn(self) -> None:
        """重置 per-turn 计数器，在新用户消息发送时调用。"""
```

#### tmux 操作映射

| action | 场景 | tmux 操作 |
|--------|------|-----------|
| `select` | ask_user | 计算需要按 `↓` / `↑` 的次数，移动 `❯` 到目标选项，然后 `Enter` |
| `select_and_type` | ask_user (Other) | 导航到最后一项（Other）+ `Enter` → 等待 TUI 切换 → `send_keys(text, enter=True)` |
| `text` | free_text | `send_keys(text, enter=True)` |
| `confirm` | confirmation | 选择 Yes (通常是 `↓` + `Enter` 或直接 `Enter`) |
| `permission` | permission | 选择 Allow/Yes |

#### select 操作的细节

Claude Code TUI 中 AskUserQuestion 的选项渲染：
```
  1. 保留最终状态
❯ 2. 完成后删除     ← 当前选中
  3. 替换为摘要
```

移动 `❯` 到目标选项：
- 当前 `selected_index` = 1（第 2 项），目标 = 0（第 1 项）→ 按 1 次 `Up`
- 当前 `selected_index` = 1，目标 = 2（第 3 项）→ 按 1 次 `Down`
- 计算公式: `delta = target - selected_index`，正数按 `Down`，负数按 `Up`

通过 `tmux send-keys -t session Down` 或 `tmux send-keys -t session Up` 发送方向键。

### 4. 集成修改

#### 4.1 AdaptivePoller 修改

文件: `tools/claude_session/adaptive_poller.py`

在 `_poll_once()` 中增加场景检测：

```python
def _poll_once(self) -> None:
    # ... 现有逻辑 ...

    # 新增: 场景检测
    prompt_info = None
    if result.state in (ClaudeState.IDLE, ClaudeState.PERMISSION):
        prompt_info = OutputParser.detect_user_prompt(lines)

    # 新增: 传递 prompt_info 给回调
    if transition and self._on_state_change:
        transition.tool_name = result.tool_name
        transition.tool_target = result.tool_target
        self._on_state_change(transition, prompt_info)
```

#### 4.2 ClaudeSessionManager 修改

文件: `tools/claude_session/manager.py`

- 在 `__init__()` 中初始化 `self._auto_responder = None` 和 `self._conversation_context = {}`
- 在 `start()` 中根据 `auto_responder=True` 参数创建 AutoResponder
- 修改现有 `_handle_state_change(transition)` 签名为 `_handle_state_change(transition, prompt_info=None)`，在末尾增加 AutoResponder 路由
- **不替换** `_handle_state_change`，在其逻辑之上叠加 AutoResponder 调用
- 在 `status()` 返回结果中增加 auto-responder 状态信息

## 安全机制

1. **重试限制**: 每个 turn 最多自动响应 `max_auto_responses_per_turn` 次（默认 5），防止无限循环（Claude 反复提问）
2. **冷却期**: 两次自动响应之间至少 `cooldown_seconds`（默认 2s），防止竞态
3. **决策审计**: 每次自动决策的 `Decision`（含 reasoning）记录到 turn history
4. **错误降级**: LLM 调用失败或 JSON 解析失败时，记录错误日志但不注入任何输入，等待人工干预
5. **配置开关**: auto-responder 通过配置启用/禁用，不影响现有功能

## 依赖

- 无新外部依赖
- 复用 Hermes 已有的 LLM client 接口
- 所有 tmux 操作使用现有 `TmuxInterface`

## 测试策略

1. **OutputParser 单元测试**: 用模拟的 TUI 输出文本测试各种场景检测
2. **DecisionEngine 单元测试**: Mock LLM client，测试决策逻辑
3. **AutoResponder 集成测试**: Mock tmux interface，测试操作映射
4. **端到端测试**: 在真实 tmux 中启动 Claude Code，验证完整流程
