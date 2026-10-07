"""Linux X11 hotkey through a fake ``Xlib`` injected into ``sys.modules``."""

from __future__ import annotations

import os
import sys
import threading
import types

import pytest

from chatforge.desktop import hotkey as hk
from chatforge.desktop.hotkey import HotkeyThread, to_x11

CONTROL, MOD1, SHIFT, MOD4, LOCK, MOD2, MOD5 = 0x04, 0x08, 0x01, 0x40, 0x02, 0x10, 0x80
KEYSYMS = {"space": 0x20, "k": 0x6B, "a": 0x61, "grave": 0x60, "F23": 0xFFD4, "Return": 0xFF0D}


class BadAccess(Exception):
    pass


class FakeCatchError:
    def __init__(self, *errors) -> None:
        self.error = None

    def get_error(self):
        return self.error


class FakeRoot:
    def __init__(self, display) -> None:
        self.display = display
        self.grabbed: set[tuple[int, int]] = set()
        self.grab_calls: list[tuple] = []

    def grab_key(self, keycode, modifiers, owner_events, pointer_mode, keyboard_mode, onerror=None):
        self.grab_calls.append((keycode, modifiers, owner_events, pointer_mode, keyboard_mode))
        if keycode in self.display.taken:
            onerror.error = BadAccess()
        else:
            self.grabbed.add((keycode, modifiers))

    def ungrab_key(self, keycode, modifiers):
        self.grabbed.discard((keycode, modifiers))


class FakeDisplay:
    instances: list[FakeDisplay] = []
    fail_open: Exception | None = None
    taken: set[int] = set()

    def __init__(self) -> None:
        if FakeDisplay.fail_open:
            raise FakeDisplay.fail_open
        self.root = FakeRoot(self)
        self.events: list[types.SimpleNamespace] = []
        self._r, self._w = os.pipe()
        self.closed = False
        self.taken = FakeDisplay.taken
        FakeDisplay.instances.append(self)

    def screen(self):
        return types.SimpleNamespace(root=self.root)

    def keysym_to_keycode(self, keysym):
        return keysym + 8 if keysym != 0x6B else 0  # "k" has no key on this layout

    def sync(self):
        pass

    def fileno(self):
        return self._r

    def pending_events(self):
        return len(self.events)

    def next_event(self):
        os.read(self._r, 1)
        return self.events.pop(0)

    def close(self):
        self.closed = True

    def push(self, detail, state, type_=2):
        self.events.append(types.SimpleNamespace(type=type_, detail=detail, state=state))
        os.write(self._w, b"e")


@pytest.fixture
def xlib(monkeypatch):
    FakeDisplay.instances = []
    FakeDisplay.fail_open = None
    FakeDisplay.taken = set()
    pkg = types.ModuleType("Xlib")
    X = types.SimpleNamespace(KeyPress=2, KeyRelease=3, GrabModeAsync=1)
    XK = types.SimpleNamespace(string_to_keysym=lambda name: KEYSYMS.get(name, 0))
    display = types.SimpleNamespace(Display=FakeDisplay)
    error = types.SimpleNamespace(
        CatchError=FakeCatchError, BadAccess=BadAccess, BadValue=BadAccess
    )
    pkg.X, pkg.XK, pkg.display, pkg.error = X, XK, display, error
    monkeypatch.setitem(sys.modules, "Xlib", pkg)
    monkeypatch.setattr(hk, "_is_windows", lambda: False)
    monkeypatch.setenv("DISPLAY", ":0")
    return FakeDisplay


@pytest.fixture
def pressed():
    return types.SimpleNamespace(event=threading.Event(), count=0)


@pytest.fixture
def thread(xlib, pressed):
    def on_press():
        pressed.count += 1
        pressed.event.set()

    t = HotkeyThread(on_press)
    yield t
    t.stop()


def lock_combos(mask: int) -> set[int]:
    return {mask | a | b | c for a in (0, LOCK) for b in (0, MOD2) for c in (0, MOD5)}


# --- pure parts (run everywhere) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "mask", "name"),
    [
        ("Ctrl+Alt+Space", CONTROL | MOD1, "space"),
        ("ctrl+shift+K", CONTROL | SHIFT, "k"),
        ("Win+Q", MOD4, "q"),
        ("Super+Alt+1", MOD4 | MOD1, "1"),
        ("Ctrl+F23", CONTROL, "F23"),
        ("Ctrl+Enter", CONTROL, "Return"),
        ("Ctrl+Esc", CONTROL, "Escape"),
        ("Ctrl+Backspace", CONTROL, "BackSpace"),
        ("Ctrl+PageUp", CONTROL, "Prior"),
        ("Ctrl+PageDown", CONTROL, "Next"),
        ("Ctrl+`", CONTROL, "grave"),
        ("Ctrl+\\", CONTROL, "backslash"),
        ("Ctrl+/", CONTROL, "slash"),
        ("Ctrl+Left", CONTROL, "Left"),
    ],
)
def test_to_x11(spec, mask, name):
    assert to_x11(spec) == (mask, name)


@pytest.mark.parametrize("spec", ["", "Space", "Ctrl+", "Ctrl+F25", "Ctrl+Hyper"])
def test_to_x11_rejects(spec):
    with pytest.raises(ValueError):
        to_x11(spec)


def test_to_x11_has_no_copilot():
    with pytest.raises(ValueError, match="Windows only"):
        to_x11("Copilot")


def test_without_a_display_start_says_what_to_do(monkeypatch):
    monkeypatch.setattr(hk, "_is_windows", lambda: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    thread = HotkeyThread(lambda: None)
    message = thread.start("Ctrl+Alt+Space")
    assert message == "No X11 display: bind a desktop shortcut to `chatforge --show` instead"
    assert thread.error == message and thread._x11 is None and thread.current is None
    monkeypatch.setenv("DISPLAY", "")
    assert HotkeyThread(lambda: None).start("Ctrl+Alt+Space") == message


@pytest.mark.parametrize("spec", ["Copilot", "copilot key"])
def test_copilot_on_linux_is_windows_only(monkeypatch, spec):
    monkeypatch.setattr(hk, "_is_windows", lambda: False)
    monkeypatch.setenv("DISPLAY", ":0")
    thread = HotkeyThread(lambda: None)
    message = thread.start(spec)
    assert "Windows only" in message and thread.error == message and thread._x11 is None
    assert "Windows only" in thread.register(spec)


def test_macos_is_not_supported(monkeypatch):
    monkeypatch.setattr(hk, "_is_windows", lambda: False)
    monkeypatch.setattr(hk.sys, "platform", "darwin")
    assert "only supported on Windows and Linux" in HotkeyThread(lambda: None).start("Ctrl+Q")


# --- the grab thread (real thread, fake Xlib; select() on pipes: not on Windows) -------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="select() on pipes")


@posix_only
def test_grabs_every_lock_combination_and_fires_on_keypress(thread, xlib, pressed):
    assert thread.start("Ctrl+Alt+Space") is None
    display = xlib.instances[0]
    keycode = 0x20 + 8
    assert display.root.grabbed == {(keycode, m) for m in lock_combos(CONTROL | MOD1)}
    assert all(call[2:] == (True, 1, 1) for call in display.root.grab_calls)
    assert thread.current == "Ctrl+Alt+Space" and thread.error is None
    assert thread.status == "Hotkey grabbed: Ctrl+Alt+Space"

    display.push(keycode, CONTROL | MOD1 | LOCK | MOD2)  # NumLock and CapsLock on
    assert pressed.event.wait(2) and pressed.count == 1


@posix_only
def test_other_keys_and_modifiers_do_not_fire(thread, xlib, pressed):
    thread.start("Ctrl+Alt+Space")
    display = xlib.instances[0]
    display.push(0x20 + 8, CONTROL | MOD1 | SHIFT)  # extra Shift
    display.push(0x61 + 8, CONTROL | MOD1)  # another key
    display.push(0x20 + 8, CONTROL | MOD1, type_=3)  # KeyRelease
    display.push(0x20 + 8, CONTROL | MOD1)  # the real one, last
    assert pressed.event.wait(2)
    assert pressed.count == 1


@posix_only
def test_auto_repeat_is_debounced(thread, xlib, pressed, monkeypatch):
    thread.start("Ctrl+Alt+Space")
    display = xlib.instances[0]
    for _ in range(3):
        display.push(0x20 + 8, CONTROL | MOD1)
    display.push(0x20 + 8, CONTROL | MOD1)
    assert pressed.event.wait(2)
    thread._x11.stop()  # drains: the loop has handled everything queued before the quit
    assert pressed.count == 1


@posix_only
def test_a_grab_taken_by_another_client_is_a_normal_hotkey_error(thread, xlib):
    xlib.taken = {0x20 + 8}
    FakeDisplay.taken = xlib.taken
    message = thread.start("Ctrl+Alt+Space")
    assert message == "Ctrl+Alt+Space is in use by another app. Choose a different combination."
    assert thread.current is None and thread.error == message
    assert xlib.instances[0].root.grabbed == set()  # nothing half-grabbed


@posix_only
def test_changing_the_hotkey_ungrabs_the_old_one(thread, xlib):
    thread.start("Ctrl+Alt+Space")
    root = xlib.instances[0].root
    assert thread.register("ctrl + shift + a") is None
    assert thread.current == "Ctrl+Shift+A"
    assert root.grabbed == {(0x61 + 8, m) for m in lock_combos(CONTROL | SHIFT)}


@posix_only
def test_a_refused_change_restores_the_previous_hotkey(thread, xlib):
    thread.start("Ctrl+Alt+Space")
    xlib.taken.add(0x61 + 8)
    root = xlib.instances[0].root
    message = thread.register("Ctrl+A")
    assert "in use by another app" in message and "Ctrl+Alt+Space still works" in message
    assert thread.current == "Ctrl+Alt+Space" and thread.error == message
    assert root.grabbed == {(0x20 + 8, m) for m in lock_combos(CONTROL | MOD1)}


@posix_only
def test_a_key_the_layout_lacks_is_reported(thread, xlib):
    thread.start("Ctrl+Alt+Space")
    message = thread.register("Ctrl+K")
    assert "no key on this keyboard layout" in message and "Ctrl+Alt+Space still works" in message
    message = thread.register("Ctrl+Z")  # not in the fake keysym table at all
    assert "no key on this keyboard layout" in message


@posix_only
def test_bad_specs_are_rejected_before_the_thread(thread, xlib):
    thread.start("Ctrl+Alt+Space")
    assert "unknown key" in thread.register("Ctrl+Hyper")
    assert thread.current == "Ctrl+Alt+Space"


@posix_only
def test_stop_ungrabs_closes_and_joins(xlib, pressed):
    thread = HotkeyThread(lambda: None)
    thread.start("Ctrl+Alt+Space")
    x11_thread = thread._x11._thread
    display = xlib.instances[0]
    thread.stop()
    assert not x11_thread.is_alive()
    assert display.root.grabbed == set() and display.closed
    assert thread.current is None and thread._x11 is None
    thread.stop()  # idempotent


@posix_only
def test_start_without_a_spec_opens_the_display_and_register_grabs(thread, xlib):
    assert thread.start() is None
    assert xlib.instances[0].root.grabbed == set()
    assert thread.register("Ctrl+Alt+Space") is None
    assert thread.current == "Ctrl+Alt+Space"


@posix_only
def test_display_open_failure_is_reported(thread, xlib):
    FakeDisplay.fail_open = RuntimeError("cannot connect")
    message = thread.start("Ctrl+Alt+Space")
    assert message == "Could not open the X11 display (cannot connect)."
    assert thread.error == message and thread._x11 is None


@posix_only
def test_missing_python_xlib_is_reported(thread, monkeypatch):
    monkeypatch.setitem(sys.modules, "Xlib", None)  # makes "from Xlib import ..." fail
    message = thread.start("Ctrl+Alt+Space")
    assert "python-xlib is not available" in message


def test_register_before_start_says_not_running(xlib):
    assert HotkeyThread(lambda: None).register("Ctrl+Alt+Space") == (
        "The hotkey thread is not running."
    )
