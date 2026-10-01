"""App and tray icon rendering: the StudioForge frame and palette, per-size drawings in the
``.ico``, the simplified small-size glyph, and a distinct status dot per state."""

from __future__ import annotations

import pytest
from PIL import Image

from chatforge.desktop.icon import (
    BUBBLE,
    DOT_COLOURS,
    ICO_SIZES,
    ICON_BG,
    RING,
    SPARK,
    STATES,
    dot_colour,
    frame_metrics,
    make_app_icon,
    make_icon_image,
    state_for_runtime,
    write_app_ico,
)

#: Windows 11 taskbar colours in the light and dark themes.
LIGHT_TASKBAR = (243, 243, 243)
DARK_TASKBAR = (32, 32, 32)


def _luminance(rgb: tuple[int, ...]) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _glyph_box(size: int) -> tuple[int, int]:
    """Origin and span (px) of the box inside the ring that the glyph is laid out in."""
    pad, _radius, ring = frame_metrics(size)
    lo = pad + ring
    return lo, size - 2 * lo


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


def test_app_icon_ico_has_every_size(tmp_path) -> None:
    assert make_app_icon(48).size == (48, 48)
    path = write_app_ico(tmp_path / "app.ico")
    with Image.open(path) as ico:
        assert ico.format == "ICO"
        assert {s[0] for s in ico.info["sizes"]} == set(ICO_SIZES)
    first = path.stat().st_mtime_ns
    write_app_ico(path)  # already there: left alone
    assert path.stat().st_mtime_ns == first


@pytest.mark.parametrize("size", [16, 24, 32])
def test_ico_small_sizes_are_their_own_drawings(tmp_path, size: int) -> None:
    """The small frames are drawn at their size, not shrunk from the 256 px one."""
    path = write_app_ico(tmp_path / "app.ico")
    with Image.open(path) as ico:
        ico.size = (size, size)
        ico.load()
        frame = ico.convert("RGBA")
    assert frame.tobytes() == make_app_icon(size).tobytes()


def test_studioforge_frame_and_palette() -> None:
    """Navy rounded square, cyan ring, white bubble, blue spark (StudioForge's colours)."""
    img = make_app_icon(256)
    pad, _radius, ring = frame_metrics(256)
    lo, span = _glyph_box(256)
    assert img.getpixel((0, 0))[3] == 0
    assert img.getpixel((128, pad + ring // 2)) == RING
    assert img.getpixel((128, lo + 4)) == ICON_BG
    assert img.getpixel((round(lo + 0.2 * span), round(lo + 0.445 * span))) == BUBBLE
    assert img.getpixel((round(lo + 0.5 * span), round(lo + 0.445 * span))) == SPARK


def test_frame_metrics_follow_studioforge() -> None:
    # StudioForge: inset size/16 (>= 1), radius size/5 (>= 3), ring size/24 (>= 2).
    assert frame_metrics(256) == (16, 51, 10)
    assert frame_metrics(16) == (1, 3, 2)
    # The tray ring is heavier, so it survives Windows shrinking the 64 px image.
    assert frame_metrics(64, tray=True)[2] == 4 > frame_metrics(64)[2]
    assert frame_metrics(16, tray=True)[2] == 2


def test_tray_ring_is_heavier_than_app_ring() -> None:
    def nearer_ring(px: tuple[int, ...]) -> bool:
        def dist(c: tuple[int, ...]) -> int:
            return sum((a - b) ** 2 for a, b in zip(px[:3], c[:3], strict=True))

        return dist(RING) < dist(ICON_BG)

    # Four pixels down from the top edge: still ring in the tray, already navy in the app icon.
    assert nearer_ring(make_icon_image("off", 64).getpixel((32, 7)))
    assert not nearer_ring(make_app_icon(64).getpixel((32, 7)))


@pytest.mark.parametrize(("size", "companion"), [(24, False), (32, False), (48, True), (256, True)])
def test_small_sizes_drop_the_companion_spark(size: int, companion: bool) -> None:
    lo, span = _glyph_box(size)
    pixel = make_app_icon(size).getpixel((int(lo + 0.755 * span), int(lo + 0.30 * span)))
    is_blue = pixel[2] - pixel[0] > 100  # blue spark, not the near-white bubble
    assert is_blue is companion, pixel


def test_legible_on_light_and_dark_taskbars() -> None:
    # The cyan ring carries the outline on a dark taskbar, the navy body on a light one.
    assert _contrast(RING, DARK_TASKBAR) >= 3
    assert _contrast(ICON_BG, LIGHT_TASKBAR) >= 3
    # Inside: the bubble against the navy, the spark against the bubble.
    assert _contrast(BUBBLE, ICON_BG) >= 3
    assert _contrast(SPARK, BUBBLE) >= 3
