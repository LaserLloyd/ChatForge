"""create_document's Excel, PowerPoint, CSV and HTML output, and the Word improvements.

Every package is checked the way Office reads it: each part is well-formed XML, each part
has a content type, each override and each internal relationship points at a real part.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from chatforge.chat.engine import DOCUMENTS_SENTENCE
from chatforge.tools import doc_pptx, doc_xlsx, documents
from chatforge.tools.doc_html import CSP_META, markdown_to_html
from chatforge.tools.doc_markdown import find_tables, parse, unfence
from chatforge.tools.doc_pptx import markdown_to_pptx
from chatforge.tools.doc_xlsx import cell_value, column_letter, markdown_to_xlsx, sheet_name
from chatforge.tools.documents import create_document, markdown_to_docx, sanitize_filename
from chatforge.tools.registry import TOOL_NAMES, ToolRegistry

CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def open_package(data: bytes) -> zipfile.ZipFile:
    """The package, after checking it is one Office opens: XML parts, content types for
    every part, and relationships that resolve."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert zf.namelist()[0] == "[Content_Types].xml"
    for name in names:
        ET.fromstring(zf.read(name))  # well-formed
    types = ET.fromstring(zf.read("[Content_Types].xml"))
    overrides = {o.get("PartName") for o in types.iter(f"{CT}Override")}
    defaults = {d.get("Extension") for d in types.iter(f"{CT}Default")}
    assert {"rels", "xml"} <= defaults
    for name in names:
        assert f"/{name}" in overrides or name.rsplit(".", 1)[-1] in defaults, name
    for part in overrides:
        assert part[1:] in names, f"content type for a missing part {part}"
    for name in (n for n in names if n.endswith(".rels")):
        source_dir = posixpath.dirname(posixpath.dirname(name))
        if name != "_rels/.rels":
            source = posixpath.join(source_dir, posixpath.basename(name)[: -len(".rels")])
            assert source in names, f"{name} belongs to a missing part"
        ids = []
        for rel in ET.fromstring(zf.read(name)).iter(f"{PR}Relationship"):
            ids.append(rel.get("Id"))
            if rel.get("TargetMode") == "External":
                continue
            target = posixpath.normpath(posixpath.join(source_dir, rel.get("Target")))
            assert target in names, f"{name} -> missing {target}"
        assert len(ids) == len(set(ids)), f"duplicate relationship ids in {name}"
    return zf


def xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


# --------------------------------------------------------------------------- #
# Names and formats
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("filename", "fmt", "expected"),
    [
        ("budget.xlsx", None, "budget.xlsx"),
        ("talk.PPTX", None, "talk.pptx"),
        ("budget", "excel", "budget.xlsx"),
        ("budget", "spreadsheet", "budget.xlsx"),
        ("talk", "PowerPoint", "talk.pptx"),
        ("talk", "slides", "talk.pptx"),
        ("old.doc", None, "old.docx"),
        ("old.xls", None, "old.xlsx"),
        ("old.ppt", None, "old.pptx"),
        ("notes.markdown", None, "notes.md"),
        ("page", "html", "page.html"),
    ],
)
def test_office_names_and_aliases(filename: str, fmt: str | None, expected: str) -> None:
    assert sanitize_filename(filename, fmt) == expected


def test_the_model_is_told_about_every_office_format() -> None:
    schema = next(
        s for s in ToolRegistry().schemas(TOOL_NAMES) if s["function"]["name"] == "create_document"
    )["function"]
    words = schema["description"] + " ".join(
        p["description"] for p in schema["parameters"]["properties"].values()
    )
    for needle in (".docx", ".xlsx", ".pptx", ".csv", "Markdown table", "## heading", "# title"):
        assert needle.lower() in words.lower(), needle
    assert len(words) < 450  # read by small models: keep it short
    for word in ("Word", "Excel", "PowerPoint", "CSV", "create_document"):
        assert word in DOCUMENTS_SENTENCE
    assert {".xlsx", ".pptx"} <= documents.SAFE_TO_OPEN


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #

BOOK = """# Q3 workbook

Prepared by finance, from the ERP export.

## Sales

| Region | Units | Revenue | Share | Day | Code |
|---|---:|---:|---:|---|---|
| North | 1,200 | $12,000.50 | 45% | 2024-03-31 | 007 |
| South | 800 | $8,000 | 30.5% | 2024-04-01 14:30 | 0042 |
| **Total** | **2,000** | | 100% | not a date | 12345678901234567890 |

## Costs

| Item | Amount |
|---|---|
| Rent | 1500 |
| Power | 320.75 |
"""


def _cells(zf: zipfile.ZipFile, sheet: int) -> dict[str, ET.Element]:
    root = xml(zf, f"xl/worksheets/sheet{sheet}.xml")
    return {c.get("r"): c for c in root.iter(f"{S}c")}


def _strings(zf: zipfile.ZipFile) -> list[str]:
    return [
        "".join(t.text or "" for t in si.iter(f"{S}t"))
        for si in xml(zf, "xl/sharedStrings.xml").iter(f"{S}si")
    ]


def _text(zf: zipfile.ZipFile, cell: ET.Element) -> str:
    assert cell.get("t") == "s", cell.get("r")
    return _strings(zf)[int(cell.find(f"{S}v").text)]


def _format(zf: zipfile.ZipFile, cell: ET.Element) -> str:
    """The number format code a cell shows with."""
    styles = xml(zf, "xl/styles.xml")
    xf = styles.find(f"{S}cellXfs")[int(cell.get("s", "0"))]
    fmt_id = int(xf.get("numFmtId"))
    custom = {int(f.get("numFmtId")): f.get("formatCode") for f in styles.iter(f"{S}numFmt")}
    builtin = {0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%"}
    return custom.get(fmt_id) or builtin[fmt_id]


def _bold(zf: zipfile.ZipFile, cell: ET.Element) -> bool:
    styles = xml(zf, "xl/styles.xml")
    xf = styles.find(f"{S}cellXfs")[int(cell.get("s", "0"))]
    font = styles.find(f"{S}fonts")[int(xf.get("fontId"))]
    return font.find(f"{S}b") is not None


def test_xlsx_one_sheet_per_table_with_typed_cells(tmp_path) -> None:
    result = create_document("Q3", BOOK, "excel", folder=str(tmp_path))
    assert result.ok, result.content
    assert result.document["name"] == "Q3.xlsx" and result.document["kind"] == "xlsx"
    assert "2 sheets: Sales, Costs" in result.content and "download" in result.content
    zf = open_package((tmp_path / "Q3.xlsx").read_bytes())
    workbook = xml(zf, "xl/workbook.xml")
    assert [s.get("name") for s in workbook.iter(f"{S}sheet")] == ["Sales", "Costs"]
    # The prose paragraph is not a table, although it has a comma.
    assert len(list(workbook.iter(f"{S}sheet"))) == 2
    filters = [d.text for d in workbook.iter(f"{S}definedName")]
    assert filters == ["'Sales'!$A$1:$F$4", "'Costs'!$A$1:$B$3"]

    sheet = xml(zf, "xl/worksheets/sheet1.xml")
    pane = sheet.find(f"{S}sheetViews/{S}sheetView/{S}pane")
    assert (pane.get("ySplit"), pane.get("state"), pane.get("topLeftCell")) == ("1", "frozen", "A2")
    assert sheet.find(f"{S}autoFilter").get("ref") == "A1:F4"
    assert sheet.find(f"{S}dimension").get("ref") == "A1:F4"
    widths = [float(c.get("width")) for c in sheet.iter(f"{S}col")]
    assert len(widths) == 6 and all(8 <= w <= 60 for w in widths)

    cells = _cells(zf, 1)
    assert _text(zf, cells["A1"]) == "Region" and _bold(zf, cells["A1"])
    assert all(_bold(zf, cells[f"{c}1"]) for c in "ABCDEF")
    # Numbers are numbers, shown as written.
    assert cells["B2"].get("t") is None and cells["B2"].find(f"{S}v").text == "1200"
    assert _format(zf, cells["B2"]) == "#,##0"
    assert cells["C2"].find(f"{S}v").text == "12000.50"
    assert _format(zf, cells["C2"]) == '"$"#,##0.00'
    assert cells["D2"].find(f"{S}v").text == "0.45" and _format(zf, cells["D2"]) == "0%"
    assert cells["D3"].find(f"{S}v").text == "0.305" and _format(zf, cells["D3"]) == "0.0%"
    # ISO dates are dates.
    assert cells["E2"].find(f"{S}v").text == "45382" and _format(zf, cells["E2"]) == "yyyy-mm-dd"
    assert cells["E3"].find(f"{S}v").text.startswith("45383.604166")
    assert _format(zf, cells["E3"]) == "yyyy-mm-dd hh:mm"
    # Codes keep their leading zeros, long digit strings and words stay text.
    assert _text(zf, cells["F2"]) == "007" and _text(zf, cells["F3"]) == "0042"
    assert _text(zf, cells["F4"]) == "12345678901234567890"
    assert _text(zf, cells["E4"]) == "not a date"
    # A **bold** cell is bold, without the asterisks; empty cells are left out.
    assert _text(zf, cells["A4"]) == "Total" and _bold(zf, cells["A4"])
    assert cells["B4"].find(f"{S}v").text == "2000" and _bold(zf, cells["B4"])
    assert "C4" not in cells
    assert not _bold(zf, cells["A2"])
    costs = _cells(zf, 2)
    assert costs["B3"].find(f"{S}v").text == "320.75" and _format(zf, costs["B3"]) == "0.00"
    title = xml(zf, "docProps/core.xml").find("{http://purl.org/dc/elements/1.1/}title")
    assert title.text == "Q3"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("42", ("42", 0)),
        ("-7", ("-7", 0)),
        ("0.5", ("0.5", "0.0")),
        ("3.50", ("3.50", "0.00")),
        ("1,234", ("1234", "#,##0")),
        ("1,234,567.891", ("1234567.891", "#,##0.000")),
        ("12%", ("0.12", "0%")),
        ("-3.5%", ("-0.035", "0.0%")),
        ("100 %", ("1", "0%")),
        ("$9.99", ("9.99", '"$"#,##0.00')),
        ("-€1,000", ("-1000", '"€"#,##0')),
        ("£0.99", ("0.99", '"£"#,##0.00')),
        ("2024-03-31", ("45382", "yyyy-mm-dd")),
        ("2024-03-31T14:30:15", ("45382.6043402778", "yyyy-mm-dd hh:mm:ss")),
        ("007", None),
        ("+5", None),
        ("1e5", None),
        ("1.2.3", None),
        ("1,5", None),
        ("12345678901234567", None),
        ("2024-02-30", None),
        ("1899-12-31", None),
        ("N/A", None),
        ("", None),
    ],
)
def test_cell_values(raw: str, expected) -> None:
    assert cell_value(raw) == expected


def test_xlsx_from_csv_in_its_many_shapes() -> None:
    # Plain comma CSV (quoted commas too), as all of the content.
    book = markdown_to_xlsx('name,city,score\nAna,Lisboa,9.5\n"Smith, J",Porto,7\n')
    zf = open_package(book.data)
    assert book.sheets == ["Sheet1"]
    cells = _cells(zf, 1)
    assert _text(zf, cells["A3"]) == "Smith, J" and cells["C3"].find(f"{S}v").text == "7"
    # Semicolons (decimal commas stay text), tabs, and a ```csv block under a heading.
    assert markdown_to_xlsx("a;b\n1;2,5\n").sheets == ["Sheet1"]
    zf = open_package(markdown_to_xlsx("a;b\n1;2,5\n").data)
    assert _text(zf, _cells(zf, 1)["B2"]) == "2,5"
    assert markdown_to_xlsx("a\tb\n1\t2\n").sheets == ["Sheet1"]
    book = markdown_to_xlsx("## Scores\n\nSome words first.\n\n```csv\nn,v\nx,1\n```\n")
    assert book.sheets == ["Scores"]
    # A whole answer wrapped in one fence.
    assert markdown_to_xlsx("```\n| a | b |\n|---|---|\n| 1 | 2 |\n```").sheets == ["Sheet1"]


def test_xlsx_without_a_table_is_a_clear_tool_error(tmp_path) -> None:
    for content in ("Just a sentence, with a comma.", "# Title\n\n- a bullet\n- another"):
        result = create_document("data.xlsx", content, folder=str(tmp_path))
        assert result.ok is False and result.document is None
        assert "no table" in result.content and "Markdown table" in result.content
        assert "|---|---|" in result.content and "CSV" in result.content
        assert "create_document again" in result.content
    assert list(tmp_path.iterdir()) == []


def test_prose_next_to_a_real_table_is_not_a_table() -> None:
    content = "Source: ERP, export\nOwner: finance, Lisbon\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    tables = find_tables(parse(content))
    assert [t.source for t in tables] == ["markdown"]
    # Without a Markdown table, the same two lines would read as CSV.
    assert [
        t.source for t in find_tables(parse("Source: ERP, export\nOwner: finance, Lisbon"))
    ] == ["guess"]
    # A ```csv block is real data too: the prose next to it is not a sheet.
    content = "Here is the data, as requested\nSee below, thanks\n\n```csv\na,b\n1,2\n```"
    assert markdown_to_xlsx(content).sheets == ["Sheet1"]
    assert [t.source for t in find_tables(parse(content))] == ["fence"]


def test_a_whole_csv_fence_stays_csv() -> None:
    # One column, ragged rows and a first line starting with '#' are still CSV.
    for content, rows in (
        ("```csv\na\n1\n2\n```", [["a"], ["1"], ["2"]]),
        ("```csv\na,b\n1\n```", [["a", "b"], ["1", ""]]),
        ("```csv\n# count,name\n3,x\n```", [["# count", "name"], ["3", "x"]]),
    ):
        book = markdown_to_xlsx(content)
        zf = open_package(book.data)
        assert book.sheets == ["Sheet1"], content
        cells = _cells(zf, 1)
        got = [
            [
                (_text(zf, cells[f"{c}{r}"]) if cells[f"{c}{r}"].get("t") == "s"
                 else cells[f"{c}{r}"].find(f"{S}v").text)
                if f"{c}{r}" in cells else ""
                for c in "AB"[: len(rows[0])]
            ]
            for r in range(1, len(rows) + 1)
        ]  # fmt: skip
        assert got == rows, content


def test_bracket_heavy_lines_stay_fast() -> None:
    import time

    start = time.perf_counter()
    markdown_to_docx("[" * 40_000 + "\n" + "[a](" * 10_000)
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        ("Sales 2024/25: by region?", "Sales 2024-25 by region"),
        ("[Draft] *Q3*", "Draft Q3"),
        ("'Quoted'", "Quoted"),
        ("A" * 30 + "'quoted", "A" * 30),  # no apostrophe left at the end by the cut
        ("x" * 40, "x" * 31),
        ("", "Sheet1"),
        ("   ", "Sheet1"),
        ("History", "History (2)"),
    ],
)
def test_sheet_names_follow_excel_rules(wanted: str, expected: str) -> None:
    assert sheet_name(wanted, set(), "Sheet1") == expected


def test_sheet_names_are_unique_and_short() -> None:
    taken: set[str] = set()
    names = [sheet_name(n, taken, f"Sheet{i}") for i, n in enumerate(["Data", "DATA", "data"], 1)]
    assert names == ["Data", "DATA (2)", "data (3)"]
    long = sheet_name("A very long heading that goes on and on", taken, "x")
    again = sheet_name("A very long heading that goes on and on", taken, "x")
    assert len(long) == 31 and len(again) == 31 and again.endswith(" (2)")
    assert [column_letter(i) for i in (0, 25, 26, 701, 702)] == ["A", "Z", "AA", "ZZ", "AAA"]


def test_xlsx_unicode_long_cells_and_control_characters() -> None:
    content = (
        "| Name | City | Note |\n|---|---|---|\n| Zoë | 東京 | a\x0bb 🎉 |\n| x | y | "
        + ("z" * 40_000)
        + " |\n"
    )
    zf = open_package(markdown_to_xlsx(content).data)
    strings = _strings(zf)
    assert {"Zoë", "東京", "ab 🎉"} <= set(strings)
    assert max(len(s) for s in strings) == doc_xlsx.MAX_CELL_CHARS
    widths = [float(c.get("width")) for c in xml(zf, "xl/worksheets/sheet1.xml").iter(f"{S}col")]
    assert widths[2] == 60  # capped


def test_xlsx_caps_rows_and_columns_and_says_so(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doc_xlsx, "MAX_ROWS", 5)
    monkeypatch.setattr(doc_xlsx, "MAX_COLUMNS", 3)
    rows = "\n".join(f"| {i} | {i} | {i} | {i} |" for i in range(20))
    content = f"## Big\n\n| a | b | c | d |\n|---|---|---|---|\n{rows}\n"
    result = create_document("big.xlsx", content, folder=str(tmp_path))
    assert result.ok
    assert "Note: Sheet 'Big' was cut to its first 5 rows." in result.content
    assert "first 3 columns" in result.content
    zf = open_package((tmp_path / "big.xlsx").read_bytes())
    sheet = xml(zf, "xl/worksheets/sheet1.xml")
    assert len(sheet.findall(f"{S}sheetData/{S}row")) == 6  # header + 5
    assert sheet.find(f"{S}autoFilter").get("ref") == "A1:C6"


def test_xlsx_caps_the_sheet_count(monkeypatch) -> None:
    monkeypatch.setattr(doc_xlsx, "MAX_SHEETS", 2)
    content = "\n\n".join(f"## T{i}\n\n| a |\n|---|\n| {i} |" for i in range(4))
    book = markdown_to_xlsx(content)
    assert book.sheets == ["T0", "T1"]
    assert book.notes == ["Only the first 2 of 4 tables were written."]
    open_package(book.data)


# --------------------------------------------------------------------------- #
# PowerPoint
# --------------------------------------------------------------------------- #

DECK = """# Quarterly Review

Finance & Operations
October 2026

## Highlights

- Revenue **up 12%**
- Costs held *flat*
  - Energy down 4%
    - Mostly lighting
- See [the report](https://example.com/q3?x=1&y=é) and [this](javascript:alert(1))

## Plan

3. Hire
4. Launch
   1. Pilot

---

### Numbers

Sales by region:

| Region | Revenue |
|---|---:|
| North | 12 |
| South | 9 |

---

```python
print("hi")
```

> A quote.
"""


def _slide_texts(zf: zipfile.ZipFile, n: int) -> list[str]:
    root = xml(zf, f"ppt/slides/slide{n}.xml")
    return ["".join(t.text or "" for t in p.iter(f"{A}t")) for p in root.iter(f"{A}p")]


def _layout_of(zf: zipfile.ZipFile, n: int) -> str:
    rels = xml(zf, f"ppt/slides/_rels/slide{n}.xml.rels")
    return next(
        r.get("Target")
        for r in rels.iter(f"{PR}Relationship")
        if r.get("Type").endswith("/slideLayout")
    )


def test_pptx_title_slide_bullets_numbers_tables_and_rules(tmp_path) -> None:
    result = create_document("review.pptx", DECK, folder=str(tmp_path))
    assert result.ok, result.content
    assert "5 slides" in result.content and result.document["kind"] == "pptx"
    zf = open_package((tmp_path / "review.pptx").read_bytes())
    pres = xml(zf, "ppt/presentation.xml")
    size = pres.find(f"{P}sldSz")
    assert (size.get("cx"), size.get("cy")) == ("12192000", "6858000")  # 16:9
    assert len(pres.findall(f"{P}sldIdLst/{P}sldId")) == 5
    for part in (
        "ppt/slideMasters/slideMaster1.xml",
        "ppt/slideLayouts/slideLayout1.xml",
        "ppt/slideLayouts/slideLayout2.xml",
        "ppt/theme/theme1.xml",
    ):
        assert part in zf.namelist()

    # 1: the title slide, the paragraph under the # heading as its subtitle.
    assert _layout_of(zf, 1).endswith("slideLayout1.xml")
    assert _slide_texts(zf, 1) == ["Quarterly Review", "Finance & OperationsOctober 2026"]
    title_ph = [ph.get("type") for ph in xml(zf, "ppt/slides/slide1.xml").iter(f"{P}ph")]
    assert title_ph == ["ctrTitle", "subTitle"]

    # 2: bullets on three levels; bold/italic runs; a live https link, a refused script link.
    assert _layout_of(zf, 2).endswith("slideLayout2.xml")
    slide2 = xml(zf, "ppt/slides/slide2.xml")
    body = [sp for sp in slide2.iter(f"{P}sp") if sp.find(f".//{P}ph").get("idx") == "1"][0]
    levels = [
        (p.find(f"{A}pPr").get("lvl") if p.find(f"{A}pPr") is not None else "0")
        for p in body.iter(f"{A}p")
    ]
    assert levels == ["0", "0", "1", "2", "0"]
    assert any(r.get("b") == "1" for r in slide2.iter(f"{A}rPr"))
    assert any(r.get("i") == "1" for r in slide2.iter(f"{A}rPr"))
    texts = _slide_texts(zf, 2)
    assert texts[0] == "Highlights"
    assert "this (javascript:alert(1))" in texts[-1]
    links = [h.get(R_ID) for h in slide2.iter(f"{A}hlinkClick")]
    assert len(links) == 1
    rels = {
        r.get("Id"): r
        for r in xml(zf, "ppt/slides/_rels/slide2.xml.rels").iter(f"{PR}Relationship")
    }
    assert rels[links[0]].get("TargetMode") == "External"
    assert rels[links[0]].get("Target") == "https://example.com/q3?x=1&y=%C3%A9"

    # 3: a numbered list that starts at 3, with a nested level.
    autonum = [a for a in xml(zf, "ppt/slides/slide3.xml").iter(f"{A}buAutoNum")]
    assert [a.get("type") for a in autonum] == ["arabicPeriod", "arabicPeriod", "alphaLcPeriod"]
    assert autonum[0].get("startAt") == "3" and autonum[1].get("startAt") == "3"

    # 4: after ---, the ### heading is the title; text above a real table.
    assert _slide_texts(zf, 4)[0] == "Numbers"
    table = xml(zf, "ppt/slides/slide4.xml").find(f".//{A}tbl")
    assert table is not None and len(table.findall(f"{A}tr")) == 3
    assert len(table.findall(f"{A}tblGrid/{A}gridCol")) == 2
    # The column of numbers is right-aligned (its header too); the names are not.
    aligns = [
        [
            (p.find(f"{A}pPr").get("algn") if p.find(f"{A}pPr") is not None else None)
            for p in tr.iter(f"{A}p")
        ]
        for tr in table.findall(f"{A}tr")
    ]
    assert aligns == [[None, "r"], [None, "r"], [None, "r"]]
    assert "Sales by region:" in _slide_texts(zf, 4)

    # 5: no title (a rule then code), code in Consolas, the quote in italics.
    slide5 = xml(zf, "ppt/slides/slide5.xml")
    assert [ph.get("type") for ph in slide5.iter(f"{P}ph")] == [None]  # body only
    assert any(f.get("typeface") == "Consolas" for f in slide5.iter(f"{A}latin"))
    assert 'print("hi")' in _slide_texts(zf, 5)


def test_pptx_long_text_shrinks_then_continues(tmp_path) -> None:
    some = "## Notes\n\n" + "\n".join(f"- point {i} " + "word " * 12 for i in range(8))
    zf = open_package(markdown_to_pptx(some).data)
    fits = [f.get("fontScale") for f in xml(zf, "ppt/slides/slide1.xml").iter(f"{A}normAutofit")]
    assert fits[0] is None and int(fits[1]) < 100_000  # the title fits; the body is shrunk
    many = "## Notes\n\n" + "\n".join(f"- point {i} " + "word " * 12 for i in range(40))
    deck = markdown_to_pptx(many)
    zf = open_package(deck.data)
    assert deck.slides > 2
    titles = [_slide_texts(zf, n)[0] for n in range(1, deck.slides + 1)]
    assert titles[0] == "Notes" and set(titles[1:]) == {"Notes (cont.)"}
    bullets = sum(len(_slide_texts(zf, n)) - 1 for n in range(1, deck.slides + 1))
    assert bullets == 40  # nothing lost


def test_pptx_long_table_continues_with_its_header() -> None:
    rows = "\n".join(f"| r{i} | {i} |" for i in range(30))
    deck = markdown_to_pptx(f"## Data\n\n| Name | Value |\n|---|---|\n{rows}\n")
    zf = open_package(deck.data)
    assert deck.slides == 3  # 12 + 12 + 6 data rows
    for n in range(1, 4):
        table = xml(zf, f"ppt/slides/slide{n}.xml").find(f".//{A}tbl")
        first_row = "".join(t.text for t in table.find(f"{A}tr").iter(f"{A}t"))
        assert first_row == "NameValue"
    assert len(xml(zf, "ppt/slides/slide3.xml").find(f".//{A}tbl").findall(f"{A}tr")) == 7


def test_pptx_without_headings_and_with_nothing(tmp_path) -> None:
    deck = markdown_to_pptx("- one\n- two")
    zf = open_package(deck.data)
    assert deck.slides == 1
    assert [ph.get("type") for ph in xml(zf, "ppt/slides/slide1.xml").iter(f"{P}ph")] == [None]
    result = create_document("empty.pptx", "---\n\n---\n", folder=str(tmp_path))
    assert result.ok is False and "nothing to put on a slide" in result.content
    assert "## Slide title" in result.content
    assert list(tmp_path.iterdir()) == []
    # Headings with nothing under them: a title-only slide each; a later "#" with nothing
    # under it is a section slide (the title layout).
    deck = markdown_to_pptx("## Questions?\n\n## Thank you\n\n# Part two\n\n## Next\n\n- x")
    zf = open_package(deck.data)
    assert deck.slides == 4
    assert [_slide_texts(zf, n) for n in (1, 2, 3)] == [["Questions?"], ["Thank you"], ["Part two"]]
    assert _layout_of(zf, 3).endswith("slideLayout1.xml")
    # A very long title is shrunk to its smallest step rather than left to overflow.
    zf = open_package(markdown_to_pptx("## " + "long title words " * 30 + "\n\n- x").data)
    fits = [f.get("fontScale") for f in xml(zf, "ppt/slides/slide1.xml").iter(f"{A}normAutofit")]
    assert fits[0] == "55000"


def test_pptx_unicode_control_characters_and_slide_cap(monkeypatch) -> None:
    deck = markdown_to_pptx('# Ünïcødé 東京\n\n## Ψ\n\n- a\x0bb 🎉\n- <tag> & "quotes"')
    zf = open_package(deck.data)
    assert _slide_texts(zf, 1)[0] == "Ünïcødé 東京"
    assert _slide_texts(zf, 2)[1:] == ["ab 🎉", '<tag> & "quotes"']
    # A list that starts past PowerPoint's limit for startAt (1-32767) is clamped.
    zf = open_package(markdown_to_pptx("100000. a\n100001. b").data)
    starts = {a.get("startAt") for a in xml(zf, "ppt/slides/slide1.xml").iter(f"{A}buAutoNum")}
    assert starts == {"32767"}
    # A wide table gets smaller text.
    wide = "| " + " | ".join(f"c{i}" for i in range(12)) + " |\n|" + "---|" * 12 + "\n"
    zf = open_package(markdown_to_pptx(wide + "| " + " | ".join("x" * 12) + " |").data)
    sizes = {r.get("sz") for r in xml(zf, "ppt/slides/slide1.xml").iter(f"{A}rPr")}
    assert sizes == {"1000"}
    monkeypatch.setattr(doc_pptx, "MAX_SLIDES", 2)
    deck = markdown_to_pptx("\n".join(f"## S{i}\n- x" for i in range(5)))
    assert deck.slides == 2 and deck.notes == ["Only the first 2 of 5 slides were written."]
    open_package(deck.data)


# --------------------------------------------------------------------------- #
# CSV, TSV and HTML
# --------------------------------------------------------------------------- #


def test_csv_from_a_markdown_table(tmp_path) -> None:
    content = (
        "Here is the data:\n\n| Name | City, Country | Note |\n|---|---|---|\n"
        '| **Zoë** | Porto, PT | says "hi" |\n| A \\| B | Kraków | `x` |\n'
    )
    result = create_document("people.csv", content, folder=str(tmp_path))
    assert result.ok and "Note:" not in result.content
    data = (tmp_path / "people.csv").read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert data[3:].decode("utf-8") == (
        'Name,"City, Country",Note\r\nZoë,"Porto, PT","says ""hi"""\r\nA | B,Kraków,x\r\n'
    )


def test_csv_keeps_the_first_of_several_tables_and_says_so(tmp_path) -> None:
    content = "| a |\n|---|\n| 1 |\n\n| b |\n|---|\n| 2 |\n"
    result = create_document("t.csv", content, folder=str(tmp_path))
    assert "Only the first of 2 tables was written" in result.content and ".xlsx" in result.content
    assert (tmp_path / "t.csv").read_bytes()[3:] == b"a\r\n1\r\n"


def test_csv_from_fences_and_plain_text(tmp_path) -> None:
    create_document("a.csv", "Data:\n\n```csv\nx,y\n1,2\n```\n", folder=str(tmp_path))
    assert (tmp_path / "a.csv").read_bytes()[3:] == b"x,y\r\n1,2\r\n"
    create_document("b.csv", "```csv\nx,y\n1,2\n```", folder=str(tmp_path))
    assert (tmp_path / "b.csv").read_bytes()[3:] == b"x,y\n1,2"  # kept as written
    create_document("c.tsv", "| a | b |\n|---|---|\n| 1 | 2 |", folder=str(tmp_path))
    assert (tmp_path / "c.tsv").read_bytes() == b"a\tb\r\n1\t2\r\n"  # no BOM for TSV


def test_html_from_markdown_is_a_standalone_page(tmp_path) -> None:
    content = (
        "# Report <draft>\n\nIntro with **bold**, *italic*, `code` and [a link](https://example.com)"
        " and [bad](javascript:alert(1)).\n\n- one\n  - nested\n- two\n\n5. five\n6. six\n\n"
        "| A | B |\n|---|---|\n| 1<br>2 | <script>x</script> |\n\n```js\nif (a < b) {}\n```\n\n"
        "> quoted\n\n---\n"
    )
    result = create_document("report.html", content, folder=str(tmp_path))
    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert result.ok and page.startswith("<!DOCTYPE html>")
    assert "<title>Report &lt;draft&gt;</title>" in page
    assert "<h1>Report &lt;draft&gt;</h1>" in page
    assert "<strong>bold</strong>" in page and "<em>italic</em>" in page
    assert '<a href="https://example.com">a link</a>' in page
    assert "javascript:" not in page.split("</style>")[1].split("bad (")[0]
    assert "bad (javascript:alert(1))" in page
    assert "<ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>" in page.replace("\n", "")
    assert '<ol start="5"><li>five</li><li>six</li></ol>' in page.replace("\n", "")
    assert "<th>A</th>" in page and "<td>1<br>2</td>" in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert '<pre><code class="language-js">if (a &lt; b) {}</code></pre>' in page
    assert "<blockquote><p>quoted</p></blockquote>" in page and "<hr>" in page
    assert "<script" not in page.lower().replace("&lt;script", "")


def test_html_content_is_kept_as_written(tmp_path) -> None:
    raw = "<!doctype html><html><body><p>Hi</p></body></html>"
    create_document("page.htm", raw, folder=str(tmp_path))
    # Kept as written, plus the one Content-Security-Policy line.
    written = (tmp_path / "page.htm").read_text(encoding="utf-8")
    assert written.replace(f"\n{CSP_META}\n", "") == raw
    assert markdown_to_html("plain").count("<p>plain</p>") == 1


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #


def test_docx_cell_line_breaks_and_a_fenced_answer() -> None:
    zf = open_package(markdown_to_docx("| A | B |\n|---|---|\n| 1<br>2 | x |\n"))
    cell = xml(zf, "word/document.xml").findall(f".//{W}tc")[2]
    assert len(cell.findall(f".//{W}br")) == 1
    assert [t.text for t in cell.iter(f"{W}t")] == ["1", "2"]
    # A model that wraps the whole document in ```markdown still gets headings.
    result_bytes = documents.build_document(".docx", "```markdown\n# Title\n\nText\n```")[0]
    doc = xml(open_package(result_bytes), "word/document.xml")
    styles = [s.get(f"{W}val") for s in doc.iter(f"{W}pStyle")]
    assert styles == ["Heading1"]


def test_unfence_only_unwraps_whole_fences() -> None:
    assert unfence("```csv\na,b\n```") == "a,b"
    assert unfence("text\n```csv\na,b\n```") == "text\n```csv\na,b\n```"
    md_only = frozenset({"markdown", "md", ""})
    assert unfence("```python\nx = 1\n```", md_only) == "```python\nx = 1\n```"
    assert unfence("```md\n# T\n```", md_only) == "# T"


def test_every_office_file_has_a_valid_package(tmp_path) -> None:
    for name, content in (
        ("a.docx", "# T\n\n| a | b |\n|---|---|\n| 1 | 2 |"),
        ("b.xlsx", "| a | b |\n|---|---|\n| 1 | 2 |"),
        ("c.pptx", "# T\n\n## S\n\n- x"),
    ):
        result = create_document(name, content, folder=str(tmp_path))
        assert result.ok, result.content
        open_package(Path(result.document["path"]).read_bytes())
    assert re.search(r"\(\d+(\.\d)? (KB|bytes)", result.content)
