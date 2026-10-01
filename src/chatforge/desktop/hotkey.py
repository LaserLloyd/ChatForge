"""Global hotkey: ``RegisterHotKey`` on its own thread with a ``GetMessageW`` loop.

The hotkey is bound to the thread that registers it, so registration, re-registration
(after a settings change) and un-registration all happen on :class:`HotkeyThread`. Other
threads ask for a change by storing a request and posting ``WM_APP`` to the thread; the
answer (``None`` or an error message) comes back through an ``Event``. A change that
fails puts the previous hotkey back, so the one Settings shows as active still works.

The string form (``"Ctrl+Alt+Space"``) is parsed by :func:`chatforge.config.parse_hotkey`;
:func:`to_win32` maps the canonical result to ``MOD_*`` flags and a virtual-key code.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from chatforge.config import parse_hotkey

_log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_APP_REREGISTER = WM_APP + 1
WM_APP_QUIT = WM_APP + 2
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

HOTKEY_ID = 0xA1C7

_MODIFIER_FLAGS = {"Ctrl": MOD_CONTROL, "Alt": MOD_ALT, "Shift": MOD_SHIFT, "Win": MOD_WIN}

_NAMED_VK = {
    "Space": 0x20,
    "Enter": 0x0D,
    "Tab": 0x09,
    "Esc": 0x1B,
    "Backspace": 0x08,
    "Delete": 0x2E,
    "Insert": 0x2D,
    "Home": 0x24,
    "End": 0x23,
    "PageUp": 0x21,
    "PageDown": 0x22,
    "Up": 0x26,
    "Down": 0x28,
    "Left": 0x25,
    "Right": 0x27,
}

_PUNCT_VK = {
    "`": 0xC0,  # VK_OEM_3
    "-": 0xBD,  # VK_OEM_MINUS
    "=": 0xBB,  # VK_OEM_PLUS
    "[": 0xDB,  # VK_OEM_4
    "]": 0xDD,  # VK_OEM_6
    "\\": 0xDC,  # VK_OEM_5
    ";": 0xBA,  # VK_OEM_1
    "'": 0xDE,  # VK_OEM_7
    ",": 0xBC,  # VK_OEM_COMMA
    ".": 0xBE,  # VK_OEM_PERIOD
    "/": 0xBF,  # VK_OEM_2
}


def to_win32(spec: str) -> tuple[int, int]:
    """``"Ctrl+Alt+Space"`` -> ``(MOD_CONTROL | MOD_ALT, 0x20)``. Raises ``ValueError``."""
    mods, key = parse_hotkey(spec)
    flags = 0
    for mod in mods:
        flags |= _MODIFIER_FLAGS[mod]
    if key in _NAMED_VK:
        vk = _NAMED_VK[key]
    elif key in _PUNCT_VK:
        vk = _PUNCT_VK[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("F") and key[1:].isdigit():
        n = int(key[1:])
        if not 1 <= n <= 24:
            raise ValueError(f"unknown key {key!r} in hotkey")
        vk = 0x70 + n - 1
    else:  # pragma: no cover - parse_hotkey already rejects everything else
        raise ValueError(f"unknown key {key!r} in hotkey")
    return flags, vk


def canonical(spec: str) -> str:
    """``"ctrl + alt + space"`` -> ``"Ctrl+Alt+Space"``."""
    mods, key = parse_hotkey(spec)
    return "+".join((*mods, key))


def describe_error(spec: str, error_code: int) -> str:
    if error_code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return f"{spec} is in use by another app. Choose a different combination."
    return f"Could not register {spec} (Windows error {error_code})."


def _last_error() -> int:
    """``GetLastError`` right after a failed ``RegisterHotKey`` (a seam for tests)."""
    return int(ctypes.GetLastError())


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class HotkeyThread:
    """Owns the global hotkey registration and its message loop."""

    def __init__(self, on_press: Callable[[], None]) -> None:
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._tid: int | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._pending_done = threading.Event()
        self._pending_result: str | None = None
        self.current: str | None = None
        self.error: str | None = None

    # --- lifecycle ------------------------------------------------------------------

    def start(self, spec: str | None = None) -> str | None:
        """Start the thread and register ``spec``; returns the error message, if any."""
        if os.name != "nt":
            self.error = "Global hotkeys are only supported on Windows."
            return self.error
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="chatforge-hotkey", daemon=True)
            self._thread.start()
            self._ready.wait(5)
        if spec:
            return self.register(spec)
        return None

    def stop(self, timeout: float = 2.0) -> None:
        thread, tid = self._thread, self._tid
        if thread is None or tid is None:
            return
        ctypes.windll.user32.PostThreadMessageW(tid, WM_APP_QUIT, 0, 0)
        thread.join(timeout=timeout)
        self._thread = None
        self._tid = None

    # --- requests from other threads --------------------------------------------------

    def register(self, spec: str, timeout: float = 5.0) -> str | None:
        """(Re)register ``spec`` on the hotkey thread. Returns ``None`` or an error text."""
        try:
            spec = canonical(spec)
            to_win32(spec)
        except ValueError as exc:
            return str(exc)
        if self._tid is None:
            return "The hotkey thread is not running."
        with self._lock:
            self._pending = spec
            self._pending_done.clear()
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_APP_REREGISTER, 0, 0)
            if not self._pending_done.wait(timeout):
                return "The hotkey thread did not answer."
            return self._pending_result

    # --- the thread -------------------------------------------------------------------

    @staticmethod
    def _register(user32: Any, spec: str) -> int | None:
        """``RegisterHotKey`` for ``spec``: ``None`` on success, else the Windows error."""
        mods, vk = to_win32(spec)
        if user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
            return None
        return _last_error()

    def _apply_pending(self, user32: Any) -> None:
        spec = self._pending
        self._pending = None
        if spec is None:
            self._pending_done.set()
            return
        previous = self.current
        if previous is not None:
            # One id per thread: the old combination has to go before the new one can
            # take the id, and comes back below if the new one is refused.
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.current = None
        code = self._register(user32, spec)
        if code is None:
            self.current = spec
            self.error = None
            self._pending_result = None
            _log.info("hotkey registered: %s", spec)
            self._pending_done.set()
            return
        message = describe_error(spec, code)
        if previous is not None:
            if self._register(user32, previous) is None:
                self.current = previous
                message += f" {previous} still works."
            else:
                message += f" {previous} could not be restored either; set a hotkey again."
        self.error = message
        self._pending_result = message
        _log.warning("hotkey registration failed: %s (active: %s)", message, self.current)
        self._pending_done.set()

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        msg = _MSG()
        # Force the creation of this thread's message queue before anyone posts to it.
        user32.PeekMessageW(ctypes.byref(msg), None, WM_APP, WM_APP, PM_NOREMOVE)
        self._tid = int(kernel32.GetCurrentThreadId())
        self._ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self._fire()
                elif msg.message == WM_APP_REREGISTER:
                    self._apply_pending(user32)
                elif msg.message == WM_APP_QUIT:
                    break
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self.current is not None:
                user32.UnregisterHotKey(None, HOTKEY_ID)
                self.current = None

    def _fire(self) -> None:
        # Off this thread: the handler places and shows a window, which must not stall
        # the message loop (a stalled loop delays the next hotkey).
        threading.Thread(
            target=self._safe_press, name="chatforge-hotkey-press", daemon=True
        ).start()

    def _safe_press(self) -> None:
        try:
            self._on_press()
        except Exception:  # noqa: BLE001
            _log.exception("hotkey handler failed")
