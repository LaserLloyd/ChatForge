"""Tables -> ``.xlsx``: minimal SpreadsheetML with the standard library.

One sheet per table (see :func:`doc_markdown.find_tables`), named after the heading above it
(Excel's rules: at most 31 characters, none of ``[]:*?/\\``, unique, not "History"). The
header row is bold, shaded, frozen and has an AutoFilter. Cells that read as numbers are
stored as numbers with a format that shows them as written (``1,234.50``, ``12%``,
``$9.99``); ISO dates (``2024-03-31``, ``2024-03-31 14:30``) become real dates. Leading-zero
codes (``007``), long digit strings and everything else stay text.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from chatforge.tools import doc_markdown as md
from chatforge.tools.doc_ooxml import (
    CORE_PROPS,
    CT_CORE,
    CT_OOXML,
    OFFICE_DOC,
    REL_NS,
    XML_HEAD,
    attr,
    content_types,
    core_properties,
    package,
    rels,
    text,
)

MAX_SHEETS = 100
MAX_ROWS = 100_000  # data rows per sheet (Excel's own limit is 1,048,575)
MAX_COLUMNS = 500
MAX_CELL_CHARS = 32_767  # Excel's limit
SHEET_NAME_CHARS = 31
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_WIDTH_SAMPLE_ROWS = 2000


class NoTableError(ValueError):
    """The content has no table to put in a workbook."""


@dataclass
class Workbook:
    data: bytes
    #: Sheet names in order.
    sheets: list[str]
    #: What was left out (rows or columns over the caps), for the model.
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Sheet names
# --------------------------------------------------------------------------- #

_NAME_DROP = str.maketrans({"[": "", "]": "", ":": "", "*": "", "?": "", "/": "-", "\\": "-"})


def sheet_name(wanted: str, taken: set[str], fallback: str) -> str:
    """An Excel-safe sheet name for ``wanted``, unique among ``taken`` (lower-cased names,
    updated here)."""
    name = md.clean_xml_text(wanted).translate(_NAME_DROP)
    name = " ".join(name.split()).strip("'").strip()
    # Cut, then trim again: Excel refuses a name that starts or ends with an apostrophe.
    name = name[:SHEET_NAME_CHARS].rstrip().strip("'").strip() or fallback
    base, n = name, 1
    while name.lower() in taken or name.lower() == "history":
        n += 1
        suffix = f" ({n})"
        name = base[: SHEET_NAME_CHARS - len(suffix)].rstrip() + suffix
    taken.add(name.lower())
    return name


def column_letter(index: int) -> str:
    """``0 -> A``, ``25 -> Z``, ``26 -> AA``."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# --------------------------------------------------------------------------- #
# Cell values
# --------------------------------------------------------------------------- #

_PLAIN = re.compile(r"^(-?)(0|[1-9]\d*)(?:\.(\d+))?$")
_GROUPED = re.compile(r"^(-?)([1-9]\d{0,2}(?:,\d{3})+)(?:\.(\d+))?$")
_PERCENT = re.compile(r"^(-?)(0|[1-9]\d*)(?:\.(\d+))?\s?%$")
_CURRENCY = re.compile(r"^(-?)([$€£¥])\s?(0|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)(?:\.(\d+))?$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?$")
_EPOCH = dt.datetime(1899, 12, 30)
_FIRST_DATE = dt.datetime(1900, 3, 1)  # Excel's serials are off by one before this
_MAX_DIGITS = 15  # what a double holds exactly


def _decimals(n: int) -> str:
    return "." + "0" * n if n else ""


def cell_value(raw: str) -> tuple[str, str | int] | None:
    """``(value, number format)`` when ``raw`` reads as a number or date, else ``None``.
    The format is a built-in id (``0`` = General) or a format code."""
    s = raw.strip()
    if not s or len(s) > 40:
        return None
    if (m := _PLAIN.match(s)) is not None:
        sign, whole, frac = m.groups()
        if len(whole) + len(frac or "") > _MAX_DIGITS:
            return None
        if frac and len(frac) > 10:
            return None
        return s, (f"0{_decimals(len(frac))}" if frac else 0)
    if (m := _GROUPED.match(s)) is not None:
        sign, whole, frac = m.groups()
        digits = whole.replace(",", "")
        if len(digits) + len(frac or "") > _MAX_DIGITS:
            return None
        value = f"{sign}{digits}" + (f".{frac}" if frac else "")
        return value, f"#,##0{_decimals(len(frac or ''))}"
    if (m := _PERCENT.match(s)) is not None:
        sign, whole, frac = m.groups()
        try:
            value = Decimal(f"{sign}{whole}" + (f".{frac}" if frac else "")) / 100
        except InvalidOperation:  # pragma: no cover - the pattern only lets numbers through
            return None
        return format(value, "f"), f"0{_decimals(len(frac or ''))}%"
    if (m := _CURRENCY.match(s)) is not None:
        sign, symbol, whole, frac = m.groups()
        digits = whole.replace(",", "")
        if len(digits) + len(frac or "") > _MAX_DIGITS:
            return None
        value = f"{sign}{digits}" + (f".{frac}" if frac else "")
        return value, f'"{symbol}"#,##0{_decimals(len(frac or ""))}'
    if (m := _DATE.match(s)) is not None:
        y, mo, d, hh, mm, ss = m.groups()
        try:
            when = dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
        except ValueError:
            return None
        if when < _FIRST_DATE:
            return None
        delta = when - _EPOCH
        serial = delta.days + delta.seconds / 86400
        value = str(delta.days) if hh is None else f"{serial:.10f}".rstrip("0").rstrip(".")
        fmt = "yyyy-mm-dd" if hh is None else ("yyyy-mm-dd hh:mm:ss" if ss else "yyyy-mm-dd hh:mm")
        return value, fmt
    return None


def _display_width(s: str) -> int:
    longest = max(s.split("\n"), key=len) if s else ""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in longest)


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #


class _Styles:
    """``styles.xml`` built as cells ask for formats: ``xf(fmt, bold=..., header=...)``."""

    _BUILTIN = {"0": 1, "0.00": 2, "#,##0": 3, "#,##0.00": 4, "0%": 9, "0.00%": 10}

    def __init__(self) -> None:
        self._codes: dict[str, int] = {}
        self._xfs: dict[tuple[int, int, int, int, bool], int] = {(0, 0, 0, 0, False): 0}

    def _fmt_id(self, fmt: str | int) -> int:
        if isinstance(fmt, int):
            return fmt
        if fmt in self._BUILTIN:
            return self._BUILTIN[fmt]
        return self._codes.setdefault(fmt, 164 + len(self._codes))

    def xf(
        self, fmt: str | int = 0, *, bold: bool = False, header: bool = False, wrap=False
    ) -> int:
        key = (
            self._fmt_id(fmt),
            1 if bold or header else 0,
            2 if header else 0,
            1 if header else 0,
            bool(wrap),
        )
        return self._xfs.setdefault(key, len(self._xfs))

    def xml(self) -> str:
        fmts = "".join(
            f'<numFmt numFmtId="{i}" formatCode={attr(code)}/>' for code, i in self._codes.items()
        )
        num_fmts = f'<numFmts count="{len(self._codes)}">{fmts}</numFmts>' if self._codes else ""
        xfs = []
        for (num, font, fill, border, wrap), _ in sorted(self._xfs.items(), key=lambda kv: kv[1]):
            applied = "".join(
                f' apply{name}="1"'
                for name, on in (
                    ("NumberFormat", num),
                    ("Font", font),
                    ("Fill", fill),
                    ("Border", border),
                    ("Alignment", wrap),
                )
                if on
            )
            head = f'<xf numFmtId="{num}" fontId="{font}" fillId="{fill}" borderId="{border}" '
            if wrap:
                xfs.append(f'{head}xfId="0"{applied}><alignment vertical="top" wrapText="1"/></xf>')
            else:
                xfs.append(f'{head}xfId="0"{applied}/>')
        return (
            f'{XML_HEAD}<styleSheet xmlns="{_MAIN_NS}">{num_fmts}'
            '<fonts count="2">'
            '<font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/>'
            '<scheme val="minor"/></font>'
            '<font><b/><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/>'
            '<scheme val="minor"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFDDE5F0"/>'
            '<bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
            '<border><left/><right/><top/><bottom style="thin"><color rgb="FF8EA4C8"/>'
            "</bottom><diagonal/></border></borders>"
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
            f'</cellStyleXfs><cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '<dxfs count="0"/><tableStyles count="0"/></styleSheet>'
        )


# --------------------------------------------------------------------------- #
# Workbook
# --------------------------------------------------------------------------- #


class _Strings:
    def __init__(self) -> None:
        self.index: dict[str, int] = {}
        self.refs = 0

    def add(self, s: str) -> int:
        self.refs += 1
        return self.index.setdefault(s, len(self.index))

    def xml(self) -> str:
        items = "".join(f'<si><t xml:space="preserve">{text(s)}</t></si>' for s in self.index)
        return (
            f'{XML_HEAD}<sst xmlns="{_MAIN_NS}" count="{self.refs}" '
            f'uniqueCount="{len(self.index)}">{items}</sst>'
        )


def _sheet_xml(
    rows: list[list[str]],
    bold: list[list[bool]],
    strings: _Strings,
    styles: _Styles,
    *,
    first: bool,
) -> tuple[str, str]:
    """``(worksheet XML, its range as $A$1:$C$9)``. ``rows`` are padded to one width."""
    width = max(1, len(rows[0]))
    last_col, last_row = column_letter(width - 1), len(rows)
    widths = [0] * width
    body: list[str] = []
    for r, row in enumerate(rows, start=1):
        cells: list[str] = []
        header = r == 1
        for c, raw in enumerate(row):
            if not raw:
                continue
            ref = f"{column_letter(c)}{r}"
            if r <= _WIDTH_SAMPLE_ROWS:
                widths[c] = max(widths[c], _display_width(raw) + (2 if header else 0))
            value = None if header else cell_value(raw)
            strong = not header and bold[r - 1][c]
            if value is not None:
                number, fmt = value
                s = styles.xf(fmt, bold=strong)
                style = f' s="{s}"' if s else ""
                cells.append(f'<c r="{ref}"{style}><v>{number}</v></c>')
            else:
                content = raw[:MAX_CELL_CHARS]
                s = styles.xf(0, bold=strong, header=header, wrap="\n" in content)
                style = f' s="{s}"' if s else ""
                cells.append(f'<c r="{ref}"{style} t="s"><v>{strings.add(content)}</v></c>')
        body.append(f'<row r="{r}">{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{min(60, max(8, w + 2))}" customWidth="1"/>'
        for i, w in enumerate(widths)
    )
    selected = ' tabSelected="1"' if first else ""
    area = f"A1:{last_col}{last_row}"
    xml = (
        f'{XML_HEAD}<worksheet xmlns="{_MAIN_NS}" xmlns:r="{REL_NS}">'
        f'<dimension ref="{area}"/>'
        f'<sheetViews><sheetView workbookViewId="0"{selected}>'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{cols}</cols><sheetData>{''.join(body)}</sheetData>"
        f'<autoFilter ref="{area}"/>'
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        "</worksheet>"
    )
    return xml, f"$A$1:${last_col}${last_row}"


def _bold_cells(table: md.Table, width: int) -> list[list[bool]]:
    """Which cells are all ``**bold**`` (only in Markdown tables)."""
    return [
        [table.markdown and c < len(row) and md.is_bold_cell(row[c]) for c in range(width)]
        for row in table.rows
    ]


def tables_to_xlsx(tables: list[md.Table], title: str = "") -> Workbook:
    """A workbook with one sheet per table. Raises :class:`NoTableError` for none."""
    if not tables:
        raise NoTableError("no table found")
    notes: list[str] = []
    if len(tables) > MAX_SHEETS:
        notes.append(f"Only the first {MAX_SHEETS} of {len(tables)} tables were written.")
        tables = tables[:MAX_SHEETS]
    strings, styles = _Strings(), _Styles()
    taken: set[str] = set()
    names: list[str] = []
    sheets: list[tuple[str, str]] = []
    for i, table in enumerate(tables):
        name = sheet_name(table.title, taken, f"Sheet{i + 1}")
        rows = md.table_text(table)
        if len(rows) - 1 > MAX_ROWS:
            notes.append(f"Sheet '{name}' was cut to its first {MAX_ROWS:,} rows.")
            rows = rows[: MAX_ROWS + 1]
        if len(rows[0]) > MAX_COLUMNS:
            notes.append(f"Sheet '{name}' was cut to its first {MAX_COLUMNS} columns.")
            rows = [r[:MAX_COLUMNS] for r in rows]
        names.append(name)
        bold = _bold_cells(table, len(rows[0]))
        sheets.append(_sheet_xml(rows, bold, strings, styles, first=i == 0))
    sheet_list = "".join(
        f'<sheet name={attr(name)} sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i, name in enumerate(names)
    )
    filters = "".join(
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
        f"{text(_quoted(name))}!{area}</definedName>"
        for i, (name, (_, area)) in enumerate(zip(names, sheets, strict=True))
    )
    workbook = (
        f'{XML_HEAD}<workbook xmlns="{_MAIN_NS}" xmlns:r="{REL_NS}">'
        '<bookViews><workbookView activeTab="0"/></bookViews>'
        f"<sheets>{sheet_list}</sheets><definedNames>{filters}</definedNames></workbook>"
    )
    n = len(names)
    workbook_rels = rels(
        [(f"rId{i + 1}", f"{REL_NS}/worksheet", f"worksheets/sheet{i + 1}.xml") for i in range(n)]
        + [
            (f"rId{n + 1}", f"{REL_NS}/styles", "styles.xml"),
            (f"rId{n + 2}", f"{REL_NS}/sharedStrings", "sharedStrings.xml"),
        ]
    )
    overrides = {"/xl/workbook.xml": f"{CT_OOXML}spreadsheetml.sheet.main+xml"}
    for i in range(n):
        overrides[f"/xl/worksheets/sheet{i + 1}.xml"] = f"{CT_OOXML}spreadsheetml.worksheet+xml"
    overrides["/xl/styles.xml"] = f"{CT_OOXML}spreadsheetml.styles+xml"
    overrides["/xl/sharedStrings.xml"] = f"{CT_OOXML}spreadsheetml.sharedStrings+xml"
    overrides["/docProps/core.xml"] = CT_CORE
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types(overrides),
        "_rels/.rels": rels(
            [("rId1", OFFICE_DOC, "xl/workbook.xml"), ("rId2", CORE_PROPS, "docProps/core.xml")]
        ),
        "docProps/core.xml": core_properties(title or names[0]),
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
    }
    for i, (sheet, _) in enumerate(sheets):
        parts[f"xl/worksheets/sheet{i + 1}.xml"] = sheet
    parts["xl/styles.xml"] = styles.xml()
    parts["xl/sharedStrings.xml"] = strings.xml()
    return Workbook(package(parts), names, notes)


def _quoted(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def markdown_to_xlsx(content: str, title: str = "") -> Workbook:
    """A workbook from the tables in ``content`` (Markdown tables or CSV); raises
    :class:`NoTableError` when there are none."""
    # Only a ```markdown (or bare) fence around everything is unwrapped: a ```csv one stays a
    # CSV block, read as such (one column, ragged rows, a first line starting with '#').
    return tables_to_xlsx(md.find_tables(md.parse(md.unfence(content, md.MARKDOWN_FENCES))), title)


__all__ = [
    "MAX_COLUMNS",
    "MAX_ROWS",
    "MAX_SHEETS",
    "NoTableError",
    "Workbook",
    "cell_value",
    "column_letter",
    "markdown_to_xlsx",
    "sheet_name",
    "tables_to_xlsx",
]
