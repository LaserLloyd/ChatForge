"""Python -> JS events: a queue, 50 ms delta coalescing and one dispatch thread (PLAN 1.5).

``EventSink.emit(evt)`` is safe from any thread. A single dispatch thread drains the
queue, waits up to :data:`COALESCE_MS` for more, merges consecutive ``chat.delta`` events
of the same request (content and reasoning text are concatenated, order preserved), and
delivers each batch with **one** ``window.run_js(...)`` per open window (§7 item 6:
``run_js``, never ``evaluate_js``, which uses ``eval`` and blocks). Delivery goes through
``window.__chatforge.emit`` (installed by ``bridge.js`` at module evaluation) and is guarded
with ``window.__chatforge &&`` so an event that arrives before the page has booted is dropped
rather than raising.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)

COALESCE_MS = 50
_STOP = object()

Deliver = Callable[[str], None]


def coalesce(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge runs of ``chat.delta`` events that share a ``request_id``.

    Pure. Only *consecutive* deltas of the same request are merged, so ordering relative
    to every other event (``chat.phase``, ``chat.tool_call`` ...) is untouched. Merged
    events carry ``content`` and/or ``reasoning`` only when at least one input had them.
    """
    out: list[dict[str, Any]] = []
    for evt in events:
        if (
            evt.get("type") == "chat.delta"
            and out
            and out[-1].get("type") == "chat.delta"
            and out[-1].get("request_id") == evt.get("request_id")
        ):
            merged = out[-1]
            for field in ("content", "reasoning"):
                piece = evt.get(field)
                if piece:
                    merged[field] = (merged.get(field) or "") + piece
            continue
        out.append(dict(evt))
    return out


def batch_script(events: list[dict[str, Any]]) -> str:
    """The JS that hands a batch to the page. One statement, no eval."""
    payload = json.dumps(events, ensure_ascii=True, default=str)
    return (
        "(function(evs){if(!window.__chatforge||typeof window.__chatforge.emit!=='function')return;"
        "for(const e of evs){try{window.__chatforge.emit(e)}catch(err){console.error(err)}}})("
        + payload
        + ")"
    )


class EventSink:
    """Thread-safe event queue with a dispatch thread that pushes batches to the windows.

    ``deliver`` (tests) replaces the default delivery, which calls ``run_js`` on every
    window registered with :meth:`attach`. Windows are pywebview windows or anything with
    a ``run_js(script)`` method.
    """

    def __init__(
        self,
        *,
        deliver: Deliver | None = None,
        coalesce_ms: float = COALESCE_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._deliver = deliver
        self._coalesce_s = max(0.0, coalesce_ms) / 1000.0
        self._clock = clock
        self._windows: list[Any] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.batches_sent = 0

    # --- windows --------------------------------------------------------------------

    def attach(self, window: Any) -> None:
        with self._lock:
            if window not in self._windows:
                self._windows.append(window)

    def detach(self, window: Any) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._windows.remove(window)

    def windows(self) -> list[Any]:
        with self._lock:
            return list(self._windows)

    # --- producers ------------------------------------------------------------------

    def emit(self, evt: dict[str, Any]) -> None:
        """Queue one event (``{"type": ..., ...}``). Never blocks, never raises."""
        if not isinstance(evt, dict) or not isinstance(evt.get("type"), str):
            _log.warning("dropping malformed event: %r", evt)
            return
        self._queue.put(evt)

    def emitter(self, event_type: str) -> Callable[..., None]:
        """A callback that emits ``event_type`` with the given fields (``cb(**fields)``)."""

        def _cb(*args: Any, **fields: Any) -> None:
            data: dict[str, Any] = {}
            for arg in args:
                if isinstance(arg, dict):
                    data.update(arg)
            data.update(fields)
            data["type"] = event_type
            self.emit(data)

        return _cb

    # --- lifecycle ------------------------------------------------------------------

    def start(self) -> EventSink:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="chatforge-events", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._queue.put(_STOP)
        thread.join(timeout=timeout)
        self._thread = None

    # --- dispatch -------------------------------------------------------------------

    def _collect(self) -> list[dict[str, Any]] | None:
        """Block for the first event, then gather what arrives within the window."""
        first = self._queue.get()
        if first is _STOP:
            return None
        batch = [first]
        deadline = self._clock() + self._coalesce_s
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if item is _STOP:
                self._queue.put(_STOP)  # leave it for the loop to see after this batch
                break
            batch.append(item)
        return batch

    def _run(self) -> None:
        while True:
            try:
                batch = self._collect()
            except Exception:  # noqa: BLE001 - the dispatcher must never die
                _log.exception("event collection failed")
                continue
            if batch is None:
                return
            try:
                self.dispatch(batch)
            except Exception:  # noqa: BLE001
                _log.exception("event dispatch failed")

    def dispatch(self, events: list[dict[str, Any]]) -> None:
        """Coalesce and deliver one batch (public for tests)."""
        merged = coalesce(events)
        if not merged:
            return
        script = batch_script(merged)
        self.batches_sent += 1
        if self._deliver is not None:
            self._deliver(script)
            return
        for window in self.windows():
            try:
                window.run_js(script)
            except Exception as exc:  # noqa: BLE001 - a closed window must not stop others
                _log.debug("run_js failed on %r: %s", window, type(exc).__name__)
