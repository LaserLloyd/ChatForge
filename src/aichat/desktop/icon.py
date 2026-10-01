"""App and tray icon images in the StudioForge style: a navy rounded square with a cyan ring,
holding a white chat bubble with a blue spark.

# Adapted from StudioForge src/studioforge/tray/tray_app.py `make_icon_image` (MIT, LaserLloyd)

The frame and palette are StudioForge's: a slate-900 rounded square inset by ``size/16`` with
a ``size/5`` corner radius and a sky-400 ring, flat colours (no gradient, no shadow), and a
status dot in the lower right corner of the tray icon. StudioForge draws its ``SF`` letters in
the ring's cyan with a blue-500 accent; this app draws a white speech bubble holding a blue-500
four-pointed spark (the "AI" sparkle, and a spark off the forge). Every colour is StudioForge's,
but the white bubble keeps the two trays tellable apart at 16 px when they sit side by side.

The app icon (window title bars, taskbar, Alt+Tab) is written once as a multi-size ``.ico`` by
:func:`write_app_ico`; without it pywebview shows the icon of ``pythonw.exe``. The tray icon is
the same artwork plus a four-state status dot (PLAN WS7 step 6): ``ready`` green, ``loading``
amber (starting or compiling), ``off`` grey (unloaded, or a cloud provider), ``error`` red.

Everything is drawn with PIL rather than shipped as binary files, so the package stays pure
Python. Each size is drawn on its own at 1024 px or more, with the frame measured in whole
pixels of the final size, and scaled down with Lanczos, so the 16 px and 24 px icons keep a
crisp two-pixel ring instead of a smeared copy of the 256 px one. Below 40 px the glyph is
simplified: the small companion spark is left out, the main spark is centred on a pixel, and the
bubble's tail is squarer.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw

IconState = Literal["ready", "loading", "off", "error"]

#: StudioForge's ``ICON_BG`` (slate-900): the background, and the ring around the status dot
#: so the dot stands out on any taskbar colour.
ICON_BG = (15, 23, 42, 255)
#: StudioForge's ``SF_CYAN`` (sky-400): the ring.
RING = (56, 189, 248, 255)
#: The chat bubble (slate-50).
BUBBLE = (248, 250, 252, 255)
#: StudioForge's ``SF_BLUE`` (blue-500): the spark.
SPARK = (59, 130, 246, 255)

DOT_COLOURS: dict[str, tuple[int, int, int, int]] = {
    "ready": (46, 204, 113, 255),  # green
    "loading": (255, 179, 0, 255),  # amber
    "off": (128, 134, 144, 255),  # grey
    "error": (231, 76, 60, 255),  # red
}

STATES: tuple[IconState, ...] = ("ready", "loading", "off", "error")

#: Sizes in the ``.ico``: small (title bar, tray) to large (Alt+Tab, high-DPI taskbar).
ICO_SIZES: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)
#: Each size is drawn at least this large, then scaled down.
_MASTER = 1024
#: Below this size the glyph is simplified: no companion spark, a pixel-centred spark.
_SMALL = 40


def state_for_runtime(runtime_state: str | None, *, local_selected: bool = True) -> IconState:
    """Map a ``runtime.status.state`` to an icon state."""
    if not local_selected:
        return "off"
    if runtime_state == "ready":
        return "ready"
    if runtime_state in ("starting", "compiling", "unloading"):
        return "loading"
    if runtime_state == "error":
        return "error"
    return "off"


def frame_metrics(size: int, *, tray: bool = False) -> tuple[int, int, int]:
    """StudioForge's frame at ``size`` px, in whole pixels: inset, corner radius, ring width.

    The tray ring is twice as heavy: pystray hands Windows the 64 px tray image, which the shell
    shrinks to 16-24 px, and a ``size/24`` ring would thin out to half a pixel on the way down.
    """
    pad = max(1, size // 16)
    radius = max(3, size // 5)
    ring = max(2, size // 16 if tray else size // 24)
    return pad, radius, ring


def _spark(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill: tuple) -> None:
    """A four-pointed star: long points on the axes, a narrow waist on the diagonals."""
    waist = r * 0.28
    pts = [
        (cx, cy - r),
        (cx + waist, cy - waist),
        (cx + r, cy),
        (cx + waist, cy + waist),
        (cx, cy + r),
        (cx - waist, cy + waist),
        (cx - r, cy),
        (cx - waist, cy - waist),
    ]
    draw.polygon(pts, fill=fill)


@lru_cache(maxsize=32)
def _render(size: int, tray: bool) -> Image.Image:
    k = max(4, math.ceil(_MASTER / size))  # supersampling factor
    s = size * k
    pad, radius, ring = frame_metrics(size, tray=tray)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [pad * k, pad * k, (size - pad) * k - 1, (size - pad) * k - 1],
        radius=radius * k,
        fill=ICON_BG,
        outline=RING,
        width=ring * k,
    )

    # The glyph is laid out in the box inside the ring, in units of that box (0..1).
    lo = pad + ring
    span = size - 2 * lo

    def px(v: float) -> float:  # box units -> final-size pixels
        return lo + v * span

    def at(x: float, y: float) -> tuple[float, float]:
        return px(x) * k, px(y) * k

    def edge(v: float) -> int:  # a bubble edge, on a whole final-size pixel
        return round(px(v)) * k

    small = size < _SMALL
    # Speech bubble with its tail at the lower left, clear of the tray's status dot.
    draw.rounded_rectangle(
        [edge(0.12), edge(0.19), edge(0.88) - 1, edge(0.70) - 1],
        radius=0.16 * span * k,
        fill=BUBBLE,
    )
    if small:
        # A squarer tail with a straight left edge on a pixel line, so it survives at 16 px.
        x = edge(0.21)
        draw.polygon([(x, px(0.6) * k), (x, px(0.90) * k), at(0.50, 0.68)], fill=BUBBLE)
    else:
        draw.polygon([at(0.21, 0.62), at(0.19, 0.88), at(0.46, 0.68)], fill=BUBBLE)
    # The spark, centred in the bubble, with a small companion at the upper right. Small sizes
    # drop the companion and centre the spark on a pixel, so it lands as a crisp cross.
    cx, cy = px(0.5), px(0.445)
    if small:
        cx, cy = math.floor(cx) + 0.5, math.floor(cy) + 0.5
        _spark(draw, cx * k, cy * k, 0.24 * span * k, SPARK)
    else:
        _spark(draw, cx * k, cy * k, 0.19 * span * k, SPARK)
        _spark(draw, *at(0.755, 0.30), 0.07 * span * k, SPARK)

    img = img.resize((size, size), Image.Resampling.LANCZOS)
    # Lanczos leaves a faint haze (alpha 1-7) outside the rounded corners at small sizes;
    # clear it so the corners are truly transparent.
    img.putalpha(img.getchannel("A").point(lambda a: 0 if a < 8 else a))
    return img


def make_app_icon(size: int = 256) -> Image.Image:
    """The app icon at ``size`` px (no status dot). Cached: copy before drawing on it."""
    return _render(size, False)


def write_app_ico(path: Path | str) -> Path:
    """Write the multi-size app icon to ``path`` (skipped when it is already there).

    Every size in :data:`ICO_SIZES` is its own drawing, not a shrunken copy of the 256 px one.
    """
    target = Path(path)
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    largest = max(ICO_SIZES)
    make_app_icon(largest).save(
        tmp,
        format="ICO",
        sizes=[(n, n) for n in ICO_SIZES],
        append_images=[make_app_icon(n) for n in ICO_SIZES if n != largest],
    )
    tmp.replace(target)
    return target


def _dot_geometry(size: int) -> tuple[float, float, float]:
    pad = max(1, size // 16)
    r = size * 0.185
    c = size - pad - r * 0.95
    return c, c, r


def make_icon_image(state: IconState | str = "off", size: int = 64) -> Image.Image:
    """The tray icon: the artwork, with the heavier tray ring, plus the status dot in the
    lower right corner.

    The dot is the only part that carries information, so it is drawn large, at the final
    size (crisp edges), where it survives being scaled down to 16 px.
    """
    if state not in DOT_COLOURS:
        state = "off"
    img = _render(size, True).copy()
    draw = ImageDraw.Draw(img)
    cx, cy, r = _dot_geometry(size)
    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r],
        fill=DOT_COLOURS[state],
        outline=ICON_BG,
        width=max(1, size // 32),
    )
    return img


def dot_colour(img: Image.Image) -> tuple[int, int, int, int]:
    """The pixel at the status dot's centre (tests)."""
    cx, cy, _r = _dot_geometry(img.size[0])
    return tuple(img.getpixel((int(cx), int(cy))))  # type: ignore[return-value]
