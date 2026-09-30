"""Hotkey strings -> Win32 modifier flags and virtual-key codes."""

from __future__ import annotations

import pytest

from aichat.desktop.hotkey import (
    ERROR_HOTKEY_ALREADY_REGISTERED,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    canonical,
    describe_error,
    to_win32,
)


@pytest.mark.parametrize(
    ("spec", "mods", "vk"),
    [
        ("Ctrl+Alt+Space", MOD_CONTROL | MOD_ALT, 0x20),
        ("ctrl+alt+space", MOD_CONTROL | MOD_ALT, 0x20),
        ("Alt+Ctrl+Space", MOD_CONTROL | MOD_ALT, 0x20),  # order does not matter
        ("Win+Shift+A", MOD_WIN | MOD_SHIFT, ord("A")),
        ("Ctrl+a", MOD_CONTROL, ord("A")),
        ("Ctrl+1", MOD_CONTROL, ord("1")),
        ("Ctrl+F1", MOD_CONTROL, 0x70),
        ("Ctrl+F12", MOD_CONTROL, 0x7B),
        ("Ctrl+F24", MOD_CONTROL, 0x87),
        ("Ctrl+Enter", MOD_CONTROL, 0x0D),
        ("Ctrl+Esc", MOD_CONTROL, 0x1B),
        ("Ctrl+Escape", MOD_CONTROL, 0x1B),
        ("Ctrl+Tab", MOD_CONTROL, 0x09),
        ("Ctrl+Home", MOD_CONTROL, 0x24),
        ("Ctrl+PageUp", MOD_CONTROL, 0x21),
        ("Ctrl+Up", MOD_CONTROL, 0x26),
        ("Ctrl+`", MOD_CONTROL, 0xC0),
        ("Ctrl+/", MOD_CONTROL, 0xBF),
        ("Ctrl+Alt+Shift+Win+Z", MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN, ord("Z")),
        ("Control+Super+Space", MOD_CONTROL | MOD_WIN, 0x20),
    ],
)
def test_to_win32(spec: str, mods: int, vk: int) -> None:
    assert to_win32(spec) == (mods, vk)


@pytest.mark.parametrize(
    "spec",
    ["", "Space", "Ctrl+", "+Space", "Ctrl+Alt", "Foo+Space", "Ctrl+F25", "Ctrl+Hyper", "Ctrl+AB"],
)
def test_rejects(spec: str) -> None:
    with pytest.raises(ValueError):
        to_win32(spec)


def test_canonical_form() -> None:
    assert canonical("shift + ctrl + space") == "Ctrl+Shift+Space"
    assert canonical("alt+F3") == "Alt+F3"
    assert canonical("win+q") == "Win+Q"


def test_describe_error_mentions_conflict() -> None:
    text = describe_error("Ctrl+Alt+Space", ERROR_HOTKEY_ALREADY_REGISTERED)
    assert "in use" in text
    assert "Ctrl+Alt+Space" in text
    assert "1234" in describe_error("Ctrl+Q", 1234)
