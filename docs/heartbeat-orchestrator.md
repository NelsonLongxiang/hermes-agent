# Heartbeat Orchestrator (dev-flow-decide → Step 1 + Step 2 + Step 3)

**Q1-Q7 dev-flow-decide**: Q1=A1.2 / Q2=A2.1 / Q3=A3.2 (折中) / Q4=A4.3 / Q5=A5.2 / Q6=A6.1 / Q7=A7.1

## Step 1 骨架（done）

On every `agent:end` event, discover all `~/.hermes/skills/heartbeat-*/` skills
that declare `heartbeat.enabled: true`, run their `decide()` in parallel
(`asyncio.gather(..., return_exceptions=True)`), merge returned hints into a
single `system`-role message, and append to the session via
`SessionDB().append_message(...)`.

## Step 2 写入 + 识别（done）

- **Q1 prompt 引导句** — `agent/prompt_builder.py::HEARTBEAT_HINT_GUIDANCE`
  ships in the cached stable tier of every system prompt.  Tells the model
  that `[hint: <skill-name>]` system-role messages are authoritative
  follow-up context.  Gated by config.yaml `agent.heartbeat_hint_guidance`
  (default True).
- **Q2 write_back schema** — SKILL.yaml `heartbeat.write_back: bool` (default
  false).  When true and `decide()` returns `write_back.append_md: str`, the
  orchestrator appends the markdown to `SKILL.md` with a `<!-- heartbeat
  write_back <name> @ <ts> -->` separator.
- **R1 dedup** — exact-repeat hint payloads are short-circuited (no new
  SessionDB row).  Compaction of older hints is left to the existing
  conversation-compression pipeline — the hook does NOT soft-delete rows
  to avoid corrupting the active conversation tree.

**Layout (user-level, NOT in repo):**
- `~/.hermes/hooks/heartbeat-orchestrator/HOOK.yaml`
- `~/.hermes/hooks/heartbeat-orchestrator/handler.py`
- `~/.hermes/skills/heartbeat-echo/SKILL.yaml` (stub; Step-2 ships with
  `write_back: true` enabled)
- `~/.hermes/skills/heartbeat-echo/SKILL.md` (dynamic state)
- `~/.hermes/skills/heartbeat-echo/decide.py`

**In repo (commit `023eceed6` → next):**
- `agent/prompt_builder.py` — `HEARTBEAT_HINT_GUIDANCE` constant
- `agent/system_prompt.py` — gated inject into stable tier
- `tests/gateway/test_heartbeat_orchestrator.py` — Step 1 (1) + Step 2 (3)
  acceptance tests, all 4 passing

## 部署

```bash
# Copy hook + stub skill into user-level dirs (idempotent):
mkdir -p ~/.hermes/hooks/heartbeat-orchestrator
mkdir -p ~/.hermes/skills/heartbeat-echo
# (place HOOK.yaml, handler.py, SKILL.yaml, SKILL.md, decide.py)

# Verify:
python3 -m unittest tests.gateway.test_heartbeat_orchestrator -v
```

## Step 3 未回答问题检测（done）

**Q7 A7.1 第一个真实场景落地**：`heartbeat-unanswered` skill。

**触发链路**：每轮 `agent:end` → orchestrator 扫描最近 `window_size`（默认 6）条消息 →
提取含 `?`/`？` 且长度 ≥ `min_question_length`（默认 6）的 user 消息 →
检查后续 assistant 消息是否有关键词重叠（CJK 感知的 token 交集）→
未回答的问题生成 hint 注入下一轮 system-role 消息。

**Dedup**：SKILL.md 作为唯一持久状态，`dedup_ttl_turns`（默认 3）控制回看多少条
最近 log block。TTL=0 完全禁用 dedup（每次都注入）。同一组问题不会反复刷屏。

**Tokenizer**：`[A-Za-z0-9一-鿿]+` 正则提取 CJK + latin token，去掉 stopword，取长度 ≥ 3 的。
**不依赖 NLU/embedding**——recall > precision，false positive 交由 agent 自身判断。

**Skill 文件布局（用户态，不入仓）**：
- `~/.hermes/skills/heartbeat-unanswered/SKILL.yaml` — config（window_size / min_question_length / dedup_ttl_turns）
- `~/.hermes/skills/heartbeat-unanswered/SKILL.md` — write_back log 目标文件（动态状态）
- `~/.hermes/skills/heartbeat-unanswered/decide.py` — 启发式决策逻辑

**测试**：`tests/gateway/test_heartbeat_unanswered.py` — 14 tests passing，覆盖
answered/unanswered/dedup/TTL/fail-soft/CJK/short-Q/window-cutoff/write_back 格式。

## 风险缓解

| 风险 | 缓解 | 状态 |
|---|---|---|
| R1 system 累加污染 | dedup on exact repeat; compression pipeline owns old rows | done |
| R2 心跳失败阻塞 | `try/except` + `gather(return_exceptions=True)` 双层 | done (Step 1) |
| R5 hint 时机错位 | 同步写（不延迟） | done |
| R10 SKILL.md 误读指令 | `<!-- heartbeat write_back -->` HTML 注释包裹 | done |

## 下一步

- 测试机 gateway 重启后飞书实跑端到端验证（heartbeat-echo + heartbeat-unanswered 同时跑）
- `agent.heartbeat_hint_guidance` 配置项文档（config.yaml schema）
- WS reconnect 修复未合 main — 决定是否合入
- gh auth login + PR #37961 决策
