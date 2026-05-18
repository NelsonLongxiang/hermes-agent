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


# ── _maybe_drain_general_on_send_failure ────────────────────────────────


def _make_mock_app_with_general():
    """Build a mock Application with separable polling and general requests."""
    mock_general_req = AsyncMock()
    mock_general_req.shutdown = AsyncMock()
    mock_general_req.initialize = AsyncMock()

    mock_polling_req = AsyncMock()
    mock_polling_req.shutdown = AsyncMock()
    mock_polling_req.initialize = AsyncMock()

    mock_bot = MagicMock()
    mock_bot._request = (mock_polling_req, mock_general_req)

    mock_updater = MagicMock()
    mock_updater.running = True

    mock_app = MagicMock()
    mock_app.updater = mock_updater
    mock_app.bot = mock_bot
    return mock_app, mock_general_req


class TestMaybeDrainGeneralOnSendFailure:
    """Time-windowed consecutive-failure drain with cooldown."""

    @staticmethod
    def _pump_loop():
        """Run the default event loop briefly so asyncio.ensure_future tasks execute."""
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.sleep(0))

    def test_no_drain_on_first_failure(self):
        """A single send failure must NOT drain — could be a one-off timeout."""
        adapter = _make_adapter()
        mock_app, general_req = _make_mock_app_with_general()
        adapter._app = mock_app

        adapter._maybe_drain_general_on_send_failure()
        self._pump_loop()
        general_req.shutdown.assert_not_called()

    def test_drain_after_consecutive_failures(self):
        """Two consecutive failures within the window → drain."""
        adapter = _make_adapter()
        mock_app, general_req = _make_mock_app_with_general()
        adapter._app = mock_app

        adapter._maybe_drain_general_on_send_failure()  # 1st
        adapter._maybe_drain_general_on_send_failure()  # 2nd → triggers drain
        self._pump_loop()
        general_req.shutdown.assert_called_once()
        general_req.initialize.assert_called_once()

    def test_no_drain_if_failures_outside_time_window(self):
        """Failures far apart (outside 60s window) must not accumulate."""
        adapter = _make_adapter()
        mock_app, general_req = _make_mock_app_with_general()
        adapter._app = mock_app

        adapter._maybe_drain_general_on_send_failure()
        # Simulate the previous failure was long ago
        adapter._last_send_failure_mono = time.monotonic() - 120.0
        adapter._maybe_drain_general_on_send_failure()  # outside window → count resets
        self._pump_loop()
        general_req.shutdown.assert_not_called()

    def test_drain_cooldown_prevents_thrashing(self):
        """After a drain, the next failure should not immediately drain again."""
        adapter = _make_adapter()
        mock_app, general_req = _make_mock_app_with_general()
        adapter._app = mock_app

        adapter._maybe_drain_general_on_send_failure()  # 1st
        adapter._maybe_drain_general_on_send_failure()  # 2nd → drain
        self._pump_loop()
        assert general_req.shutdown.call_count == 1

        # Reset count (as if send succeeded after drain), then fail again
        adapter._consecutive_send_timeouts = 0
        adapter._maybe_drain_general_on_send_failure()  # 1st after drain
        adapter._maybe_drain_general_on_send_failure()  # 2nd → but cooldown blocks
        self._pump_loop()
        # Cooldown should prevent second drain — still only 1 shutdown call
        assert general_req.shutdown.call_count == 1

    def test_success_resets_counter(self):
        """After a successful send (reset_send_timeout_counter), next failures start from 0."""
        adapter = _make_adapter()
        mock_app, general_req = _make_mock_app_with_general()
        adapter._app = mock_app

        adapter._maybe_drain_general_on_send_failure()  # count=1
        adapter._reset_send_timeout_counter()             # reset
        adapter._maybe_drain_general_on_send_failure()  # count=1 again (fresh)
        self._pump_loop()
        general_req.shutdown.assert_not_called()

    def test_noop_without_app(self):
        """Must not raise when _app is None."""
        adapter = _make_adapter()
        adapter._app = None
        adapter._maybe_drain_general_on_send_failure()  # should not raise
