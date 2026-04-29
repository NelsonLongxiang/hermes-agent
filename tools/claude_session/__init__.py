"""Claude Session — context pipeline for Claude Code."""

from tools.claude_session.session import ClaudeSession

# Backward compatibility alias
ClaudeSessionManager = ClaudeSession

__all__ = ["ClaudeSession", "ClaudeSessionManager"]
