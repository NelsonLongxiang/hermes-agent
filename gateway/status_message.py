"""Gateway status message manager — maintains a single edited Telegram status message.

Shows real-time Claude Code activity (state, tool calls, elapsed time)
as a continuously-edited message. Replaced with a summary on completion.
"""

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("gateway.status_message")


class StatusMessageManager:
    """Manages a single Telegram status message that is continuously edited.

    Usage::

        mgr = StatusMessageManager(adapter, chat_id, metadata)
        await mgr.update({"state": "THINKING", ...})
        await mgr.update({"state": "TOOL_CALL", "tool_name": "Read", ...})
        await mgr.finish("Done. 3 tool calls, 45s")
    """

    # Throttling
    _MIN_EDIT_INTERVAL = 2.0      # seconds between edits
    _MAX_EDITS_PER_SESSION = 30   # hard cap to avoid rate limits
    _MAX_FLOOD_STRIKES = 3        # consecutive flood failures → stop editing
    _MAX_MESSAGE_CHARS = 500      # Telegram status message length cap

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        metadata: Optional[dict] = None,
    ):
        self._adapter = adapter
        self._chat_id = chat_id
        self._metadata = metadata
        self._message_id: Optional[str] = None
        self._last_edit_time: float = 0.0
        self._edit_count: int = 0
        self._flood_strikes: int = 0
        self._edit_disabled: bool = False
        self._last_state: Optional[str] = None
        self._last_tool_name: Optional[str] = None

    async def update(self, status_info: dict) -> None:
        """Send or edit the status message with new info.

        Throttled: skips if called too fast or flood-control active.
        """
        if self._edit_disabled:
            return

        # Throttle: skip if too soon after last edit (unless first send)
        now = time.monotonic()
        if (
            self._message_id is not None
            and (now - self._last_edit_time) < self._MIN_EDIT_INTERVAL
        ):
            return

        # Deduplicate: skip if state and tool name unchanged
        new_state = status_info.get("state")
        new_tool = status_info.get("tool_name")
        if (
            self._message_id is not None
            and new_state == self._last_state
            and new_tool == self._last_tool_name
        ):
            return

        # Edit cap
        if self._edit_count >= self._MAX_EDITS_PER_SESSION:
            return

        text = self._format_status(status_info)

        try:
            if self._message_id is None:
                # First message — send new
                result = await self._adapter.send(
                    chat_id=self._chat_id,
                    content=text,
                    metadata=self._metadata,
                )
                if result.success and result.message_id:
                    self._message_id = str(result.message_id)
                    self._last_edit_time = time.monotonic()
                    self._edit_count += 1
            else:
                # Subsequent — edit existing
                result = await self._adapter.edit_message(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    content=text,
                )
                if result.success:
                    self._last_edit_time = time.monotonic()
                    self._edit_count += 1
                    self._flood_strikes = 0
                else:
                    self._handle_edit_failure(result)

            self._last_state = new_state
            self._last_tool_name = new_tool

        except Exception as e:
            logger.debug("Status message update error: %s", e)

    async def finish(self, summary: str) -> None:
        """Replace the status message with a final summary."""
        if not self._message_id:
            return
        try:
            await self._adapter.edit_message(
                chat_id=self._chat_id,
                message_id=self._message_id,
                content=summary[:self._MAX_MESSAGE_CHARS],
            )
        except Exception as e:
            logger.debug("Status message finish error: %s", e)

    async def cancel(self) -> None:
        """Best-effort delete the status message on error/cancellation."""
        if not self._message_id:
            return
        try:
            await self._adapter.edit_message(
                chat_id=self._chat_id,
                message_id=self._message_id,
                content="(cancelled)",
            )
        except Exception:
            pass

    def _handle_edit_failure(self, result) -> None:
        """Handle an edit failure — track flood strikes."""
        err = getattr(result, "error", "") or ""
        err_lower = err.lower()
        is_flood = "flood" in err_lower or "retry after" in err_lower
        if is_flood:
            self._flood_strikes += 1
            if self._flood_strikes >= self._MAX_FLOOD_STRIKES:
                self._edit_disabled = True
                logger.debug("Status message: flood-control disabled editing")

    def _format_status(self, info: dict) -> str:
        """Format status info into a display string (≤500 chars)."""
        state = info.get("state", "THINKING")
        tool_name = info.get("tool_name")
        tool_target = info.get("tool_target")
        turn_id = info.get("turn_id", "?")
        elapsed = info.get("elapsed_seconds", 0)
        tool_calls = info.get("tool_calls", [])
        recent_output = info.get("recent_output", "")

        parts = []

        if state == "THINKING":
            parts.append("\U0001f914 思考中...")
        elif state == "TOOL_CALL":
            parts.append("\U0001f527 执行工具...")
        elif state == "PERMISSION":
            parts.append("\u26a0\ufe0f 等待授权...")
        else:
            parts.append(f"\U0001f916 {state}")

        # Tool call details (max 3 recent)
        if tool_calls:
            for tc in tool_calls[-3:]:
                name = tc.get("tool", tc.get("name", "?"))
                target = tc.get("target", "")
                if target:
                    line = f"\u25cf {name} {target}"
                else:
                    line = f"\u25cf {name}"
                parts.append(line)
        elif tool_name:
            target_str = f" {tool_target}" if tool_target else ""
            parts.append(f"\u25cf {tool_name}{target_str}")

        # Recent output (1-2 lines if meaningful and space allows)
        if recent_output and state == "THINKING":
            lines = [l.strip() for l in recent_output.split("\n") if l.strip()]
            if lines:
                tail = lines[-1][:80]
                parts.append(tail)

        # Footer
        elapsed_str = self._format_elapsed(elapsed)
        parts.append(f"\u23f1 {elapsed_str} | turn {turn_id}")

        text = "\n".join(parts)
        return text[:self._MAX_MESSAGE_CHARS]

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        """Format elapsed seconds as human-readable string."""
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m {s}s"
        h, m = divmod(m, 60)
        return f"{h}h {m}m"

    def format_summary(self, status_info: dict) -> str:
        """Format the completion summary."""
        elapsed = status_info.get("elapsed_seconds", 0)
        tool_calls = status_info.get("tool_calls", [])
        turn_id = status_info.get("turn_id", "?")

        # Count tools by name
        tool_counts: dict[str, int] = {}
        for tc in tool_calls:
            name = tc.get("tool", tc.get("name", "?"))
            tool_counts[name] = tool_counts.get(name, 0) + 1

        parts = ["\u2705 完成"]
        if tool_counts:
            tool_parts = [
                f"\u25cf {name} x{count}"
                for name, count in tool_counts.items()
            ]
            parts.append("  ".join(tool_parts[:5]))

        elapsed_str = self._format_elapsed(elapsed)
        parts.append(f"\u23f1 {elapsed_str} | turn {turn_id}")
        return "\n".join(parts)
