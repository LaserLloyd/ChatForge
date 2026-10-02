"""Markdown -> ``.pptx``: minimal PresentationML with the standard library.

The first ``#`` heading (when the text starts with it) makes a title slide, with the
paragraphs under it as the subtitle. Every other ``#``/``##`` heading, and every ``---``
rule, starts a slide; a ``###`` heading right after a rule titles that slide. Bullets
(three levels), numbered items, paragraphs, quotes and code go in the body; a Markdown
table becomes a real table. Text that would overflow is shrunk (PowerPoint's own "shrink
text on overflow"), and a slide too full even for that, or a long table, continues on the
next slide. 16:9, white, Calibri, one accent colour.
"""

from __future__ import annotations

import math
import re
import urllib.parse
from dataclasses import dataclass, field

from chatforge.tools import doc_markdown as md
from chatforge.tools.doc_ooxml import (
    CORE_PROPS,
    CT_CORE,
    CT_OOXML,
    OFFICE_DOC,
    REL_NS,
    XML_HEAD,
    content_types,
    core_properties,
    package,
    rels,
    text,
)
from chatforge.tools.doc_xlsx import cell_value

MAX_SLIDES = 200
TABLE_ROWS_PER_SLIDE = 12  # data rows; the header repeats on each slide
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS = f'xmlns:a="{_A}" xmlns:r="{REL_NS}" xmlns:p="{_P}"'
_SLIDE_W, _SLIDE_H = 12_192_000, 6_858_000  # 13.333 x 7.5 in (16:9)
_EMU_PER_IN = 914_400
_TITLE_BOX = (838_200, 365_125, 10_515_600, 1_325_563)
_BODY_BOX = (838_200, 1_825_625, 10_515_600, 4_351_338)
_CTR_TITLE_BOX = (1_524_000, 1_122_363, 9_144_000, 2_387_600)
_SUBTITLE_BOX = (1_524_000, 3_602_038, 9_144_000, 1_655_762)
_ACCENT = "2F5597"
_TITLE_COLOUR = "1F3864"
_LEVEL_SIZES = (24.0, 20.0, 18.0)  # body text points by list level
_CODE_SIZE = 16.0
_SCALES = (1.0, 0.9, 0.8, 0.7, 0.62, 0.55)  # "shrink text on overflow" steps
_SAFE_URL = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)


@dataclass
class Deck:
    data: bytes
    slides: int
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Theme, master and layouts
# --------------------------------------------------------------------------- #


def _solid(colour: str) -> str:
    return f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'


def _theme() -> str:
    colours = (
        ("dk1", "262626"), ("lt1", "FFFFFF"), ("dk2", _TITLE_COLOUR), ("lt2", "E7E6E6"),
        ("accent1", _ACCENT), ("accent2", "ED7D31"), ("accent3", "A5A5A5"),
        ("accent4", "FFC000"), ("accent5", "5B9BD5"), ("accent6", "70AD47"),
        ("hlink", "0563C1"), ("folHlink", "954F72"),
    )  # fmt: skip
    scheme = "".join(f'<a:{n}><a:srgbClr val="{c}"/></a:{n}>' for n, c in colours)
    fills = (
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"><a:tint val="50000"/></a:schemeClr></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"><a:shade val="80000"/></a:schemeClr></a:solidFill>'
    )
    lines = "".join(
        f'<a:ln w="{w}" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/>'
        '</a:solidFill><a:prstDash val="solid"/><a:miter lim="800000"/></a:ln>'
        for w in (6350, 12700, 19050)
    )
    effects = "<a:effectStyle><a:effectLst/></a:effectStyle>" * 3

    def font(face: str) -> str:
        return f'<a:latin typeface="{face}"/><a:ea typeface=""/><a:cs typeface=""/>'

    return (
        f'{XML_HEAD}<a:theme xmlns:a="{_A}" name="ChatForge"><a:themeElements>'
        f'<a:clrScheme name="ChatForge">{scheme}</a:clrScheme>'
        f'<a:fontScheme name="ChatForge"><a:majorFont>{font("Calibri Light")}</a:majorFont>'
        f"<a:minorFont>{font('Calibri')}</a:minorFont></a:fontScheme>"
        f'<a:fmtScheme name="ChatForge"><a:fillStyleLst>{fills}</a:fillStyleLst>'
        f"<a:lnStyleLst>{lines}</a:lnStyleLst><a:effectStyleLst>{effects}</a:effectStyleLst>"
        f"<a:bgFillStyleLst>{fills}</a:bgFillStyleLst></a:fmtScheme></a:themeElements>"
        "<a:objectDefaults/><a:extraClrSchemeLst/></a:theme>"
    )


_GROUP = (
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/>'
    '<a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
)
_FONT_REFS = '<a:latin typeface="+{k}-lt"/><a:ea typeface="+{k}-ea"/><a:cs typeface="+{k}-cs"/>'


def _xfrm(box: tuple[int, int, int, int]) -> str:
    x, y, cx, cy = box
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'


def _rect(sid: int, name: str, box: tuple[int, int, int, int], colour: str) -> str:
    """A plain filled rectangle (the accent bar)."""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/><p:cNvSpPr/><p:nvPr userDrawn="1"/>'
        f'</p:nvSpPr><p:spPr>{_xfrm(box)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f"{_solid(colour)}<a:ln><a:noFill/></a:ln></p:spPr>"
        '<p:txBody><a:bodyPr rtlCol="0" anchor="ctr"/><a:lstStyle/><a:p>'
        '<a:endParaRPr lang="en-US"/></a:p></p:txBody></p:sp>'
    )


def _placeholder(
    sid: int,
    name: str,
    ph: str,
    paragraphs: str,
    *,
    box: tuple[int, int, int, int] | None = None,
    body_pr: str = "<a:bodyPr/>",
    lst_style: str = "<a:lstStyle/>",
    geometry: bool = False,
) -> str:
    sp_pr = (_xfrm(box) if box else "") + (
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>' if geometry else ""
    )
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/>'
        f"</p:cNvSpPr><p:nvPr>{ph}</p:nvPr></p:nvSpPr>"
        f"{f'<p:spPr>{sp_pr}</p:spPr>' if sp_pr else '<p:spPr/>'}"
        f"<p:txBody>{body_pr}{lst_style}{paragraphs}</p:txBody></p:sp>"
    )


def _prompt(words: str) -> str:
    return f'<a:p><a:r><a:rPr lang="en-US"/><a:t>{words}</a:t></a:r></a:p>'


def _level(i: int, size: float, bullet: str) -> str:
    mar = 228_600 + i * 457_200
    before = 1000 if i == 0 else 500
    return (
        f'<a:lvl{i + 1}pPr marL="{mar}" indent="-228600" algn="l" defTabSz="914400" rtl="0" '
        'eaLnBrk="1" latinLnBrk="0" hangingPunct="1"><a:lnSpc><a:spcPct val="100000"/></a:lnSpc>'
        f'<a:spcBef><a:spcPts val="{before}"/></a:spcBef>'
        '<a:buFont typeface="Arial" panose="020B0604020202020204" pitchFamily="34" charset="0"/>'
        f'<a:buChar char="{bullet}"/><a:defRPr sz="{int(size * 100)}" kern="1200">'
        f'<a:solidFill><a:schemeClr val="tx1"/></a:solidFill>{_FONT_REFS.format(k="mn")}'
        f"</a:defRPr></a:lvl{i + 1}pPr>"
    )


def _master() -> str:
    title = _placeholder(
        2,
        "Title Placeholder 1",
        '<p:ph type="title"/>',
        _prompt("Click to edit Master title style"),
        box=_TITLE_BOX,
        body_pr='<a:bodyPr vert="horz" lIns="91440" tIns="45720" rIns="91440" bIns="45720" '
        'rtlCol="0" anchor="b"><a:normAutofit/></a:bodyPr>',
        geometry=True,
    )
    body = _placeholder(
        3,
        "Text Placeholder 2",
        '<p:ph type="body" idx="1"/>',
        _prompt("Click to edit Master text styles"),
        box=_BODY_BOX,
        body_pr='<a:bodyPr vert="horz" lIns="91440" tIns="45720" rIns="91440" bIns="45720" '
        'rtlCol="0"><a:normAutofit/></a:bodyPr>',
        geometry=True,
    )
    bar = _rect(4, "Accent Bar", (_TITLE_BOX[0] + 91_440, 1_720_000, 914_400, 45_720), _ACCENT)
    levels = "".join(
        _level(i, s, b) for i, (s, b) in enumerate(zip(_LEVEL_SIZES, "•–•", strict=True))
    )
    title_style = (
        '<a:lvl1pPr algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" '
        'hangingPunct="1"><a:lnSpc><a:spcPct val="90000"/></a:lnSpc><a:spcBef>'
        '<a:spcPct val="0"/></a:spcBef><a:buNone/><a:defRPr sz="3600" kern="1200">'
        f'<a:solidFill><a:schemeClr val="tx2"/></a:solidFill>{_FONT_REFS.format(k="mj")}'
        "</a:defRPr></a:lvl1pPr>"
    )
    other = (
        '<a:defPPr><a:defRPr lang="en-US"/></a:defPPr><a:lvl1pPr marL="0" algn="l" '
        'defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" hangingPunct="1">'
        '<a:defRPr sz="1800" kern="1200"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'
        f"{_FONT_REFS.format(k='mn')}</a:defRPr></a:lvl1pPr>"
    )
    return (
        f"{XML_HEAD}<p:sldMaster {_NS}><p:cSld><p:bg><p:bgPr>"
        '<a:solidFill><a:schemeClr val="bg1"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
        f"<p:spTree>{_GROUP}{title}{body}{bar}</p:spTree></p:cSld>"
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
        'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" '
        'folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/>'
        '<p:sldLayoutId id="2147483650" r:id="rId2"/></p:sldLayoutIdLst>'
        f"<p:txStyles><p:titleStyle>{title_style}</p:titleStyle>"
        f"<p:bodyStyle>{levels}</p:bodyStyle><p:otherStyle>{other}</p:otherStyle>"
        "</p:txStyles></p:sldMaster>"
    )


def _title_layout() -> str:
    title = _placeholder(
        2,
        "Title 1",
        '<p:ph type="ctrTitle"/>',
        _prompt("Click to edit Master title style"),
        box=_CTR_TITLE_BOX,
        body_pr='<a:bodyPr anchor="b"><a:normAutofit/></a:bodyPr>',
        lst_style='<a:lstStyle><a:lvl1pPr algn="ctr"><a:defRPr sz="5400"/></a:lvl1pPr>'
        "</a:lstStyle>",
    )
    subtitle = _placeholder(
        3,
        "Subtitle 2",
        '<p:ph type="subTitle" idx="1"/>',
        _prompt("Click to edit Master subtitle style"),
        box=_SUBTITLE_BOX,
        body_pr="<a:bodyPr><a:normAutofit/></a:bodyPr>",
        lst_style='<a:lstStyle><a:lvl1pPr marL="0" indent="0" algn="ctr"><a:buNone/>'
        f'<a:defRPr sz="2400">{_solid("595959")}</a:defRPr></a:lvl1pPr></a:lstStyle>',
    )
    bar = _rect(
        4, "Accent Bar", ((_SLIDE_W - 1_219_200) // 2, 3_530_000, 1_219_200, 45_720), _ACCENT
    )
    return (
        f'{XML_HEAD}<p:sldLayout {_NS} type="title" preserve="1" showMasterSp="0">'
        f'<p:cSld name="Title Slide"><p:spTree>{_GROUP}{title}{subtitle}{bar}</p:spTree>'
        "</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )


def _content_layout() -> str:
    title = _placeholder(2, "Title 1", '<p:ph type="title"/>', _prompt("Click to edit title"))
    body = _placeholder(3, "Content Placeholder 2", '<p:ph idx="1"/>', _prompt("Click to add text"))
    return (
        f'{XML_HEAD}<p:sldLayout {_NS} type="obj" preserve="1">'
        f'<p:cSld name="Title and Content"><p:spTree>{_GROUP}{title}{body}</p:spTree>'
        "</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


class _Links:
    """Hyperlink targets across the deck; a slide's rels list the ones it uses."""

    def __init__(self) -> None:
        self.urls: dict[str, str] = {}

    def rid(self, url: str) -> str:
        return self.urls.setdefault(url, f"rIdL{len(self.urls) + 1}")


def _safe_url(url: str) -> str | None:
    """``url`` percent-encoded for a relationship target when it is http(s) or mailto,
    else ``None`` (an odd target makes PowerPoint offer to repair the file)."""
    if not _SAFE_URL.match(url):
        return None
    quoted = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%-._~")
    try:
        urllib.parse.urlsplit(quoted)
    except ValueError:
        return None
    return quoted


def _runs(
    line: str,
    links: _Links,
    *,
    size: float | None = None,
    bold: bool = False,
    italic: bool = False,
    colour: str | None = None,
) -> str:
    """``<a:r>`` runs for one line of Markdown text; http(s) and mailto links are live."""
    out: list[str] = []
    for span in md.spans(line, bold=bold):
        props = ' lang="en-US"'
        if size:
            props += f' sz="{round(size * 100)}"'
        if span.bold:
            props += ' b="1"'
        if span.italic or italic:
            props += ' i="1"'
        inner = _solid(colour) if colour else ""
        if span.code:
            inner += '<a:latin typeface="Consolas"/>'
        label = span.text
        if span.url:
            url = _safe_url(span.url)
            if url:
                inner += f'<a:hlinkClick r:id="{links.rid(url)}"/>'
            else:
                label = md.link_text(span)
        if not label:
            continue
        rpr = f'<a:rPr{props} dirty="0">{inner}</a:rPr>' if inner else f'<a:rPr{props} dirty="0"/>'
        out.append(f"<a:r>{rpr}<a:t>{text(label)}</a:t></a:r>")
    return "".join(out)


@dataclass
class _Para:
    xml: str
    chars: int
    size: float  # points before any shrinking
    indent_in: float  # left margin, inches


_NO_BULLET = '<a:pPr marL="0" indent="0"><a:buNone/></a:pPr>'
_CODE_PPR = '<a:pPr marL="0" indent="0"><a:spcBef><a:spcPts val="0"/></a:spcBef><a:buNone/></a:pPr>'


def _body_paragraphs(blocks: list[md.Block], links: _Links, starts: list[int]) -> list[_Para]:
    out: list[_Para] = []
    for b in blocks:
        chars = len(md.plain(b.text))
        if b.kind == "bullet":
            ppr = f'<a:pPr lvl="{b.level}"/>' if b.level else ""
            xml = f"<a:p>{ppr}{_runs(b.text, links)}</a:p>"
            out.append(_Para(xml, chars, _LEVEL_SIZES[b.level], 0.25 + 0.5 * b.level))
        elif b.kind == "number":
            start = starts[b.list_id] if 0 <= b.list_id < len(starts) else 1
            start = min(start, 32_767)  # PowerPoint's limit for startAt
            scheme = ("arabicPeriod", "alphaLcPeriod", "romanLcPeriod")[b.level]
            at = f' startAt="{start}"' if start > 1 and b.level == 0 else ""
            lvl = f' lvl="{b.level}"' if b.level else ""
            ppr = (
                f'<a:pPr marL="{342_900 + b.level * 457_200}"{lvl} indent="-342900">'
                f'<a:buFont typeface="+mj-lt"/><a:buAutoNum type="{scheme}"{at}/></a:pPr>'
            )
            xml = f"<a:p>{ppr}{_runs(b.text, links)}</a:p>"
            out.append(_Para(xml, chars, _LEVEL_SIZES[b.level], 0.375 + 0.5 * b.level))
        elif b.kind == "para":
            runs = "<a:br/>".join(_runs(line, links) for line in b.lines)
            chars = sum(len(md.plain(line)) for line in b.lines)
            out.append(_Para(f"<a:p>{_NO_BULLET}{runs}</a:p>", chars, _LEVEL_SIZES[0], 0))
        elif b.kind == "heading":
            xml = f"<a:p>{_NO_BULLET}{_runs(b.text, links, bold=True)}</a:p>"
            out.append(_Para(xml, chars, _LEVEL_SIZES[0], 0))
        elif b.kind == "quote":
            ppr = '<a:pPr marL="457200" indent="0"><a:buNone/></a:pPr>'
            xml = f"<a:p>{ppr}{_runs(b.text, links, italic=True)}</a:p>"
            out.append(_Para(xml, chars, _LEVEL_SIZES[0], 0.5))
        elif b.kind == "code":
            sz = round(_CODE_SIZE * 100)
            for line in b.lines or [""]:
                if line:
                    run = (
                        f'<a:r><a:rPr lang="en-US" sz="{sz}" dirty="0">'
                        f'<a:latin typeface="Consolas"/></a:rPr><a:t>{text(line)}</a:t></a:r>'
                    )
                else:
                    run = f'<a:endParaRPr lang="en-US" sz="{sz}"/>'
                out.append(_Para(f"<a:p>{_CODE_PPR}{run}</a:p>", len(line), _CODE_SIZE, 0))
    return out


def _height_in(paras: list[_Para], scale: float, width_in: float) -> float:
    """Estimated height of ``paras`` in inches at ``scale`` (Calibri averages about half
    an em per character; lines are 1.2 em; about 10 pt before each paragraph)."""
    spacing = 1.0 if scale >= 1 else (0.9 if scale > 0.7 else 0.8)
    total = 0.0
    for p in paras:
        size = p.size * scale
        per_line = max(8.0, (width_in - p.indent_in) / (0.5 * size / 72))
        lines = max(1, math.ceil(p.chars / per_line))
        total += lines * size * 1.2 * spacing / 72 + 10 * scale / 72
    return total


def _fit(paras: list[_Para], height_in: float, width_in: float) -> float | None:
    """The largest shrink step at which ``paras`` fit, or ``None``."""
    for scale in _SCALES:
        if _height_in(paras, scale, width_in) <= height_in:
            return scale
    return None


def _autofit(scale: float | None) -> str:
    if scale is None or scale >= 1:
        return "<a:bodyPr><a:normAutofit/></a:bodyPr>"
    reduction = 10_000 if scale > 0.7 else 20_000
    return (
        f'<a:bodyPr><a:normAutofit fontScale="{round(scale * 100_000)}" '
        f'lnSpcReduction="{reduction}"/></a:bodyPr>'
    )


def _pages(paras: list[_Para], height_in: float, width_in: float) -> list[list[_Para]]:
    """``paras`` split so that each page fits at 80% size or larger."""
    if _fit(paras, height_in, width_in) is not None:
        return [paras]
    pages: list[list[_Para]] = [[]]
    for p in paras:
        if pages[-1] and _height_in([*pages[-1], p], 0.8, width_in) > height_in:
            pages.append([])
        pages[-1].append(p)
    # The same number of pages, evenly filled (6/6/6/2 -> 5/5/5/5), when that still fits.
    size = math.ceil(len(paras) / len(pages))
    even = [paras[i : i + size] for i in range(0, len(paras), size)]
    if len(even) == len(pages) and all(_height_in(e, 0.8, width_in) <= height_in for e in even):
        return even
    return pages


def _inches(emu: int) -> float:
    return emu / _EMU_PER_IN


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def _table_metrics(rows: list[list[str]]) -> tuple[float, int]:
    """``(font size in points, row height in EMU)`` for a table of ``rows``."""
    data_rows = len(rows) - 1
    size = 16.0 if data_rows <= 5 else (14.0 if data_rows <= 8 else 12.0)
    cols = max(len(r) for r in rows)
    if cols > 6:  # wide tables get smaller text, down to 8 pt
        size = max(8.0, size - (cols - 6))
    return size, int(size * 12_700 * 1.25 + 91_440)


def _table_frame(rows: list[list[str]], links: _Links, y: int, sid: int) -> str:
    """A table graphic frame at ``y``: header row in the accent colour, banded rows."""
    cols = max(len(r) for r in rows)
    size, row_h = _table_metrics(rows)
    weights = [
        min(40, max(3, max(len(md.plain(r[c])) if c < len(r) else 0 for r in rows)))
        for c in range(cols)
    ]
    # Columns of numbers (amounts, percentages, dates) are right-aligned, header included.
    right = [
        any(r[c].strip() for r in rows[1:] if c < len(r))
        and all(cell_value(md.plain(r[c])) for r in rows[1:] if c < len(r) and r[c].strip())
        for c in range(cols)
    ]
    x, _, width, _ = _BODY_BOX
    col_w = [width * w // sum(weights) for w in weights]
    col_w[-1] += width - sum(col_w)
    border = "".join(
        f'<a:{side} w="9525" cap="flat" cmpd="sng" algn="ctr">{_solid("BFBFBF")}'
        f'<a:prstDash val="solid"/></a:{side}>'
        for side in ("lnL", "lnR", "lnT", "lnB")
    )
    trs = []
    for r, row in enumerate(rows):
        header = r == 0
        fill = _TITLE_COLOUR if header else ("FFFFFF" if r % 2 else "F2F2F2")
        tcs = []
        for c in range(cols):
            cell = row[c] if c < len(row) else ""
            runs = "<a:br/>".join(
                _runs(part, links, size=size, bold=header, colour="FFFFFF" if header else None)
                for part in cell.split("\n")
            )
            end = f'<a:endParaRPr lang="en-US" sz="{round(size * 100)}"/>' if not runs else ""
            align = '<a:pPr algn="r"/>' if right[c] else ""
            tcs.append(
                f"<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>{align}{runs}{end}</a:p></a:txBody>"
                '<a:tcPr marL="91440" marR="91440" marT="45720" marB="45720" anchor="ctr">'
                f"{border}{_solid(fill)}</a:tcPr></a:tc>"
            )
        trs.append(f'<a:tr h="{row_h}">{"".join(tcs)}</a:tr>')
    grid = "".join(f'<a:gridCol w="{w}"/>' for w in col_w)
    return (
        f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="Table {sid - 1}"/>'
        '<p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/>'
        f'</p:nvGraphicFramePr><p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{width}" '
        f'cy="{row_h * len(rows)}"/></p:xfrm><a:graphic><a:graphicData '
        'uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>'
        f'<a:tblPr firstRow="1" bandRow="1"/><a:tblGrid>{grid}</a:tblGrid>{"".join(trs)}'
        "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def _table_pages(rows: list[list[str]]) -> list[list[list[str]]]:
    """``rows`` in slide-sized pages, each starting with the header row."""
    header, data = rows[0], rows[1:]
    if not data:
        return [rows]
    step = TABLE_ROWS_PER_SLIDE
    return [[header, *data[i : i + step]] for i in range(0, len(data), step)]


# --------------------------------------------------------------------------- #
# Slides
# --------------------------------------------------------------------------- #


@dataclass
class _Section:
    title: str | None
    level: int  # the heading level that started it; 0 for a rule or the start
    blocks: list[md.Block] = field(default_factory=list)


def _sections(blocks: list[md.Block]) -> list[_Section]:
    sections = [_Section(None, 0)]
    for b in blocks:
        if b.kind == "heading" and b.level <= 2:
            sections.append(_Section(b.text, b.level))
        elif b.kind == "rule":
            sections.append(_Section(None, 0))
        else:
            sections[-1].blocks.append(b)
    out: list[_Section] = []
    for s in sections:
        if s.title is None and s.blocks and s.blocks[0].kind == "heading":
            s.title, s.blocks = s.blocks[0].text, s.blocks[1:]
        if s.title is not None or s.blocks:
            out.append(s)
    return out


def _title_shape(title: str, links: _Links, ph: str, box: tuple[int, int, int, int]) -> str:
    """A title placeholder, shrunk when the title is long."""
    _, _, cx, cy = box
    para = _Para("", len(md.plain(title)), 54.0 if "ctrTitle" in ph else 36.0, 0)
    scale = _fit([para], _inches(cy) - 0.1, _inches(cx) - 0.2) or _SCALES[-1]
    body_pr = ('<a:bodyPr anchor="b">' if "ctrTitle" in ph else "<a:bodyPr>") + _autofit(
        scale
    ).removeprefix("<a:bodyPr>")
    return _placeholder(2, "Title 1", ph, f"<a:p>{_runs(title, links)}</a:p>", body_pr=body_pr)


def _title_slide(title: str, subtitle: list[md.Block], links: _Links) -> str:
    shapes = _title_shape(title, links, '<p:ph type="ctrTitle"/>', _CTR_TITLE_BOX)
    paras = []
    for b in subtitle:
        if b.kind == "para":
            paras.append("<a:br/>".join(_runs(line, links) for line in b.lines))
        else:
            paras.append(_runs(b.text, links, bold=b.kind == "heading", italic=b.kind == "quote"))
    if paras:
        shapes += _placeholder(
            3,
            "Subtitle 2",
            '<p:ph type="subTitle" idx="1"/>',
            "".join(f"<a:p>{p}</a:p>" for p in paras),
            body_pr="<a:bodyPr><a:normAutofit/></a:bodyPr>",
        )
    return shapes


def _content_slides(
    title: str | None, blocks: list[md.Block], links: _Links, starts: list[int]
) -> list[str]:
    """The slides of one section: its text (split when too long), then its tables (split
    when long); a short text and the first table share a slide."""
    x, y, w, h = _BODY_BOX
    width_in, height_in = _inches(w) - 0.2, _inches(h) - 0.1
    paras = _body_paragraphs([b for b in blocks if b.kind != "table"], links, starts)
    tables = [p for b in blocks if b.kind == "table" for p in _table_pages(b.rows)]
    text_pages = _pages(paras, height_in, width_in) if paras else []
    slides: list[tuple[list[_Para], list[list[str]] | None]] = [(p, None) for p in text_pages]
    for rows in tables:
        if slides and slides[-1][1] is None and len(text_pages) == 1:
            text_h = _height_in(slides[-1][0], 1.0, width_in) + 0.1
            room = _inches(h) - text_h - 0.15
            if (
                text_h <= _inches(h) * 0.45
                and _table_metrics(rows)[1] * len(rows) <= room * _EMU_PER_IN
            ):
                slides[-1] = (slides[-1][0], rows)
                continue
        slides.append(([], rows))
    if not slides and title:
        slides.append(([], None))  # a heading with nothing under it: a title-only slide
    out: list[str] = []
    for n, (page, rows) in enumerate(slides):
        heading = title if n == 0 or not title else f"{title} (cont.)"
        shapes = _title_shape(heading, links, '<p:ph type="title"/>', _TITLE_BOX) if heading else ""
        table_y = y
        if page:
            box = None
            scale = _fit(page, height_in, width_in)
            if rows is not None:
                text_h = int((_height_in(page, 1.0, width_in) + 0.1) * _EMU_PER_IN)
                box, scale, table_y = (x, y, w, text_h), 1.0, y + text_h + int(0.15 * _EMU_PER_IN)
            shapes += _placeholder(
                3,
                "Content Placeholder 2",
                '<p:ph idx="1"/>',
                "".join(p.xml for p in page),
                box=box,
                body_pr=_autofit(scale if scale is not None else _SCALES[-1]),
            )
        if rows is not None:
            shapes += _table_frame(rows, links, table_y, 4)
        out.append(shapes)
    return out


def _subtitle_split(blocks: list[md.Block]) -> tuple[list[md.Block], list[md.Block]]:
    """The short text right under the title (the subtitle) and everything else."""
    subtitle: list[md.Block] = []
    chars = 0
    for i, b in enumerate(blocks):
        size = sum(len(line) for line in b.lines) if b.kind == "para" else len(b.text)
        if b.kind not in ("para", "heading", "quote") or chars + size > 240:
            return subtitle, blocks[i:]
        subtitle.append(b)
        chars += size
    return subtitle, []


_LINK_ID = re.compile(r'r:id="(rIdL\d+)"')


def markdown_to_pptx(markdown: str, title: str = "") -> Deck:
    """A presentation from Markdown-style text (see the module docstring). Raises
    ``ValueError`` when there is nothing to put on a slide."""
    blocks = md.parse(markdown)
    starts = md.list_starts(blocks)
    links = _Links()
    slides: list[tuple[int, str]] = []  # (layout 1 = title slide / 2 = content, shapes)
    deck_title = title
    for k, sec in enumerate(_sections(blocks)):
        if sec.level == 1 and sec.title is not None and (k == 0 or not sec.blocks):
            if k == 0:
                deck_title = md.plain(sec.title).strip() or title
            subtitle, rest = _subtitle_split(sec.blocks)
            slides.append((1, _title_slide(sec.title, subtitle, links)))
            slides.extend((2, s) for s in _content_slides(sec.title, rest, links, starts) if rest)
        else:
            slides.extend((2, s) for s in _content_slides(sec.title, sec.blocks, links, starts))
    if not slides:
        raise ValueError("there is nothing to put on a slide")
    notes: list[str] = []
    if len(slides) > MAX_SLIDES:
        notes.append(f"Only the first {MAX_SLIDES} of {len(slides)} slides were written.")
        slides = slides[:MAX_SLIDES]
    n = len(slides)
    urls = {rid: url for url, rid in links.urls.items()}
    parts: dict[str, str | bytes] = {}
    for i, (layout, shapes) in enumerate(slides, start=1):
        parts[f"ppt/slides/slide{i}.xml"] = (
            f"{XML_HEAD}<p:sld {_NS}><p:cSld><p:spTree>{_GROUP}{shapes}</p:spTree></p:cSld>"
            "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
        )
        used = dict.fromkeys(_LINK_ID.findall(shapes))
        parts[f"ppt/slides/_rels/slide{i}.xml.rels"] = rels(
            [("rId1", f"{REL_NS}/slideLayout", f"../slideLayouts/slideLayout{layout}.xml")]
            + [(rid, f"{REL_NS}/hyperlink", urls[rid], True) for rid in used]
        )
    slide_ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, n + 1))
    parts["ppt/presentation.xml"] = (
        f'{XML_HEAD}<p:presentation {_NS} saveSubsetFonts="1">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f'<p:sldIdLst>{slide_ids}</p:sldIdLst><p:sldSz cx="{_SLIDE_W}" cy="{_SLIDE_H}"/>'
        '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
    )
    parts["ppt/_rels/presentation.xml.rels"] = rels(
        [("rId1", f"{REL_NS}/slideMaster", "slideMasters/slideMaster1.xml")]
        + [(f"rId{i + 1}", f"{REL_NS}/slide", f"slides/slide{i}.xml") for i in range(1, n + 1)]
        + [
            (f"rId{n + 2}", f"{REL_NS}/presProps", "presProps.xml"),
            (f"rId{n + 3}", f"{REL_NS}/viewProps", "viewProps.xml"),
            (f"rId{n + 4}", f"{REL_NS}/theme", "theme/theme1.xml"),
            (f"rId{n + 5}", f"{REL_NS}/tableStyles", "tableStyles.xml"),
        ]
    )
    parts["ppt/slideMasters/slideMaster1.xml"] = _master()
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = rels(
        [
            ("rId1", f"{REL_NS}/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", f"{REL_NS}/slideLayout", "../slideLayouts/slideLayout2.xml"),
            ("rId3", f"{REL_NS}/theme", "../theme/theme1.xml"),
        ]
    )
    for k, layout in ((1, _title_layout()), (2, _content_layout())):
        parts[f"ppt/slideLayouts/slideLayout{k}.xml"] = layout
        parts[f"ppt/slideLayouts/_rels/slideLayout{k}.xml.rels"] = rels(
            [("rId1", f"{REL_NS}/slideMaster", "../slideMasters/slideMaster1.xml")]
        )
    parts["ppt/theme/theme1.xml"] = _theme()
    parts["ppt/presProps.xml"] = f"{XML_HEAD}<p:presentationPr {_NS}/>"
    parts["ppt/viewProps.xml"] = f"{XML_HEAD}<p:viewPr {_NS}/>"
    parts["ppt/tableStyles.xml"] = (
        f'{XML_HEAD}<a:tblStyleLst xmlns:a="{_A}" def="{{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}}"/>'
    )
    parts["docProps/core.xml"] = core_properties(deck_title or "Presentation")
    parts["_rels/.rels"] = rels(
        [("rId1", OFFICE_DOC, "ppt/presentation.xml"), ("rId2", CORE_PROPS, "docProps/core.xml")]
    )
    ct = f"{CT_OOXML}presentationml."
    overrides = {
        "/ppt/presentation.xml": f"{ct}presentation.main+xml",
        "/ppt/slideMasters/slideMaster1.xml": f"{ct}slideMaster+xml",
        "/ppt/slideLayouts/slideLayout1.xml": f"{ct}slideLayout+xml",
        "/ppt/slideLayouts/slideLayout2.xml": f"{ct}slideLayout+xml",
    }
    for i in range(1, n + 1):
        overrides[f"/ppt/slides/slide{i}.xml"] = f"{ct}slide+xml"
    overrides.update(
        {
            "/ppt/theme/theme1.xml": f"{CT_OOXML}theme+xml",
            "/ppt/presProps.xml": f"{ct}presProps+xml",
            "/ppt/viewProps.xml": f"{ct}viewProps+xml",
            "/ppt/tableStyles.xml": f"{ct}tableStyles+xml",
            "/docProps/core.xml": CT_CORE,
        }
    )
    parts["[Content_Types].xml"] = content_types(overrides)
    return Deck(package(parts), n, notes)


__all__ = ["MAX_SLIDES", "TABLE_ROWS_PER_SLIDE", "Deck", "markdown_to_pptx"]
