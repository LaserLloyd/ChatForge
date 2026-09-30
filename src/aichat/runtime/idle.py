"""Unload the local model after ``local.idle_unload_minutes`` without use (PLAN §2 WS6, §3).

Adapted from StudioForge src/studioforge/core/manager.py (MIT, LaserLloyd): the
``_ttl_loop`` / ``_sweep_step`` / ``_sweep_ttl`` shape -- re-check ``ready`` and the
in-flight count under the manager lock before unloading, and "the sweeper must never
die" (every error is logged and the loop carries on).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aichat.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_INTERVAL_S = 15.0


class IdleReaper:
    """Every ``interval_s``, unload the model if it has been idle for ``ttl_s()``.

    ``ttl_s`` is read on every sweep (so a settings change applies without a restart);
    0 or less disables unloading. ``clock`` must be the same monotonic clock the
    manager uses for ``last_activity``. ``sleep`` is injectable for fake-clock tests.
    """

    def __init__(
        self,
        manager: Any,
        ttl_s: Callable[[], float],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self.manager = manager
        self._ttl_s = ttl_s
        self._clock = clock
        self._sleep = sleep
        self.interval_s = interval_s
        self.sweeps = 0
        self.unloads = 0

    async def run(self) -> None:
        """Sweep forever (cancel the task to stop). Never dies on an error."""
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the sweeper must never die
                log.exception("idle_sweep_failed")
            try:
                await self._sleep(self.interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("idle_sleep_failed")
                await asyncio.sleep(self.interval_s)

    def _ttl(self) -> float:
        try:
            return float(self._ttl_s() or 0)
        except Exception:  # noqa: BLE001 - a bad setting disables unloading, not the app
            log.exception("idle_ttl_unreadable")
            return 0.0

    def _eligible(self, ttl: float) -> bool:
        m = self.manager
        if m.state != "ready" or m.in_flight > 0:
            return False  # never while starting/compiling/unloading, or during a request
        return self._clock() - m.last_activity >= ttl

    async def sweep_once(self) -> bool:
        """One check; returns True if the model was unloaded."""
        self.sweeps += 1
        ttl = self._ttl()
        if ttl <= 0:
            return False
        # Cheap pre-check without the lock: a long load holds the lock for minutes.
        if not self._eligible(ttl):
            return False
        m = self.manager
        async with m.lock:
            if not self._eligible(ttl):  # a request may have arrived meanwhile
                return False
            log.info("idle_unload", model_id=m.model_id, ttl_s=ttl)
            await m.unload_locked("idle")
        self.unloads += 1
        return True


__all__ = ["DEFAULT_INTERVAL_S", "IdleReaper"]
