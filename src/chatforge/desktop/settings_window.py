"""The settings window: one normal, resizable 900x700 window, at most one instance."""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from chatforge.desktop import theme, win32util, xutil

_log = logging.getLogger(__name__)

TITLE = "ChatForge Settings"
WIDTH = 900
HEIGHT = 700
MIN_SIZE = (640, 480)


class SettingsWindow:
    """Creates the window on first ``open()`` (after ``webview.start()``) and re-uses it."""

    def __init__(
        self,
        *,
        url: str,
        api: Any,
        events: Any = None,
        on_opening: Callable[[], None] | None = None,
        theme_slug: Callable[[], str | None] | None = None,
        title: str = TITLE,
    ) -> None:
        self._url = url
        self._api = api
        self._events = events
        self._on_opening = on_opening
        #: The configured theme, so the window opens in its colour.
        self._theme_slug = theme_slug
        self._title = title
        self._lock = threading.Lock()
        self.window: Any = None

    @property
    def is_open(self) -> bool:
        return self.window is not None

    @property
    def hwnd(self) -> int | None:
        return win32util.hwnd_of(self.window) if self.window is not None else None

    def open(self) -> Any:
        """Show the settings window, creating it if needed. Safe from any thread."""
        import webview

        if self._on_opening is not None:
            with contextlib.suppress(Exception):
                self._on_opening()
        with self._lock:
            window = self.window
            if window is not None:
                self._raise(window)
                return window
            slug = None
            if self._theme_slug is not None:
                with contextlib.suppress(Exception):
                    slug = self._theme_slug()
            window = webview.create_window(
                self._title,
                url=self._url,
                js_api=self._api,
                width=WIDTH,
                height=HEIGHT,
                min_size=MIN_SIZE,
                resizable=True,
                text_select=True,
                **theme.window_options(slug),
            )
            self.window = window
            window.events.closed += self._on_closed
            if self._events is not None:
                self._events.attach(window)
            _log.info("settings window opened")
            return window

    def _raise(self, window: Any) -> None:
        try:
            window.show()
            if xutil.is_linux() and not win32util.IS_WINDOWS:
                xutil.present(getattr(window, "native", None))  # GTK: raise and focus
                return
            hwnd = win32util.hwnd_of(window)
            if hwnd is not None and win32util.IS_WINDOWS:
                win32util.force_foreground(hwnd)
        except Exception as exc:  # noqa: BLE001
            _log.debug("could not raise the settings window: %s", type(exc).__name__)

    def _on_closed(self, *_args: Any) -> None:
        with self._lock:
            window, self.window = self.window, None
        if window is not None and self._events is not None:
            self._events.detach(window)
        _log.info("settings window closed")

    def close(self) -> None:
        with self._lock:
            window = self.window
        if window is not None:
            with contextlib.suppress(Exception):
                window.destroy()
