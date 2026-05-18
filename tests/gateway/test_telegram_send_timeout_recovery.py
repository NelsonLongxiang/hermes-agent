"""Tests for Telegram send timeout recovery — connect-timeout classification
and general connection pool drain on send failures.

Regression tests for the proxy-interruption unrecoverable send failure.
"""
import asyncio
import time
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from gateway.platforms.telegram import TelegramAdapter  # noqa: E402


@pytest.fixture(autouse=True)
def _no_auto_discovery(monkeypatch):
    async def _noop():
        return []
    monkeypatch.setattr("gateway.platforms.telegram.discover_fallback_ips", _noop)


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))


# -- _is_connect_timeout classification ----------------------------------------


class TestIsConnectTimeout:
    """Verify _is_connect_timeout detects connect-level timeouts only."""

    def test_connect_timeout_detected(self):
        """Exception chain containing httpcore.ConnectTimeout -> True."""
        import httpcore
        import httpx

        httpcore_err = httpcore.ConnectTimeout("connect timed out")
        httpx_err = httpx.ConnectTimeout("connect timed out", request=MagicMock())
        httpx_err.__cause__ = httpcore_err

        assert TelegramAdapter._is_connect_timeout(httpx_err) is True

    def test_read_timeout_not_detected(self):
        """Exception chain containing ReadTimeout but no ConnectTimeout -> False."""
        import httpx

        httpx_err = httpx.ReadTimeout("read timed out", request=MagicMock())
        assert TelegramAdapter._is_connect_timeout(httpx_err) is False

    def test_plain_timeout_string_not_detected(self):
        """Plain string 'timed out' without exception chain -> False (conservative)."""
        err = RuntimeError("Timed out")
        assert TelegramAdapter._is_connect_timeout(err) is False

    def test_connect_timeout_in_nested_chain(self):
        """ConnectTimeout buried two levels deep -> True."""
        import httpcore

        httpcore_err = httpcore.ConnectTimeout("connect timed out")
        middle_err = OSError("proxy fail")
        middle_err.__cause__ = httpcore_err
        top_err = RuntimeError("Timed out")
        top_err.__cause__ = middle_err

        assert TelegramAdapter._is_connect_timeout(top_err) is True

    def test_os_error_without_cause_not_detected(self):
        """Plain OSError without ConnectTimeout in chain -> False."""
        err = OSError("Connection refused")
        assert TelegramAdapter._is_connect_timeout(err) is False

    def test_none_is_not_connect_timeout(self):
        """None -> False."""
        assert TelegramAdapter._is_connect_timeout(None) is False
