"""Hardening of the file side: script names, formula cells, HTML policy, atomic writes,
XML and zip limits, parse time budget, and fixed-host API redirects."""

from __future__ import annotations

import errno
import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from chatforge import attachments as att
from chatforge.attachments import AttachmentError, extract_text
from chatforge.tools import documents
from chatforge.tools.doc_html import CSP_META, markdown_to_html, with_csp
from chatforge.tools.documents import create_document, sanitize_filename
from chatforge.tools.webapi import ApiError, get_json

# --------------------------------------------------------------------------- #
# 1. Script types are saved as text; bidi controls leave the name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ext", ["js", "ps1", "py", "sh"])
def test_script_gets_txt_appended_and_content_is_unchanged(tmp_path: Path, ext: str) -> None:
    code = "console.log('hi')\n"
    result = create_document(f"report.{ext}", code, folder=str(tmp_path))
    assert result.ok
    saved = tmp_path / f"report.{ext}.txt"
    assert saved.read_text(encoding="utf-8") == code
    assert not (tmp_path / f"report.{ext}").exists()
    assert result.document["name"] == saved.name and result.document["path"] == str(saved)
    assert f"report.{ext}.txt" in result.content and ".txt added" in result.content


def test_script_by_format_argument_and_taken_names(tmp_path: Path) -> None:
    assert create_document("tool", "x = 1\n", "python", folder=str(tmp_path)).ok
    again = create_document("tool.py", "x = 2\n", folder=str(tmp_path))
    assert (tmp_path / "tool.py.txt").read_text() == "x = 1\n"
    assert again.document["name"] == "tool.py (2).txt"


def test_other_text_types_are_not_renamed(tmp_path: Path) -> None:
    create_document("notes.md", "# a\n", folder=str(tmp_path))
    create_document("page.css", "a{}\n", folder=str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.md", "page.css"]


def test_bidi_controls_are_stripped_from_names() -> None:
    name = "invoice‮gpj.exe‬⁦x⁩.txt"
    assert sanitize_filename(name) == "invoicegpj.exex.txt"
    assert sanitize_filename("a‎b‏.md") == "ab.md"
    for code in [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]:
        assert chr(code) not in sanitize_filename(f"r{chr(code)}eport.txt")


# --------------------------------------------------------------------------- #
# 2. CSV/TSV formula injection
# --------------------------------------------------------------------------- #

TABLE = (
    '| Name | Total |\n|---|---|\n| =HYPERLINK("http://x") | +1 |\n| -2 | @SUM(A1) |\n| ok | 5 |\n'
)


def test_markdown_table_cells_are_defused_in_csv(tmp_path: Path) -> None:
    create_document("t.csv", TABLE, folder=str(tmp_path))
    text = (tmp_path / "t.csv").read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == "Name,Total"
    assert lines[1] == '"\'=HYPERLINK(""http://x"")",\'+1'
    assert lines[2] == "'-2,'@SUM(A1)"
    assert lines[3] == "ok,5"


def test_table_cells_are_defused_in_tsv(tmp_path: Path) -> None:
    create_document("t.tsv", TABLE, folder=str(tmp_path))
    rows = [ln.split("\t") for ln in (tmp_path / "t.tsv").read_text().splitlines()]
    assert rows[2] == ["'-2", "'@SUM(A1)"] and rows[3] == ["ok", "5"]


def test_plain_csv_text_is_defused_cell_by_cell(tmp_path: Path) -> None:
    create_document("p.csv", 'a,b\n=1+1,"x,y"\n"\t5",ok\n', folder=str(tmp_path))
    assert (tmp_path / "p.csv").read_text(encoding="utf-8-sig") == ("a,b\n'=1+1,\"x,y\"\n'\t5,ok\n")


def test_plain_csv_without_formulas_is_written_as_it_is(tmp_path: Path) -> None:
    text = 'a, b\r\n"1, 2",x\r\n'
    create_document("p.csv", text, folder=str(tmp_path))
    assert (tmp_path / "p.csv").read_bytes() == b"\xef\xbb\xbf" + text.encode()


def test_xlsx_is_unaffected_by_formula_defusing(tmp_path: Path) -> None:
    result = create_document("t.xlsx", TABLE, folder=str(tmp_path))
    assert result.ok
    with zipfile.ZipFile(tmp_path / "t.xlsx") as zf:
        shared = zf.read("xl/sharedStrings.xml").decode("utf-8")
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "'=" not in shared and "'+" not in shared and "'@" not in shared
    assert ">=HYPERLINK(" in shared and ">@SUM(A1)<" in shared
    assert "<f>" not in sheet  # still no formulas, just shared strings


# --------------------------------------------------------------------------- #
# 3. Content-Security-Policy in generated HTML
# --------------------------------------------------------------------------- #

POLICY = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "style-src 'unsafe-inline'; img-src data:\">"
)


def test_policy_constant_is_the_agreed_one() -> None:
    assert CSP_META == POLICY


def test_converted_markdown_page_has_the_policy_once_in_head() -> None:
    page = markdown_to_html("# Hello\n\ntext")
    assert page.count(POLICY) == 1
    assert page.index("<head>") < page.index(POLICY) < page.index("</head>")


@pytest.mark.parametrize("name", ["a.html", "a.htm"])
def test_markdown_written_as_html_has_the_policy(tmp_path: Path, name: str) -> None:
    create_document(name, "# T\n\nbody", folder=str(tmp_path))
    assert (tmp_path / name).read_text(encoding="utf-8").count(POLICY) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "<!doctype html><html><head><title>t</title></head><body><script>1</script></body></html>",
        "<p>fragment</p><script>alert(1)</script>",
        "<html><body onload=x()></body></html>",
        "<script>alert(1)</script><head><title>x</title></head>",
        '<html a=">"><script>alert(1)</script>',
    ],
)
def test_model_written_html_has_the_policy_before_any_content(tmp_path: Path, raw: str) -> None:
    create_document("m.html", raw, folder=str(tmp_path))
    page = (tmp_path / "m.html").read_text(encoding="utf-8")
    assert page.count(POLICY) == 1
    assert "<script" not in page[: page.index(POLICY)].lower()
    assert page.replace(f"\n{POLICY}\n", "") == raw


def test_policy_goes_inside_the_head_when_the_page_opens_one() -> None:
    page = with_csp("<!DOCTYPE html>\n<html lang='en'>\n<head>\n<title>t</title></head>")
    assert page.index("<head>") < page.index(POLICY) < page.index("<title>")


# --------------------------------------------------------------------------- #
# 4. Atomic document writes
# --------------------------------------------------------------------------- #


class _FullDisk:
    """A file object that takes a few bytes and then reports ENOSPC."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __enter__(self) -> _FullDisk:
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        self._real.write(data[:5])
        self._real.flush()
        raise OSError(errno.ENOSPC, "No space left on device")


def test_disk_full_leaves_no_partial_file(tmp_path: Path, monkeypatch) -> None:
    real_open = open

    def fake_open(path: Any, mode: str = "r", *a: Any, **kw: Any) -> Any:
        fh = real_open(path, mode, *a, **kw)
        return _FullDisk(fh) if "b" in mode and "x" in mode else fh

    monkeypatch.setattr(documents, "open", fake_open, raising=False)
    result = create_document("big.txt", "hello world\n" * 10, folder=str(tmp_path))
    assert not result.ok and "could not be saved" in result.content
    assert list(tmp_path.iterdir()) == []  # no big.txt, no leftover temp file


def test_data_appears_under_the_real_name_only_when_complete(tmp_path: Path, monkeypatch) -> None:
    seen: list[list[str]] = []
    real_link = documents.os.link

    def spy(src: Any, dst: Any, **kw: Any) -> None:
        seen.append(sorted(p.name for p in tmp_path.iterdir()))
        assert Path(src).read_bytes() == b"payload"  # complete before it gets its name
        real_link(src, dst, **kw)

    monkeypatch.setattr(documents.os, "link", spy)
    path = documents._write_new(tmp_path, "a.txt", b"payload")
    assert path.read_bytes() == b"payload"
    assert len(seen[0]) == 1 and seen[0][0].startswith(".") and seen[0][0].endswith(".part")
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]  # the temp name is gone


def test_existing_file_is_never_replaced(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"mine")
    path = documents._write_new(tmp_path, "a.txt", b"new")
    assert path.name == "a (2).txt"
    assert (tmp_path / "a.txt").read_bytes() == b"mine"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a (2).txt", "a.txt"]


def test_without_hard_links_the_name_is_claimed_then_renamed(tmp_path: Path, monkeypatch) -> None:
    def no_link(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.EPERM, "links not supported")

    monkeypatch.setattr(documents.os, "link", no_link)
    (tmp_path / "a.txt").write_bytes(b"mine")
    path = documents._write_new(tmp_path, "a.txt", b"new")
    assert path.name == "a (2).txt" and path.read_bytes() == b"new"
    assert (tmp_path / "a.txt").read_bytes() == b"mine"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a (2).txt", "a.txt"]


def test_failed_rename_leaves_nothing_behind(tmp_path: Path, monkeypatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(documents.os, "link", boom)
    monkeypatch.setattr(documents.os, "replace", boom)
    with pytest.raises(OSError):
        documents._write_new(tmp_path, "a.txt", b"x")
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# 5. XML and zip hardening
# --------------------------------------------------------------------------- #

CT = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types"/>'
)
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(body: str, *, prefix: str = "", extra: int = 0) -> bytes:
    xml = f'{prefix}<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CT)
        zf.writestr("word/document.xml", xml)
        for i in range(extra):
            zf.writestr(f"junk/{i}.txt", "")
    return buf.getvalue()


PARA = "<w:p><w:r><w:t>Hello</w:t></w:r></w:p>"


def test_a_normal_docx_still_reads() -> None:
    assert extract_text("a.docx", _docx(PARA)).text.strip() == "Hello"


@pytest.mark.parametrize(
    "prefix",
    [
        '<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaa">]>',
        '<?xml version="1.0"?><!DOCTYPE root>',
        '<?xml version="1.0"?><!ENTITY x "y">',
        '<?xml version="1.0"?>' + " " * 3000 + "<!DOCTYPE w>",
    ],
)
def test_parts_with_a_doctype_or_entity_are_refused(prefix: str) -> None:
    with pytest.raises(AttachmentError, match="not supported"):
        extract_text("a.docx", _docx(PARA, prefix=prefix))


def test_utf16_parts_with_a_doctype_are_refused() -> None:
    xml = f'<?xml version="1.0"?><!DOCTYPE w><w:document xmlns:w="{W_NS}"/>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml.encode("utf-16"))
    with pytest.raises(AttachmentError, match="not supported"):
        extract_text("a.docx", buf.getvalue())


def test_a_doctype_after_a_long_padding_is_refused_too() -> None:
    # Whitespace may precede a DOCTYPE, so a scan of the first 4 KB alone would miss it.
    prefix = '<?xml version="1.0"?>' + " " * 5000 + "<!DOCTYPE w [<!ENTITY a 'b'>]>"
    with pytest.raises(AttachmentError, match="not supported"):
        extract_text("a.docx", _docx(PARA, prefix=prefix))


def test_a_huge_prolog_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(att, "MAX_PROLOG_BYTES", 8192)
    with pytest.raises(AttachmentError, match="not supported"):
        extract_text("a.docx", _docx(PARA, prefix="<!-- " + "x" * 20_000 + " -->"))


def test_text_that_mentions_doctype_after_the_root_is_fine() -> None:
    para = "<w:p><w:r><w:t>&lt;!DOCTYPE html&gt;</w:t></w:r></w:p>"
    assert extract_text("a.docx", _docx(para)).text.strip() == "<!DOCTYPE html>"


def test_namelist_is_called_once_per_archive(monkeypatch) -> None:
    calls = 0
    real = zipfile.ZipFile.namelist

    def counting(self: zipfile.ZipFile) -> list[str]:
        nonlocal calls
        calls += 1
        return real(self)

    monkeypatch.setattr(zipfile.ZipFile, "namelist", counting)
    out = extract_text("a.docx", _docx(PARA))
    assert out.text.strip() == "Hello" and calls == 1


def test_too_many_zip_entries_are_refused(monkeypatch) -> None:
    monkeypatch.setattr(att, "MAX_ZIP_ENTRIES", 5)
    assert extract_text("a.docx", _docx(PARA, extra=2)).text.strip() == "Hello"
    with pytest.raises(AttachmentError, match="too complex"):
        extract_text("a.docx", _docx(PARA, extra=10))


def test_the_default_entry_cap() -> None:
    assert att.MAX_ZIP_ENTRIES == 20_000


# --------------------------------------------------------------------------- #
# 6. Parse time budget
# --------------------------------------------------------------------------- #


class _Clock:
    """A clock that moves ``step`` seconds on every reading."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_small_files_are_not_marked(monkeypatch) -> None:
    monkeypatch.setattr(att, "_clock", _Clock(0.0))
    out = extract_text("a.docx", _docx(PARA * 3))
    assert not out.truncated and out.warning is None


def test_a_slow_file_stops_at_the_deadline_with_a_note(monkeypatch) -> None:
    monkeypatch.setattr(att, "_clock", _Clock(1.0))  # 45 readings and the time is up
    out = extract_text("a.docx", _docx(PARA * 500))
    assert out.truncated and "Reading stopped after 45 seconds" in (out.warning or "")
    assert 0 < out.text.count("Hello") < 500


def test_time_up_before_any_text_is_an_error(monkeypatch) -> None:
    monkeypatch.setattr(att, "PARSE_TIMEOUT_S", -1.0)
    with pytest.raises(AttachmentError, match="took too long"):
        extract_text("a.docx", _docx(PARA))


def test_the_budget_is_per_call(monkeypatch) -> None:
    monkeypatch.setattr(att, "_clock", _Clock(1.0))
    extract_text("a.docx", _docx(PARA * 500))
    monkeypatch.setattr(att, "_clock", _Clock(0.0))
    assert not extract_text("a.docx", _docx(PARA * 500)).truncated


class _Page:
    def __init__(self, text: str, clock: _Clock | None = None) -> None:
        self._text, self._clock = text, clock

    def extract_text(self) -> str:
        if self._clock:
            self._clock.now += 20
        return self._text


class _Reader:
    def __init__(self, pages: list[_Page]) -> None:
        self.pages = pages
        self.is_encrypted = False


def _fake_pypdf(monkeypatch, pages: list[_Page]) -> list[int]:
    import sys
    import types

    asked: list[int] = []
    for p in pages:
        original = p.extract_text
        p.extract_text = lambda f=original: (asked.append(1), f())[1]  # type: ignore[method-assign]
    module = types.SimpleNamespace(PdfReader=lambda _fh: _Reader(pages))
    monkeypatch.setitem(sys.modules, "pypdf", module)
    return asked


def test_pdf_is_capped_at_500_pages(monkeypatch) -> None:
    asked = _fake_pypdf(monkeypatch, [_Page(f"page text {i}") for i in range(620)])
    out = extract_text("a.pdf", b"%PDF-1.4 fake")
    assert len(asked) == 500
    assert out.truncated and "first 500 of 620 pages" in (out.warning or "")
    assert "## Page 500" in out.text and "## Page 501" not in out.text


def test_pdf_stops_at_the_deadline(monkeypatch) -> None:
    clock = _Clock(0.0)
    monkeypatch.setattr(att, "_clock", clock)
    asked = _fake_pypdf(monkeypatch, [_Page(f"text {i}", clock) for i in range(50)])
    out = extract_text("a.pdf", b"%PDF-1.4 fake")
    assert len(asked) == 3  # 20 s each: 60 s have passed once the third page is read
    assert out.truncated and "Reading stopped after 45 seconds" in (out.warning or "")
    assert "## Page 2" in out.text and "## Page 3" not in out.text


def test_small_pdf_output_is_unchanged(monkeypatch) -> None:
    _fake_pypdf(monkeypatch, [_Page("one"), _Page(""), _Page("three")])
    out = extract_text("a.pdf", b"%PDF-1.4 fake")
    assert out.text == "## Page 1\none\n\n## Page 3\nthree"
    assert (
        not out.truncated
        and out.warning == "1 of 3 pages have no text (scanned pages are not read)."
    )


# --------------------------------------------------------------------------- #
# 7. Fixed-host APIs do not follow redirects
# --------------------------------------------------------------------------- #


async def test_redirects_are_an_error_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.example":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/x"})
        return httpx.Response(200, json={"leaked": True})

    with pytest.raises(ApiError, match="redirected") as info:
        await get_json("https://api.example/v1", transport=httpx.MockTransport(handler))
    assert info.value.status == 302
    assert seen == ["https://api.example/v1"]


async def test_plain_replies_still_work() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": 1}))
    assert await get_json("https://api.example/v1", transport=transport) == {"ok": 1}
