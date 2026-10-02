"""Win32 helpers for the popup: work area, DPI, placement, resizing, rounded corners,
foreground.

Everything that talks to ``user32``/``dwmapi`` is import-guarded so the pure parts
(:func:`place`) are testable on any OS. Coordinates from the OS are physical pixels;
``place`` turns the logical popup size from config into physical pixels using the
window's DPI (192 at 200 %).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# --- constants -----------------------------------------------------------------

MONITOR_DEFAULTTONEAREST = 2
MONITOR_DEFAULTTOPRIMARY = 1

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNA = 8
SW_RESTORE = 9

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

#: ``DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2``
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

ASFW_ANY = -1
SPI_GETWORKAREA = 0x0030

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
SM_SWAPBUTTON = 23


# --- pure geometry ----------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """A rectangle in physical pixels, ``right``/``bottom`` exclusive (Win32 RECT semantics)."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, other: Rect) -> bool:
        return (
            other.left >= self.left
            and other.top >= self.top
            and other.right <= self.right
            and other.bottom <= self.bottom
        )

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


def place(
    work_rect: Rect | tuple[int, int, int, int],
    dpi: int,
    logical_w: int,
    logical_h: int,
    margin: int,
) -> Rect:
    """Lower-right placement of a ``logical_w`` x ``logical_h`` window inside ``work_rect``.

    Pure. ``dpi`` scales the logical size and margin to physical pixels (96 = 100 %).
    The result always lies inside the work area: a window larger than the work area is
    shrunk to fit, and the margin collapses before the window does.
    """
    work = work_rect if isinstance(work_rect, Rect) else Rect(*work_rect)
    scale = max(dpi, 1) / 96.0
    w = max(1, round(logical_w * scale))
    h = max(1, round(logical_h * scale))
    m = max(0, round(margin * scale))

    avail_w = max(1, work.width)
    avail_h = max(1, work.height)
    # A margin that would not leave room for the window collapses first.
    mx = m if w + 2 * m <= avail_w else max(0, (avail_w - w) // 2)
    my = m if h + 2 * m <= avail_h else max(0, (avail_h - h) // 2)
    w = min(w, avail_w - 2 * mx)
    h = min(h, avail_h - 2 * my)

    left = work.right - mx - w
    top = work.bottom - my - h
    left = max(work.left, left)
    top = max(work.top, top)
    return Rect(left, top, left + w, top + h)


def resize_from(
    start: Rect,
    moves: tuple[bool, bool],
    press: tuple[int, int],
    point: tuple[int, int],
    min_size: tuple[int, int],
    bounds: Rect,
) -> Rect:
    """``start`` resized by dragging its left and/or top edge from ``press`` to ``point``.

    Pure, physical pixels. ``moves`` is ``(left, top)``: which edges follow the pointer. The
    bottom-right corner stays put; the window never gets smaller than ``min_size`` and
    never grows past the left or top of ``bounds`` (the work area), unless it already
    reached past it before the drag.
    """
    left, top = start.left, start.top
    if moves[0]:
        left = start.left + point[0] - press[0]
        left = max(min(bounds.left, start.left), min(left, start.right - min_size[0]))
    if moves[1]:
        top = start.top + point[1] - press[1]
        top = max(min(bounds.top, start.top), min(top, start.bottom - min_size[1]))
    return Rect(left, top, start.right, start.bottom)


# --- Win32 bindings -----------------------------------------------------------------


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _user32() -> Any:
    return ctypes.windll.user32  # type: ignore[attr-defined]


def _dwmapi() -> Any:
    return ctypes.windll.dwmapi  # type: ignore[attr-defined]


def _kernel32() -> Any:
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def ensure_dpi_awareness() -> bool:
    """Make the process per-monitor-DPI-aware (v2) before any window exists.

    Must run before ``webview`` creates a window. pywebview calls the older
    ``SetProcessDPIAware`` itself; once v2 is set that call is a harmless no-op.
    Returns whether the call succeeded (``False`` on a non-Windows host or when the
    awareness was already fixed by an earlier call or a manifest).
    """
    if not IS_WINDOWS:
        return False
    user32 = _user32()
    try:
        fn = user32.SetProcessDpiAwarenessContext
    except AttributeError:  # pragma: no cover - Windows < 10 1703
        return bool(user32.SetProcessDPIAware())
    fn.argtypes = [ctypes.c_void_p]
    fn.restype = wintypes.BOOL
    ok = bool(fn(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))
    if not ok:
        _log.debug("SetProcessDpiAwarenessContext failed (already set?): %s", ctypes.GetLastError())
    return ok


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    _user32().GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def primary_button_down() -> bool:
    """Whether the primary mouse button is held right now.

    ``GetAsyncKeyState`` reads the physical buttons, so with the buttons swapped in the
    mouse settings the primary one is the physical right button.
    """
    user32 = _user32()
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    vk = VK_RBUTTON if user32.GetSystemMetrics(SM_SWAPBUTTON) else VK_LBUTTON
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _rect_of(rc: wintypes.RECT) -> Rect:
    return Rect(int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))


def monitor_info_at(point: tuple[int, int]) -> tuple[Rect, Rect]:
    """``(monitor_rect, work_rect)`` of the monitor containing ``point`` (physical px)."""
    user32 = _user32()
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    hmon = user32.MonitorFromPoint(wintypes.POINT(point[0], point[1]), MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        rc = wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rc), 0)
        work = _rect_of(rc)
        return work, work
    return _rect_of(info.rcMonitor), _rect_of(info.rcWork)


def work_area_at_cursor() -> Rect:
    """Work area (taskbar excluded) of the monitor under the mouse cursor."""
    return monitor_info_at(cursor_pos())[1]


def dpi_for_window(hwnd: int) -> int:
    """``GetDpiForWindow``; falls back to the system DPI, then 96."""
    user32 = _user32()
    try:
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        dpi = int(user32.GetDpiForWindow(wintypes.HWND(hwnd)))
        if dpi:
            return dpi
    except AttributeError:  # pragma: no cover - Windows < 10 1607
        pass
    try:
        return int(user32.GetDpiForSystem())
    except AttributeError:  # pragma: no cover
        return 96


def window_rect(hwnd: int) -> Rect:
    rc = wintypes.RECT()
    _user32().GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rc))
    return _rect_of(rc)


def set_window_pos(
    hwnd: int,
    rect: Rect,
    *,
    topmost: bool = True,
    activate: bool = False,
    keep_zorder: bool = False,
) -> bool:
    """Move and size a window in physical pixels without showing it. ``keep_zorder``
    leaves its place in the z-order alone (``topmost`` is ignored then)."""
    flags = 0 if activate else SWP_NOACTIVATE
    if keep_zorder:
        flags |= SWP_NOZORDER
    insert_after = HWND_TOPMOST if topmost else HWND_NOTOPMOST
    user32 = _user32()
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    return bool(
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(insert_after),
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            flags,
        )
    )


def set_rounded_corners(hwnd: int, preference: int = DWMWCP_ROUND) -> bool:
    """``DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE)``; ``True`` on S_OK."""
    dwm = _dwmapi()
    dwm.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwm.DwmSetWindowAttribute.restype = ctypes.c_long
    value = ctypes.c_int(preference)
    hr = dwm.DwmSetWindowAttribute(
        wintypes.HWND(hwnd),
        DWMWA_WINDOW_CORNER_PREFERENCE,
        ctypes.byref(value),
        ctypes.sizeof(value),
    )
    return hr == 0


def get_corner_preference(hwnd: int) -> int | None:
    """Read back the corner preference (for the spike and the launch check)."""
    dwm = _dwmapi()
    dwm.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    value = ctypes.c_int(0)
    hr = dwm.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        DWMWA_WINDOW_CORNER_PREFERENCE,
        ctypes.byref(value),
        ctypes.sizeof(value),
    )
    return int(value.value) if hr == 0 else None


def is_window_visible(hwnd: int) -> bool:
    return bool(_user32().IsWindowVisible(wintypes.HWND(hwnd)))


def is_foreground(hwnd: int) -> bool:
    user32 = _user32()
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    fg = user32.GetForegroundWindow()
    return bool(fg) and int(fg) == int(hwnd)


def show_window(hwnd: int, activate: bool = True) -> None:
    _user32().ShowWindow(wintypes.HWND(hwnd), SW_SHOW if activate else SW_SHOWNA)


def hide_window(hwnd: int) -> None:
    _user32().ShowWindow(wintypes.HWND(hwnd), SW_HIDE)


def force_foreground(hwnd: int) -> bool:
    """``SetForegroundWindow`` with the usual fallbacks.

    Windows refuses to hand focus to a process that did not receive the last input.
    A hotkey press or a tray click grants that right; otherwise
    ``AllowSetForegroundWindow(ASFW_ANY)`` plus attaching to the foreground thread's
    input queue gets the popup on top in practice.
    """
    user32 = _user32()
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]

    if user32.SetForegroundWindow(wintypes.HWND(hwnd)) and is_foreground(hwnd):
        return True

    user32.AllowSetForegroundWindow(wintypes.DWORD(0xFFFFFFFF))  # ASFW_ANY
    fg = user32.GetForegroundWindow()
    this_tid = _kernel32().GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(wintypes.HWND(fg), None) if fg else 0
    attached = False
    if fg_tid and fg_tid != this_tid:
        attached = bool(user32.AttachThreadInput(fg_tid, this_tid, True))
    try:
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        user32.SetFocus(wintypes.HWND(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(fg_tid, this_tid, False)
    return is_foreground(hwnd)


def hwnd_of(window: Any) -> int | None:
    """The native HWND of a pywebview window (WinForms backend), or ``None`` until created."""
    native = getattr(window, "native", None)
    if native is None:
        return None
    try:
        handle = native.Handle
        return int(handle.ToInt32()) if hasattr(handle, "ToInt32") else int(handle)
    except Exception:  # noqa: BLE001 - a disposed form raises ObjectDisposedException
        return None


def find_window(title: str) -> int | None:
    """``FindWindowW(None, title)`` for tests and the launch check."""
    if not IS_WINDOWS:
        return None
    user32 = _user32()
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    hwnd = user32.FindWindowW(None, title)
    return int(hwnd) if hwnd else None


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    _user32().GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def open_path(path: os.PathLike[str] | str) -> None:
    """Open a folder in Explorer (``os.startfile``)."""
    # Adapted from StudioForge src/studioforge/tray/tray_app.py `_open_path` (MIT, LaserLloyd)
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - explorer, user-initiated
    else:  # pragma: no cover - the app targets Windows
        import subprocess

        subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
