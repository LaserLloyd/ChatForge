"""Markdown -> ``.docx``: minimal WordprocessingML with the standard library.

Headings, paragraphs, bullet and numbered lists (three levels; each numbered list restarts),
tables (header row bold and repeated on each page), quotes, code, rules, bold, italic and
code spans; links are written as "text (url)".
"""

from __future__ import annotations

from chatforge.tools import doc_markdown as md
from chatforge.tools.doc_ooxml import (
    CT_OOXML,
    OFFICE_DOC,
    REL_NS,
    XML_HEAD,
    content_types,
    package,
    rels,
    text,
)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_CONTENT_TYPES = content_types(
    {
        "/word/document.xml": f"{CT_OOXML}wordprocessingml.document.main+xml",
        "/word/styles.xml": f"{CT_OOXML}wordprocessingml.styles+xml",
        "/word/numbering.xml": f"{CT_OOXML}wordprocessingml.numbering+xml",
    }
)
_PACKAGE_RELS = rels([("rId1", OFFICE_DOC, "word/document.xml")])
_DOCUMENT_RELS = rels(
    [("rId1", f"{REL_NS}/styles", "styles.xml"), ("rId2", f"{REL_NS}/numbering", "numbering.xml")]
)


def _heading_style(sid: str, name: str, level: int, size: int) -> str:
    return (
        f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="320" w:after="120"/>'
        f'<w:outlineLvl w:val="{level}"/></w:pPr>'
        f'<w:rPr><w:b/><w:bCs/><w:color w:val="1F3864"/><w:sz w:val="{size}"/>'
        f'<w:szCs w:val="{size}"/></w:rPr></w:style>'
    )


_BORDER = 'w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"'
_STYLES = (
    XML_HEAD + f'<w:styles xmlns:w="{_W_NS}">'
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US"/>'
    "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
    '<w:spacing w:after="160" w:line="259" w:lineRule="auto"/>'
    "</w:pPr></w:pPrDefault></w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    + _heading_style("Heading1", "heading 1", 0, 32)
    + _heading_style("Heading2", "heading 2", 1, 28)
    + _heading_style("Heading3", "heading 3", 2, 24)
    + '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="60"/>'
    '<w:ind w:left="720"/><w:contextualSpacing/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:ind w:left="720"/></w:pPr>'
    '<w:rPr><w:i/><w:iCs/><w:color w:val="404040"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>'
    '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
    '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
    '<w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>'
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>'
    "<w:tblPr><w:tblBorders>"
    f"<w:top {_BORDER}/><w:left {_BORDER}/><w:bottom {_BORDER}/><w:right {_BORDER}/>"
    f"<w:insideH {_BORDER}/><w:insideV {_BORDER}/>"
    '</w:tblBorders><w:tblCellMar><w:left w:w="108" w:type="dxa"/>'
    '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
    "</w:styles>"
)

_BULLETS = ("•", "◦", "▪")
_NUMBER_FORMATS = (("decimal", "%1."), ("lowerLetter", "%2."), ("lowerRoman", "%3."))
_BULLET_NUM_ID = 1
_BREAK = "<w:r><w:br/></w:r>"


def _numbering(starts: list[int]) -> str:
    """``numbering.xml``: numId 1 is the bullet list; each numbered list gets its own
    numId (2, 3, ...) so its numbering restarts at its first number."""

    def level(i: int, fmt: str, lvl_text: str) -> str:
        indent = 720 * (i + 1)
        return (
            f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
            f'<w:lvlText w:val="{lvl_text}"/><w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{indent}" w:hanging="360"/></w:pPr></w:lvl>'
        )

    bullets = "".join(level(i, "bullet", _BULLETS[i]) for i in range(md.LIST_LEVELS))
    numbers = "".join(level(i, *_NUMBER_FORMATS[i]) for i in range(md.LIST_LEVELS))
    nums = f'<w:num w:numId="{_BULLET_NUM_ID}"><w:abstractNumId w:val="0"/></w:num>'
    for k, start in enumerate(starts):
        nums += (
            f'<w:num w:numId="{k + 2}"><w:abstractNumId w:val="1"/>'
            f'<w:lvlOverride w:ilvl="0"><w:startOverride w:val="{max(0, start)}"/>'
            "</w:lvlOverride></w:num>"
        )
    return (
        XML_HEAD + f'<w:numbering xmlns:w="{_W_NS}">'
        '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>'
        f"{bullets}</w:abstractNum>"
        '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>'
        f"{numbers}</w:abstractNum>{nums}</w:numbering>"
    )


def _run(s: str, *, bold: bool = False, italic: bool = False, code: bool = False) -> str:
    if not s:
        return ""
    props = ""
    if code:
        props += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
    if bold:
        props += "<w:b/><w:bCs/>"
    if italic:
        props += "<w:i/><w:iCs/>"
    pieces = "<w:tab/>".join(
        f'<w:t xml:space="preserve">{text(part)}</w:t>' if part else "" for part in s.split("\t")
    )
    return f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}{pieces}</w:r>"


def _inline(line: str, *, bold: bool = False) -> str:
    """Runs for one line of Markdown text (links written as "text (url)")."""
    out: list[str] = []
    for span in md.spans(line, bold=bold):
        if span.url:
            out.append(_run(md.link_text(span), bold=span.bold))
        else:
            out.append(_run(span.text, bold=span.bold, italic=span.italic, code=span.code))
    return "".join(out)


def _paragraph(runs: str, style: str | None = None, num: tuple[int, int] | None = None) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if num is not None:
        num_id, level = num
        props += f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>'
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{runs}</w:p>"


def _table(rows: list[list[str]]) -> str:
    cols = max(len(r) for r in rows)
    width = 9360 // cols  # the text width of a Letter page with 1" margins, in twips
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(cols))
    out = [
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
        f'<w:tblLook w:val="04A0"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
    ]
    for r, row in enumerate(rows):
        header = r == 0
        out.append("<w:tr><w:trPr><w:tblHeader/></w:trPr>" if header else "<w:tr>")
        for c in range(cols):
            cell = row[c] if c < len(row) else ""
            runs = _BREAK.join(_inline(part, bold=header) for part in cell.split("\n"))
            out.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
                f"{_paragraph(runs)}</w:tc>"
            )
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


_RULE_PARAGRAPH = (
    '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
    'w:color="A6A6A6"/></w:pBdr></w:pPr></w:p>'
)


def _body(blocks: list[md.Block]) -> str:
    out: list[str] = []
    for b in blocks:
        if b.kind == "heading":
            out.append(_paragraph(_inline(b.text), f"Heading{min(b.level, 3)}"))
        elif b.kind == "para":
            out.append(_paragraph(_BREAK.join(_inline(p) for p in b.lines)))
        elif b.kind == "bullet":
            out.append(_paragraph(_inline(b.text), "ListParagraph", (_BULLET_NUM_ID, b.level)))
        elif b.kind == "number":
            out.append(_paragraph(_inline(b.text), "ListParagraph", (b.list_id + 2, b.level)))
        elif b.kind == "table":
            out.append(_table(b.rows))
        elif b.kind == "code":
            out.extend(_paragraph(_run(c), "Code") for c in b.lines or [""])
        elif b.kind == "quote":
            out.append(_paragraph(_inline(b.text), "Quote"))
        elif b.kind == "rule":
            out.append(_RULE_PARAGRAPH)
    if not out or out[-1].startswith("<w:tbl>"):
        out.append("<w:p/>")  # Word wants a paragraph after a table that ends the body
    return "".join(out)


def markdown_to_docx(markdown: str) -> bytes:
    """A Word document from Markdown-style text (see the module docstring)."""
    blocks = md.parse(markdown)
    document = (
        XML_HEAD + f'<w:document xmlns:w="{_W_NS}" xmlns:r="{REL_NS}"><w:body>{_body(blocks)}'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>'
        "</w:sectPr></w:body></w:document>"
    )
    return package(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": _PACKAGE_RELS,
            "word/_rels/document.xml.rels": _DOCUMENT_RELS,
            "word/document.xml": document,
            "word/styles.xml": _STYLES,
            "word/numbering.xml": _numbering(md.list_starts(blocks)),
        }
    )


__all__ = ["markdown_to_docx"]
