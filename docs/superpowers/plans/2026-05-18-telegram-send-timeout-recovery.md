# Telegram Send Timeout Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the unrecoverable send failure when proxy/network transiently interrupts — after recovery, httpx general connection pool remains stale and all sends fail with TimedOut forever.

**Architecture:** Four-layer defense: (1) classify timeout as connect vs read, (2) connect-timeout → safe to drain + retry, (3) polling reconnect drains general pool too, (4) time-windowed consecutive read-timeout counter triggers drain. All changes confined to `TelegramAdapter` and its tests.

**Tech Stack:** Python 3.12, python-telegram-bot 22.x, httpx, pytest-asyncio

---

## File Structure

| File | Change | Responsibility |
|------|--------|---------------|
| `gateway/platforms/telegram.py:280-300` | Modify | Add instance vars: `_consecutive_send_timeouts`, `_last_send_failure_mono`, `_last_general_drain_mono` |
| `gateway/platforms/telegram.py:482-492` | Modify | Add `_is_connect_timeout()` static method |
| `gateway/platforms/telegram.py:494-527` | Modify | Add `_maybe_drain_general_on_send_failure()` method with time window + cooldown |
| `gateway/platforms/telegram.py:529-607` | Modify | `_handle_polling_network_error()` — also drain general pool |
| `gateway/platforms/telegram.py:1390-1405` | Modify | Inner retry: connect-timeout → drain + retry instead of raise |
| `gateway/platforms/telegram.py:1436-1447` | Modify | Outer except: use `_maybe_drain_general_on_send_failure()` + connect-timeout → retryable |
| `tests/gateway/test_telegram_send_timeout_recovery.py` | Create | All new tests |

---

### Task 1: Add timeout classifier — `_is_connect_timeout()`

**Files:**
- Modify: `gateway/platforms/telegram.py:482-492` (after `_is_pool_timeout_error`)
- Create: `tests/gateway/test_telegram_send_timeout_recovery.py`

- [ ] **Step 1: Write the failing tests**

```python
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


# ── _is_connect_timeout classification ──────────────────────────────────


class TestIsConnectTimeout:
    """Verify _is_connect_timeout detects connect-level timeouts only."""

    def test_connect_timeout_detected(self):
        """Exception chain containing httpcore.ConnectTimeout → True."""
        import httpcore
        import httpx

        httpcore_err = httpcore.ConnectTimeout("connect timed out")
        httpx_err = httpx.ConnectTimeout("connect timed out", request=MagicMock())
        httpx_err.__cause__ = httpcore_err

        assert TelegramAdapter._is_connect_timeout(httpx_err) is True

    def test_read_timeout_not_detected(self):
        """Exception chain containing ReadTimeout but no ConnectTimeout → False."""
        import httpx

        httpx_err = httpx.ReadTimeout("read timed out", request=MagicMock())
        assert TelegramAdapter._is_connect_timeout(httpx_err) is False

    def test_plain_timeout_string_not_detected(self):
        """Plain string 'timed out' without exception chain → False (conservative)."""
        err = RuntimeError("Timed out")
        assert TelegramAdapter._is_connect_timeout(err) is False

    def test_connect_timeout_in_nested_chain(self):
        """ConnectTimeout buried two levels deep → True."""
        import httpcore

        httpcore_err = httpcore.ConnectTimeout("connect timed out")
        middle_err = OSError("proxy fail")
        middle_err.__cause__ = httpcore_err
        top_err = RuntimeError("Timed out")
        top_err.__cause__ = middle_err

        assert TelegramAdapter._is_connect_timeout(top_err) is True

    def test_os_error_without_cause_not_detected(self):
        """Plain OSError without ConnectTimeout in chain → False."""
        err = OSError("Connection refused")
        assert TelegramAdapter._is_connect_timeout(err) is False

    def test_none_is_not_connect_timeout(self):
        """None → False."""
        assert TelegramAdapter._is_connect_timeout(None) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_send_timeout_recovery.py::TestIsConnectTimeout -v`
Expected: FAIL — `AttributeError: type object 'TelegramAdapter' has no attribute '_is_connect_timeout'`

- [ ] **Step 3: Write minimal implementation**

Add the static method after `_is_pool_timeout_error` (after line 492) in `gateway/platforms/telegram.py`:

```python
    @staticmethod
    def _is_connect_timeout(error: Exception) -> bool:
        """Return True if the error originates from a connect-level timeout.

        ConnectTimeout means the TCP connection was never established — no
        HTTP data was sent to Telegram.  This is distinct from a ReadTimeout
        where the request may have reached the server.

        Only returns True when there is **clear evidence** in the exception
        chain (httpcore.ConnectTimeout class name).  When in doubt, returns
        False so the caller falls through to the existing conservative
        behaviour (don't retry, don't drain).
        """
        if error is None:
            return False
        cause = error
        visited = 0
        while cause is not None and visited < 10:
            cls_name = cause.__class__.__name__
            if cls_name == "ConnectTimeout":
                return True
            cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
            visited += 1
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_send_timeout_recovery.py::TestIsConnectTimeout -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/platforms/telegram.py tests/gateway/test_telegram_send_timeout_recovery.py
git commit -m "feat(telegram): add _is_connect_timeout classifier for timeout type detection"
```

---

### Task 2: Add send-failure drain helper — `_maybe_drain_general_on_send_failure()`

**Files:**
- Modify: `gateway/platforms/telegram.py:280-300` (instance vars in `__init__`)
- Modify: `gateway/platforms/telegram.py:494-527` (after `_drain_general_connections`)
- Modify: `tests/gateway/test_telegram_send_timeout_recovery.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/gateway/test_telegram_send_timeout_recovery.py`:

```python
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

    def test_no_drain_on_first_failure(self):
        """A single send failure must NOT drain — could be a one-off timeout."""
        adapter = _make_adapter()
        mock_app, general_req = _make_mock_app_with_general()
        adapter._app = mock_app

        adapter._maybe_drain_general_on_send_failure()
        general_req.shutdown.assert_not_called()

    def test_drain_after_consecutive_failures(self):
        """Two consecutive failures within the window → drain."""
        adapter = _make_adapter()
        mock_app, general_req = _make_mock_app_with_general()
        adapter._app = mock_app

        adapter._maybe_drain_general_on_send_failure()  # 1st
        adapter._maybe_drain_general_on_send_failure()  # 2nd → triggers drain
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
        general_req.shutdown.assert_not_called()

    def test_drain_cooldown_prevents_thrashing(self):
        """After a drain, the next failure should not immediately drain again."""
        adapter = _make_adapter()
        mock_app, general_req = _make_mock_app_with_general()
        adapter._app = mock_app

        adapter._maybe_drain_general_on_send_failure()  # 1st
        adapter._maybe_drain_general_on_send_failure()  # 2nd → drain
        assert general_req.shutdown.call_count == 1

        # Reset count (as if send succeeded after drain), then fail again
        adapter._consecutive_send_timeouts = 0
        adapter._maybe_drain_general_on_send_failure()  # 1st after drain
        adapter._maybe_drain_general_on_send_failure()  # 2nd → but cooldown blocks
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
        general_req.shutdown.assert_not_called()

    def test_noop_without_app(self):
        """Must not raise when _app is None."""
        adapter = _make_adapter()
        adapter._app = None
        adapter._maybe_drain_general_on_send_failure()  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_send_timeout_recovery.py::TestMaybeDrainGeneralOnSendFailure -v`
Expected: FAIL — `AttributeError: 'TelegramAdapter' object has no attribute '_maybe_drain_general_on_send_failure'`

- [ ] **Step 3: Write minimal implementation**

First, add instance variables in `__init__` (around line 297, after `_polling_network_error_count`):

```python
        self._consecutive_send_timeouts: int = 0
        self._last_send_failure_mono: float = 0.0
        self._last_general_drain_mono: float = 0.0
```

Then add methods after `_drain_general_connections` (after line 527):

```python
    _SEND_DRAIN_THRESHOLD = 2
    _SEND_DRAIN_WINDOW_SEC = 60.0
    _SEND_DRAIN_COOLDOWN_SEC = 30.0

    def _maybe_drain_general_on_send_failure(self) -> None:
        """Track consecutive send failures and drain general pool when threshold reached.

        Uses a sliding time window: only failures within ``_SEND_DRAIN_WINDOW_SEC``
        seconds accumulate.  A drain cooldown prevents thrashing when the network
        is still down — once drained, the pool needs time for new connections to
        establish before another drain would be useful.
        """
        now = time.monotonic()

        # Reset if outside the time window
        if now - self._last_send_failure_mono > self._SEND_DRAIN_WINDOW_SEC:
            self._consecutive_send_timeouts = 0

        self._consecutive_send_timeouts += 1
        self._last_send_failure_mono = now

        if self._consecutive_send_timeouts < self._SEND_DRAIN_THRESHOLD:
            return

        # Drain cooldown — don't drain more than once per _SEND_DRAIN_COOLDOWN_SEC
        if now - self._last_general_drain_mono < self._SEND_DRAIN_COOLDOWN_SEC:
            return

        self._consecutive_send_timeouts = 0
        self._last_general_drain_mono = now
        logger.warning(
            "[%s] %d consecutive send timeouts within %.0fs, draining general pool",
            self.name, self._consecutive_send_timeouts, self._SEND_DRAIN_WINDOW_SEC,
        )
        # Fire-and-forget drain — errors are caught inside _drain_general_connections
        asyncio.ensure_future(self._drain_general_connections())

    def _reset_send_timeout_counter(self) -> None:
        """Reset the consecutive send timeout counter (called on successful send)."""
        self._consecutive_send_timeouts = 0
```

Also add `import time` at the top of the file if not already present (it is — line 15 has `import os`, check for `import time`). If not present, add `import time` near the other stdlib imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_send_timeout_recovery.py::TestMaybeDrainGeneralOnSendFailure -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Run all existing telegram tests to check for regressions**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_network.py tests/gateway/test_telegram_network_reconnect.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add gateway/platforms/telegram.py tests/gateway/test_telegram_send_timeout_recovery.py
git commit -m "feat(telegram): add time-windowed send failure drain with cooldown"
```

---

### Task 3: Connect-timeout → drain + retryable in send path

**Files:**
- Modify: `gateway/platforms/telegram.py:1390-1405` (inner retry loop)
- Modify: `gateway/platforms/telegram.py:1436-1447` (outer except)
- Modify: `tests/gateway/test_telegram_send_timeout_recovery.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/gateway/test_telegram_send_timeout_recovery.py`:

```python
# ── Connect-timeout send recovery ───────────────────────────────────────


class TestSendConnectTimeoutRecovery:
    """Connect-timeout on send should drain + mark retryable (message never sent)."""

    def _make_connected_adapter(self):
        """Build an adapter with mock app and bot for send() calls."""
        adapter = _make_adapter()
        mock_general_req = AsyncMock()
        mock_general_req.shutdown = AsyncMock()
        mock_general_req.initialize = AsyncMock()
        mock_polling_req = AsyncMock()
        mock_bot = MagicMock()
        mock_bot._request = (mock_polling_req, mock_general_req)
        mock_bot.send_message = AsyncMock(side_effect=_make_telegram_timeout("connect"))
        mock_updater = MagicMock()
        mock_updater.running = True
        mock_app = MagicMock()
        mock_app.updater = mock_updater
        mock_app.bot = mock_bot
        adapter._app = mock_app
        return adapter, mock_general_req

    def test_connect_timeout_returns_retryable(self):
        """Connect-timeout send result should have retryable=True."""
        adapter, _ = self._make_connected_adapter()
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            adapter.send(chat_id="123", content="hello")
        )
        assert result.retryable is True

    def test_connect_timeout_triggers_general_drain(self):
        """Connect-timeout should drain the general connection pool."""
        adapter, general_req = self._make_connected_adapter()
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            adapter.send(chat_id="123", content="hello")
        )
        general_req.shutdown.assert_called()


def _make_telegram_timeout(kind: str = "connect") -> Exception:
    """Build a telegram.error.TimedOut with the right exception chain.

    kind='connect' → chain contains httpcore.ConnectTimeout
    kind='read'    → chain contains httpcore.ReadTimeout
    kind='plain'   → no chain (plain TimedOut)
    """
    import httpcore

    telegram_err = _import_telegram_timed_out()
    if kind == "connect":
        httpcore_err = httpcore.ConnectTimeout("connect timed out")
        mapped_err = Exception("Timed out")
        mapped_err.__cause__ = httpcore_err
        return telegram_err("Timed out")
    elif kind == "read":
        httpcore_err = httpcore.ReadTimeout("read timed out")
        mapped_err = Exception("Timed out")
        mapped_err.__cause__ = httpcore_err
        return telegram_err("Timed out")
    else:
        return telegram_err("Timed out")


def _import_telegram_timed_out():
    """Import telegram.error.TimedOut or a mock equivalent."""
    try:
        from telegram.error import TimedOut
        return TimedOut
    except ImportError:
        class TimedOut(Exception):
            pass
        return TimedOut
```

**Note:** The tests above use `asyncio.get_event_loop().run_until_complete()` for simplicity. If the project uses `pytest-asyncio`, convert them to `async def test_` with `@pytest.mark.asyncio`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_send_timeout_recovery.py::TestSendConnectTimeoutRecovery -v`
Expected: FAIL — connect-timeout currently returns `retryable=False` and does not drain

- [ ] **Step 3: Write minimal implementation**

Modify the inner retry loop (around line 1390-1405) to detect connect-timeout:

```python
                        # TimedOut is also a subclass of NetworkError but
                        # indicates the request may have reached the server —
                        # retrying risks duplicate message delivery.
                        # Exception: pool timeout means no request was sent,
                        # so draining and retrying is safe.
                        # Exception: connect timeout means TCP was never
                        # established — message definitely not sent.
                        if _TimedOut and isinstance(send_err, _TimedOut):
                            if self._is_pool_timeout_error(send_err):
                                logger.warning(
                                    "[%s] Pool timeout on send (attempt %d/3), draining general pool",
                                    self.name, _send_attempt + 1,
                                )
                                await self._drain_general_connections()
                                if _send_attempt < 2:
                                    await asyncio.sleep(1)
                                    continue
                            elif self._is_connect_timeout(send_err):
                                logger.warning(
                                    "[%s] Connect timeout on send (attempt %d/3), draining general pool and retrying",
                                    self.name, _send_attempt + 1,
                                )
                                await self._drain_general_connections()
                                if _send_attempt < 2:
                                    await asyncio.sleep(1)
                                    continue
                            raise
```

Modify the outer except (around line 1436-1447):

```python
        except Exception as e:
            logger.error("[%s] Failed to send Telegram message: %s", self.name, e, exc_info=True)
            # TimedOut means the request may have reached Telegram —
            # mark as non-retryable so _send_with_retry() doesn't re-send.
            # Exception: pool timeout means no request was sent — safe to retry.
            # Exception: connect timeout means TCP never connected — safe to retry.
            _to = locals().get("_TimedOut")
            err_str = str(e).lower()
            is_timeout = (_to and isinstance(e, _to)) or "timed out" in err_str
            is_pool_timeout = self._is_pool_timeout_error(e)
            is_connect_timeout = self._is_connect_timeout(e)
            if is_pool_timeout or is_connect_timeout:
                await self._drain_general_connections()
            if is_timeout and not is_pool_timeout and not is_connect_timeout:
                self._maybe_drain_general_on_send_failure()
            else:
                self._reset_send_timeout_counter()
            retryable = is_pool_timeout or is_connect_timeout or not is_timeout
            return SendResult(success=False, error=str(e), retryable=retryable)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_send_timeout_recovery.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run existing network tests for regressions**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_network.py tests/gateway/test_telegram_network_reconnect.py -v`
Expected: All PASS — except `test_reconnect_drains_polling_request_only` which asserts general pool is NOT drained. This test will be updated in Task 4.

- [ ] **Step 6: Commit**

```bash
git add gateway/platforms/telegram.py tests/gateway/test_telegram_send_timeout_recovery.py
git commit -m "feat(telegram): connect-timeout drains general pool + marks retryable"
```

---

### Task 4: Polling reconnect also drains general pool

**Files:**
- Modify: `gateway/platforms/telegram.py:574` (after `_drain_polling_connections()`)
- Modify: `tests/gateway/test_telegram_network_reconnect.py` (update existing test)
- Modify: `tests/gateway/test_telegram_send_timeout_recovery.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/gateway/test_telegram_send_timeout_recovery.py`:

```python
# ── Polling reconnect drains general pool ───────────────────────────────


class TestPollingReconnectDrainsGeneral:
    """When polling detects network error, general pool should also be drained."""

    def test_reconnect_drains_general_pool_too(self):
        """_handle_polling_network_error must drain general pool alongside polling pool."""
        adapter = _make_adapter()
        adapter._polling_network_error_count = 1

        mock_general_req = AsyncMock()
        mock_general_req.shutdown = AsyncMock()
        mock_general_req.initialize = AsyncMock()
        mock_polling_req = AsyncMock()
        mock_polling_req.shutdown = AsyncMock()
        mock_polling_req.initialize = AsyncMock()
        mock_bot = MagicMock()
        mock_bot._request = (mock_polling_req, mock_general_req)
        mock_bot.get_me = AsyncMock(return_value=MagicMock())
        mock_updater = MagicMock()
        mock_updater.running = True
        mock_updater.stop = AsyncMock()
        mock_updater.start_polling = AsyncMock()
        mock_app = MagicMock()
        mock_app.updater = mock_updater
        mock_app.bot = mock_bot
        adapter._app = mock_app

        with patch("asyncio.sleep", new_callable=AsyncMock):
            asyncio.get_event_loop().run_until_complete(
                adapter._handle_polling_network_error(Exception("Bad Gateway"))
            )

        mock_polling_req.shutdown.assert_called_once()
        mock_general_req.shutdown.assert_called_once()
```

- [ ] **Step 2: Update existing test that asserts general pool is NOT drained**

In `tests/gateway/test_telegram_network_reconnect.py`, update `test_reconnect_drains_polling_request_only` (line 199-227):

Change the test name and the assertion. The test currently asserts `general_req.shutdown.assert_not_called()`. After the fix, it SHOULD be called. Update:

```python
@pytest.mark.asyncio
async def test_reconnect_drains_both_pools():
    """During reconnect, both polling and general request pools must be cycled.

    When polling detects a network error, the general pool may also contain
    stale connections (e.g. from a proxy interruption) that would cause
    subsequent send_message calls to fail.  Both pools must be drained.
    """
    adapter = _make_adapter()
    adapter._polling_network_error_count = 1

    mock_app, mock_polling_req = _make_mock_app()
    adapter._app = mock_app

    general_req = mock_app.bot._request[1]

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await adapter._handle_polling_network_error(Exception("Bad Gateway"))

    # Both pools must be drained
    mock_polling_req.shutdown.assert_called_once()
    mock_polling_req.initialize.assert_called_once()
    general_req.shutdown.assert_called_once()
    general_req.initialize.assert_called_once()

    # Reconnect must still succeed
    mock_app.updater.start_polling.assert_called_once()
    assert adapter._polling_network_error_count == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_network_reconnect.py::test_reconnect_drains_polling_request_only tests/gateway/test_telegram_send_timeout_recovery.py::TestPollingReconnectDrainsGeneral -v`
Expected: FAIL — general pool not drained yet

- [ ] **Step 4: Write minimal implementation**

In `_handle_polling_network_error`, after `await self._drain_polling_connections()` (line 574), add:

```python
        await self._drain_polling_connections()
        await self._drain_general_connections()
```

- [ ] **Step 5: Run all telegram network tests**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_network.py tests/gateway/test_telegram_network_reconnect.py tests/gateway/test_telegram_send_timeout_recovery.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add gateway/platforms/telegram.py tests/gateway/test_telegram_network_reconnect.py tests/gateway/test_telegram_send_timeout_recovery.py
git commit -m "feat(telegram): polling reconnect also drains general connection pool"
```

---

### Task 5: Reset send timeout counter on successful send

**Files:**
- Modify: `gateway/platforms/telegram.py:1360` (after `break  # success`)
- Modify: `tests/gateway/test_telegram_send_timeout_recovery.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/gateway/test_telegram_send_timeout_recovery.py`:

```python
# ── Send timeout counter reset on success ───────────────────────────────


class TestSendTimeoutCounterReset:
    """Successful send must reset the consecutive timeout counter."""

    def test_counter_resets_on_success(self):
        """After a failed then successful send, the counter must be 0."""
        adapter = _make_adapter()
        adapter._consecutive_send_timeouts = 1

        # Simulate successful send by calling the reset directly
        adapter._reset_send_timeout_counter()
        assert adapter._consecutive_send_timeouts == 0
```

- [ ] **Step 2: Run test — should pass (reset method exists from Task 2)**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_send_timeout_recovery.py::TestSendTimeoutCounterReset -v`
Expected: PASS

- [ ] **Step 3: Wire `_reset_send_timeout_counter()` into the send success path**

In the inner retry loop, after `break  # success` (around line 1360), add the reset:

```python
                        break  # success
                else:
                    # for-loop completed without break — should not happen
                    pass
                self._reset_send_timeout_counter()
```

Wait — the `break` is inside the retry for-loop. The reset should happen when `msg is not None` (send succeeded). The cleanest place is right after the retry for-loop, before the message_ids append:

After the `for _send_attempt in range(3):` loop (which ends with `break  # success` on line 1360), add reset:

```python
                    else:
                        raise
                # If we got here with a non-None msg, send succeeded
                if msg is not None:
                    self._reset_send_timeout_counter()
                message_ids.append(str(msg.message_id))
```

Actually, looking at the code flow: the `break  # success` exits the for-loop, then `message_ids.append(str(msg.message_id))` runs. So insert the reset between the for-loop and the append:

Find:
```python
                        break  # success
                    except _NetErr as send_err:
```

Wait, the `break` is inside the try block. Let me re-read the structure:

```python
                for _send_attempt in range(3):
                    try:
                        ...
                        msg = await self._bot.send_message(...)
                        ...
                        break  # success
                    except _NetErr as send_err:
                        ...
                    except Exception as send_err:
                        ...
                message_ids.append(str(msg.message_id))
```

So after the for-loop, if `break` was hit, `msg` is set and the next line is `message_ids.append(...)`. Add the reset there:

After the for-loop's closing line and before `message_ids.append`, insert:

```python
                # Send succeeded — reset consecutive timeout counter
                self._reset_send_timeout_counter()
                message_ids.append(str(msg.message_id))
```

- [ ] **Step 4: Run all tests**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/test_telegram_send_timeout_recovery.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/platforms/telegram.py tests/gateway/test_telegram_send_timeout_recovery.py
git commit -m "feat(telegram): reset send timeout counter on successful send"
```

---

### Task 6: Full regression suite

**Files:** None (verification only)

- [ ] **Step 1: Run the complete gateway test suite**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/gateway/ -v --timeout=60`
Expected: All PASS

- [ ] **Step 2: Run the full project test suite (if CI exists)**

Run: `cd /mnt/f/Projects/hermes-agent && python -m pytest tests/ -x --timeout=120 -q 2>&1 | tail -30`
Expected: No failures related to telegram or network tests

---

## Self-Review Checklist

### Spec Coverage
- [x] Connect-timeout detection → Task 1
- [x] Connect-timeout → drain + retryable → Task 3
- [x] Time-windowed consecutive read-timeout → drain → Task 2
- [x] Polling reconnect drains general pool → Task 4
- [x] Send success resets counter → Task 5
- [x] Drain cooldown → Task 2
- [x] Time window prevents cross-session accumulation → Task 2

### Placeholder Scan
- No TBD/TODO found
- All test code is complete
- All implementation code is complete
- No "similar to" references

### Type Consistency
- `_is_connect_timeout(error: Exception) -> bool` — used consistently in Task 3
- `_maybe_drain_general_on_send_failure()` — no args, reads instance vars — consistent
- `_reset_send_timeout_counter()` — no args, resets instance vars — consistent
- `_consecutive_send_timeouts: int` — int throughout
- `_last_send_failure_mono: float` — float throughout
- `_last_general_drain_mono: float` — float throughout
