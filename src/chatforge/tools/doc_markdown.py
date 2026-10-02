"""Markdown-style text as a flat list of blocks, shared by the ``create_document`` writers
(Word, Excel, PowerPoint, HTML and CSV), plus the tables found in it.

Only what models write in practice: ``#`` headings, paragraphs, ``-``/``*``/``+`` bullets
and ``1.`` numbered items (nested by indentation, three levels), ``|`` tables, fenced code,
``>`` quotes and ``---`` rules; inline ``**bold**``, ``*italic*``, `` `code` `` and
``[text](url)``. Standard library only.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

LIST_LEVELS = 3

#: Characters XML 1.0 cannot hold (and lone surrogates, which UTF-8 cannot encode).
INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
_INLINE = re.compile(
    r"(\*\*\*[^*\n]+?\*\*\*"  # ***bold italic***
    r"|\*\*[^\n]+?\*\*"  # **bold**
    r"|__[^_\n]+?__"  # __bold__
    r"|`[^`\n]+`"  # `code`
    r"|\*[^*\s](?:[^*\n]*?[^*\s])?\*"  # *italic*
    r"|(?<!\w)_[^_\s](?:[^_\n]*?[^_\s])?_(?!\w)"  # _italic_
    r"|\[[^\]\n]{1,500}\]\([^)\s]{1,2000}\))"  # [text](url); bounded: '[' * 40k stays fast
)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$|^\s*\|\s*:?-+:?\s*\|\s*$")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_FENCED = re.compile(r"^\s*(```|~~~)([^\n]*)\n(.*?)\n?\s*\1\s*$", re.DOTALL)
#: Fence languages of a whole document wrapped in a code block by mistake.
MARKDOWN_FENCES = frozenset({"", "markdown", "md"})
#: Fence languages whose block is read as a table (``csv``, ``tsv``) or may be one.
_TABLE_LANGS = frozenset({"csv", "tsv"})
_MAYBE_TABLE_LANGS = frozenset({"", "text", "txt", "plaintext"})


@dataclass
class Block:
    """One block of the document.

    ``kind`` is ``heading`` (``text``, ``level`` 1-6), ``para`` (``lines``), ``bullet`` /
    ``number`` (``text``, ``level`` 0-2; a number also has ``list_id``, which changes when a
    new list starts, and ``start``, the first number of its list), ``table`` (``rows`` of
    cells), ``code`` (``lines``, ``lang``), ``quote`` (``text``) or ``rule``.
    """

    kind: str
    text: str = ""
    level: int = 0
    lines: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    lang: str = ""
    list_id: int = -1
    start: int = 1


@dataclass(frozen=True)
class Span:
    """A run of inline text; ``url`` makes it a link whose label is ``text``."""

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    url: str = ""


@dataclass
class Table:
    """A table found in the content: ``rows`` of cell text (the first row is the header)
    and the heading above it (``title``). ``source`` is ``markdown`` (a ``|`` table; cells
    hold inline Markdown), ``fence`` (a ```` ```csv ```` block) or ``guess`` (a paragraph
    or plain code block that reads as CSV)."""

    rows: list[list[str]]
    title: str = ""
    source: str = "markdown"

    @property
    def markdown(self) -> bool:
        return self.source == "markdown"


def clean_xml_text(text: str) -> str:
    """``text`` without the characters XML cannot hold."""
    return INVALID_XML.sub("", text)


def unfence(text: str, langs: frozenset[str] | None = None) -> str:
    """The inside of ``text`` when all of it is one fenced code block (whose language is
    in ``langs``, when given), else ``text``."""
    m = _FENCED.match(text)
    if m is None:
        return text
    info = m.group(2).split()
    if langs is not None and (info[0].lower() if info else "") not in langs:
        return text
    return m.group(3)


# --------------------------------------------------------------------------- #
# Inline
# --------------------------------------------------------------------------- #


def spans(text: str, *, bold: bool = False) -> list[Span]:
    """The inline runs of one line: ``**bold**``, ``*italic*``, `` `code` `` and links.
    ``bold`` makes every run bold (a table header)."""
    out: list[Span] = []
    for i, token in enumerate(_INLINE.split(text)):
        if not token:
            continue
        if i % 2 == 0:
            out.append(Span(token, bold=bold))
        elif token.startswith("***"):
            out.append(Span(token[3:-3], bold=True, italic=True))
        elif token.startswith(("**", "__")):
            out.append(Span(token[2:-2], bold=True))
        elif token.startswith("`"):
            out.append(Span(token[1:-1], bold=bold, code=True))
        elif token.startswith("["):
            m = _LINK.fullmatch(token)
            if m:
                out.append(Span(m.group(1), bold=bold, url=m.group(2)))
            else:  # pragma: no cover - the pattern only splits out whole links
                out.append(Span(token, bold=bold))
        else:
            out.append(Span(token[1:-1], bold=bold, italic=True))
    return out


def link_text(span: Span) -> str:
    """A link written out as "label (url)" (just the label when it is the URL)."""
    return f"{span.text} ({span.url})" if span.url and span.url != span.text else span.text


def plain(text: str) -> str:
    """``text`` without inline Markdown (links as "label (url)")."""
    return "".join(link_text(s) for s in spans(text))


def is_bold_cell(text: str) -> bool:
    """A cell whose whole text is ``**bold**`` (a total row, say)."""
    parts = spans(text.strip())
    return bool(parts) and all(p.bold for p in parts)


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #


def split_cells(line: str) -> list[str]:
    """The cells of one ``| a | b |`` table row (``\\|`` is a literal bar; ``<br>`` is a
    line break inside the cell)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [_BR.sub("\n", c.strip().replace("\\|", "|")) for c in re.split(r"(?<!\\)\|", s)]


def parse(markdown: str) -> list[Block]:
    """``markdown`` as blocks, in order."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[Block] = []
    para: list[str] = []
    lists = 0  # numbered lists started so far
    current: int | None = None  # list_id of the numbered list in progress

    def flush() -> None:
        if para:
            out.append(Block("para", lines=list(para)))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            current = None
            fence = stripped[:3]
            info = stripped[3:].strip().strip("`~").split()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(Block("code", lines=code, lang=info[0].lower() if info else ""))
            continue
        if not stripped:
            flush()
            i += 1
            continue
        if (m := _HEADING.match(line)) is not None:
            flush()
            current = None
            out.append(Block("heading", text=m.group(2), level=len(m.group(1))))
        elif _RULE.match(line):
            flush()
            current = None
            out.append(Block("rule"))
        elif "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            current = None
            rows = [split_cells(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(split_cells(lines[i]))
                i += 1
            out.append(Block("table", rows=rows))
            continue
        elif (m := _BULLET.match(line)) is not None:
            flush()
            level = min(len(m.group(1).expandtabs(4)) // 2, LIST_LEVELS - 1)
            if level == 0:
                current = None
            out.append(Block("bullet", text=m.group(2), level=level))
        elif (m := _NUMBERED.match(line)) is not None:
            flush()
            level = min(len(m.group(1).expandtabs(4)) // 2, LIST_LEVELS - 1)
            start = 1
            if current is None:
                start = int(m.group(2)) if level == 0 else 1
                current = lists
                lists += 1
            out.append(Block("number", text=m.group(3), level=level, list_id=current, start=start))
        elif (m := _QUOTE.match(line)) is not None:
            flush()
            current = None
            out.append(Block("quote", text=m.group(1)))
        else:
            if not line[:1].isspace():
                current = None
            para.append(stripped)
        i += 1
    flush()
    return out


def list_starts(blocks: list[Block]) -> list[int]:
    """The first number of each numbered list, by ``list_id``."""
    starts: list[int] = []
    for b in blocks:
        if b.kind == "number" and b.list_id == len(starts):
            starts.append(b.start)
    return starts


# --------------------------------------------------------------------------- #
# Tables (for Excel and CSV)
# --------------------------------------------------------------------------- #


def _csv_rows(lines: list[str], *, sure: bool, delimiter: str | None = None) -> list[list[str]]:
    """``lines`` read as CSV, or ``[]`` when they do not look like it.

    Tab, comma and semicolon are tried in that order; the first that splits the first line
    into two or more cells wins. Unless ``sure`` (a ```` ```csv ```` block), it takes at least
    two rows that all have the same number of cells, so prose with commas is not a table.
    """
    data = "\n".join(lines).strip("\n")
    if not data.strip():
        return []

    def read(d: str) -> list[list[str]]:
        try:
            reader = csv.reader(io.StringIO(data), delimiter=d, skipinitialspace=True)
            return [r for r in reader if any(c.strip() for c in r)]
        except csv.Error:
            return []

    for d in (delimiter,) if delimiter else ("\t", ",", ";"):
        rows = read(d)
        if not rows or len(rows[0]) < 2:
            continue
        if sure or (len(rows) >= 2 and all(len(r) == len(rows[0]) for r in rows)):
            return rows
    return read(delimiter or ",") if sure else []


def find_tables(blocks: list[Block]) -> list[Table]:
    """The tables in ``blocks``, each with the heading above it: Markdown tables and
    ```` ```csv ```` blocks; when there is neither, also paragraphs and plain code blocks that
    read as CSV (so prose next to real tables is never taken for one)."""
    found: list[Table] = []
    title = ""
    for b in blocks:
        if b.kind == "heading":
            title = plain(b.text).strip()
        elif b.kind == "table":
            found.append(Table(b.rows, title, "markdown"))
        elif b.kind == "code" and b.lang in _TABLE_LANGS:
            rows = _csv_rows(b.lines, sure=True, delimiter="\t" if b.lang == "tsv" else None)
            if rows:
                found.append(Table(rows, title, "fence"))
        elif (b.kind == "code" and b.lang in _MAYBE_TABLE_LANGS) or b.kind == "para":
            rows = _csv_rows(b.lines, sure=False)
            if rows:
                found.append(Table(rows, title, "guess"))
    if any(t.source != "guess" for t in found):
        found = [t for t in found if t.source != "guess"]
    return found


def table_text(table: Table) -> list[list[str]]:
    """The rows of ``table`` as plain text (inline Markdown removed), padded to the same
    number of cells."""
    width = max((len(r) for r in table.rows), default=0)
    rows = [[(plain(c) if table.markdown else c).strip() for c in r] for r in table.rows]
    return [r + [""] * (width - len(r)) for r in rows]


__all__ = [
    "LIST_LEVELS",
    "MARKDOWN_FENCES",
    "Block",
    "Span",
    "Table",
    "clean_xml_text",
    "find_tables",
    "is_bold_cell",
    "link_text",
    "list_starts",
    "parse",
    "plain",
    "spans",
    "split_cells",
    "table_text",
    "unfence",
]
