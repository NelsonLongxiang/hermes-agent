# Heartbeat Orchestrator (dev-flow-decide → Step 1)

**Q1-Q7 dev-flow-decide**: Q1=A1.2 / Q2=A2.1 / Q3=A3.2 (折中) / Q4=A4.3 / Q5=A5.2 / Q6=A6.1 / Q7=A7.1

## Step 1 骨架（done）

On every `agent:end` event, discover all `~/.hermes/skills/heartbeat-*/` skills
that declare `heartbeat.enabled: true`, run their `decide()` in parallel
(`asyncio.gather(..., return_exceptions=True)`), merge returned hints into a
single `system`-role message, and append to the session via
`SessionDB().append_message(...)`.

**Layout (user-level, NOT in repo):**
- `~/.hermes/hooks/heartbeat-orchestrator/HOOK.yaml`
- `~/.hermes/hooks/heartbeat-orchestrator/handler.py`
- `~/.hermes/skills/heartbeat-echo/SKILL.yaml` (stub for Step-1 validation)
- `~/.hermes/skills/heartbeat-echo/SKILL.md` (dynamic state)
- `~/.hermes/skills/heartbeat-echo/decide.py`

**In repo:**
- `tests/gateway/test_heartbeat_orchestrator.py` — Step-1 acceptance test

## 部署

```bash
# Copy hook + stub skill into user-level dirs (idempotent):
mkdir -p ~/.hermes/hooks/heartbeat-orchestrator
mkdir -p ~/.hermes/skills/heartbeat-echo
# (place HOOK.yaml, handler.py, SKILL.yaml, SKILL.md, decide.py)

# Verify:
python3 -m unittest tests.gateway.test_heartbeat_orchestrator -v
```

## 下一步

- **Step 2**: 写 Q1 (prompt 模板基类引导句) + Q2 (write_back schema 文档 + heartbeat-echo write_back 演示)
- **Step 3**: heartbeat-unanswered skill (Q7 A7.1 真实场景)
