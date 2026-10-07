"""Global hotkey: ``RegisterHotKey`` on its own thread with a ``GetMessageW`` loop.

Two more backends hang off the same :class:`HotkeyThread`:

* ``Copilot`` (the Copilot key, which emits Left Win + Left Shift + F23 and is owned by the
  shell, so ``RegisterHotKey`` refuses it) installs a ``WH_KEYBOARD_LL`` hook from the same
  thread instead, see :class:`_CopilotHook`.
* On Linux with an X11 ``DISPLAY`` the combination is grabbed with ``XGrabKey`` through
  python-xlib on a thread of its own, see :class:`_X11Grabber`. Without a display there is
  no global hotkey; bind a desktop shortcut to ``chatforge --show`` instead.

The hotkey is bound to the thread that registers it, so registration, re-registration
(after a settings change) and un-registration all happen on :class:`HotkeyThread`. Other
threads ask for a change by storing a request and posting ``WM_APP`` to the thread; the
answer (``None`` or an error message) comes back through an ``Event``. A change that
fails puts the previous hotkey back, so the one Settings shows as active still works.

The string form (``"Ctrl+Alt+Space"``) is parsed by :func:`chatforge.config.parse_hotkey`;
:func:`to_win32` maps the canonical result to ``MOD_*`` flags and a virtual-key code.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import select
import sys
import threading
import time
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

COPILOT = "Copilot"
NO_X11_DISPLAY = "No X11 display: bind a desktop shortcut to `chatforge --show` instead"
COPILOT_WINDOWS_ONLY = "The Copilot key hotkey is Windows only."
UNSUPPORTED_PLATFORM = "Global hotkeys are only supported on Windows and Linux (X11)."

# Low-level keyboard hook (Copilot key).
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x10
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_LSHIFT = 0xA0
VK_LWIN = 0x5B
VK_F23 = 0x86
VK_DUMMY = 0xFF  # reserved/unassigned: pressing it between Win down and up cancels Start
#: ``dwExtraInfo`` on the keys we inject, so the hook can tell them from real ones.
INJECT_TAG = 0x43464B31

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
    if key == COPILOT:
        raise ValueError("the Copilot key is hooked with a keyboard hook, not RegisterHotKey")
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


_X11_MODIFIER_MASKS = {
    "Shift": 0x01,
    "Ctrl": 0x04,
    "Alt": 0x08,
    "Win": 0x40,
}  # Shift/Control/Mod1/Mod4
_X11_LOCK_MASK = 0x02  # CapsLock
_X11_NUMLOCK_MASK = 0x10  # Mod2
_X11_SCROLLLOCK_MASK = 0x80  # Mod5 (ScrollLock on most maps; a harmless extra grab otherwise)
_X11_LOCK_COMBOS = tuple(
    (_X11_LOCK_MASK if a else 0)
    | (_X11_NUMLOCK_MASK if b else 0)
    | (_X11_SCROLLLOCK_MASK if c else 0)
    for a in (0, 1)
    for b in (0, 1)
    for c in (0, 1)
)
_X11_RELEVANT_STATE = 0x01 | 0x04 | 0x08 | 0x40

_X11_NAMED = {
    "Space": "space",
    "Enter": "Return",
    "Tab": "Tab",
    "Esc": "Escape",
    "Backspace": "BackSpace",
    "Delete": "Delete",
    "Insert": "Insert",
    "Home": "Home",
    "End": "End",
    "PageUp": "Prior",
    "PageDown": "Next",
    "Up": "Up",
    "Down": "Down",
    "Left": "Left",
    "Right": "Right",
}

_X11_PUNCT = {
    "`": "grave",
    "-": "minus",
    "=": "equal",
    "[": "bracketleft",
    "]": "bracketright",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "period",
    "/": "slash",
}


def to_x11(spec: str) -> tuple[int, str]:
    """``"Ctrl+Alt+Space"`` -> ``(ControlMask | Mod1Mask, "space")``: X modifier mask and
    keysym name (for ``XK.string_to_keysym``). Alt is Mod1, Super (Win) is Mod4.
    Raises ``ValueError``."""
    mods, key = parse_hotkey(spec)
    if key == COPILOT:
        raise ValueError(COPILOT_WINDOWS_ONLY)
    mask = 0
    for mod in mods:
        mask |= _X11_MODIFIER_MASKS[mod]
    if key in _X11_NAMED:
        name = _X11_NAMED[key]
    elif key in _X11_PUNCT:
        name = _X11_PUNCT[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        name = key.lower()
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        name = key
    else:  # pragma: no cover - parse_hotkey already rejects everything else
        raise ValueError(f"unknown key {key!r} in hotkey")
    return mask, name


def canonical(spec: str) -> str:
    """``"ctrl + alt + space"`` -> ``"Ctrl+Alt+Space"``."""
    mods, key = parse_hotkey(spec)
    return "+".join((*mods, key))


def describe_error(spec: str, error_code: int) -> str:
    if spec == COPILOT:
        return f"Could not hook the Copilot key (Windows error {error_code})."
    if error_code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return f"{spec} is in use by another app. Choose a different combination."
    return f"Could not register {spec} (Windows error {error_code})."


def _is_windows() -> bool:
    """Which backend to use (a seam for tests)."""
    return os.name == "nt"


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


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):  # only here so the union has the real size
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


#: ``LRESULT CALLBACK (int nCode, WPARAM, LPARAM)``. ``WINFUNCTYPE`` only exists on Windows;
#: ``CFUNCTYPE`` lets the same wrapper be built (and called) by tests elsewhere.
_HOOKPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


def _module_handle() -> int:
    """``GetModuleHandleW(NULL)`` for ``SetWindowsHookExW`` (a seam for tests)."""
    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    return int(kernel32.GetModuleHandleW(None) or 0)


def _configure_user32(user32: Any) -> None:
    """Pointer-sized signatures for the hook calls (a private ``WinDLL``, never the
    shared ``windll.user32`` other modules set their own prototypes on)."""
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        _HOOKPROC,
        wintypes.HINSTANCE,
        wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short


class _CopilotHook:
    """``WH_KEYBOARD_LL`` hook that turns Left Win + Left Shift + F23 into a hotkey press.

    Rules the callback keeps, because a slow or wrong low-level hook hurts every key
    in the session (and Windows drops hooks that exceed ``LowLevelHooksTimeout``):

    * it does almost nothing: a few comparisons, one ``GetAsyncKeyState`` pair on the
      F23 press, and ``_fire`` (which hands off to a thread);
    * everything it does not swallow goes to ``CallNextHookEx``; it swallows only F23
      (the press, its auto-repeats and the matching release), never a modifier, so no
      modifier can end up stuck;
    * the modifier state comes from the hook's own events and is confirmed with
      ``GetAsyncKeyState`` before firing, so a missed key-up can not cause a ghost press;
    * Start would open when Win is released with no other key in between: the Win key-up
      is preceded by an injected dummy key (``VK_DUMMY`` down+up, tagged with
      ``INJECT_TAG``, which this hook ignores) once the chord was used.
    """

    def __init__(self, user32: Any, fire: Callable[[], None]) -> None:
        self._user32 = user32
        self._fire = fire
        self.hhook: Any = None
        self.proc: Any = None  # the ctypes callback: must outlive the hook
        self.win_down = False
        self.shift_down = False
        self.swallowing = False  # an F23 press was swallowed and its release is due
        self.mask_pending = False  # the chord fired: mask Win before its key-up passes

    # --- install / remove ---------------------------------------------------------------

    def install(self) -> int | None:
        """``None`` on success, else the Windows error of ``SetWindowsHookExW``."""
        self.proc = _HOOKPROC(self._callback)
        hhook = self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.proc, _module_handle(), 0)
        if not hhook:
            self.proc = None
            return _last_error()
        self.hhook = hhook
        self.win_down = self.shift_down = self.swallowing = self.mask_pending = False
        return None

    def remove(self) -> None:
        hhook, self.hhook = self.hhook, None
        if hhook:
            self._user32.UnhookWindowsHookEx(hhook)
        self.proc = None  # only after UnhookWindowsHookEx: the hook may call it until then

    # --- the hook -----------------------------------------------------------------------

    def _callback(self, n_code: int, w_param: int, l_param: int) -> int:
        swallow = False
        if n_code == HC_ACTION:
            try:
                kb = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                swallow = self.process(int(w_param), int(kb.vkCode), int(kb.dwExtraInfo))
            except Exception:  # noqa: BLE001 - never raise into the hook chain
                _log.exception("copilot hook failed")
        if swallow:
            return 1
        return int(self._user32.CallNextHookEx(self.hhook, n_code, w_param, l_param))

    def process(self, message: int, vk: int, extra: int = 0) -> bool:
        """One key event; ``True`` means swallow it."""
        if extra == INJECT_TAG:
            return False  # our own dummy key
        down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        if not down and message not in (WM_KEYUP, WM_SYSKEYUP):
            return False
        if vk == VK_LWIN:
            self.win_down = down
            if not down and self.mask_pending:
                self.mask_pending = False
                self._inject_dummy_key()  # before this key-up reaches the shell
        elif vk == VK_LSHIFT:
            self.shift_down = down
        elif vk == VK_F23:
            if not down:
                was = self.swallowing
                self.swallowing = False
                return was
            if self.swallowing:
                return True  # auto-repeat of a press we already handled
            if self.win_down and self.shift_down and self._confirmed():
                self.swallowing = True
                self.mask_pending = True
                self._fire()
                return True
        return False

    def _confirmed(self) -> bool:
        """Both modifiers really are down (``GetAsyncKeyState``), else forget our state."""
        win = bool(self._user32.GetAsyncKeyState(VK_LWIN) & 0x8000)
        shift = bool(self._user32.GetAsyncKeyState(VK_LSHIFT) & 0x8000)
        if not (win and shift):
            self.win_down, self.shift_down = win, shift
        return win and shift

    def _inject_dummy_key(self) -> None:
        inputs = (_INPUT * 2)()
        for item, flags in zip(inputs, (0, KEYEVENTF_KEYUP), strict=True):
            item.type = INPUT_KEYBOARD
            item.ki.wVk = VK_DUMMY
            item.ki.dwFlags = flags
            item.ki.dwExtraInfo = INJECT_TAG
        self._user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))


class _X11Grabber:
    """``XGrabKey`` on the root window from a thread with its own ``Display``.

    Every NumLock/CapsLock/ScrollLock combination is grabbed, since X matches the lock
    bits exactly. Requests from other threads (re-grab, stop) are stored, then the thread
    is woken through a pipe it ``select``s on together with the display connection.
    """

    DEBOUNCE_S = 0.25

    def __init__(self, fire: Callable[[], None]) -> None:
        self._fire = fire
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._pending_done = threading.Event()
        self._pending_result: str | None = None
        self._quit = False
        self._wake_r = -1
        self._wake_w = -1
        self._start_error: str | None = None
        self._last_press = float("-inf")
        self._grabbed: tuple[int, int] | None = None  # (keycode, modifier mask)
        self.current: str | None = None

    def start(self) -> str | None:
        if self._thread is not None:
            return None
        self._wake_r, self._wake_w = os.pipe()
        self._ready.clear()
        self._start_error = None
        self._quit = False
        self._thread = threading.Thread(target=self._run, name="chatforge-hotkey-x11", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self._start_error = "The X11 hotkey thread did not start."
        if self._start_error:
            self.stop()
            return self._start_error
        return None

    def stop(self, timeout: float = 2.0) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            self._quit = True
            self._wake()
            thread.join(timeout=timeout)
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._wake_r = self._wake_w = -1
        self.current = None

    def register(self, spec: str, timeout: float = 5.0) -> str | None:
        if self._thread is None:
            return "The hotkey thread is not running."
        with self._lock:
            self._pending = spec
            self._pending_done.clear()
            self._wake()
            if not self._pending_done.wait(timeout):
                return "The hotkey thread did not answer."
            return self._pending_result

    def _wake(self) -> None:
        with contextlib.suppress(OSError):
            os.write(self._wake_w, b"x")

    # --- the thread ---------------------------------------------------------------------

    def _run(self) -> None:
        try:
            from Xlib import XK, X, display, error  # imported here: Linux-only, optional
        except Exception as exc:  # noqa: BLE001
            self._start_error = f"python-xlib is not available ({exc})."
            self._ready.set()
            return
        try:
            disp = display.Display()
        except Exception as exc:  # noqa: BLE001
            self._start_error = f"Could not open the X11 display ({exc})."
            self._ready.set()
            return
        root = disp.screen().root
        x = (X, XK, error)
        self._ready.set()
        try:
            while not self._quit:
                while disp.pending_events():
                    self._on_event(X, disp.next_event())
                readable, _, _ = select.select([disp.fileno(), self._wake_r], [], [])
                if self._wake_r in readable:
                    os.read(self._wake_r, 4096)
                if self._quit:
                    break
                if self._pending is not None:
                    self._apply_pending(disp, root, x)
        except Exception:  # noqa: BLE001
            _log.exception("x11 hotkey loop failed")
        finally:
            try:
                self._ungrab(disp, root)
                disp.close()
            except Exception:  # noqa: BLE001
                _log.debug("x11 cleanup failed", exc_info=True)
            self.current = None

    def _on_event(self, X: Any, event: Any) -> None:
        if event.type != X.KeyPress or self._grabbed is None:
            return
        keycode, mask = self._grabbed
        if event.detail != keycode or (event.state & _X11_RELEVANT_STATE) != mask:
            return
        now = time.monotonic()
        if now - self._last_press < self.DEBOUNCE_S:  # auto-repeat arrives as press/release
            return
        self._last_press = now
        self._fire()

    def _apply_pending(self, disp: Any, root: Any, x: tuple[Any, Any, Any]) -> None:
        spec, self._pending = self._pending, None
        if spec is None:
            self._pending_done.set()
            return
        previous = self.current
        self._ungrab(disp, root)
        self.current = None
        message = self._grab(disp, root, x, spec)
        if message is None:
            self.current = spec
            self._pending_result = None
            _log.info("hotkey grabbed (X11): %s", spec)
        else:
            if previous is not None:
                if self._grab(disp, root, x, previous) is None:
                    self.current = previous
                    message += f" {previous} still works."
                else:
                    message += f" {previous} could not be restored either; set a hotkey again."
            self._pending_result = message
            _log.warning("x11 hotkey failed: %s (active: %s)", message, self.current)
        self._pending_done.set()

    def _grab(self, disp: Any, root: Any, x: tuple[Any, Any, Any], spec: str) -> str | None:
        X, XK, error = x
        mask, name = to_x11(spec)
        keysym = XK.string_to_keysym(name)
        keycode = disp.keysym_to_keycode(keysym) if keysym else 0
        if not keycode:
            return f"{spec} has no key on this keyboard layout."
        catch = error.CatchError(error.BadAccess, error.BadValue)
        for extra in _X11_LOCK_COMBOS:
            root.grab_key(
                keycode, mask | extra, True, X.GrabModeAsync, X.GrabModeAsync, onerror=catch
            )
        disp.sync()
        if catch.get_error():
            for extra in _X11_LOCK_COMBOS:
                root.ungrab_key(keycode, mask | extra)
            disp.sync()
            return f"{spec} is in use by another app. Choose a different combination."
        self._grabbed = (keycode, mask)
        return None

    def _ungrab(self, disp: Any, root: Any) -> None:
        grabbed, self._grabbed = self._grabbed, None
        if grabbed is None:
            return
        keycode, mask = grabbed
        for extra in _X11_LOCK_COMBOS:
            root.ungrab_key(keycode, mask | extra)
        disp.sync()


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
        self._copilot: _CopilotHook | None = None
        self._x11: _X11Grabber | None = None
        self.current: str | None = None
        self.error: str | None = None
        #: Last success line ("Copilot key hooked", ...), for the app to log or show.
        self.status: str | None = None

    # --- lifecycle ------------------------------------------------------------------

    def start(self, spec: str | None = None) -> str | None:
        """Start the thread and register ``spec``; returns the error message, if any."""
        if not _is_windows():
            return self._start_x11(spec)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="chatforge-hotkey", daemon=True)
            self._thread.start()
            self._ready.wait(5)
        if spec:
            return self.register(spec)
        return None

    def _start_x11(self, spec: str | None) -> str | None:
        if spec and _is_copilot(spec):
            self.error = COPILOT_WINDOWS_ONLY
        elif sys.platform == "darwin":
            self.error = UNSUPPORTED_PLATFORM
        elif not os.environ.get("DISPLAY"):
            self.error = NO_X11_DISPLAY
        else:
            if self._x11 is None:
                self._x11 = _X11Grabber(self._fire)
                failure = self._x11.start()
                if failure:
                    self._x11 = None
                    self.error = failure
                    return failure
            return self.register(spec) if spec else None
        return self.error

    def stop(self, timeout: float = 2.0) -> None:
        x11, self._x11 = self._x11, None
        if x11 is not None:
            x11.stop(timeout)
            self.current = None
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
            if not _is_windows():
                to_x11(spec)
            elif spec != COPILOT:
                to_win32(spec)
        except ValueError as exc:
            return str(exc)
        if self._x11 is not None:
            return self._register_x11(spec, timeout)
        if self._tid is None:
            return "The hotkey thread is not running."
        with self._lock:
            self._pending = spec
            self._pending_done.clear()
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_APP_REREGISTER, 0, 0)
            if not self._pending_done.wait(timeout):
                return "The hotkey thread did not answer."
            return self._pending_result

    def _register_x11(self, spec: str, timeout: float) -> str | None:
        assert self._x11 is not None
        message = self._x11.register(spec, timeout)
        self.current = self._x11.current
        self.error = message
        self.status = None if message else f"Hotkey grabbed: {spec}"
        return message

    # --- the thread -------------------------------------------------------------------

    def _register(self, user32: Any, spec: str) -> int | None:
        """Register ``spec`` (``RegisterHotKey``, or the Copilot hook): ``None`` on
        success, else the Windows error."""
        if spec == COPILOT:
            hook = _CopilotHook(user32, self._fire)
            code = hook.install()
            if code is None:
                self._copilot = hook
            return code
        mods, vk = to_win32(spec)
        if user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
            return None
        return _last_error()

    def _unregister(self, user32: Any, spec: str | None) -> None:
        if spec == COPILOT:
            hook, self._copilot = self._copilot, None
            if hook is not None:
                hook.remove()
        elif spec is not None:
            user32.UnregisterHotKey(None, HOTKEY_ID)

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
            self._unregister(user32, previous)
            self.current = None
        code = self._register(user32, spec)
        if code is None:
            self.current = spec
            self.error = None
            self._pending_result = None
            self.status = "Copilot key hooked" if spec == COPILOT else f"Hotkey registered: {spec}"
            _log.info("%s", self.status)
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
        # A private WinDLL: the hook calls get pointer-sized prototypes that must not leak
        # onto the shared ``windll.user32`` other modules use.
        user32 = ctypes.WinDLL("user32")  # type: ignore[attr-defined]
        _configure_user32(user32)
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
            self._unregister(user32, self.current)
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


def _is_copilot(spec: str) -> bool:
    try:
        return canonical(spec) == COPILOT
    except ValueError:
        return False
