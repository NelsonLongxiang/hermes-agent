"""tools/claude_session/tmux_interface.py — Low-level tmux operations."""

import logging
import os
import signal
import subprocess
import time
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

        IMPORTANT: This method performs NO delays. The caller is responsible
        for timing — multi-line text needs a delay before Enter to allow
        bracketed paste to complete, but that delay must NOT be inside a lock.
        """
        cmd = ["send-keys", "-t", self.session_name, "-l", text]
        self._run(cmd)
        if enter:
            self.send_special_key("Enter")

    def send_special_key(self, key: str) -> None:
        """Send a special key sequence (e.g., C-c, C-d, Enter)."""
        self._run(["send-keys", "-t", self.session_name, key])

    def kill_session(self) -> None:
        """Kill the tmux session and terminate all child processes.
        
        This is critical for preventing resource leaks: tmux kill-session
        destroys the session but leaves child processes (like Claude Code)
        running as orphans. We must explicitly terminate them first.
        """
        # Step 1: Get the pane PID (the shell process)
        pane_pid = None
        try:
            r = self._run([
                "list-panes",
                "-t", self.session_name,
                "-F", "#{pane_pid}"
            ], timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                pane_pid = int(r.stdout.strip())
                logger.debug("Session %s pane PID: %d", self.session_name, pane_pid)
        except Exception as e:
            logger.warning("Failed to get pane PID for %s: %s", self.session_name, e)

        # Step 2: Find and kill the Claude Code process group
        if pane_pid:
            try:
                # Get the process group ID
                pgid = os.getpgid(pane_pid)
                logger.debug("Session %s PGID: %d", self.session_name, pgid)
                
                # Send SIGTERM to the entire process group (graceful shutdown)
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    logger.info("Sent SIGTERM to PGID %d for session %s", pgid, self.session_name)
                except ProcessLookupError:
                    # Process already gone - that's fine
                    logger.debug("PGID %d already terminated", pgid)
                    pgid = None
                
                # Step 3: Wait up to 3 seconds for graceful termination
                if pgid:
                    for _ in range(30):  # 30 * 0.1s = 3 seconds
                        try:
                            os.killpg(pgid, 0)  # Check if process group exists
                            time.sleep(0.1)
                        except ProcessLookupError:
                            # Process group terminated successfully
                            logger.debug("PGID %d terminated gracefully", pgid)
                            break
                    else:
                        # Step 4: Force kill with SIGKILL if still running
                        logger.warning("PGID %d did not terminate gracefully, sending SIGKILL", pgid)
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                            time.sleep(0.5)  # Brief wait for SIGKILL to take effect
                        except ProcessLookupError:
                            pass  # Already gone
                        
            except ProcessLookupError:
                logger.debug("Pane PID %d no longer exists", pane_pid)
            except PermissionError:
                logger.warning("No permission to kill PGID for PID %d", pane_pid)
            except Exception as e:
                logger.warning("Failed to kill process group: %s", e)

        # Step 5: Finally kill the tmux session
        try:
            self._run(["kill-session", "-t", self.session_name], timeout=5)
            logger.info("Tmux session %s killed", self.session_name)
        except Exception as e:
            logger.warning("Failed to kill tmux session %s: %s", self.session_name, e)