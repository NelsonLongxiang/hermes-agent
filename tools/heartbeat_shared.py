"""Shared utilities for heartbeat skills discovery.

Used by:
  - tools/heartbeat_guide_tool.py (active tool path)
  - optional-skills/heartbeat/hook/handler.py (reference hook impl)
"""
import importlib.util
import logging
import sys
from pathlib import Path

import yaml

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)


def discover_heartbeat_skills() -> list:
    """Return list of (name, module, hb_config, state_md_path, skill_config).

    Each entry comes from ~/.hermes/skills/heartbeat-*/ containing both
    SKILL.yaml (with heartbeat.enabled: true) and decide.py.
    """
    skills_dir = get_hermes_home() / "skills"
    found = []
    if not skills_dir.exists():
        return found
    for skill_dir in sorted(skills_dir.iterdir()):
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
            if mod_name in sys.modules:
                mod = sys.modules[mod_name]
            else:
                spec = importlib.util.spec_from_file_location(mod_name, decide_py)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
            state_md = skill_dir / "SKILL.md"
            skill_cfg = meta.get("config") or {}
            found.append((skill_dir.name, mod, hb, state_md, skill_cfg))
        except Exception as e:
            logger.debug("heartbeat: failed to load %s: %s", skill_dir.name, e)
    return found
