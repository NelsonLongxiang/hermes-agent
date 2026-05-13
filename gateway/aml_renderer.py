"""AML (AI Markup Language) detection and rendering for Hermes Gateway.

Bridges the Rust AML parser (installed as `aml` CLI) with the Python
gateway.  When AI output contains AML directives the content is rendered
to Telegram HTML via subprocess and sent with ParseMode.HTML instead of
MarkdownV2.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any, Dict, Optional

logger = logging.getLogger("gateway.aml_renderer")

# AML directive keywords that start with '@'
_AML_DIRECTIVES = frozenset([
    "warn", "danger", "info", "ok", "card", "detail",
    "metric", "progress", "btn", "confirm",
    "form", "field", "chart", "tabs", "tab", "cols", "if",
    "else",  # @else inside @if blocks
])

# Matches @keyword at line start (possibly indented) followed by a
# delimiter or end-of-line — excludes email addresses like user@host.
_AML_PATTERN = re.compile(
    r"(?:^|\n)\s*@(?:/)?(" + "|".join(re.escape(k) for k in _AML_DIRECTIVES) + r")"
    r"(?:\[|\{|\s|$|:)",
    re.MULTILINE,
)

# Also detect inline $badge.color[...] and $color[...] patterns
_INLINE_STYLE_PATTERN = re.compile(r"\$(?:badge|bg|icon)\.[a-z]+\[")

# Cache: whether the AML CLI is available (None = not yet checked)
_aml_cli_available: Optional[bool] = None


def _check_aml_cli() -> bool:
    """Check once whether the AML CLI is available."""
    global _aml_cli_available
    if _aml_cli_available is not None:
        return _aml_cli_available
    cli_path = os.environ.get("AML_CLI_PATH", "aml")
    _aml_cli_available = shutil.which(cli_path) is not None
    if not _aml_cli_available:
        logger.debug("AML CLI not found in PATH — AML rendering disabled")
    return _aml_cli_available


def is_aml_content(text: str) -> bool:
    """Return True if *text* contains AML directives.

    Uses a conservative heuristic: the text must contain an ``@keyword``
    pattern that matches a known AML directive.  Bare email addresses
    (``user@host``) and common social-media ``@mentions`` are excluded
    because the pattern requires ``@`` to be at line start.
    """
    if not text:
        return False
    if not _check_aml_cli():
        return False
    if _AML_PATTERN.search(text):
        return True
    if _INLINE_STYLE_PATTERN.search(text):
        return True
    return False


async def render_aml_telegram(text: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """Render AML *text* to Telegram format via the ``aml`` CLI.

    Non-blocking: uses asyncio subprocess to avoid blocking the event loop.

    Returns a dict ``{"text": ..., "keyboard": ...}`` on success,
    or ``None`` on any failure (CLI not found, parse error, timeout).
    """
    try:
        cli_path = os.environ.get("AML_CLI_PATH", "aml")
        proc = await asyncio.create_subprocess_exec(
            cli_path, "render", "--telegram",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=text.encode()),
            timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning("AML CLI returned %d: %s", proc.returncode, stderr.decode().strip())
            return None
        output = json.loads(stdout.decode())
        if "text" not in output:
            logger.warning("AML CLI output missing 'text' field")
            return None
        return output
    except FileNotFoundError:
        logger.debug("AML CLI not found in PATH — skipping AML rendering")
        return None
    except asyncio.TimeoutError:
        logger.warning("AML CLI timed out after %.1fs", timeout)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("AML rendering failed: %s", exc)
        return None
