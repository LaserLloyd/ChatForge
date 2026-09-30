"""Tray icon image: a dark rounded square, an ``AI`` glyph and a four-state status dot.

# Adapted from StudioForge src/studioforge/tray/tray_app.py `_load_font`, `make_icon_image` (MIT, LaserLloyd)

States (PLAN WS7 step 6): ``ready`` green, ``loading`` amber (starting or compiling),
``off`` grey (unloaded, or a cloud provider), ``error`` red.
"""

from __future__ import annotations

from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFont

IconState = Literal["ready", "loading", "off", "error"]

ICON_BG = (24, 27, 34, 255)
ICON_EDGE = (78, 168, 255, 255)
GLYPH = (232, 236, 244, 255)
ACCENT = (78, 168, 255, 255)

DOT_COLOURS: dict[str, tuple[int, int, int, int]] = {
    "ready": (46, 204, 113, 255),  # green
    "loading": (255, 179, 0, 255),  # amber
    "off": (128, 134, 144, 255),  # grey
    "error": (231, 76, 60, 255),  # red
}

STATES: tuple[IconState, ...] = ("ready", "loading", "off", "error")


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


# Adapted from StudioForge src/studioforge/tray/tray_app.py `_load_font` (MIT, LaserLloyd)
def _load_font(size: int) -> Any:
    for name in ("arialbd.ttf", "seguisb.ttf", "segoeuib.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


# Adapted from StudioForge src/studioforge/tray/tray_app.py `make_icon_image` (MIT, LaserLloyd)
def make_icon_image(state: IconState | str = "off", size: int = 64) -> Image.Image:
    """Draw the tray icon.

    Generated with PIL rather than shipped as a binary ``.ico`` so the package stays pure
    Python and the icon can change with the state. The dot is the only part that carries
    information, so it is drawn large and in the corner, where it survives being scaled
    down to 16 px.
    """
    if state not in DOT_COLOURS:
        state = "off"
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = max(1, size // 16)
    radius = max(3, size // 5)
    draw.rounded_rectangle(
        [pad, pad, size - pad - 1, size - pad - 1],
        radius=radius,
        fill=ICON_BG,
        outline=ICON_EDGE,
        width=max(1, size // 24),
    )

    # Accent bar under the glyph, kept clear of the status dot's corner.
    bar_h = max(1, size // 12)
    draw.rounded_rectangle(
        [size * 0.22, size * 0.70, size * 0.60, size * 0.70 + bar_h],
        radius=bar_h // 2,
        fill=ACCENT,
    )

    font = _load_font(max(6, int(size * 0.46)))
    try:
        draw.text((size * 0.47, size * 0.44), "AI", font=font, fill=GLYPH, anchor="mm")
    except (ValueError, TypeError):  # bitmap fallback font: no anchor support
        draw.text((size * 0.22, size * 0.26), "AI", font=font, fill=GLYPH)

    r = size * 0.185
    cx = size - pad - r * 0.95
    cy = size - pad - r * 0.95
    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r],
        fill=DOT_COLOURS[state],
        outline=ICON_BG,
        width=max(1, size // 32),
    )
    return img


def dot_colour(img: Image.Image) -> tuple[int, int, int, int]:
    """The pixel at the status dot's centre (tests)."""
    size = img.size[0]
    pad = max(1, size // 16)
    r = size * 0.185
    c = int(size - pad - r * 0.95)
    return tuple(img.getpixel((c, c)))  # type: ignore[return-value]
