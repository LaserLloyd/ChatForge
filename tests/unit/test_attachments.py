"""aichat.attachments: text extraction per file type, limits, the pending store, blocks."""

from __future__ import annotations

import base64
import codecs
import io
import sys
import types
import zipfile

import pytest

from aichat import attachments as att
from aichat.attachments import (
    AttachmentError,
    AttachmentStore,
    Extracted,
    extract_text,
    file_block,
    kind_for,
    user_content,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def make_zip(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in parts.items():
            zf.writestr(name, text)
    return buf.getvalue()


def rels(*targets: tuple[str, str]) -> str:
    items = "".join(f'<Relationship Id="{i}" Type="x" Target="{t}"/>' for i, t in targets)
    return f'<Relationships xmlns="{PKG_NS}">{items}</Relationships>'


# --------------------------------------------------------------------------- #
# Tiny Office files
# --------------------------------------------------------------------------- #


def tiny_docx() -> bytes:
    def p(text: str, style: str | None = None, numbered: bool = False) -> str:
        props = ""
        if style or numbered:
            props = "<w:pPr>"
            props += f'<w:pStyle w:val="{style}"/>' if style else ""
            props += (
                '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if numbered else ""
            )
            props += "</w:pPr>"
        return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"

    body = (
        # A German Word file: the style id is localised, its name is not.
        p("Quarterly report", "berschrift1")
        + '<w:p><w:r><w:t xml:space="preserve">Sales were </w:t></w:r>'
        "<w:r><w:rPr><w:b/></w:rPr><w:t>up</w:t></w:r><w:r><w:tab/><w:t>a lot</w:t></w:r>"
        "<w:del><w:r><w:delText>deleted words</w:delText></w:r></w:del></w:p>"
        + p("First point", numbered=True)
        + "<w:tbl><w:tr><w:tc>"
        + p("Region")
        + "</w:tc><w:tc>"
        + p("Total")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + p("North")
        + "</w:tc><w:tc>"
        + p("12")
        + "</w:tc></w:tr></w:tbl>"
        # A text box: Word stores it twice (Choice + Fallback); it must appear once.
        + '<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><w:txbxContent>'
        + p("Boxed note")
        + "</w:txbxContent></w:drawing></mc:Choice><mc:Fallback><w:pict><w:txbxContent>"
        + p("Boxed note")
        + "</w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>"
        + p("The end")
    )
    document = (
        f'<w:document xmlns:w="{W_NS}" xmlns:mc="{MC_NS}"><w:body>{body}</w:body></w:document>'
    )
    styles = (
        f'<w:styles xmlns:w="{W_NS}"><w:style w:type="paragraph" w:styleId="berschrift1">'
        '<w:name w:val="heading 1"/></w:style></w:styles>'
    )
    return make_zip({"word/document.xml": document, "word/styles.xml": styles})


def tiny_xlsx() -> bytes:
    workbook = (
        f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>'
        '<sheet name="Data" sheetId="1" r:id="rId1"/>'
        '<sheet name="Empty" sheetId="2" r:id="rId2"/>'
        "</sheets></workbook>"
    )
    shared = (
        f'<sst xmlns="{S_NS}"><si><t>Name</t></si>'
        "<si><r><t>A</t></r><r><t>nn</t></r><rPh><t>phonetic</t></rPh></si>"
        "<si><t>a,b</t></si></sst>"
    )
    styles = (
        f'<styleSheet xmlns="{S_NS}"><numFmts count="1">'
        '<numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>'
        '<cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs>'
        "</styleSheet>"
    )
    sheet1 = (
        f'<worksheet xmlns="{S_NS}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>Score</t></is></c>'
        '<c r="C1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>3.5</v></c>'
        '<c r="C2" s="1"><v>44927</v></c><c r="D2" t="b"><v>1</v></c>'
        '<c r="E2" s="2"><v>44927.5</v></c></row>'
        '<row r="4"><c r="A4" t="str"><f>A1</f><v>x</v></c><c r="C4"><v>0.30000000000000004</v></c></row>'
        "</sheetData></worksheet>"
    )
    sheet2 = f'<worksheet xmlns="{S_NS}"><sheetData/></worksheet>'
    return make_zip(
        {
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": rels(
                ("rId1", "worksheets/sheet1.xml"), ("rId2", "/xl/worksheets/sheet2.xml")
            ),
            "xl/sharedStrings.xml": shared,
            "xl/styles.xml": styles,
            "xl/worksheets/sheet1.xml": sheet1,
            "xl/worksheets/sheet2.xml": sheet2,
        }
    )


def tiny_pptx() -> bytes:
    def slide(*paragraphs: str) -> str:
        ps = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paragraphs)
        return (
            f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}"><p:cSld><p:spTree><p:sp><p:txBody>'
            f"{ps}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )

    presentation = (
        f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}"><p:sldIdLst>'
        '<p:sldId id="256" r:id="rId3"/><p:sldId id="257" r:id="rId2"/>'
        "</p:sldIdLst></p:presentation>"
    )
    return make_zip(
        {
            "ppt/presentation.xml": presentation,
            "ppt/_rels/presentation.xml.rels": rels(
                ("rId2", "slides/slide1.xml"), ("rId3", "slides/slide2.xml")
            ),
            "ppt/slides/slide1.xml": slide("Second slide title", "Body text"),
            "ppt/slides/slide2.xml": slide("Opening"),
        }
    )


# --------------------------------------------------------------------------- #
# Plain text and code
# --------------------------------------------------------------------------- #


def test_kinds_by_extension() -> None:
    assert kind_for("notes.TXT") == "text" and kind_for("README.md") == "text"
    assert kind_for("app.py") == "code" and kind_for("run.ps1") == "code"
    assert kind_for("data.csv") == "data" and kind_for("cfg.yaml") == "data"
    assert kind_for("page.htm") == "html"
    assert kind_for("a.docx") == "docx" and kind_for("b.xlsx") == "xlsx"
    assert kind_for("c.pptx") == "pptx" and kind_for("d.pdf") == "pdf"
    assert kind_for(".gitignore") == "code" and kind_for("Makefile") == "code"
    assert kind_for("photo.png") is None and kind_for("LICENSE") is None


@pytest.mark.parametrize(
    "data",
    [
        "café\r\nnaïve".encode(),
        codecs.BOM_UTF8 + "café\r\nnaïve".encode(),
        "café\r\nnaïve".encode("utf-16"),  # with a BOM
        "café\r\nnaïve".encode("utf-16-le"),  # without one
        "café\r\nnaïve".encode("cp1252"),
    ],
)
def test_text_encodings(data: bytes) -> None:
    out = extract_text("notes.txt", data)
    assert out == Extracted("notes.txt", "text", "café\nnaïve", 10)


def test_code_and_unknown_text_files() -> None:
    out = extract_text("C:\\Users\\me\\src\\app.py", b"def f():\n    return 1\n")
    assert (out.name, out.kind, out.text) == ("app.py", "code", "def f():\n    return 1")
    assert extract_text("LICENSE", b"MIT License").kind == "text"
    assert extract_text("data.csv", b"a,b\n1,2").text == "a,b\n1,2"


def test_html_keeps_visible_text_only() -> None:
    page = b"<html><head><title>Menu</title><script>var x=1</script></head><body><p>Soup</p></body></html>"
    out = extract_text("menu.html", page)
    assert out.kind == "html"
    assert "Menu" in out.text and "Soup" in out.text and "var x" not in out.text


# --------------------------------------------------------------------------- #
# Office
# --------------------------------------------------------------------------- #


def test_docx_paragraphs_headings_lists_and_tables() -> None:
    out = extract_text("report.docx", tiny_docx())
    assert out.kind == "docx"
    lines = out.text.split("\n")
    assert lines[0] == "# Quarterly report"
    assert lines[1] == "Sales were up\ta lot"  # runs joined; deleted text left out
    assert "- First point" in lines
    assert "Region | Total" in lines and "North | 12" in lines
    assert out.text.count("Boxed note") == 1
    assert lines[-1] == "The end"


def test_xlsx_sheets_as_csv() -> None:
    out = extract_text("scores.xlsx", tiny_xlsx())
    assert out.kind == "xlsx"
    assert out.text.split("\n") == [
        "## Sheet: Data",
        'Name,Score,"a,b"',
        "Ann,3.5,2023-01-01,TRUE,2023-01-01 12:00:00",
        "x,,0.3",
        "",
        "## Sheet: Empty",
        "(empty)",
    ]


def test_pptx_slides_in_presentation_order() -> None:
    out = extract_text("deck.pptx", tiny_pptx())
    assert out.kind == "pptx"
    assert out.text == "## Slide 1\nOpening\n\n## Slide 2\nSecond slide title\nBody text"


@pytest.mark.parametrize(
    ("name", "what"),
    [("a.docx", "Word document"), ("b.xlsx", "Excel workbook"), ("c.pptx", "PowerPoint file")],
)
def test_damaged_office_files(name: str, what: str) -> None:
    with pytest.raises(AttachmentError) as ei:
        extract_text(name, b"this is not a zip file")
    assert what in ei.value.message and "password-protected" in ei.value.message
    with pytest.raises(AttachmentError):
        extract_text(name, make_zip({"unrelated.xml": "<x/>"}))


def test_zip_part_unpacking_is_capped(monkeypatch) -> None:
    monkeypatch.setattr(att, "MAX_PART_BYTES", 200)
    with pytest.raises(AttachmentError) as ei:
        extract_text("big.docx", tiny_docx())
    assert "too much data" in ei.value.message


def test_all_parts_together_are_capped(monkeypatch) -> None:
    # Every slide is under the per-part cap; together they are over the file's budget.
    deck = tiny_pptx()
    with zipfile.ZipFile(io.BytesIO(deck)) as zf:
        sizes = {i.filename: i.file_size for i in zf.infolist()}
    monkeypatch.setattr(att, "MAX_PART_BYTES", max(sizes.values()))
    monkeypatch.setattr(att, "MAX_UNPACKED_BYTES", sum(sizes.values()) - 1)
    with pytest.raises(AttachmentError) as ei:
        extract_text("deck.pptx", deck)
    assert ei.value.message == att.TOO_MUCH_DATA
    monkeypatch.setattr(att, "MAX_UNPACKED_BYTES", sum(sizes.values()))
    assert extract_text("deck.pptx", deck).text.startswith("## Slide 1\nOpening")


def _docx_body(body: str) -> bytes:
    document = f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    return make_zip({"word/document.xml": document})


def test_finished_elements_are_dropped_as_they_are_read() -> None:
    # 20,000 empty elements outside any paragraph used to stay in the tree to the end.
    data = _docx_body("<w:p><w:r><w:t>Hi</w:t></w:r></w:p>" + "<w:b/>" * 20_000)
    with att._open_zip(data) as zf:
        root = None
        widest = 0
        for _, el in att._iterparse(zf, "word/document.xml", ("start", "end")):
            root = el if root is None else root
            body = root[0] if len(root) else None
            widest = max(widest, len(body) if body is not None else 0)
    # A batch waiting to be dropped, plus what the parser read ahead (16 KB at a time).
    assert widest < 5000, widest
    assert extract_text("a.docx", data).text == "Hi"


def test_many_paragraphs_rows_and_a_nested_table_survive_the_dropping() -> None:
    paragraphs = "".join(f"<w:p><w:r><w:t>Line {i}</w:t></w:r></w:p>" for i in range(700))
    cell = "<w:tc><w:p><w:r><w:t>{}</w:t></w:r></w:p></w:tc>"
    inner = f"<w:tbl><w:tr>{cell.format('in1')}{cell.format('in2')}</w:tr></w:tbl>"
    nested = f"<w:tbl><w:tr>{cell.format('Out')}<w:tc>{inner}</w:tc></w:tr></w:tbl>"
    text = extract_text("long.docx", _docx_body(paragraphs + nested)).text
    assert text.split("\n")[:700] == [f"Line {i}" for i in range(700)]
    assert "Out | in1 | in2" in text

    rows = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 701))
    sheet = f'<worksheet xmlns="{S_NS}"><sheetData>{rows}</sheetData></worksheet>'
    out = extract_text("rows.xlsx", make_zip({"xl/worksheets/sheet1.xml": sheet}))
    assert out.text.split("\n")[1:701] == [str(i) for i in range(1, 701)]


@pytest.mark.parametrize(
    ("limit", "value", "body"),
    [
        ("MAX_ELEMENTS", 1000, "<w:b/>" * 2000),
        ("MAX_KEPT_ELEMENTS", 100, "<w:p><w:r>" + "<w:b/>" * 200 + "<w:t>x</w:t></w:r></w:p>"),
        ("MAX_DEPTH", 20, "<w:x>" * 30 + "</w:x>" * 30),
    ],
)
def test_too_complex_office_files_are_refused(monkeypatch, limit, value, body) -> None:
    data = _docx_body("<w:p><w:r><w:t>Hi</w:t></w:r></w:p>" + body)
    monkeypatch.setattr(att, limit, value)
    with pytest.raises(AttachmentError) as ei:
        extract_text("complex.docx", data)
    assert ei.value.message == "The file is too complex to read."
    monkeypatch.setattr(att, limit, value * 3)
    assert extract_text("complex.docx", data).text.startswith("Hi")


def _xlsx_cell(ref: str) -> bytes:
    sheet = (
        f'<worksheet xmlns="{S_NS}"><sheetData><row r="1"><c r="{ref}" t="inlineStr">'
        "<is><t>x</t></is></c></row></sheetData></worksheet>"
    )
    return make_zip({"xl/worksheets/sheet1.xml": sheet})


def test_xlsx_columns_stop_at_xfd() -> None:
    # XFD is Excel's last column: its row is 16,384 fields wide, and nothing is wider.
    out = extract_text("wide.xlsx", _xlsx_cell("XFD1"))
    assert out.text.split("\n")[1] == "," * 16_383 + "x"
    for ref in ("XFE1", "AAAA1", "ZZZZZ1", "Z" * 10_000 + "1"):
        with pytest.raises(AttachmentError) as ei:
            extract_text("bad.xlsx", _xlsx_cell(ref))
        assert "not a real Excel workbook" in ei.value.message
    # Cells without a reference count on from the last one, and stop there too.
    cells = '<c r="XFC1"><v>1</v></c><c><v>2</v></c><c><v>3</v></c>'
    sheet = f'<worksheet xmlns="{S_NS}"><sheetData><row r="1">{cells}</row></sheetData></worksheet>'
    with pytest.raises(AttachmentError):
        extract_text("bad.xlsx", make_zip({"xl/worksheets/sheet1.xml": sheet}))


def test_xlsx_1904_dates() -> None:
    workbook = (
        f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><workbookPr date1904="1"/><sheets>'
        '<sheet name="Mac" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    styles = (
        f'<styleSheet xmlns="{S_NS}"><cellStyleXfs count="1"><xf numFmtId="14"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>'
    )
    sheet = (
        f'<worksheet xmlns="{S_NS}"><sheetData><row r="1">'
        '<c r="A1" s="1"><v>0</v></c><c r="B1" s="0"><v>0</v></c></row></sheetData></worksheet>'
    )
    data = make_zip(
        {
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": rels(("rId1", "worksheets/sheet1.xml")),
            "xl/styles.xml": styles,
            "xl/worksheets/sheet1.xml": sheet,
        }
    )
    assert extract_text("mac.xlsx", data).text.split("\n")[:2] == ["## Sheet: Mac", "1904-01-01,0"]


# --------------------------------------------------------------------------- #
# PDF (pypdf is optional)
# --------------------------------------------------------------------------- #


def test_pdf_without_pypdf_says_how_to_add_it(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)  # import pypdf -> ImportError
    with pytest.raises(AttachmentError) as ei:
        extract_text("paper.pdf", b"%PDF-1.7")
    assert ei.value.message == "Reading PDFs needs the pypdf package: py -3.12 -m uv add pypdf"
    assert ei.value.code == "unsupported"


def _fake_pypdf(pages: list[str], *, encrypted: bool = False, password_ok: bool = True):
    class Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class PdfReader:
        def __init__(self, stream) -> None:
            assert stream.read(4) == b"%PDF"
            self.is_encrypted = encrypted
            self.pages = [Page(t) for t in pages]

        def decrypt(self, password: str) -> int:
            return 1 if password_ok else 0

    return types.SimpleNamespace(PdfReader=PdfReader)


def test_pdf_with_pypdf(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["Page one text", "", "Page three"]))
    out = extract_text("paper.pdf", b"%PDF-1.7 ...")
    assert out.kind == "pdf"
    assert out.text == "## Page 1\nPage one text\n\n## Page 3\nPage three"
    assert out.warning == "1 of 3 pages have no text (scanned pages are not read)."


def test_pdf_scanned_or_locked(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["", ""]))
    with pytest.raises(AttachmentError) as ei:
        extract_text("scan.pdf", b"%PDF-1.7")
    assert "no text" in ei.value.message
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["x"], encrypted=True, password_ok=False))
    with pytest.raises(AttachmentError) as ei:
        extract_text("locked.pdf", b"%PDF-1.7")
    assert "password-protected" in ei.value.message


# --------------------------------------------------------------------------- #
# Refusals and limits
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("photo.PNG", b"\x89PNG\r\n\x1a\n", "Images are not supported yet"),
        ("old.doc", b"\xd0\xcf\x11\xe0", "Save it as .docx in Word"),
        ("budget.xls", b"\xd0\xcf\x11\xe0", "Save it as .xlsx in Excel"),
        ("setup.exe", b"MZ\x90\x00", ".exe files are not supported yet"),
        ("blob", bytes(range(256)) * 4, "This type of file is not supported yet"),
        ("words", b"\x00\x01\x02\x03\x00\xff" * 10, "This type of file is not supported yet"),
    ],
)
def test_unsupported_files(name: str, data: bytes, expected: str) -> None:
    with pytest.raises(AttachmentError) as ei:
        extract_text(name, data)
    assert expected in ei.value.message
    assert ei.value.code == "unsupported"


def test_binary_content_in_a_text_extension_is_refused() -> None:
    with pytest.raises(AttachmentError) as ei:
        extract_text("notes.txt", b"\x00\x01\x02\x03\xff\x00\x00\x07" * 20)
    assert "not text" in ei.value.message


def test_empty_file_is_refused() -> None:
    with pytest.raises(AttachmentError) as ei:
        extract_text("empty.txt", b"\r\n \n")
    assert ei.value.message == "There is no text in this file."


def test_size_limit() -> None:
    with pytest.raises(AttachmentError) as ei:
        extract_text("huge.txt", b"a" * (att.MAX_FILE_BYTES + 1))
    assert ei.value.code == "too_large" and "20 MB" in ei.value.message


def test_text_is_capped_and_marked_truncated() -> None:
    out = extract_text("long.txt", b"abcdefghij" * 500, max_chars=1000)
    assert out.truncated is True and out.chars == 1000 and len(out.text) == 1000
    assert out.warning == "Only the first 1,000 characters are used."
    # The Office readers stop early on long files too.
    rows = "".join(
        f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>row {i}</t></is></c></row>'
        for i in range(1, 2000)
    )
    sheet = f'<worksheet xmlns="{S_NS}"><sheetData>{rows}</sheetData></worksheet>'
    big = make_zip({"xl/worksheets/sheet1.xml": sheet})
    out = extract_text("big.xlsx", big, max_chars=1000)
    assert out.truncated and out.chars == 1000 and out.text.startswith("## Sheet: Sheet1\nrow 1\n")


def test_read_path_and_base64(tmp_path, monkeypatch) -> None:
    f = tmp_path / "notes.md"
    f.write_bytes(b"# Hi\nthere")
    out, size = att.read_path(f)
    assert (out.name, out.kind, out.text, size) == ("notes.md", "text", "# Hi\nthere", 10)
    with pytest.raises(AttachmentError) as ei:
        att.read_path(tmp_path / "missing.txt")
    assert ei.value.code == "not_found"

    encoded = base64.b64encode(b"hello").decode()
    assert att.decode_base64(encoded) == b"hello"
    assert att.decode_base64(f"data:text/plain;base64,{encoded}") == b"hello"
    with pytest.raises(AttachmentError):
        att.decode_base64("not base64!!")
    monkeypatch.setattr(att, "MAX_FILE_BYTES", 8)
    with pytest.raises(AttachmentError) as ei:
        att.decode_base64(base64.b64encode(b"x" * 9).decode())
    assert ei.value.code == "too_large"
    with pytest.raises(AttachmentError) as ei:
        att.read_path(f)  # 10 bytes > 8, refused before reading
    assert ei.value.code == "too_large"


def test_dialog_filter_is_valid_for_pywebview() -> None:
    from webview.util import parse_file_type

    for entry in att.DIALOG_FILE_TYPES:
        parse_file_type(entry)  # raises ValueError when malformed
    assert "*.docx" in att.DIALOG_FILE_TYPES[0] and att.DIALOG_FILE_TYPES[-1] == "All files (*.*)"


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def _file(name: str = "a.txt", text: str = "hello") -> Extracted:
    return Extracted(name, "text", text, len(text))


def test_store_ids_are_random_and_views_complete() -> None:
    store = AttachmentStore()
    a = store.add(_file("a.txt"), 5)
    b = store.add(_file("b.txt"), 7)
    assert a.id != b.id and a.id.startswith("att_") and len(a.id) >= 20
    assert a.view() == {
        "id": a.id,
        "name": "a.txt",
        "kind": "text",
        "chars": 5,
        "size": 5,
        "truncated": False,
        "warning": None,
    }
    assert [f.name for f in store.peek([b.id, a.id])] == ["b.txt", "a.txt"]
    assert len(store) == 2  # peek leaves them
    with pytest.raises(AttachmentError) as ei:
        store.peek([a.id, "att_guess"])
    assert ei.value.code == "not_found"
    store.discard([a.id])
    assert a.id not in store and b.id in store
    assert store.remove(b.id) is True and store.remove(b.id) is False


def test_store_forgets_the_oldest_past_its_limit() -> None:
    store = AttachmentStore(max_items=3)
    ids = [store.add(_file(f"{i}.txt"), 1).id for i in range(5)]
    assert len(store) == 3
    assert ids[0] not in store and ids[1] not in store and ids[4] in store


# --------------------------------------------------------------------------- #
# What the model sees
# --------------------------------------------------------------------------- #


def test_file_blocks() -> None:
    files = [att.as_record(_file("report.docx", "line one\nline two"))]
    assert user_content("Summarize", files) == (
        'Summarize\n\n<file name="report.docx">\nline one\nline two\n</file>'
    )
    assert user_content("", files).startswith('<file name="report.docx">')
    assert file_block('a "b".txt', "x") == '\n\n<file name="a &quot;b&quot;.txt">\nx\n</file>'
    cut = user_content("Q", files, [4], att.NOTE_CUT_LOCAL)
    assert cut == f'Q\n\n<file name="report.docx">\nline\n{att.NOTE_CUT_LOCAL}\n</file>'
    capped = att.as_record(Extracted("big.txt", "text", "abc", 3, truncated=True))
    assert "[file truncated: only the first 3 characters were read]" in user_content("Q", [capped])
