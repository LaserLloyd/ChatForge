"""HotkeyThread re-registration: a refused change keeps the previous hotkey working.

``_apply_pending`` runs against a fake ``user32`` (no real ``RegisterHotKey``).
"""

from __future__ import annotations

import pytest

from chatforge.desktop import hotkey as hotkey_mod
from chatforge.desktop.hotkey import (
    ERROR_HOTKEY_ALREADY_REGISTERED,
    HOTKEY_ID,
    MOD_NOREPEAT,
    HotkeyThread,
    to_win32,
)


class FakeUser32:
    """One id, like the real thread: Register fails while the id is held or the
    combination is taken by "another app"."""

    def __init__(self, taken: tuple[str, ...] = ()) -> None:
        self.taken = {to_win32(spec) for spec in taken}
        self.held: tuple[int, int] | None = None
        self.steal_on_unregister = False

    def RegisterHotKey(self, hwnd, hid, mods, vk):  # noqa: N802 - Win32 name
        assert hwnd is None and hid == HOTKEY_ID and mods & MOD_NOREPEAT
        key = (mods & ~MOD_NOREPEAT, vk)
        if self.held is not None or key in self.taken:
            return 0
        self.held = key
        return 1

    def UnregisterHotKey(self, hwnd, hid):  # noqa: N802 - Win32 name
        if self.steal_on_unregister and self.held is not None:
            self.taken.add(self.held)  # another app grabs it in that instant
        self.held = None
        return 1


@pytest.fixture(autouse=True)
def last_error(monkeypatch):
    monkeypatch.setattr(hotkey_mod, "_last_error", lambda: ERROR_HOTKEY_ALREADY_REGISTERED)


def apply(thread: HotkeyThread, user32: FakeUser32, spec: str) -> str | None:
    thread._pending = spec
    thread._pending_done.clear()
    thread._apply_pending(user32)
    assert thread._pending_done.is_set()
    return thread._pending_result


def test_register_then_change():
    thread, user32 = HotkeyThread(lambda: None), FakeUser32()
    assert apply(thread, user32, "Ctrl+Alt+Space") is None
    assert thread.current == "Ctrl+Alt+Space" and user32.held == to_win32("Ctrl+Alt+Space")
    assert apply(thread, user32, "Ctrl+Shift+K") is None
    assert thread.current == "Ctrl+Shift+K" and user32.held == to_win32("Ctrl+Shift+K")
    assert thread.error is None


def test_refused_change_restores_the_previous_hotkey():
    thread, user32 = HotkeyThread(lambda: None), FakeUser32(taken=("Ctrl+Alt+Delete",))
    apply(thread, user32, "Ctrl+Alt+Space")
    message = apply(thread, user32, "Ctrl+Alt+Delete")
    assert message and "in use by another app" in message
    assert "Ctrl+Alt+Space still works" in message
    assert thread.current == "Ctrl+Alt+Space"
    assert user32.held == to_win32("Ctrl+Alt+Space")  # really registered again


def test_reports_when_the_previous_hotkey_cannot_come_back():
    thread, user32 = HotkeyThread(lambda: None), FakeUser32(taken=("Ctrl+Alt+Delete",))
    apply(thread, user32, "Ctrl+Alt+Space")
    user32.steal_on_unregister = True
    message = apply(thread, user32, "Ctrl+Alt+Delete")
    assert "could not be restored" in message
    assert thread.current is None and user32.held is None


def test_first_registration_failure_leaves_no_hotkey():
    thread, user32 = HotkeyThread(lambda: None), FakeUser32(taken=("Ctrl+Alt+Space",))
    message = apply(thread, user32, "Ctrl+Alt+Space")
    assert message == "Ctrl+Alt+Space is in use by another app. Choose a different combination."
    assert thread.current is None and thread.error == message


def test_re_registering_the_same_hotkey_is_fine():
    thread, user32 = HotkeyThread(lambda: None), FakeUser32()
    apply(thread, user32, "Ctrl+Alt+Space")
    assert apply(thread, user32, "Ctrl+Alt+Space") is None
    assert thread.current == "Ctrl+Alt+Space"
