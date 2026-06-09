"""Heartbeat orchestrator — runs heartbeat-* skills after agent:end.

Discover every `heartbeat-*` skill under ~/.hermes/skills/. For each,
call decide(ctx) concurrently (return_exceptions=True). Merge the
returned hints as a single system-role message and append to the
session. Each skill owns its own SKILL.md state writes.

SKILL.yaml schema (heartbeat section):
  enabled:     bool  — must be true to run
  trigger:     str   — informational only (orchestrator fires on agent:end)
  inject_as:   str   — informational only (orchestrator always uses system)
  write_back:  bool  — when true, skill may return write_back: {append_md: str}
                       and the orchestrator appends it to SKILL.md with a
                       timestamp + marker.  Default false.

Decide() return shape:
  has_followup: bool
  text:         str   — shown in injected hint
  write_back:   dict  — optional.  When SKILL.yaml heartbeat.write_back
                        is true and this is present, the orchestrator
                        appends write_back["append_md"] to SKILL.md.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermes_cli.config import get_hermes_home

SKILLS_DIR = get_hermes_home() / "skills"


def _discover_heartbeat_skills() -> list:
    """Return list of (name, module, skill_yaml_dict, state_md_path)."""
    found = []
    if not SKILLS_DIR.exists():
        return found
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir() or not skill_dir.name.startswith("heartbeat-"):
            continue
        manifest = skill_dir / "SKILL.yaml"
        decide_py = skill_dir / "decide.py"
        if not manifest.exists() or not decide_py.exists():
            continue
        try:
            meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            hb = (meta.get("heartbeat") or {})
            if not hb.get("enabled", False):
                continue
            mod_name = f"heartbeat_skill_{skill_dir.name.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(mod_name, decide_py)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            state_md = skill_dir / "SKILL.md"
            skill_cfg = meta.get("config") or {}
            found.append((skill_dir.name, mod, hb, state_md, skill_cfg))
        except Exception as e:
            print(f"[heartbeat] Failed to load {skill_dir.name}: {e}", flush=True)
    return found


async def _run_one(name, mod, hb, state_md, skill_cfg, ctx):
    """Run a single skill's decide(), never let it propagate."""
    # Pass SKILL.yaml config into ctx so decide() can read tuned parameters.
    if skill_cfg:
        ctx = {**ctx, "config": skill_cfg}
    try:
        return await mod.decide(ctx, state_md) if asyncio.iscoroutinefunction(mod.decide) \
            else mod.decide(ctx, state_md)
    except Exception as e:
        print(f"[heartbeat] Skill {name} failed: {e}", flush=True)
        return None


async def handle(event_type, context):
    """agent:end handler. Discovers skills, runs decide() in parallel, merges hints."""
    if event_type != "agent:end":
        return
    ctx = context or {}
    sid = ctx.get("session_id")
    if not sid:
        return
    # Guard against infinite followup loops: if this agent:end was triggered
    # by a heartbeat followup turn, the message starts with "[heartbeat]".
    # Check the last user message — if it's a heartbeat followup, skip.
    try:
        from hermes_state import SessionDB
        _db = SessionDB()
        _msgs = _db.get_messages(sid) or []
    except Exception:
        _msgs = []
    ctx["messages"] = _msgs
    # Check if the last user-role message is a heartbeat followup trigger
    for _m in reversed(_msgs[-3:]):
        if _m.get("role") == "user" and isinstance(_m.get("content"), str) \
                and _m["content"].startswith("[heartbeat]"):
            return  # We're inside a followup turn — don't trigger again
    skills = _discover_heartbeat_skills()
    if not skills:
        return

    results = await asyncio.gather(
        *[_run_one(n, m, h, s, c, ctx) for (n, m, h, s, c) in skills],
        return_exceptions=True,
    )
    hints = []
    for (name, _, _, _, _), r in zip(skills, results):
        if not r or not isinstance(r, dict):
            continue
        if not r.get("has_followup"):
            continue
        text = (r.get("text") or "").strip()
        if not text:
            continue
        hints.append(f"[hint: {name}] {text}")
    if not hints:
        return
    merged = "\n\n".join(hints)
    _trigger_followup = False
    try:
        # Dedup: compare the hint text about to be injected against the
        # last hint recorded in any skill's SKILL.md (write_back stores
        # the latest hint in overwrite mode).
        _last_hint = ""
        for (_, _, hb, state_md, _), r in zip(skills, results):
            if not isinstance(r, dict) or not hb.get("write_back", False):
                continue
            if state_md.exists():
                try:
                    _last_hint = state_md.read_text(encoding="utf-8")
                except Exception:
                    pass
            break
        # Extract just the hint text from merged (strip "[hint: name] " prefix)
        _hint_body = merged.split("] ", 1)[-1] if "] " in merged else merged
        if _last_hint and _hint_body.strip() and _hint_body.strip() in _last_hint:
            print(f"[heartbeat] Deduped repeat hint for session {sid[:12]}…", flush=True)
            return
        print(f"[heartbeat] Injected {len(hints)} hint(s) into session {sid[:12]}…", flush=True)
        _trigger_followup = True
    except Exception as e:
        print(f"[heartbeat] Inject failed for {sid[:12]}…: {e}", flush=True)

    # ── Q2 write_back: skills with heartbeat.write_back=true and a
    # write_back.append_md payload get a state overwrite on SKILL.md.  Failures
    # here MUST NOT block other skills' writes.
    for (name, _, hb, state_md, _), r in zip(skills, results):
        if not r or not isinstance(r, dict):
            continue
        if not hb.get("write_back", False):
            continue
        wb = r.get("write_back") or {}
        append_md = (wb.get("append_md") or "").strip()
        if not append_md:
            continue
        try:
            _ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            state_md.parent.mkdir(parents=True, exist_ok=True)
            # Overwrite (not append) so SKILL.md always shows only the latest
            # heartbeat state — no unbounded growth.
            with state_md.open("w", encoding="utf-8") as _f:
                _f.write(f"<!-- heartbeat write_back {name} @ {_ts} -->\n")
                _f.write(append_md + "\n")
            print(f"[heartbeat] write_back ok for {name} → {state_md.name}", flush=True)
        except Exception as e:
            print(f"[heartbeat] write_back failed for {name}: {e}", flush=True)

    if _trigger_followup:
        return {"trigger_followup": True, "hints": hints}
