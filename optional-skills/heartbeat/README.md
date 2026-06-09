# Heartbeat Orchestrator

A hook + skill system that injects contextual hints between agent turns.

## Architecture

```
agent:end hook (handler.py)
  ├── discover heartbeat-* skills
  ├── run decide(ctx) in parallel
  ├── dedup against SKILL.md last hint
  ├── write_back to SKILL.md (overwrite mode)
  └── return {"trigger_followup": True, "hints": [...]}
        │
        ▼
run.py emit_collect → followup turn
  └── _run_agent(message="[heartbeat] ...{hints}")
```

## Components

- **hook/** — HOOK.yaml + handler.py (the orchestrator)
- **heartbeat-sop/** — SOUL-aware workflow guidance
- **heartbeat-unanswered/** — Detects unanswered user questions
- **heartbeat-echo/** — Test stub (disabled by default)

## Deployment

```bash
# Install hook
cp hook/HOOK.yaml ~/.hermes/hooks/heartbeat-orchestrator/HOOK.yaml
cp hook/handler.py ~/.hermes/hooks/heartbeat-orchestrator/handler.py

# Install skills
cp -r heartbeat-* ~/.hermes/skills/

# For profile-specific deployment, symlink:
ln -s ~/.hermes/skills/heartbeat-sop ~/.hermes/profiles/<name>/skills/heartbeat-sop
```

## SKILL.yaml Schema

```yaml
name: heartbeat-sop
description: SOUL-aware workflow guidance
heartbeat:
  enabled: true        # must be true to run
  trigger: agent_end   # informational
  write_back: true     # allow write_back to SKILL.md
config:                # passed to decide() as ctx["config"]
  keywords:
    - temu
    - 入库
```

## decide() Return Shape

```python
{
    "has_followup": True,       # trigger a followup agent turn
    "text": "hint text",        # shown to agent in followup message
    "write_back": {             # optional, requires write_back: true
        "append_md": "last hint: > ..."
    }
}
```
