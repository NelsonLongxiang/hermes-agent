# Claude Activity Visibility 实施计划

> **For Claude:** 使用 subagent-driven-development 技能逐任务实施。

**目标：** 在 `wait_for_idle()` 返回中增加 `current_activity` 字段，让 Hermes 知道 Claude 在做什么。

**架构：**
在 `AdaptivePoller._poll_once()` 中解析 Claude TUI 输出，提取当前活动信息，通过 `status_callback` 传递给 Hermes。

---

## Task 1: 分析 Claude TUI 输出格式

**Objective:** 了解 Claude TUI 的输出格式，找出能体现"当前活动"的关键词

**Files:**
- Analyze: `tools/claude_session/manager.py` 中的 `_build_status_info()`
- Analyze: `tools/claude_session/output_parser.py`

**Step 1: 阅读 _build_status_info 方法**
```bash
grep -n "_build_status_info" tools/claude_session/manager.py
```

**Step 2: 阅读 output_parser.py 中的检测逻辑**
```bash
cat tools/claude_session/output_parser.py | head -100
```

**Step 3: 分析 Claude TUI 常见输出模式**
常见活动关键词：
- `Reading` / `Read` — 读取文件
- `Writing` / `Write` — 写入文件
- `Executing` / `Bash` — 执行命令
- `Searching` / `Grepping` — 搜索
- `Thinking` / `Processing` — 思考
- `●` — 工具执行中

---

## Task 2: 在 OutputParser 中增加 activity 检测

**Objective:** 在 `OutputParser` 类中添加 `detect_activity()` 方法

**Files:**
- Modify: `tools/claude_session/output_parser.py`

**Step 1: 添加 ACTIVITY_PATTERNS 正则**
在文件开头添加：

```python
# Claude TUI 活动检测模式
_ACTIVITY_PATTERNS = {
    "reading": re.compile(r"(?:Read|Reading)\s+([^\n]+)"),
    "writing": re.compile(r"(?:Write|Writing)\s+([^\n]+)"),
    "executing": re.compile(r"(?:Bash|Executing|Running)\s+([^\n]+)"),
    "searching": re.compile(r"(?:Search|Grep|Searching)\s+([^\n]+)"),
    "thinking": re.compile(r"(?:Thinking|Processing|Cogitating)"),
    "tool_call": re.compile(r"●\s+(\w+)"),
}
```

**Step 2: 添加 detect_activity 方法**
```python
@staticmethod
def detect_activity(lines: list[str]) -> dict:
    """检测 Claude 当前活动。

    Returns:
        dict: {
            "activity": str,  # "reading"|"writing"|"executing"|"searching"|"thinking"|"tool_call"|"idle"
            "detail": str,   # 具体细节，如文件名
            "raw": str,     # 原始匹配行
        }
    """
    # 优先检测具体活动（高优先级）
    for line in lines[-20:]:  # 只检查最近20行
        for activity, pattern in _ACTIVITY_PATTERNS.items():
            if activity == "thinking":
                continue  # thinking 最后检测
            match = pattern.search(line)
            if match:
                return {
                    "activity": activity,
                    "detail": match.group(1) if match.groups() else "",
                    "raw": line.strip(),
                }

    # 检测 thinking 状态
    for line in lines[-20:]:
        if _ACTIVITY_PATTERNS["thinking"].search(line):
            return {
                "activity": "thinking",
                "detail": "",
                "raw": line.strip(),
            }

    return {
        "activity": "idle",
        "detail": "",
        "raw": "",
    }
```

**Step 3: 验证语法**
```bash
cd /mnt/f/Projects/hermes-agent && python3 -c "from tools.claude_session.output_parser import OutputParser; print('OK')"
```

---

## Task 3: 在 _poll_once 中集成 activity 检测

**Objective:** 在 `AdaptivePoller._poll_once()` 中调用 `detect_activity()` 并传递给 callback

**Files:**
- Modify: `tools/claude_session/adaptive_poller.py`

**Step 1: 找到 _poll_once 方法**
```bash
grep -n "_poll_once" tools/claude_session/adaptive_poller.py
```

**Step 2: 在 _poll_once 中添加 activity 检测**
在 `result = OutputParser.detect_state(lines)` 后添加：

```python
# 检测当前活动
activity_info = OutputParser.detect_activity(lines)
```

**Step 3: 在 _fire_callback 中传递 activity_info**
找到 `_fire_callback(transition, prompt_info)` 调用，添加 activity_info：

```python
if transition or prompt_info or activity_info["activity"] != "idle":
    self._fire_callback(transition, prompt_info, activity_info)
```

**Step 4: 修改 _fire_callback 签名**
```python
def _fire_callback(self, transition, prompt_info, activity_info=None):
    if activity_info is None:
        activity_info = {"activity": "idle", "detail": "", "raw": ""}
    # ... 原有逻辑 ...
```

---

## Task 4: 在 status_info 中包含 activity

**Objective:** 确保 activity 信息最终传递到 Hermes

**Files:**
- Modify: `tools/claude_session/manager.py` 中的 `_handle_state_change()`

**Step 1: 找到 _handle_state_change 方法**
```bash
grep -n "_handle_state_change" tools/claude_session/manager.py
```

**Step 2: 在 _handle_state_change 中接收 activity_info**
修改方法签名和实现，接收并传递 activity 信息：

```python
def _handle_state_change(self, transition=None, prompt_info=None, activity_info=None):
    # ...
    if activity_info:
        status_info["current_activity"] = activity_info["activity"]
        status_info["activity_detail"] = activity_info.get("detail", "")
    # ...
```

---

## Task 5: 测试 activity 检测

**Objective:** 验证 activity 检测功能正常

**Step 1: 启动测试会话**
```python
claude_session(action="start", name="test-activity", workdir="/mnt/f/Projects/hermes-agent", permission_mode="skip")
```

**Step 2: 发送一个会产生活动的任务**
```python
claude_session(action="send", message="Read tools/claude_session/manager.py and tell me its size")
```

**Step 3: 等待并检查 status 返回中是否包含 current_activity**
```python
claude_session(action="status")
# 期望返回中包含 "current_activity": "reading" 等
```

---

## Task 6: 提交更改

**Step 1: 检查更改**
```bash
git diff tools/claude_session/output_parser.py tools/claude_session/adaptive_poller.py tools/claude_session/manager.py
```

**Step 2: 提交**
```bash
git add tools/claude_session/output_parser.py tools/claude_session/adaptive_poller.py tools/claude_session/manager.py
git commit -m "feat(claude-session): add activity detection for better visibility

- Add OutputParser.detect_activity() to parse Claude TUI output
- Support activity types: reading, writing, executing, searching, thinking, tool_call
- Integrate with AdaptivePoller to detect and report current activity
- Pass activity info through status_callback to Hermes
"
```

---

## 验证步骤

1. 启动新会话：`claude_session(action="start", name="test", workdir="/mnt/f/Projects/hermes-agent")`
2. 发送任务：`claude_session(action="send", message="echo hello")`
3. 检查状态：`claude_session(action="status")`
4. 验证返回中包含 `current_activity` 字段
