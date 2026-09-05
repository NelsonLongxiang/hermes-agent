"""Periodic process memory usage logging for the gateway.

Emits one grep-friendly ``[MEMORY] ...`` line every N seconds (default 300)
from a daemon thread so slow leaks show up as an RSS time series in the logs.
A baseline snapshot is logged on start and a final one on stop.  Uses stdlib
``resource`` first, ``psutil`` as fallback (Windows); if neither works the
monitor warns once and stays disabled.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_BYTES_TO_MB = 1024 * 1024

# RSS guardrails — enabled via environment variables (default: disabled).
# Soft limit: trigger gc.collect() when exceeded.
# Hard limit: os._exit(137) to let the service manager restart the process
# before the kernel OOM killer strikes indiscriminately.
_RSS_SOFT_LIMIT_MB: Optional[int] = (
    int(v) if (v := os.getenv("HERMES_RSS_LIMIT_MB", "0").strip()).isdigit() and int(v) > 0 else None
)
_RSS_HARD_LIMIT_MB: Optional[int] = (
    int(v) if (v := os.getenv("HERMES_RSS_HARD_LIMIT_MB", "0").strip()).isdigit() and int(v) > 0 else None
)

_monitor_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_start_time: Optional[float] = None
_lock = threading.Lock()


def _get_rss_mb() -> Optional[int]:
    """Return process RSS high-water mark in MB, or None if unavailable."""
    try:
        import resource

        # ru_maxrss is KB on Linux but bytes on macOS.
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (_BYTES_TO_MB if sys.platform == "darwin" else 1024))
    except Exception:
        pass
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss / _BYTES_TO_MB)
    except Exception:
        return None


def _get_current_rss_mb() -> Optional[int]:
    """Return the *current* RSS in MB (not the high-water mark).

    Used for limit enforcement where accuracy matters — ``ru_maxrss``
    never decreases within a process lifetime, so it would trigger
    false-positive kills after a transient spike.
    """
    # On Linux, /proc/self/status has VmRSS which is the actual current RSS.
    if sys.platform == "linux":
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # "VmRSS:    123456 kB"
                        kb = int(line.split()[1])
                        return int(kb / 1024)
        except Exception:
            pass
    # Fallback: psutil gives current RSS on all platforms.
    try:
        import psutil  # type: ignore

        rss = psutil.Process(os.getpid()).memory_info().rss
        return int(rss / _BYTES_TO_MB)
    except Exception:
        pass
    # Last resort: ru_maxrss (high-water mark, less accurate for limits).
    return _get_rss_mb()


def log_memory_usage(prefix: str = "") -> None:
    """Log ``[MEMORY] [<prefix> ]rss=... gc=... threads=... uptime=...``; safe from any thread."""
    rss = _get_rss_mb()
    logger.info(
        "[MEMORY] %srss=%s gc=%s threads=%d uptime=%ds", f"{prefix} " if prefix else "",
        "unavailable" if rss is None else f"{rss}MB", gc.get_count(), threading.active_count(),
        int(time.monotonic() - _start_time) if _start_time else 0,
    )


def _check_rss_limits() -> None:
    """Check RSS against soft/hard limits and act if exceeded.

    Called from the monitor thread.  Soft limit triggers ``gc.collect()``;
    hard limit calls ``os._exit(137)`` (mimics OOM-kill so service managers
    treat it as a crash and restart the gateway).
    """
    rss = _get_current_rss_mb()
    if rss is None:
        return
    if _RSS_SOFT_LIMIT_MB and rss > _RSS_SOFT_LIMIT_MB:
        logger.warning(
            "[MEMORY] RSS %dMB exceeds soft limit %dMB — triggering gc",
            rss, _RSS_SOFT_LIMIT_MB,
        )
        gc.collect()
    if _RSS_HARD_LIMIT_MB and rss > _RSS_HARD_LIMIT_MB:
        log_memory_usage(prefix="oom-guard")
        logger.error(
            "[MEMORY] RSS %dMB exceeds hard limit %dMB — exiting to prevent OOM kill",
            rss, _RSS_HARD_LIMIT_MB,
        )
        os._exit(137)  # 128+9 = SIGKILL exit code


def _monitor_loop(stop_event: threading.Event, interval: float) -> None:
    """Background thread body — log every ``interval`` seconds until stopped."""
    while not stop_event.wait(interval):
        try:
            log_memory_usage()
            _check_rss_limits()
        except Exception as e:
            # Never let the monitor crash the gateway; just log and carry on.
            logger.debug("Memory monitor iteration failed: %s", e)


def start_memory_monitoring(interval_seconds: float = 300.0) -> bool:
    """Start periodic logging in a daemon thread (baseline logged immediately).  False if
    already running or RSS introspection is unavailable (warned once)."""
    global _monitor_thread, _stop_event, _start_time

    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return False
        if _get_rss_mb() is None:
            logger.warning(
                "[MEMORY] Memory monitoring unavailable: neither resource.getrusage nor psutil could read process RSS "
                "— skipping periodic logging.",
            )
            return False
        _start_time = time.monotonic()
        _stop_event = threading.Event()
        log_memory_usage(prefix="baseline")
        _monitor_thread = threading.Thread(
            target=_monitor_loop, args=(_stop_event, float(interval_seconds)), name="gateway-memory-monitor", daemon=True
        )
        _monitor_thread.start()

        logger.info(
            "[MEMORY] Periodic memory monitoring started (interval: %ds, soft_limit=%sMB, hard_limit=%sMB)",
            int(interval_seconds),
            _RSS_SOFT_LIMIT_MB or "off",
            _RSS_HARD_LIMIT_MB or "off",
        )
        return True


def stop_memory_monitoring(timeout: float = 2.0) -> None:
    """Stop the monitor thread and log a final ``shutdown`` snapshot. No-op if never started."""
    global _monitor_thread, _stop_event

    with _lock:
        if _stop_event is None or _monitor_thread is None:
            return
        with contextlib.suppress(Exception):
            log_memory_usage(prefix="shutdown")
        _stop_event.set()
        thread, _monitor_thread, _stop_event = _monitor_thread, None, None
    with contextlib.suppress(Exception):  # join outside the lock so a stuck log call can't deadlock the stop path
        thread.join(timeout=timeout)
    logger.info("[MEMORY] Periodic memory monitoring stopped")


def is_running() -> bool:
    """True if the background monitor thread is alive."""
    with _lock:
        return _monitor_thread is not None and _monitor_thread.is_alive()
