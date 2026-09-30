"""The chat popup: a hidden, frameless, on-top pywebview window placed at the lower right.

Show/hide/toggle/pin per PLAN WS7 step 4. Placement is recomputed on every ``show`` from
the cursor's monitor work area and the window's live DPI, then applied with
``SetWindowPos`` on the native HWND (never pywebview's logical x/y). Hide-on-blur comes
from the page (a 150 ms debounced ``blur`` listener that calls ``hide_popup``, already
ignoring pinned, ``hide_on_blur=false`` and "settings is opening"); the host adds the
tray-click-after-blur guard: a tray click within 400 ms of a blur-hide keeps the popup
hidden, so a click on the icon toggles instead of flickering.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from aichat.desktop import win32util

_log = logging.getLogger(__name__)

TITLE = "AI Chat"
#: A tray click this soon after a hide keeps the popup hidden (a toggle, not a flicker).
TRAY_CLICK_GUARD_S = 0.4
#: Blur-hides are ignored for this long after Settings starts opening.
SETTINGS_OPENING_S = 1.5


class Popup:
    """Owns the popup window. Thread-safe: every public method may run on any thread."""

    def __init__(
        self,
        *,
        url: str,
        api: Any,
        config: Callable[[], Any],
        events: Any,
        on_shown: Callable[[], None] | None = None,
        title: str = TITLE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._api = api
        self._config = config
        self._events = events
        self._on_shown = on_shown
        self._title = title
        self._clock = clock
        self._lock = threading.RLock()
        self.window: Any = None
        self.pinned = False
        self._visible = False
        self._last_hide_at: float = float("-inf")
        self._last_hide_reason: str | None = None
        self._settings_opening_until: float = float("-inf")
        self._blur_suspended = 0
        self._corners_done = False
        self._destroying = False

    # --- creation ---------------------------------------------------------------------

    def create(self) -> Any:
        """Create the (hidden) window. Call before ``webview.start()``."""
        import webview

        cfg = self._config()
        ui = cfg.ui
        self.window = webview.create_window(
            self._title,
            url=self._url,
            js_api=self._api,
            width=int(ui.width),
            height=int(ui.height),
            min_size=(320, 400),
            resizable=False,
            frameless=True,
            easy_drag=False,  # §7 item 5: text selection must not drag the window
            on_top=True,
            hidden=True,
            text_select=True,
            background_color="#1b1e26",
        )
        self.window.events.closing += self._on_closing
        self.window.events.closed += self._on_closed
        if self._events is not None:
            self._events.attach(self.window)
        return self.window

    def _on_closing(self, *_args: Any) -> bool:
        """Alt+F4 / WM_CLOSE hides the popup instead of destroying it."""
        if self._destroying:
            return True
        self.hide(reason="close")
        return False

    def _on_closed(self, *_args: Any) -> None:
        if self._events is not None and self.window is not None:
            self._events.detach(self.window)
        self._visible = False

    def destroy(self) -> None:
        window = self.window
        if window is None:
            return
        self._destroying = True
        with contextlib.suppress(Exception):
            window.destroy()

    # --- state ------------------------------------------------------------------------

    @property
    def hwnd(self) -> int | None:
        return win32util.hwnd_of(self.window) if self.window is not None else None

    @property
    def visible(self) -> bool:
        hwnd = self.hwnd
        if hwnd is not None and win32util.IS_WINDOWS:
            try:
                return win32util.is_window_visible(hwnd)
            except Exception:  # noqa: BLE001
                pass
        return self._visible

    def set_pinned(self, flag: bool) -> bool:
        self.pinned = bool(flag)
        return self.pinned

    def note_settings_opening(self) -> None:
        """Blur-hides are ignored while the settings window is coming up."""
        self._settings_opening_until = self._clock() + SETTINGS_OPENING_S

    @contextlib.contextmanager
    def suspend_blur(self) -> Iterator[None]:
        """Ignore blur-hides while a native dialog is up."""
        with self._lock:
            self._blur_suspended += 1
        try:
            yield
        finally:
            with self._lock:
                self._blur_suspended -= 1

    def _blur_ignored(self) -> bool:
        cfg = self._config()
        if self.pinned or self._blur_suspended > 0:
            return True
        if not getattr(getattr(cfg, "ui", None), "hide_on_blur", True):
            return True
        return self._clock() < self._settings_opening_until

    # --- placement ---------------------------------------------------------------------

    def target_rect(self) -> win32util.Rect | None:
        """Where the popup goes right now (pure placement over live monitor data)."""
        hwnd = self.hwnd
        if hwnd is None or not win32util.IS_WINDOWS:
            return None
        ui = self._config().ui
        dpi = win32util.dpi_for_window(hwnd)
        work = win32util.work_area_at_cursor()
        return win32util.place(work, dpi, int(ui.width), int(ui.height), int(ui.margin))

    def place(self) -> win32util.Rect | None:
        hwnd = self.hwnd
        rect = self.target_rect()
        if hwnd is None or rect is None:
            return None
        win32util.set_window_pos(hwnd, rect, topmost=True, activate=False)
        if not self._corners_done:
            try:
                self._corners_done = win32util.set_rounded_corners(hwnd)
            except Exception as exc:  # noqa: BLE001 - DWM may be off (RDP, safe mode)
                _log.debug("rounded corners unavailable: %s", type(exc).__name__)
                self._corners_done = True
        return rect

    # --- show / hide / toggle ------------------------------------------------------------

    def show(self, source: str = "app") -> None:
        """Place, show, bring to the foreground, then emit ``popup.shown``."""
        with self._lock:
            window = self.window
            if window is None:
                return
            rect = None
            try:
                rect = self.place()
            except Exception as exc:  # noqa: BLE001 - placement must never block showing
                _log.warning("popup placement failed: %s", type(exc).__name__)
            try:
                window.show()
            except Exception as exc:  # noqa: BLE001 - window not created yet
                _log.warning("popup show failed: %s", type(exc).__name__)
                return
            self._visible = True
            hwnd = self.hwnd
            if hwnd is not None and win32util.IS_WINDOWS:
                try:
                    win32util.force_foreground(hwnd)
                except Exception as exc:  # noqa: BLE001
                    _log.debug("foreground failed: %s", type(exc).__name__)
            _log.debug("popup shown source=%s rect=%s", source, rect.as_tuple() if rect else None)
        if self._events is not None:
            self._events.emit({"type": "popup.shown"})
        if self._on_shown is not None:
            try:
                self._on_shown()
            except Exception:  # noqa: BLE001
                _log.exception("on_shown hook failed")

    def hide(self, reason: str = "app") -> None:
        """Hide. ``reason`` is ``blur``/``escape``/``close`` from the page, ``tray``, ``app``."""
        with self._lock:
            window = self.window
            if window is None:
                return
            if reason == "blur" and self._blur_ignored():
                return
            try:
                window.hide()
            except Exception as exc:  # noqa: BLE001
                _log.warning("popup hide failed: %s", type(exc).__name__)
                return
            self._visible = False
            self._last_hide_at = self._clock()
            self._last_hide_reason = reason

    def hide_from_js(self) -> None:
        """The page asked to hide (blur debounce, Escape or the close button)."""
        self.hide(reason="js")

    def toggle(self, source: str = "tray") -> bool:
        """Show if hidden, hide if visible. Returns the new visibility.

        A tray click that lands within :data:`TRAY_CLICK_GUARD_S` of a page-initiated hide
        is the second half of "click the icon while the popup is open": the blur already
        hid it, so the click must not bring it straight back.
        """
        with self._lock:
            if self.visible:
                self.hide(reason=source)
                return False
            recently_hidden = self._clock() - self._last_hide_at < TRAY_CLICK_GUARD_S
            if source == "tray" and recently_hidden and self._last_hide_reason == "js":
                return False
        self.show(source=source)
        return True
