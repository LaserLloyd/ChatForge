"""App and tray icon images: a blue rounded square with a white chat bubble and a sparkle.

# Adapted from StudioForge src/studioforge/tray/tray_app.py `make_icon_image` (MIT, LaserLloyd)

The app icon (window title bars, taskbar, Alt+Tab) is written once as a multi-size ``.ico``
by :func:`write_app_ico`; without it pywebview shows the icon of ``pythonw.exe``. The tray
icon is the same artwork plus a four-state status dot (PLAN WS7 step 6): ``ready`` green,
``loading`` amber (starting or compiling), ``off`` grey (unloaded, or a cloud provider),
``error`` red.

Everything is drawn with PIL rather than shipped as binary files, so the package stays pure
Python. The artwork is drawn at 1024 px and scaled down, which keeps the 16 px tray size
clean.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw

IconState = Literal["ready", "loading", "off", "error"]

#: Ring around the status dot so it stands out on any taskbar colour.
ICON_BG = (24, 27, 34, 255)
GRADIENT_TOP = (96, 178, 255)
GRADIENT_BOTTOM = (37, 99, 235)
BUBBLE = (255, 255, 255, 255)
SPARKLE = (37, 99, 235, 255)

DOT_COLOURS: dict[str, tuple[int, int, int, int]] = {
    "ready": (46, 204, 113, 255),  # green
    "loading": (255, 179, 0, 255),  # amber
    "off": (128, 134, 144, 255),  # grey
    "error": (231, 76, 60, 255),  # red
}

STATES: tuple[IconState, ...] = ("ready", "loading", "off", "error")

#: Sizes in the ``.ico``: small (title bar, tray) to large (Alt+Tab, high-DPI taskbar).
ICO_SIZES: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)
_MASTER = 1024


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


def _sparkle(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill: tuple) -> None:
    """A four-pointed star: long points on the axes, a narrow waist on the diagonals."""
    waist = r * 0.30
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


@lru_cache(maxsize=1)
def _master() -> Image.Image:
    s = _MASTER
    pad = round(s * 0.03)
    # Vertical gradient clipped to a rounded square.
    column = Image.new("RGB", (1, s))
    for y in range(s):
        t = y / (s - 1)
        column.putpixel(
            (0, y),
            tuple(
                round(a + (b - a) * t) for a, b in zip(GRADIENT_TOP, GRADIENT_BOTTOM, strict=True)
            ),
        )
    fill = column.resize((s, s)).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [pad, pad, s - pad - 1, s - pad - 1], radius=round(s * 0.23), fill=255
    )
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(fill, (0, 0), mask)

    draw = ImageDraw.Draw(img)
    # Speech bubble with a tail at the lower left.
    draw.rounded_rectangle(
        [s * 0.17, s * 0.19, s * 0.83, s * 0.69], radius=round(s * 0.15), fill=BUBBLE
    )
    draw.polygon([(s * 0.28, s * 0.62), (s * 0.24, s * 0.84), (s * 0.48, s * 0.68)], fill=BUBBLE)
    # The "AI" sparkle inside the bubble, with a small companion.
    _sparkle(draw, s * 0.47, s * 0.445, s * 0.17, SPARKLE)
    _sparkle(draw, s * 0.665, s * 0.315, s * 0.065, SPARKLE)
    return img


@lru_cache(maxsize=32)
def make_app_icon(size: int = 256) -> Image.Image:
    """The app icon at ``size`` px (no status dot)."""
    img = _master().resize((size, size), Image.Resampling.LANCZOS)
    # Lanczos leaves a faint haze (alpha 1-7) outside the rounded corners at small sizes;
    # clear it so the corners are truly transparent.
    img.putalpha(img.getchannel("A").point(lambda a: 0 if a < 8 else a))
    return img


def write_app_ico(path: Path | str) -> Path:
    """Write the multi-size app icon to ``path`` (skipped when it is already there)."""
    target = Path(path)
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    make_app_icon(256).save(tmp, format="ICO", sizes=[(n, n) for n in ICO_SIZES])
    tmp.replace(target)
    return target


def _dot_geometry(size: int) -> tuple[float, float, float]:
    pad = max(1, size // 16)
    r = size * 0.185
    c = size - pad - r * 0.95
    return c, c, r


def make_icon_image(state: IconState | str = "off", size: int = 64) -> Image.Image:
    """The tray icon: the app icon plus the status dot in the lower right corner.

    The dot is the only part that carries information, so it is drawn large, at the final
    size (crisp edges), where it survives being scaled down to 16 px.
    """
    if state not in DOT_COLOURS:
        state = "off"
    img = make_app_icon(size).copy()
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
