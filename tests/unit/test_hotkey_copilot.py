"""The Copilot key (Left Win + Left Shift + F23) through a fake ``user32`` and a real
``KBDLLHOOKSTRUCT`` passed to the hook callback, the way Windows would call it."""

from __future__ import annotations

import ctypes
import threading

import pytest

from chatforge.desktop import hotkey as hk
from chatforge.desktop.hotkey import HotkeyThread

WIN, SHIFT, F23, DUMMY = hk.VK_LWIN, hk.VK_LSHIFT, hk.VK_F23, hk.VK_DUMMY
DOWN, UP = hk.WM_KEYDOWN, hk.WM_KEYUP
PASSED = 77  # what the fake CallNextHookEx returns


class FakeUser32:
    def __init__(self, hook_result: int = 0x1234) -> None:
        self.hook_result = hook_result
        self.calls: list[tuple] = []  # ordered: ("next", vk) / ("send", [...]) / ...
        self.proc = None
        self.hooks: list[tuple] = []
        self.unhooked: list[int] = []
        self.held: set[int] = set()  # what GetAsyncKeyState reports
        self.registered: tuple | None = None
        self.taken: set[tuple] = set()

    # hook api
    def SetWindowsHookExW(self, id_hook, proc, hmod, tid):  # noqa: N802
        self.hooks.append((id_hook, hmod, tid))
        if not self.hook_result:
            return 0
        self.proc = proc
        return self.hook_result

    def UnhookWindowsHookEx(self, hhook):  # noqa: N802
        self.unhooked.append(hhook)
        return 1

    def CallNextHookEx(self, hhook, n_code, w_param, l_param):  # noqa: N802
        vk = ctypes.cast(l_param, ctypes.POINTER(hk._KBDLLHOOKSTRUCT)).contents.vkCode
        self.calls.append(("next", vk, w_param))
        return PASSED

    def GetAsyncKeyState(self, vk):  # noqa: N802
        return -32768 if vk in self.held else 0

    def SendInput(self, count, inputs, size):  # noqa: N802
        sent = [(i.ki.wVk, i.ki.dwFlags, i.ki.dwExtraInfo, i.type) for i in inputs]
        assert count == len(sent) == 2 and size == ctypes.sizeof(hk._INPUT)
        self.calls.append(("send", sent))
        return count

    # RegisterHotKey api (for switching away from / back to Copilot)
    def RegisterHotKey(self, hwnd, hid, mods, vk):  # noqa: N802
        key = (mods & ~hk.MOD_NOREPEAT, vk)
        if self.registered is not None or key in self.taken:
            return 0
        self.registered = key
        return 1

    def UnregisterHotKey(self, hwnd, hid):  # noqa: N802
        self.registered = None
        return 1

    # driving the callback
    def key(self, vk: int, message: int = DOWN, extra: int = 0, n_code: int = 0) -> int:
        struct = hk._KBDLLHOOKSTRUCT(vk, 0, 0, 0, extra)
        return self.proc(n_code, message, ctypes.addressof(struct))

    def press(self, vk: int) -> int:
        self.held.add(vk)
        return self.key(vk, DOWN)

    def release(self, vk: int) -> int:
        self.held.discard(vk)
        return self.key(vk, UP)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(hk, "_is_windows", lambda: True)
    monkeypatch.setattr(hk, "_module_handle", lambda: 0xABC)
    monkeypatch.setattr(hk, "_last_error", lambda: 5)


@pytest.fixture
def fired() -> list[int]:
    return []


@pytest.fixture
def thread(fired) -> HotkeyThread:
    t = HotkeyThread(lambda: None)
    t._fire = lambda: fired.append(1)  # type: ignore[method-assign]
    return t


def apply(thread: HotkeyThread, user32: FakeUser32, spec: str) -> str | None:
    thread._pending = spec
    thread._pending_done.clear()
    thread._apply_pending(user32)
    assert thread._pending_done.is_set()
    return thread._pending_result


@pytest.fixture
def hooked(thread):
    user32 = FakeUser32()
    assert apply(thread, user32, "Copilot") is None
    return thread, user32


# --- install / failure / unhook ---------------------------------------------------------


def test_installs_a_low_level_keyboard_hook_instead_of_registerhotkey(hooked):
    thread, user32 = hooked
    assert user32.hooks == [(hk.WH_KEYBOARD_LL, 0xABC, 0)]
    assert user32.registered is None
    assert thread.current == "Copilot" and thread.error is None
    assert thread.status == "Copilot key hooked"
    assert thread._copilot is not None and thread._copilot.proc is user32.proc  # kept alive


def test_a_refused_hook_is_a_normal_hotkey_error(thread):
    user32 = FakeUser32(hook_result=0)
    message = apply(thread, user32, "Copilot")
    assert message == "Could not hook the Copilot key (Windows error 5)."
    assert thread.current is None and thread.error == message and thread._copilot is None


def test_unhook_on_stop(hooked):
    thread, user32 = hooked
    thread._unregister(user32, thread.current)
    assert user32.unhooked == [0x1234]
    assert thread._copilot is None


def test_switching_to_a_normal_hotkey_unhooks_and_registers(hooked):
    thread, user32 = hooked
    assert apply(thread, user32, "Ctrl+Alt+Space") is None
    assert user32.unhooked == [0x1234]
    assert user32.registered == (hk.MOD_CONTROL | hk.MOD_ALT, 0x20)
    assert thread.current == "Ctrl+Alt+Space"


def test_switching_back_to_copilot_releases_the_registered_key(thread):
    user32 = FakeUser32()
    apply(thread, user32, "Ctrl+Alt+Space")
    assert apply(thread, user32, "Copilot") is None
    assert user32.registered is None and thread.current == "Copilot"


def test_a_refused_change_restores_the_copilot_hook(hooked, monkeypatch):
    thread, user32 = hooked
    monkeypatch.setattr(hk, "_last_error", lambda: hk.ERROR_HOTKEY_ALREADY_REGISTERED)
    user32.taken.add((hk.MOD_CONTROL | hk.MOD_ALT, 0x20))
    message = apply(thread, user32, "Ctrl+Alt+Space")
    assert "in use by another app" in message and "Copilot still works" in message
    assert thread.current == "Copilot"
    assert len(user32.hooks) == 2 and user32.unhooked == [0x1234]
    assert thread._copilot is not None


def test_register_accepts_copilot_and_rejects_nonsense(thread):
    # a valid spec gets as far as the thread check; the aliases canonicalise to "Copilot"
    assert thread.register("copilot KEY") == "The hotkey thread is not running."
    assert "unknown key" in thread.register("Ctrl+Copilot")


def test_to_win32_has_no_registerhotkey_form_for_copilot():
    with pytest.raises(ValueError, match="keyboard hook"):
        hk.to_win32("Copilot")


# --- chord detection and swallowing -------------------------------------------------------


def test_the_chord_fires_once_swallows_f23_and_masks_win(hooked, fired):
    _, user32 = hooked
    assert user32.press(WIN) == PASSED
    assert user32.press(SHIFT) == PASSED
    assert user32.press(F23) == 1  # swallowed, no CallNextHookEx
    assert fired == [1]
    assert user32.key(F23, DOWN) == 1  # auto-repeat: swallowed, not fired again
    assert user32.key(F23, hk.WM_SYSKEYDOWN) == 1
    assert fired == [1]
    assert user32.release(F23) == 1  # the matching release is swallowed too
    assert user32.release(SHIFT) == PASSED
    user32.calls.clear()
    assert user32.release(WIN) == PASSED
    # the dummy key goes in BEFORE the Win key-up is passed on
    assert user32.calls == [
        (
            "send",
            [
                (DUMMY, 0, hk.INJECT_TAG, hk.INPUT_KEYBOARD),
                (DUMMY, hk.KEYEVENTF_KEYUP, hk.INJECT_TAG, hk.INPUT_KEYBOARD),
            ],
        ),
        ("next", WIN, UP),
    ]


def test_modifiers_are_never_swallowed(hooked):
    _, user32 = hooked
    for vk in (WIN, SHIFT, 0x11, 0x12, 0x41):
        assert user32.press(vk) == PASSED
        assert user32.release(vk) == PASSED


def test_win_up_is_masked_only_once_and_only_after_the_chord(hooked):
    _, user32 = hooked
    user32.press(WIN)
    user32.release(WIN)  # plain Win tap: Start must still open
    assert not [c for c in user32.calls if c[0] == "send"]
    user32.press(WIN)
    user32.press(SHIFT)
    user32.press(F23)
    user32.release(F23)
    user32.release(WIN)
    user32.press(WIN)
    user32.release(WIN)  # a later plain tap is not masked again
    assert len([c for c in user32.calls if c[0] == "send"]) == 1


def test_win_can_be_released_before_shift(hooked):
    _, user32 = hooked
    user32.press(WIN)
    user32.press(SHIFT)
    user32.press(F23)
    user32.release(WIN)
    assert [c[0] for c in user32.calls if c[0] == "send"] == ["send"]
    assert user32.release(F23) == 1
    assert user32.release(SHIFT) == PASSED


@pytest.mark.parametrize("held", [(), (WIN,), (SHIFT,), (0x11, 0x10)])
def test_f23_without_the_chord_passes_through(hooked, fired, held):
    _, user32 = hooked
    for vk in held:
        user32.press(vk)
    assert user32.press(F23) == PASSED
    assert user32.release(F23) == PASSED
    assert fired == []


def test_the_chord_is_confirmed_with_getasynckeystate(hooked, fired):
    thread, user32 = hooked
    user32.press(WIN)
    user32.press(SHIFT)
    user32.held.discard(SHIFT)  # we missed the Shift key-up (secure desktop, lock screen)
    assert user32.press(F23) == PASSED
    assert fired == []
    hook = thread._copilot
    assert hook.win_down and not hook.shift_down  # state resynced from the real state
    user32.release(F23)
    user32.press(SHIFT)
    assert user32.press(F23) == 1 and fired == [1]


def test_our_own_dummy_keys_do_not_touch_the_state(hooked):
    thread, user32 = hooked
    user32.press(WIN)
    assert user32.key(WIN, UP, extra=hk.INJECT_TAG) == PASSED
    assert thread._copilot.win_down
    assert user32.key(DUMMY, DOWN, extra=hk.INJECT_TAG) == PASSED


def test_negative_ncode_and_unknown_messages_just_pass_on(hooked, fired):
    _, user32 = hooked
    user32.press(WIN)
    user32.press(SHIFT)
    assert user32.key(F23, DOWN, n_code=-1) == PASSED
    assert user32.key(F23, 0x0200) == PASSED  # not a key message
    assert fired == []


def test_an_exception_in_the_hook_still_calls_next(hooked, monkeypatch):
    thread, user32 = hooked

    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(thread._copilot, "process", boom)
    assert user32.key(F23, DOWN) == PASSED


def test_reinstalling_resets_the_state(thread):
    user32 = FakeUser32()
    apply(thread, user32, "Copilot")
    user32.press(WIN)
    user32.press(SHIFT)
    user32.press(F23)  # swallowing...
    apply(thread, user32, "Copilot")  # ...and the hook is replaced
    hook = thread._copilot
    assert not (hook.win_down or hook.shift_down or hook.swallowing or hook.mask_pending)


def test_a_real_fire_calls_the_handler_off_the_hook_thread():
    done = threading.Event()
    seen: list[str] = []

    def on_press() -> None:
        seen.append(threading.current_thread().name)
        done.set()

    thread = HotkeyThread(on_press)
    user32 = FakeUser32()
    apply(thread, user32, "Copilot")
    user32.press(WIN)
    user32.press(SHIFT)
    assert user32.press(F23) == 1
    assert done.wait(2)
    assert seen == ["chatforge-hotkey-press"]
