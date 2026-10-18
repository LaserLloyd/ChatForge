from __future__ import annotations

import pytest

from chatforge_voice import hotkey


def test_parse_hotkey():
    assert hotkey.parse_hotkey("Ctrl+Alt+Space") == (0x11, 0x12, 0x20)
    assert hotkey.parse_hotkey("f9") == (0x78,)
    assert hotkey.parse_hotkey("shift+v") == (0x10, ord("V"))
    assert hotkey.parse_hotkey("ctrl+ctrl+x") == (0x11, ord("X"))


@pytest.mark.parametrize("bad", ["", "ctrl+alt", "a+b", "ctrl+banana", "f99"])
def test_bad_hotkeys_raise_a_sentence(bad):
    with pytest.raises(ValueError) as e:
        hotkey.parse_hotkey(bad)
    assert str(e.value).endswith(".")


def test_press_and_release_events():
    down: set[int] = set()
    log: list[str] = []
    w = hotkey.HoldKeyWatcher(
        lambda: log.append("press"), lambda: log.append("release"), is_down=down.__contains__
    )
    assert w.set_spec("Ctrl+Space")
    w.tick()
    down.add(0x11)
    w.tick()
    assert log == []  # only half the combination
    down.add(0x20)
    w.tick()
    w.tick()
    assert log == ["press"]  # once, not once per poll
    down.discard(0x20)
    w.tick()
    assert log == ["press", "release"]


def test_an_inactive_watcher_releases_a_held_key_and_ignores_new_presses():
    down = {0x78}
    active = [True]
    log: list[str] = []
    w = hotkey.HoldKeyWatcher(
        lambda: log.append("p"),
        lambda: log.append("r"),
        is_down=down.__contains__,
        active=lambda: active[0],
    )
    w.set_spec("F9")
    w.tick()
    active[0] = False
    w.tick()
    w.tick()
    assert log == ["p", "r"]


def test_a_bad_spec_disables_the_watcher_and_says_why():
    w = hotkey.HoldKeyWatcher(lambda: None, lambda: None, is_down=lambda vk: True)
    assert w.set_spec("ctrl+banana") is False
    assert "banana" in w.error
    w.tick()  # no keys: nothing fires


def test_a_failing_callback_does_not_kill_the_watcher():
    down = {0x78}

    def boom():
        raise RuntimeError("x")

    w = hotkey.HoldKeyWatcher(boom, boom, is_down=down.__contains__)
    w.set_spec("F9")
    w.tick()
    down.clear()
    w.tick()  # no exception
