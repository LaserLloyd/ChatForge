"""Compile installed models for the NPU in the background, while the runtime is idle.

The first NPU load of a model compiles it (66 s for Qwen2.5-1.5B and 123 s for
Qwen3-4B in the app; minutes with ``GENERATE_HINT=BEST_PERF``). OVMS caches the result
in the model's ``--cache_dir`` and later loads import it in 3-11 s. The cache works;
what hurt was *when* the compile happened: on the first chat after a download, a model
switch, a reload-required setting change or an OVMS upgrade, with the user waiting.

:class:`Precompiler` moves that wait out of the chat. It runs on the core loop next to
the idle reaper (``LocalModelManager.start`` starts it) and, whenever the runtime is
idle, compiles each installed model whose cache is cold through
:meth:`LocalModelManager.precompile`: the same OVMS command line a chat would use, so the
blob it leaves is exactly the one a chat load imports. Then it unloads the model again.

Rules
-----
* Only while nothing is loaded, loading or in flight. A chat that arrives meanwhile joins
  the compile (same model, and the model stays loaded) or cancels it (another model).
* Only on devices whose compile is slow (:data:`PRECOMPILE_DEVICES`), and only with
  ``local.precompile`` on.
* The selected model first, then the most recently used ones. An avoid-listed model only
  once the user has picked or used it; it compiles for the device it would run on.
* Only folders the downloader or adoption verified (they have a sidecar) or that loaded
  before, so a model that is still being copied in is never compiled half-written.
* A failed compile is not retried for :data:`RETRY_FAILED_AFTER_S` (``state.json``
  ``precompile_failed``); a compile that keeps being cancelled is retried at most
  :data:`MAX_ATTEMPTS` times per run.
* New downloads are noticed by :meth:`Registry.fingerprint` (cheap; a full scan only
  when it changes), so a model is compiled within one :data:`INTERVAL_S` of arriving.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aichat.llm.errors import LLMError
from aichat.logging_setup import get_logger
from aichat.runtime import compile_cache

log = get_logger(__name__)

#: Wait after app start before the first check (let startup and a first chat go first).
FIRST_DELAY_S = 60.0
#: Time between checks.
INTERVAL_S = 120.0
#: Starts of one compile per run before giving up on it (cancellations count).
MAX_ATTEMPTS = 2
#: A compile that failed is not tried in the background again for this long.
RETRY_FAILED_AFTER_S = 3 * 86400.0
#: Devices whose first load is a long compile worth doing ahead of time.
PRECOMPILE_DEVICES = ("NPU", "GPU")
#: ``state.json`` key of failed background compiles: ``{cache_key: {"at", "error"}}``.
STATE_KEY = "precompile_failed"


class Precompiler:
    """Background compile loop for one :class:`LocalModelManager`."""

    def __init__(
        self,
        manager: Any,
        *,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        first_delay_s: float = FIRST_DELAY_S,
        interval_s: float = INTERVAL_S,
    ) -> None:
        self.manager = manager
        self._wall = wall_clock
        self._sleep = sleep
        self.first_delay_s = first_delay_s
        self.interval_s = interval_s
        self._attempts: dict[str, int] = {}
        self._fingerprint: Any = None
        self.compiled: list[str] = []

    # --- loop -------------------------------------------------------------

    async def run(self) -> None:
        """Check forever (cancel the task to stop). Never dies on an error."""
        await self._sleep(self.first_delay_s)
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a background chore must never die
                log.exception("precompile_sweep_failed")
            await self._sleep(self.interval_s)

    def enabled(self) -> bool:
        return bool(self.manager.setting("local.precompile", True))

    async def sweep_once(self) -> list[str]:
        """Compile every cold candidate while the runtime stays idle; return their ids."""
        done: list[str] = []
        if not self.enabled() or not self.manager.installed():
            return done
        if not self.manager.idle_for_background():
            return done
        self._refresh_registry()
        for model_id, key in self.candidates():
            if not self.manager.idle_for_background():
                break
            self._attempts[key] = self._attempts.get(key, 0) + 1
            log.info("precompile_start", model_id=model_id, attempt=self._attempts[key])
            try:
                outcome = await self.manager.precompile(model_id)
            except LLMError as exc:
                self._record_failure(key, exc)
                log.warning(
                    "precompile_failed",
                    model_id=model_id,
                    error=exc.message,
                    hint=(exc.hint or "")[:200],
                )
                continue
            log.info("precompile_done", model_id=model_id, outcome=outcome)
            if outcome == "compiled":
                done.append(model_id)
                self.compiled.append(model_id)
                self._clear_failure(key)
            elif outcome in ("busy", "cancelled"):
                break  # the user needs the runtime: try again at the next idle check
        return done

    # --- candidates ---------------------------------------------------------

    def _refresh_registry(self) -> None:
        """Rescan the model folders when they changed (a download finished, a copy-in)."""
        registry = self.manager.registry
        fingerprint = getattr(registry, "fingerprint", None)
        if registry is None or fingerprint is None:
            return
        try:
            current = fingerprint()
        except Exception:  # noqa: BLE001
            return
        if current != self._fingerprint:
            with contextlib.suppress(Exception):
                registry.scan()
            self._fingerprint = current

    def candidates(self) -> list[tuple[str, str]]:
        """``(model id, cache key)`` of every model to compile, most wanted first."""
        m = self.manager
        registry = m.registry
        if registry is None:
            return []
        try:
            records = [r for r in registry.all() if getattr(r, "complete", False)]
        except Exception:  # noqa: BLE001
            return []
        selected = m.setting("chat.model", None)
        failed = compile_cache.load_state(self._state_file()).get(STATE_KEY) or {}
        now = self._wall()

        def rank(rec: Any) -> tuple:
            used_at = getattr(rec, "last_used_at", None) or 0.0
            return (0 if rec.id == selected else 1, -float(used_at), rec.id.casefold())

        out: list[tuple[str, str]] = []
        for rec in sorted(records, key=rank):
            used = rec.id == selected or getattr(rec, "last_used_at", None) is not None
            if getattr(rec, "source", None) is None and not used:
                continue  # unverified folder: compile on first use instead
            if not used and self._avoided(rec.id):
                continue
            try:
                spec = m.build_spec(rec.id)
            except Exception:  # noqa: BLE001 - not loadable as configured: skip it
                continue
            if not any(dev in spec.device.upper() for dev in PRECOMPILE_DEVICES):
                continue
            try:
                if m.is_warm(spec):
                    continue
                key = m.cache_key(spec)
            except Exception:  # noqa: BLE001
                continue
            if self._attempts.get(key, 0) >= MAX_ATTEMPTS:
                continue
            entry = failed.get(key) if isinstance(failed, dict) else None
            at = entry.get("at") if isinstance(entry, dict) else None
            if isinstance(at, int | float) and now - at < RETRY_FAILED_AFTER_S:
                continue
            out.append((rec.id, key))
        return out

    def _avoided(self, model_id: str) -> bool:
        try:
            return self.manager.catalog.badge(model_id).badge == "avoid"
        except Exception:  # noqa: BLE001
            return False

    # --- state.json ---------------------------------------------------------

    def _state_file(self) -> Path | None:
        sf = getattr(self.manager.paths, "state_file", None)
        return Path(sf) if sf is not None else None

    def _record_failure(self, key: str, exc: LLMError) -> None:
        entry = {"at": self._wall(), "error": f"{exc.message} {exc.hint or ''}".strip()[:300]}

        def mutate(state: dict) -> None:
            bucket = state.get(STATE_KEY)
            if not isinstance(bucket, dict):
                bucket = state[STATE_KEY] = {}
            bucket[key] = entry

        with contextlib.suppress(Exception):
            compile_cache.update_state(self._state_file(), mutate)

    def _clear_failure(self, key: str) -> None:
        state = compile_cache.load_state(self._state_file())
        if key not in (state.get(STATE_KEY) or {}):
            return

        def mutate(data: dict) -> None:
            bucket = data.get(STATE_KEY)
            if isinstance(bucket, dict):
                bucket.pop(key, None)
                if not bucket:
                    data.pop(STATE_KEY, None)

        with contextlib.suppress(Exception):
            compile_cache.update_state(self._state_file(), mutate)


__all__ = [
    "FIRST_DELAY_S",
    "INTERVAL_S",
    "MAX_ATTEMPTS",
    "PRECOMPILE_DEVICES",
    "RETRY_FAILED_AFTER_S",
    "STATE_KEY",
    "Precompiler",
]
