"""The core thread: one asyncio loop that owns every service (PLAN 1, "Threads").

``js_api`` calls arrive on pywebview worker threads and are forwarded here with
:meth:`CoreLoop.run` (``run_coroutine_threadsafe(...).result(timeout)``) for short work.
Long work is submitted with :meth:`CoreLoop.submit` and reports through events.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

_log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_TIMEOUT_S = 30.0


class CoreLoop:
    """An asyncio event loop running on a dedicated daemon thread."""

    def __init__(self, name: str = "chatforge-core") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("core loop is not running")
        return self._loop

    @property
    def running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

    def start(self) -> CoreLoop:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                self._cancel_all(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._loop = None

    @staticmethod
    def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def stop(self, timeout: float = 5.0) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=timeout)
        self._thread = None

    # --- calling in ------------------------------------------------------------------

    def in_loop_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = DEFAULT_TIMEOUT_S) -> T:
        """Run a coroutine on the loop and wait for its result (from another thread)."""
        if self.in_loop_thread():
            raise RuntimeError("CoreLoop.run() called from the loop thread; await instead")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"core loop call timed out after {timeout}s") from None

    def call(
        self, fn: Callable[..., T], *args: Any, timeout: float | None = DEFAULT_TIMEOUT_S
    ) -> T:
        """Run a plain function on the loop thread and wait for its result."""

        async def _wrap() -> T:
            return fn(*args)

        return self.run(_wrap(), timeout)

    def submit(
        self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
    ) -> concurrent.futures.Future[Any]:
        """Fire-and-forget a coroutine; exceptions are logged, not raised."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done(f: concurrent.futures.Future[Any]) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                _log.error("background task %s failed: %r", name or coro, exc, exc_info=exc)

        future.add_done_callback(_done)
        return future

    def create_task(
        self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        """``asyncio.create_task`` that keeps a strong reference and logs an exception the
        task dies with (loop thread only)."""
        task = self.loop.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_failure)
        return task

    @staticmethod
    def _log_failure(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error("background task %s failed: %r", task.get_name(), exc, exc_info=exc)
