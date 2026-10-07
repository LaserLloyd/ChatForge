"""Theme: the ThemeForge bundle stays a verbatim drop-in, and ChatForge's own settings
(desktop/theme.py) reach every page and window without editing it."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path

import pytest

from chatforge.desktop import theme
from chatforge.desktop.webserver import WEB_ROOT, StaticServer

CORE = ["purple", "midnight-gold", "glacier", "forest", "paper", "daylight"]
TAG_RE = re.compile(r'<script src="/static/ui-theme/ui-theme\.js"([^>]*)>')


def _attrs(page: str) -> dict[str, str]:
    match = TAG_RE.search(page)
    assert match, "no ui-theme.js tag"
    return dict(re.findall(r'(data-[a-z-]+)="([^"]*)"', match.group(1)))


def test_the_bundle_is_the_unedited_themeforge_copy() -> None:
    # files.json travels inside the bundle and lists the SHA-256 of every file the bundle
    # owns (hashed over LF line endings, as update.py does; .gitattributes keeps the files
    # LF on Windows too). A mismatch means the copy was edited: move the change into
    # app.css or settings.css and run `python ui-theme/update.py` instead.
    bundle = theme.BUNDLE_DIR
    manifest = json.loads((bundle / "files.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "ThemeForge"
    version = (bundle / "VERSION").read_text(encoding="utf-8").split()
    assert version[:2] == ["ThemeForge", manifest["version"]]
    assert version[-1] == manifest["hash"]
    edited = []
    for rel, expected in manifest["files"].items():
        if rel == "files.json":
            continue
        data = (bundle / rel).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != expected:
            edited.append(rel)
    assert not edited, f"web/static/ui-theme/ differs from the ThemeForge bundle: {edited}"
    # Every file the page heads load is one the bundle owns.
    for page in ("index.html", "settings.html"):
        html = (WEB_ROOT / page).read_text(encoding="utf-8")
        for rel in re.findall(r"/static/ui-theme/([\w./-]+)", html):
            assert rel in manifest["files"], f"{page} loads {rel}, not in the bundle"


def test_the_picker_offers_the_opt_ins_then_every_core_theme() -> None:
    assert theme.picker_themes() == ["laserlloyd", "laserlloyd-light", *CORE]


def test_a_new_core_theme_needs_no_code_change_but_an_opt_in_one_does() -> None:
    reg = [*theme.registry(), {"slug": "aurora", "set": "core"}, {"slug": "neon", "set": "opt-in"}]
    themes = theme.picker_themes(reg)
    assert "aurora" in themes and "neon" not in themes
    # An opt-in theme the bundle dropped is not offered.
    assert theme.picker_themes([{"slug": "purple", "set": "core"}]) == ["purple"]


def test_an_unreadable_registry_still_offers_the_opt_ins(tmp_path: Path) -> None:
    assert theme.registry(tmp_path) == []
    (tmp_path / "themes.json").write_text("{not json", encoding="utf-8")
    assert theme.registry(tmp_path) == []
    assert theme.picker_themes([]) == ["laserlloyd", "laserlloyd-light"]


@pytest.mark.parametrize("page", ["index.html", "settings.html"])
def test_each_page_gets_the_settings_and_its_own_tag_agrees(page: str) -> None:
    raw = (WEB_ROOT / page).read_bytes()
    served = _attrs(theme.apply_to_page(raw).decode("utf-8"))
    assert served == {
        "data-themes": ",".join(["laserlloyd", "laserlloyd-light", *CORE]),
        "data-default": "laserlloyd",
        "data-storage-key": "chatforge.theme",
        "data-families": "true",
    }
    # What the file says itself (a page opened another way) does not contradict it.
    static = _attrs(raw.decode("utf-8"))
    for key in ("data-default", "data-storage-key", "data-families"):
        assert static[key] == served[key], key
    assert static["data-themes"].split(",")[:2] == ["laserlloyd", "laserlloyd-light"]
    # Only the tag changes.
    assert TAG_RE.sub("", theme.apply_to_page(raw).decode()) == TAG_RE.sub("", raw.decode())


def test_a_page_without_the_tag_is_unchanged() -> None:
    page = b"<!doctype html><script src='/static/js/x.js'></script>"
    assert theme.apply_to_page(page) == page


def test_the_server_sends_pages_with_the_settings() -> None:
    with (
        StaticServer(WEB_ROOT) as srv,
        urllib.request.urlopen(srv.url("settings.html"), timeout=5) as resp,  # noqa: S310
    ):
        body = resp.read()
        assert int(resp.headers["Content-Length"]) == len(body)
    assert _attrs(body.decode("utf-8"))["data-themes"].endswith(",".join(CORE))


def test_head_gets_the_length_of_the_page_as_sent() -> None:
    with StaticServer(WEB_ROOT) as srv:
        with urllib.request.urlopen(srv.url("index.html"), timeout=5) as resp:  # noqa: S310
            body = resp.read()
        request = urllib.request.Request(srv.url("index.html"), method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as resp:  # noqa: S310
            assert int(resp.headers["Content-Length"]) == len(body)
            assert resp.read() == b""


def test_windows_open_in_the_theme_colour() -> None:
    reg = theme.registry()
    colors = {t["slug"]: t["themeColor"] for t in reg}
    assert theme.window_options("laserlloyd-light") == {
        "background_color": colors["laserlloyd-light"]
    }
    assert theme.window_options("daylight")["background_color"] == colors["daylight"]
    # An unknown theme opens like the default one; no registry, pywebview's own default.
    assert theme.window_options("gone") == {"background_color": colors["laserlloyd"]}
    assert theme.window_options(None) == {"background_color": colors["laserlloyd"]}
    assert theme.window_options("laserlloyd", []) == {}
    bad = [{"slug": "laserlloyd", "themeColor": "red; x"}]
    assert theme.window_options("laserlloyd", bad) == {}
