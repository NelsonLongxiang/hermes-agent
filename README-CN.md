<p align="center">
  <img src="assets/banner.png" alt="Hermes Agent" width="100%">
</p>

# Hermes Agent ☤ — 中文指南

<p align="center">
  <a href="https://hermes-agent.nousresearch.com/docs/"><img src="https://img.shields.io/badge/文档-官方网站-FFD700?style=for-the-badge" alt="Documentation"></a>
  <a href="https://discord.gg/NousResearch"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/许可证-MIT-green?style=for-the-badge" alt="License: MIT"></a>
</p>

> **完整英文文档请见 [README.md](README.md) 和 [官方文档站](https://hermes-agent.nousresearch.com/docs/)**

---

## 🤖 Claude Code Session — 安装与配置指南

`claude_session` 是 Hermes 内置的工具，用于在 tmux 中控制 Claude Code CLI，实现自动化编码、研究和多步任务委托。它让 Hermes Agent 能够"指挥"另一个 Claude Code 实例工作。

### 架构概览

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Telegram    │────▶│  Hermes Gateway  │────▶│ Claude Code │
│  / CLI       │     │  (AI Agent)      │     │ (tmux 会话) │
└─────────────┘     └──────────────────┘     └─────────────┘
                     claude_session 工具         独立终端进程
```

### 系统要求

| 依赖 | 最低版本 | 说明 |
|------|---------|------|
| **tmux** | ≥ 3.0 | 终端复用器，Claude Code 的运行容器 |
| **Claude Code CLI** | ≥ 2.1 | Anthropic 的 Claude Code 命令行工具 |
| **Node.js** | ≥ 18 | Claude Code CLI 的运行时 |
| **Hermes Gateway** | — | 需要重启才能加载新工具 |

### 安装步骤

#### 第 1 步：安装 tmux

```bash
# Ubuntu / Debian
sudo apt install tmux

# macOS
brew install tmux

# CentOS / RHEL
sudo yum install tmux
```

验证：
```bash
tmux -V
# 输出类似: tmux 3.4
```

#### 第 2 步：安装 Claude Code CLI

```bash
# 需要 Node.js >= 18
npm install -g @anthropic-ai/claude-code

# 验证
claude --version
# 输出类似: 2.1.107 (Claude Code)
```

> **注意：** Claude Code CLI 需要有效的 Anthropic API Key 或 Claude Pro/Max 订阅。首次运行 `claude` 时会引导登录。

#### 第 3 步：在 Hermes 中启用 claude_session

**方式 A：首次安装时（推荐）**

运行 `hermes setup`，在工具选择清单中勾选 `🤖 Claude Code Session`：

```bash
hermes setup
```

勾选后，Hermes 会自动执行：
1. ✅ 检测 tmux 和 Claude Code CLI 是否已安装
2. ✅ 自动配置 `HERMES_STREAM_STALE_TIMEOUT=300`（防止长时间任务中断）
3. ⚠️ 如有依赖缺失，输出安装提示

**方式 B：已安装后追加**

```bash
hermes tools
# 选择对应的平台 → 勾选 Claude Code Session
```

#### 第 4 步：重启 Gateway

配置完成后**必须重启 Gateway** 才能生效：

```bash
# 在运行 Gateway 的终端中按 Ctrl+C 停止
# 然后重新启动
hermes gateway run
```

#### 第 5 步：验证安装

通过 Telegram 或 CLI 让 Agent 运行诊断：

```
请调用 claude_session(action="diagnose") 检查依赖状态
```

正常输出应类似：
```json
{
  "status": "ready",
  "checks": [
    {"dependency": "tmux", "status": "ok", "path": "/usr/bin/tmux"},
    {"dependency": "Claude Code CLI", "status": "ok", "path": "/usr/local/bin/claude"},
    {"dependency": "HERMES_STREAM_STALE_TIMEOUT", "status": "ok", "value": "300"},
    {"dependency": "tmux version", "status": "ok", "value": "tmux 3.4"},
    {"dependency": "Claude Code version", "status": "ok", "value": "2.1.114 (Claude Code)"}
  ],
  "summary": "All dependencies met — claude_session is ready to use."
}
```

或者直接运行配置脚本：
```bash
bash ~/.hermes/skills/claude-session/scripts/configure.sh
```

---

### 使用方法

#### 快速开始

通过 Hermes 给 Claude Code 派任务：

```
让 Claude 研究一下 GitHub 上的 XXX 项目
```

```
让 Claude 帮我重构 auth 模块
```

Hermes Agent 会自动通过 `claude_session` 工具启动 Claude Code 会话、发送任务、监控进度、提取结果。

#### 典型使用场景

| 场景 | 示例指令 |
|------|---------|
| **研究调查** | "让 Claude 调查 XXX 技术栈的优缺点" |
| **代码重构** | "让 Claude 重构 modules/ 下的代码" |
| **代码审查** | "让 Claude 审查最近的 git diff" |
| **文档生成** | "让 Claude 为 API 写文档" |
| **Bug 修复** | "让 Claude 排查 test_auth.py 的失败原因" |

---

### 常见问题与排查

#### ❌ 问题 1：`claude_session` 工具不可用

**现象：** Hermes Agent 说它没有 `claude_session` 工具。

**排查步骤：**
1. 检查工具是否已启用：`hermes tools --summary`
2. 如果列表中没有 `Claude Code Session`，运行 `hermes tools` 启用它
3. 确认后重启 Gateway

#### ❌ 问题 2：tmux 未安装

**现象：** `diagnose` 返回 `{"status": "missing_deps", ...}`，tmux 状态为 `missing`。

**解决：**
```bash
sudo apt install tmux    # Ubuntu/Debian
brew install tmux        # macOS
```

#### ❌ 问题 3：Claude Code CLI 未安装

**现象：** diagnose 中 `Claude Code CLI` 状态为 `missing`。

**解决：**
```bash
npm install -g @anthropic-ai/claude-code
claude --version          # 验证安装
```

#### ❌ 问题 4：Stream Stalled mid tool-call

**现象：** 长时间任务执行中，Hermes 报错：
```
⚠ Stream stalled mid tool-call (xxx); the action was not executed.
```

**原因：** Hermes 默认的流超时为 180 秒，Claude Session 执行复杂任务时上下文累积导致 prefill 超时。

**解决：** 已通过自动配置 `HERMES_STREAM_STALE_TIMEOUT=300` 解决。如果仍然出现：
```bash
# 检查当前值
cat ~/.hermes/.env | grep STALE

# 如果不存在或过小，手动设置
echo "HERMES_STREAM_STALE_TIMEOUT=300" >> ~/.hermes/.env

# 重启 Gateway
```

#### ❌ 问题 5：Claude Code 启动后不响应

**现象：** `claude_session start` 成功，但 `send` 任务后 Claude Code 卡住不动。

**可能原因：**
1. **API 服务商卡住** — 非 Anthropic 官方 API（如 z.ai、GLM）可能出现长时间无响应
2. **权限确认界面卡住** — 启动时需要多次 Permission 确认

**排查步骤：**
1. 运行 `claude_session(action="diagnose")` 确认依赖正常
2. 检查 Claude Code 的 API 配置：`claude config list`
3. 手动测试 Claude Code：在终端中运行 `claude`，发送简单消息看是否响应

#### ❌ 问题 6：Permission 状态幽灵

**现象：** `wait_for_idle` 一直返回 `PERMISSION`，但 Claude 实际在工作。

**解决：** 这是正常现象。Claude Code UI 底部的权限提示条会被检测为 PERMISSION 状态。Hermes Agent 会自动处理（多次 `respond_permission("allow")`）。

---

### 环境变量参考

| 变量 | 默认值 | 推荐值 | 说明 |
|------|--------|--------|------|
| `HERMES_STREAM_STALE_TIMEOUT` | 180 | **300** | API 流超时（秒），防止长任务中断 |
| `HERMES_HOME` | `~/.hermes` | — | Hermes 配置目录 |

配置方式：
```bash
# 写入 ~/.hermes/.env（推荐）
echo "HERMES_STREAM_STALE_TIMEOUT=300" >> ~/.hermes/.env

# 或使用 hermes env 命令
hermes env HERMES_STREAM_STALE_TIMEOUT 300
```

---

### 工作流程详解

```
用户发消息 ──▶ Hermes Agent ──▶ claude_session 工具
                 │                    │
                 │                    ├─ 1. start（启动 tmux 会话）
                 │                    ├─ 2. wait_for_idle（等待就绪）
                 │                    ├─ 3. respond_permission（处理权限）
                 │                    ├─ 4. send（发送任务指令）
                 │                    ├─ 5. wait_for_idle（等待完成，300-600s）
                 │                    ├─ 6. output（提取结果）
                 │                    └─ 7. stop（关闭会话）
                 │                    │
                 ◀────────────────────┘
              返回结果给用户
```

---

### 相关文件

| 文件 | 说明 |
|------|------|
| `tools/claude_session_tool.py` | 工具注册、handler、check_fn、diagnose |
| `tools/claude_session/manager.py` | 会话管理器核心逻辑 |
| `tools/claude_session/state_machine.py` | 状态机（7 种状态） |
| `tools/claude_session/tmux_interface.py` | tmux 底层接口 |
| `hermes_cli/tools_config.py` | 安装时自动配置逻辑 |
| `skills/claude-session/SKILL.md` | Agent 使用的技能文档 |
| `skills/claude-session/scripts/configure.sh` | 独立配置脚本 |
| `tests/tools/test_claude_session_tool.py` | 单元测试（26 个用例） |

---

### 快速安装（完整流程）

```bash
# 1. 安装 Hermes
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc

# 2. 安装依赖
sudo apt install tmux
npm install -g @anthropic-ai/claude-code

# 3. 首次配置
hermes setup
# 在工具清单中勾选 🤖 Claude Code Session

# 4. 重启 Gateway（如果正在运行）
hermes gateway run

# 5. 验证
# 发送消息: "运行 claude_session(action='diagnose') 检查状态"
```

---

<p align="center">
  <sub>Built by <a href="https://nousresearch.com">Nous Research</a> · MIT License</sub>
</p>
