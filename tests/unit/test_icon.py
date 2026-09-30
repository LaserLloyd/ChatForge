"""Tray icon rendering at 16, 32 and 64 px, with a distinct dot per state."""

from __future__ import annotations

import pytest

from aichat.desktop.icon import DOT_COLOURS, STATES, dot_colour, make_icon_image, state_for_runtime


@pytest.mark.parametrize("size", [16, 32, 64])
@pytest.mark.parametrize("state", STATES)
def test_renders_at_size(size: int, state: str) -> None:
    img = make_icon_image(state, size)
    assert img.mode == "RGBA"
    assert img.size == (size, size)
    # Corners stay transparent (rounded square), the centre is painted.
    assert img.getpixel((0, 0))[3] == 0
    assert img.getpixel((size // 2, size // 2))[3] == 255


@pytest.mark.parametrize("size", [16, 32, 64])
def test_dot_colour_per_state(size: int) -> None:
    seen = {}
    for state in STATES:
        colour = dot_colour(make_icon_image(state, size))
        assert colour == DOT_COLOURS[state], (state, size, colour)
        seen[state] = colour
    assert len(set(seen.values())) == 4


def test_unknown_state_falls_back_to_off() -> None:
    assert dot_colour(make_icon_image("weird", 32)) == DOT_COLOURS["off"]


def test_state_for_runtime() -> None:
    assert state_for_runtime("ready") == "ready"
    assert state_for_runtime("starting") == "loading"
    assert state_for_runtime("compiling") == "loading"
    assert state_for_runtime("error") == "error"
    assert state_for_runtime("unloaded") == "off"
    assert state_for_runtime("not_installed") == "off"
    assert state_for_runtime(None) == "off"
    assert state_for_runtime("ready", local_selected=False) == "off"
