"""ChatForge's theme settings (PLAN 1.9).

``web/static/ui-theme/`` is the ThemeForge drop-in bundle
(https://github.com/LaserLloyd/ThemeForge), copied verbatim: the same files in every app,
never edited here. A theme update is ``python src/chatforge/web/static/ui-theme/update.py``
(``--check`` only reports; ``--ref vX.Y.Z`` pins a release), which replaces the files the
bundle owns, verifies each one against the release's ``files.json`` and refuses a copy that
was edited by hand. No code here changes for a new bundle.

What is ChatForge's own is :data:`THEME_SETTINGS`. The static server writes it onto the
``ui-theme.js`` tag of each page as it sends it (:func:`apply_to_page`), listing every core
theme in the bundle's registry after ChatForge's opt-in ones, so a new core theme is in the
picker on the next page load; an opt-in theme appears once it is added here. The native
windows open in the theme's own colour (:func:`window_options`) so nothing flashes before
the page paints.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: The drop-in bundle: ui-theme.js, ui-theme.css, ui-theme-base.css, themes.json, ...
BUNDLE_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "ui-theme"

THEME_SETTINGS: dict[str, Any] = {
    #: Opt-in themes ChatForge offers, first in the picker; the core themes follow.
    "opt_in": ("laserlloyd", "laserlloyd-light"),
    "default": "laserlloyd",
    "storage_key": "chatforge.theme",
    #: The dark/light switch within a family (LaserLloyd and LaserLloyd Light).
    "families": True,
}

_TAG_RE = re.compile(rb'<script src="/static/ui-theme/ui-theme\.js"[^>]*>')
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def registry(bundle_dir: Path = BUNDLE_DIR) -> list[dict[str, Any]]:
    """The bundle's theme registry (``themes.json``), read each time so a new copy counts
    at once; empty when it cannot be read."""
    try:
        data = json.loads((bundle_dir / "themes.json").read_text(encoding="utf-8"))
        themes = data["themes"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _log.warning("theme registry unreadable: %s", type(exc).__name__)
        return []
    return [t for t in themes if isinstance(t, dict) and isinstance(t.get("slug"), str)]


def picker_themes(themes: list[dict[str, Any]] | None = None) -> list[str]:
    """The picker's themes, in order: ChatForge's opt-in ones, then every core theme."""
    themes = registry() if themes is None else themes
    known = {t["slug"] for t in themes}
    out = [s for s in THEME_SETTINGS["opt_in"] if s in known]
    out += [t["slug"] for t in themes if t.get("set") == "core" and t["slug"] not in out]
    return out or list(THEME_SETTINGS["opt_in"])


def tag_attributes(themes: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """:data:`THEME_SETTINGS` as the runtime's ``data-*`` settings."""
    return {
        "data-themes": ",".join(picker_themes(themes)),
        "data-default": THEME_SETTINGS["default"],
        "data-storage-key": THEME_SETTINGS["storage_key"],
        "data-families": "true" if THEME_SETTINGS["families"] else "false",
    }


def apply_to_page(page: bytes, themes: list[dict[str, Any]] | None = None) -> bytes:
    """``page`` with its ``ui-theme.js`` tag carrying :data:`THEME_SETTINGS` in place of
    the attributes written in the file (those only serve a page opened some other way).
    A page without the tag comes back unchanged."""
    attrs = "".join(f' {k}="{html.escape(v)}"' for k, v in tag_attributes(themes).items())
    tag = b'<script src="/static/ui-theme/ui-theme.js"' + attrs.encode("utf-8") + b">"
    return _TAG_RE.sub(lambda _m: tag, page, count=1)


def window_options(slug: str | None, themes: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """``create_window`` options for a native window showing ``slug``: the colour it has
    before its page paints, the theme's ``themeColor`` (the default theme's for a slug the
    bundle does not have). Empty, so pywebview's own default applies, when the registry
    cannot be read: colours live in the bundle only."""
    themes = registry() if themes is None else themes
    by_slug = {t["slug"]: t for t in themes}
    for candidate in (slug, THEME_SETTINGS["default"]):
        color = str((by_slug.get(candidate) or {}).get("themeColor") or "")
        if _COLOR_RE.match(color):
            return {"background_color": color}
    return {}
