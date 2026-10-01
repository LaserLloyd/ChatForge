"""Minimal HTML -> plain text extraction (stdlib ``HTMLParser``)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP = frozenset({"script", "style", "noscript", "nav", "footer", "svg"})
_BLOCK = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "main",
        "header", "aside", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "hr",
        "form", "dl", "dt", "dd", "figure", "figcaption",
    }
)  # fmt: skip
_WS = re.compile(r"\s+")
_SPACES = re.compile(r"[ \t\r\f\v ]+")
_BLANKS = re.compile(r"\n\s*\n+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title: list[str] = []
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
        elif self._skip_depth == 0:
            if tag == "title":
                self._in_title = True
            elif tag in _BLOCK:
                self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing tags (``<br/>``, ``<svg/>``) never open a skipped region.
        if tag not in _SKIP and self._skip_depth == 0 and tag in _BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
        elif self._skip_depth == 0:
            if tag == "title":
                self._in_title = False
            elif tag in _BLOCK:
                self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        data = _WS.sub(" ", data)
        if self._in_title:
            self._title.append(data)
        else:
            self._parts.append(data)

    def result(self) -> tuple[str, str]:
        title = " ".join("".join(self._title).split())
        return title, collapse_text("".join(self._parts))


def collapse_text(text: str) -> str:
    """Collapse whitespace: runs of spaces become one, blank-line runs become one newline."""
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n", text).strip()


def extract(html: str) -> tuple[str, str]:
    """Return ``(title, text)`` for an HTML document; whitespace is collapsed."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.result()


def html_to_text(html: str) -> str:
    """Return only the visible text of ``html``."""
    return extract(html)[1]
