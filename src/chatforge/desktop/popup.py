"""The chat popup: a hidden, frameless, on-top pywebview window placed at the lower right.

Show/hide/toggle/pin per PLAN WS7 step 4. Placement is recomputed on every ``show`` from
the cursor's monitor work area and the window's live DPI, then applied with
``SetWindowPos`` on the native HWND (never pywebview's logical x/y). Hide-on-blur comes
from the page (a 150 ms debounced ``blur`` listener that calls ``hide_popup``, already
ignoring pinned, ``hide_on_blur=false`` and "settings is opening"); the host adds the
tray-click-after-blur guard: a tray click within 400 ms of a blur-hide keeps the popup
hidden, so a click on the icon toggles instead of flickering.

Reply-finished show (``ui.show_on_reply``): :meth:`Popup.show_inactive` brings the hidden
popup back in its corner with ``ShowWindow(SW_SHOWNA)``, which neither activates it nor
takes the keyboard focus (pywebview's ``show()`` always activates). Until the user
activates it (``Form.Activated``), a page hide is ignored, so a stray blur cannot take it
away again, and a hotkey press or tray click focuses it instead of hiding it.

Threading: ``window.show()``/``hide()`` are blocking ``Control.Invoke`` calls into the GUI
thread, and ``SetWindowPos`` sends messages to it, while ``events.closing`` runs ON the GUI
thread. So ``self._lock`` guards state only and is never held across a call into the
window, and the closing handlers never take it.

Closing: pywebview's ``FormClosing`` handler cancels whenever ``events.closing`` returns
False, whatever the reason. :meth:`Popup._on_closing` returns False so Alt+F4 hides the
popup; :meth:`Popup._on_form_closing`, subscribed after pywebview's, lets every close that
is not the user's through again (sign-out, shutdown, ``Application.Exit()`` from
pywebview's Ctrl+C handler), so Windows is never told "ChatForge is preventing shutdown" and
``webview.start()`` returns for a clean quit.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from chatforge.desktop import win32util

_log = logging.getLogger(__name__)

TITLE = "ChatForge"
#: A tray click this soon after a hide keeps the popup hidden (a toggle, not a flicker).
TRAY_CLICK_GUARD_S = 0.4
#: Blur-hides are ignored for this long after Settings starts opening.
SETTINGS_OPENING_S = 1.5
#: How long ``show`` waits for the native window on a ``--show`` launch (pywebview runs
#: the start function before it creates the master window).
SHOWN_WAIT_S = 15.0
#: ``FormClosingEventArgs.CloseReason`` for Alt+F4 (``SC_CLOSE``) and ``Form.Close()``.
#: A bare ``WM_CLOSE`` from another process arrives as ``TaskManagerClosing``.
USER_CLOSING = "UserClosing"
#: Sign-out or shutdown (``WM_QUERYENDSESSION``). The session may still be cancelled.
WINDOWS_SHUTDOWN = "WindowsShutDown"


class Popup:
    """Owns the popup window.

    Every public method may run on any thread but the GUI thread: ``show`` and ``hide``
    wait on it.
    """

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
        #: Shown by :meth:`show_inactive` and not activated by the user since.
        self._shown_inactive = False

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
        self.window.events.before_show += self._on_before_show
        if self._events is not None:
            self._events.attach(self.window)
        return self.window

    def _on_before_show(self) -> None:
        """GUI thread, once the native form exists: subscribe :meth:`_on_form_closing` and
        :meth:`_on_activated`.

        pywebview subscribed its own ``FormClosing`` handler in the form's constructor, and
        WinForms runs subscribers in order, so this one sees (and can undo) its verdict.
        """
        native = getattr(self.window, "native", None)
        if native is None:
            return
        native.FormClosing += self._on_form_closing
        native.Activated += self._on_activated

    def _on_activated(self, *_args: Any) -> None:
        """``Form.Activated`` (GUI thread): the user clicked into the popup or it was shown
        with the focus. A plain store, no lock (see the module docstring)."""
        self._shown_inactive = False

    def _on_closing(self, *_args: Any) -> bool:
        """Alt+F4 hides the popup instead of destroying it.

        Runs on the GUI thread (pywebview calls ``events.closing`` synchronously), so it
        hides the form directly and never takes ``self._lock``.
        """
        if self._destroying:
            return True
        native = getattr(self.window, "native", None)
        if native is not None:
            try:
                native.Hide()  # already on the GUI thread: no Invoke, no wait for "shown"
            except Exception as exc:  # noqa: BLE001
                _log.warning("popup hide on close failed: %s", type(exc).__name__)
        self._note_hidden("close")
        return False

    def _on_form_closing(self, _sender: Any, args: Any) -> None:
        """Let every close that is not the user's through (``FormClosing``, GUI thread).

        Sign-out and shutdown must not be vetoed (Windows would report "ChatForge is
        preventing shutdown"), and neither may ``Application.Exit()``, or pywebview's
        Ctrl+C path could never end ``webview.start()``.
        """
        try:
            reason = str(args.CloseReason)
            if reason == USER_CLOSING:
                return
            if reason != WINDOWS_SHUTDOWN:
                # Exit and Task Manager closes always finish. A shutdown query can still
                # be cancelled by another app, and then the popup must keep hiding on
                # Alt+F4; when the session does end, WM_ENDSESSION closes the form
                # without asking again.
                self._destroying = True
            args.Cancel = False
            _log.info("popup closing: %s", reason)
        except Exception:  # noqa: BLE001 - an exception here would surface inside WinForms
            _log.exception("popup FormClosing handler failed")

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

    def _shown_event(self) -> Any:
        return getattr(getattr(self.window, "events", None), "shown", None)

    def _is_created(self) -> bool:
        """Whether the native window exists and has been shown once (``events.shown``)."""
        shown = self._shown_event()
        return shown is None or shown.is_set()

    @property
    def hwnd(self) -> int | None:
        """The native HWND, or ``None`` until the window exists.

        ``events.shown`` gates the read: before it, ``window.native`` may be missing or its
        ``Handle`` not created yet, and reading ``Handle`` from a worker thread then would
        create the window handle on the wrong thread.
        """
        if self.window is None or not self._is_created():
            return None
        return win32util.hwnd_of(self.window)

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

    def _wait_created(self) -> bool:
        shown = self._shown_event()
        return shown is None or shown.is_set() or bool(shown.wait(SHOWN_WAIT_S))

    def show(self, source: str = "app") -> None:
        """Place, show, bring to the foreground, then emit ``popup.shown``.

        Waits (bounded) for the native window first: a ``--show`` launch or a second
        instance's SHOW can arrive before pywebview has created it.
        """
        window = self.window
        if window is None:
            return
        if not self._wait_created():
            _log.warning("popup show skipped: no window after %.0f s", SHOWN_WAIT_S)
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
        with self._lock:
            self._visible = True
            self._shown_inactive = False
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

    def show_inactive(self, source: str = "reply") -> bool:
        """Bring the hidden popup back in its corner WITHOUT activating it or taking the
        keyboard focus (a reply finished while it was hidden). Returns whether it showed.

        Does nothing before the popup was first shown, while it is visible or quitting.
        No ``popup.shown`` event and no ``on_shown`` hook: the page focuses its input on
        ``popup.shown``, and this is not the user opening the popup.
        """
        window = self.window
        if window is None or self._destroying or not self._is_created():
            return False
        hwnd = self.hwnd
        if hwnd is None or not win32util.IS_WINDOWS:
            return False  # pywebview's own show() always activates the window
        if self.visible:
            return False
        with self._lock:
            # Before the window appears, so a page hide racing the show is ignored.
            self._shown_inactive = True
        rect = None
        try:
            rect = self.place()
        except Exception as exc:  # noqa: BLE001 - placement must never block showing
            _log.warning("popup placement failed: %s", type(exc).__name__)
        try:
            win32util.show_window(hwnd, activate=False)  # SW_SHOWNA: no activation
        except Exception as exc:  # noqa: BLE001
            _log.warning("popup show failed: %s", type(exc).__name__)
            with self._lock:
                self._shown_inactive = False
            return False
        foreground = False
        with contextlib.suppress(Exception):
            foreground = win32util.is_foreground(hwnd)
        with self._lock:
            self._visible = True
            if foreground:
                self._shown_inactive = False  # a hotkey show won the race: it has the focus
        _log.debug(
            "popup shown without focus source=%s rect=%s", source, rect.as_tuple() if rect else None
        )
        return True

    def _note_hidden(self, reason: str) -> None:
        # Plain attribute stores: _on_closing calls this on the GUI thread without the lock.
        self._visible = False
        self._shown_inactive = False
        self._last_hide_at = self._clock()
        self._last_hide_reason = reason

    def hide(self, reason: str = "app") -> None:
        """Hide. ``reason`` is ``blur``/``escape``/``close`` from the page, ``tray``, ``app``.

        A page hide (``js``) is ignored while the popup is up from :meth:`show_inactive`
        and the user has not activated it: Escape and the close button need the user to
        click into it first, so only a stray blur can arrive then.
        """
        window = self.window
        if window is None or not self._is_created():
            return  # never shown, nothing to hide (and window.hide() would wait for it)
        with self._lock:
            if reason == "blur" and self._blur_ignored():
                return
            if reason == "js" and self._shown_inactive:
                _log.debug("popup hide ignored: shown for a reply and not focused yet")
                return
        try:
            window.hide()
        except Exception as exc:  # noqa: BLE001
            _log.warning("popup hide failed: %s", type(exc).__name__)
            return
        with self._lock:
            self._note_hidden(reason)

    def hide_from_js(self) -> None:
        """The page asked to hide (blur debounce, Escape or the close button)."""
        self.hide(reason="js")

    def toggle(self, source: str = "tray") -> bool:
        """Show if hidden, hide if visible. Returns the new visibility.

        A tray click that lands within :data:`TRAY_CLICK_GUARD_S` of a page-initiated hide
        is the second half of "click the icon while the popup is open": the blur already
        hid it, so the click must not bring it straight back.

        A popup that came back on its own for a reply (:meth:`show_inactive`) and was not
        clicked into yet is brought to the foreground instead of hidden: the user pressed
        the hotkey to use it.
        """
        if self.visible:
            with self._lock:
                unfocused = self._shown_inactive
            if unfocused:
                self.show(source=source)
                return True
            self.hide(reason=source)
            return False
        with self._lock:
            recently_hidden = self._clock() - self._last_hide_at < TRAY_CLICK_GUARD_S
            hidden_by_page = self._last_hide_reason == "js"
        if source == "tray" and recently_hidden and hidden_by_page:
            return False
        self.show(source=source)
        return True
