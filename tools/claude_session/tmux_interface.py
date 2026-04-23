"""tools/claude_session/tmux_interface.py — Low-level tmux operations."""

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


class TmuxInterface:
    """Encapsulates all tmux CLI interactions for a single session."""

    def __init__(self, session_name: str):
        self.session_name = session_name

    def _run(self, args: list, timeout: int = 10) -> subprocess.CompletedProcess:
        """Run a tmux command."""
        cmd = ["tmux"] + args
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error("tmux command timed out: %s", " ".join(args))
            raise
        except FileNotFoundError:
            raise RuntimeError("tmux is not installed or not in PATH")

    def session_exists(self) -> bool:
        """Check if the tmux session exists."""
        r = self._run(["has-session", "-t", self.session_name])
        return r.returncode == 0

    def create_session(self, workdir: str, env: Optional[dict] = None) -> str:
        """Create a new detached tmux session. Returns session name."""
        cmd = [
            "new-session",
            "-d",
            "-s", self.session_name,
            "-c", workdir,
        ]
        if env:
            for k, v in env.items():
                cmd.extend(["-e", f"{k}={v}"])
        r = self._run(cmd)
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create tmux session: {r.stderr}")
        return self.session_name

    def capture_pane(self, lines: int = 200) -> str:
        """Capture visible pane output. Returns raw text with ANSI codes."""
        r = self._run([
            "capture-pane",
            "-t", self.session_name,
            "-p",
            "-S", f"-{lines}",
        ])
        return r.stdout if r.returncode == 0 else ""

    def send_keys(self, text: str, enter: bool = False) -> None:
        """Send text to the tmux session, optionally pressing Enter.

        Uses send-keys -l for literal text (no special key name interpretation).
        This avoids the need for manual escaping since subprocess.run passes
        arguments directly without shell interpretation.
        """
        cmd = ["send-keys", "-t", self.session_name, "-l", text]
        self._run(cmd)
        if enter:
            self.send_special_key("Enter")

    def send_special_key(self, key: str) -> None:
        """Send a special key sequence (e.g., C-c, C-d, Enter)."""
        self._run(["send-keys", "-t", self.session_name, key])

    def kill_session(self) -> None:
        """Kill the tmux session."""
        self._run(["kill-session", "-t", self.session_name])