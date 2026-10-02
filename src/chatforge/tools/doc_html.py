"""Markdown -> a standalone ``.html`` page (one file, inline CSS, no scripts).

Headings, paragraphs, nested bullet and numbered lists, tables, code, quotes, rules, bold,
italic, code spans and links (http, https and mailto only; anything else stays text).
"""

from __future__ import annotations

import html
import re

from chatforge.tools import doc_markdown as md

_SAFE_URL = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)
_STYLE = """
:root { color-scheme: light dark; --fg: #1f2328; --muted: #59636e; --line: #d1d9e0;
  --soft: #f6f8fa; --accent: #1f3864; --link: #0969da; }
@media (prefers-color-scheme: dark) { :root { --fg: #e6edf3; --muted: #9198a1;
  --line: #3d444d; --soft: #151b23; --accent: #9ab8e6; --link: #4493f8; } }
body { margin: 0; background: Canvas; color: var(--fg);
  font: 16px/1.6 "Segoe UI", system-ui, -apple-system, sans-serif; }
main { max-width: 52rem; margin: 0 auto; padding: 2rem 1rem 4rem; }
h1, h2, h3, h4, h5, h6 { color: var(--accent); line-height: 1.25; margin: 1.6em 0 .5em; }
h1 { font-size: 2rem; margin-top: 0; } h2 { font-size: 1.5rem; } h3 { font-size: 1.25rem; }
a { color: var(--link); }
table { border-collapse: collapse; margin: 1em 0; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: .4em .75em; text-align: left; vertical-align: top; }
th { background: var(--soft); }
tr:nth-child(even) td { background: color-mix(in srgb, var(--soft) 60%, transparent); }
code { font-family: Consolas, "Cascadia Mono", monospace; font-size: .9em;
  background: var(--soft); padding: .1em .3em; border-radius: 4px; }
pre { background: var(--soft); padding: 1em; overflow-x: auto; border-radius: 6px; }
pre code { padding: 0; background: none; }
blockquote { margin: 1em 0; padding: 0 1em; color: var(--muted); border-left: 4px solid var(--line); }
hr { border: 0; border-top: 1px solid var(--line); margin: 2em 0; }
"""


def _inline(line: str) -> str:
    out: list[str] = []
    for span in md.spans(line):
        if span.url:
            if _SAFE_URL.match(span.url):
                part = f'<a href="{html.escape(span.url)}">{html.escape(span.text)}</a>'
            else:
                part = html.escape(md.link_text(span))
        else:
            part = html.escape(span.text)
            if span.code:
                part = f"<code>{part}</code>"
        if span.italic:
            part = f"<em>{part}</em>"
        if span.bold:
            part = f"<strong>{part}</strong>"
        out.append(part)
    return "".join(out)


def _table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)

    def cells(row: list[str], tag: str) -> str:
        padded = row + [""] * (width - len(row))
        return "".join(
            f"<{tag}>{'<br>'.join(_inline(p) for p in c.split(chr(10)))}</{tag}>" for c in padded
        )

    body = "".join(f"<tr>{cells(r, 'td')}</tr>" for r in rows[1:])
    return (
        f"<table><thead><tr>{cells(rows[0], 'th')}</tr></thead>"
        f"{f'<tbody>{body}</tbody>' if body else ''}</table>"
    )


def _body(blocks: list[md.Block]) -> str:
    out: list[str] = []
    stack: list[tuple[str, int]] = []  # open lists: (tag, level)

    def close_lists(level: int = -1) -> None:
        while stack and stack[-1][1] > level:
            out.append(f"</li></{stack.pop()[0]}>")

    for b in blocks:
        if b.kind in ("bullet", "number"):
            tag = "ul" if b.kind == "bullet" else "ol"
            close_lists(b.level)
            if stack and stack[-1][1] == b.level and stack[-1][0] != tag:
                out.append(f"</li></{stack.pop()[0]}>")
            if stack and stack[-1][1] == b.level:
                out.append("</li>")
            else:
                start = f' start="{b.start}"' if tag == "ol" and b.start != 1 else ""
                out.append(f"<{tag}{start}>")
                stack.append((tag, b.level))
            out.append(f"<li>{_inline(b.text)}")
            continue
        close_lists()
        if b.kind == "heading":
            out.append(f"<h{b.level}>{_inline(b.text)}</h{b.level}>")
        elif b.kind == "para":
            out.append(f"<p>{'<br>'.join(_inline(line) for line in b.lines)}</p>")
        elif b.kind == "table":
            out.append(_table(b.rows))
        elif b.kind == "code":
            lang = f' class="language-{html.escape(b.lang)}"' if b.lang else ""
            out.append(f"<pre><code{lang}>{html.escape(chr(10).join(b.lines))}</code></pre>")
        elif b.kind == "quote":
            out.append(f"<blockquote><p>{_inline(b.text)}</p></blockquote>")
        elif b.kind == "rule":
            out.append("<hr>")
    close_lists()
    return "\n".join(out)


def looks_like_html(content: str) -> bool:
    """Content that is already HTML (it starts with a tag or a doctype)."""
    return content.lstrip()[:1] == "<"


def markdown_to_html(markdown: str, title: str = "") -> str:
    """A standalone HTML page from Markdown-style text. The ``<title>`` is the first
    heading, else ``title``."""
    blocks = md.parse(markdown)
    heading = next((md.plain(b.text) for b in blocks if b.kind == "heading"), "")
    name = html.escape((heading or title or "Document").strip())
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{name}</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n"
        f"{_body(blocks)}\n</main>\n</body>\n</html>\n"
    )


__all__ = ["looks_like_html", "markdown_to_html"]
