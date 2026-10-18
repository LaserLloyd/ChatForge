"""A hold-to-talk key: pressed and released events for a global key combination.

ChatForge's own hotkey class handles presses only (``RegisterHotKey`` has no key-up), so push-to-talk
polls the key state instead (``GetAsyncKeyState``, Windows). Polling swallows nothing: the key still
reaches the focused application, so pick a combination that does nothing there (the default is
Ctrl+Alt+Space). Elsewhere there is no global hold-key in this release; use the Voice panel button.

The state source is injectable (``is_down``) so the watcher is tested without a keyboard.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

_MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B}
_NAMED = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "capslock": 0x14,
    "pause": 0x13,
}


def parse_hotkey(spec: str) -> tuple[int, ...]:
    """``"Ctrl+Alt+Space"`` to Windows virtual-key codes. Raises ``ValueError`` with a sentence."""
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("The push-to-talk key is empty; use a form like Ctrl+Alt+Space.")
    codes: list[int] = []
    key: int | None = None
    for part in parts:
        if part in _MODS:
            codes.append(_MODS[part])
            continue
        if key is not None:
            raise ValueError(f"Use modifiers plus ONE key, not {spec!r}.")
        if part in _NAMED:
            key = _NAMED[part]
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part[0] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"Unknown key {part!r} in the push-to-talk key.")
    if key is None:
        raise ValueError("The push-to-talk key has only modifiers; add a key such as Space or F9.")
    return (*dict.fromkeys(codes), key)


def supported() -> bool:
    return sys.platform == "win32"


def windows_is_down(vk: int) -> bool:
    import ctypes

    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)  # type: ignore[attr-defined]


class HoldKeyWatcher:
    """Calls ``on_press`` when the whole combination goes down and ``on_release`` when any key of it
    comes up. ``spec`` may change at run time (``set_spec``) and the watcher may be paused
    (``active`` returns False), which also lowers the polling rate."""

    def __init__(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        *,
        is_down: Callable[[int], bool] | None = None,
        active: Callable[[], bool] = lambda: True,
        interval: float = 0.02,
        idle_interval: float = 0.25,
    ) -> None:
        self._press, self._release = on_press, on_release
        self._is_down = is_down or windows_is_down
        self._active = active
        self._interval, self._idle = interval, idle_interval
        self._keys: tuple[int, ...] = ()
        self._held = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    def set_spec(self, spec: str) -> bool:
        """Use a new combination. False (and ``error`` set) when it does not parse."""
        try:
            keys = parse_hotkey(spec)
        except ValueError as exc:
            self.error = str(exc)
            self._keys = ()
            return False
        self.error = ""
        self._keys = keys
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="chatforge-voice-hotkey", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(2)

    def tick(self) -> None:
        """One poll (the thread calls this; tests call it directly)."""
        keys = self._keys
        down = bool(keys) and self._active() and all(self._is_down(k) for k in keys)
        if down and not self._held:
            self._held = True
            self._safe(self._press)
        elif self._held and not down:
            self._held = False
            self._safe(self._release)

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - a bug in a callback must not kill the watcher
            import logging

            logging.getLogger("chatforge_voice.hotkey").exception("hold-key callback failed")

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._interval if (self._active() or self._held) else self._idle)
        if self._held:
            self._held = False
            self._safe(self._release)
