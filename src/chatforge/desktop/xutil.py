"""Linux (X11, or Wayland through XWayland) helpers for the desktop shell.

The Windows twin is :mod:`chatforge.desktop.win32util`; the pure placement math
(:func:`~chatforge.desktop.win32util.place`) is shared. Everything here is import-guarded and
degrades to a ``None``/``False`` answer: ``gi`` (PyGObject), ``Xlib`` and ``webview`` are
optional, and a missing one must leave the popup working, just less polished.

Threads: GTK is not thread-safe. pywebview runs the GTK main loop on the main thread, and
the popup is driven from worker threads, so every call that touches a Gtk widget goes
through :func:`call_on_gtk` (``GLib.idle_add`` and wait) unless the caller is already on the
main thread. The pointer is read through python-xlib on a connection of its own instead,
because the resize loop polls it 120 times a second from its own thread.

Coordinates are GDK logical pixels (what ``Gtk.Window.move``/``resize`` take). Under a
``GDK_SCALE`` of 2 the X server's pointer coordinates are twice that; :func:`gdk_scale`
divides them back.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable, MutableMapping
from typing import Any

from chatforge.desktop.win32util import Rect

_log = logging.getLogger(__name__)

#: Seam for tests (and for the "is this the Linux shell" question everywhere else).
IS_LINUX = sys.platform.startswith("linux")

#: Used when no toolkit can say how big the screen is.
DEFAULT_SCREEN = (1920, 1080)
#: How long :func:`call_on_gtk` waits for the GTK main loop before giving up.
GTK_CALL_TIMEOUT_S = 2.0
#: ``Gdk.ModifierType.BUTTON1_MASK``: the primary button in an X pointer state.
BUTTON1_MASK = 0x100


def is_linux() -> bool:
    return IS_LINUX


def prefer_x11(environ: MutableMapping[str, str] | None = None) -> bool:
    """Make GTK and Qt use X11 when the session is Wayland and XWayland is there.

    The popup is placed at a screen position, which a Wayland client cannot do for a
    toplevel; under XWayland it can. Must run before ``webview`` initialises GTK. A
    ``GDK_BACKEND`` the user set is kept. Returns whether it changed anything.
    """
    env = os.environ if environ is None else environ
    if not IS_LINUX or not env.get("WAYLAND_DISPLAY") or not env.get("DISPLAY"):
        return False
    changed = False
    if not env.get("GDK_BACKEND"):
        env["GDK_BACKEND"] = "x11"
        changed = True
    if not env.get("QT_QPA_PLATFORM"):
        env["QT_QPA_PLATFORM"] = "xcb"
        changed = True
    return changed


def session_kind(environ: MutableMapping[str, str] | None = None) -> str:
    """``"wayland"`` (``WAYLAND_DISPLAY`` set; XWayland may or may not be there), ``"x11"``
    (only ``DISPLAY``) or ``"none"`` (no display at all)."""
    env = os.environ if environ is None else environ
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "none"


# --- GTK access -----------------------------------------------------------------------


def _glib() -> Any:
    """``GLib`` if PyGObject is already loaded (never imports it: GTK must be started by
    pywebview first), else ``None``."""
    gi = sys.modules.get("gi.repository")
    return getattr(gi, "GLib", None) if gi is not None else None


def call_on_gtk(fn: Callable[..., Any], *args: Any, timeout: float = GTK_CALL_TIMEOUT_S) -> Any:
    """Run ``fn(*args)`` on the GTK main thread and return its result.

    Direct when already on the main thread or when GLib is not loaded (tests, Qt);
    otherwise through ``GLib.idle_add``, waiting up to ``timeout``. Returns ``None`` when
    the call raised or the loop did not answer.
    """
    glib = _glib()
    if glib is None or threading.current_thread() is threading.main_thread():
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - a GTK call must never break the popup
            _log.debug("gtk call failed: %s", type(exc).__name__)
            return None
    done = threading.Event()
    box: list[Any] = [None]

    def run() -> bool:
        try:
            box[0] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            _log.debug("gtk call failed: %s", type(exc).__name__)
        finally:
            done.set()
        return False  # once

    glib.idle_add(run)
    if not done.wait(timeout):
        _log.debug("gtk main loop did not answer within %.1f s", timeout)
        return None
    return box[0]


def is_gtk_window(native: Any) -> bool:
    """Whether ``window.native`` looks like a ``Gtk.Window`` (pywebview sets it on GTK)."""
    return native is not None and all(
        hasattr(native, name) for name in ("present", "set_accept_focus", "move", "resize")
    )


def hide_native(native: Any) -> bool:
    """``Gtk.Window.hide()`` for the GUI thread (the ``closing`` handler runs there)."""
    hide = getattr(native, "hide", None)
    if hide is None:
        return False
    hide()
    return True


def skip_taskbar(native: Any) -> bool:
    """Keep the popup out of the dock, the task switcher's pager and the taskbar."""
    if not is_gtk_window(native):
        return False

    def apply() -> bool:
        native.set_skip_taskbar_hint(True)
        native.set_skip_pager_hint(True)
        return True

    return bool(call_on_gtk(apply))


def present(native: Any) -> bool:
    """Raise and focus the window (``Gtk.Window.present``)."""
    if not is_gtk_window(native):
        return False

    def apply() -> bool:
        native.set_focus_on_map(True)
        native.set_accept_focus(True)
        native.present()
        return True

    return bool(call_on_gtk(apply))


def prepare_no_focus(native: Any) -> bool:
    """Before a show that must not take the keyboard focus: ``set_accept_focus(False)`` and
    ``set_focus_on_map(False)``. :func:`restore_focus` undoes it once the window is up."""
    if not is_gtk_window(native):
        return False

    def apply() -> bool:
        native.set_accept_focus(False)
        native.set_focus_on_map(False)
        return True

    return bool(call_on_gtk(apply))


def restore_focus(native: Any, delay_ms: int = 400) -> None:
    """After :func:`prepare_no_focus`, once the window was mapped: let the user click into
    it again. Deferred, so the window manager has mapped it without the focus first."""
    if not is_gtk_window(native):
        return

    def apply() -> bool:
        native.set_accept_focus(True)
        native.set_focus_on_map(True)
        return False

    glib = _glib()
    if glib is not None and hasattr(glib, "timeout_add"):
        glib.timeout_add(delay_ms, apply)
    else:
        apply()


def connect_focus_in(native: Any, handler: Callable[[], None]) -> bool:
    """Call ``handler()`` when the window gains the keyboard focus (``focus-in-event``)."""
    connect = getattr(native, "connect", None)
    if connect is None:
        return False
    connect("focus-in-event", lambda *_a: handler() or False)
    return True


def move_resize(window: Any, rect: Rect) -> bool:
    """Put the window at ``rect`` (GDK logical px, absolute on the desktop).

    Uses the native ``Gtk.Window`` when reachable: pywebview's ``window.move`` adds the
    first monitor's origin, which is wrong for an absolute position on another monitor.
    Falls back to ``window.move``/``window.resize``.
    """
    native = getattr(window, "native", None)
    if is_gtk_window(native):

        def apply() -> bool:
            native.resize(rect.width, rect.height)
            native.move(rect.left, rect.top)
            return True

        if call_on_gtk(apply):
            return True
    try:
        window.resize(rect.width, rect.height)
        window.move(rect.left, rect.top)
    except Exception as exc:  # noqa: BLE001
        _log.debug("window move/resize failed: %s", type(exc).__name__)
        return False
    return True


def window_rect(window: Any) -> Rect | None:
    """Where the window is now (GDK logical px), or ``None`` when it cannot be read."""
    native = getattr(window, "native", None)
    if not is_gtk_window(native) or not hasattr(native, "get_position"):
        return None

    def read() -> Rect:
        x, y = native.get_position()
        w, h = native.get_size()
        return Rect(int(x), int(y), int(x) + int(w), int(y) + int(h))

    return call_on_gtk(read)


# --- monitors -------------------------------------------------------------------------


def gdk_scale() -> int:
    """The primary monitor's integer scale factor (``GDK_SCALE``), 1 when unknown."""
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        return max(1, int(monitor.get_scale_factor()))
    except Exception:  # noqa: BLE001
        return 1


def _gdk_work_area() -> Rect | None:
    import gi

    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None:
        return None
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    if monitor is None:
        return None
    area = monitor.get_workarea()
    return Rect(area.x, area.y, area.x + area.width, area.y + area.height)


def _webview_work_area() -> Rect | None:
    import webview

    screens = webview.screens
    if not screens:
        return None
    s = screens[0]
    return Rect(s.x, s.y, s.x + s.width, s.y + s.height)


def work_area() -> Rect:
    """The primary monitor's work area (its size minus the panels), in logical px.

    Gdk first (it knows the panels), then pywebview's screen list (the whole monitor), then
    :data:`DEFAULT_SCREEN`. Never raises.
    """
    for probe in (_gdk_work_area, _webview_work_area):
        try:
            rect = probe()
        except Exception as exc:  # noqa: BLE001 - gi/webview missing or no display
            _log.debug("%s failed: %s", probe.__name__, type(exc).__name__)
            continue
        if rect is not None and rect.width > 0 and rect.height > 0:
            return rect
    return Rect(0, 0, *DEFAULT_SCREEN)


# --- pointer ----------------------------------------------------------------------------


class Pointer:
    """The X pointer, read over a private ``Xlib`` connection (safe on any thread)."""

    def __init__(self) -> None:
        from Xlib import display

        self._display = display.Display()
        self._root = self._display.screen().root
        self._scale = call_on_gtk(gdk_scale) or 1

    def read(self) -> tuple[tuple[int, int], bool]:
        """``((x, y), primary_button_down)`` in GDK logical px."""
        reply = self._root.query_pointer()
        point = (int(reply.root_x) // self._scale, int(reply.root_y) // self._scale)
        return point, bool(int(reply.mask) & BUTTON1_MASK)

    def close(self) -> None:
        try:
            self._display.close()
        except Exception as exc:  # noqa: BLE001
            _log.debug("pointer close failed: %s", type(exc).__name__)


def open_pointer() -> Pointer | None:
    """A :class:`Pointer`, or ``None`` without python-xlib or an X display."""
    if not os.environ.get("DISPLAY"):
        return None
    try:
        return Pointer()
    except Exception as exc:  # noqa: BLE001 - ImportError, DisplayConnectionError, ...
        _log.debug("pointer unavailable: %s", type(exc).__name__)
        return None


# --- misc -------------------------------------------------------------------------------


def set_program_name(name: str) -> bool:
    """``g_set_prgname``: GTK uses it as the window class (``WM_CLASS``), which a
    ``.desktop`` file's ``StartupWMClass`` matches. Before GTK initialises."""
    if not IS_LINUX:
        return False
    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        GLib.set_prgname(name)
        GLib.set_application_name(name)
    except Exception as exc:  # noqa: BLE001 - no PyGObject: Qt or a browser, nothing to name
        _log.debug("program name not set: %s", type(exc).__name__)
        return False
    return True


def install_unix_signals(handler: Callable[[], None], signals: tuple[int, ...]) -> bool:
    """Run ``handler`` from the GLib main loop on these signals (Python's own handlers
    only run when the interpreter gets a turn, and the GTK loop mostly sleeps in C)."""
    if not IS_LINUX:
        return False
    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        for sig in signals:
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, int(sig), lambda *_a: handler() or True)
    except Exception as exc:  # noqa: BLE001
        _log.debug("glib signal handlers unavailable: %s", type(exc).__name__)
        return False
    return True
