"""tools.documents: the create_document tool, .docx building, and the open/reveal checks."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from chatforge.attachments import extract_text
from chatforge.errors import AppError
from chatforge.tools import documents
from chatforge.tools.documents import (
    create_document,
    markdown_to_docx,
    resolve_document,
    sanitize_filename,
)
from chatforge.tools.registry import TOOL_NAMES, ToolRegistry

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# --------------------------------------------------------------------------- #
# Names and folder
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("filename", "fmt", "expected"),
    [
        ("report.docx", None, "report.docx"),
        ("report", "docx", "report.docx"),
        ("report", "Word", "report.docx"),
        ("notes", None, "notes.md"),
        ("Data.CSV", None, "Data.csv"),
        ("..\\..\\Windows\\evil.txt", None, "evil.txt"),
        ("/etc/passwd.md", None, "passwd.md"),
        ('a<b>:c"d|e?f*g.txt', None, "a_b__c_d_e_f_g.txt"),
        ("CON.txt", None, "_CON.txt"),
        ("lpt1", None, "_lpt1.md"),
        ("summary. ", "txt", "summary.txt"),
        ("report.pdf", "docx", "report.docx"),
        ("plan.v2", None, "plan.v2.md"),
        ("", None, "document.md"),
        ("...", "csv", "document.csv"),
    ],
)
def test_sanitize_filename(filename: str, fmt: str | None, expected: str) -> None:
    assert sanitize_filename(filename, fmt) == expected


def test_sanitize_limits_length_and_refuses_unknown_types() -> None:
    long = sanitize_filename("x" * 300 + ".docx")
    assert len(long) == 120 and long.endswith(".docx")
    with pytest.raises(ValueError, match=r"cannot create \.pdf files"):
        sanitize_filename("paper.pdf")
    with pytest.raises(ValueError, match=r"\.exe"):
        sanitize_filename("tool.exe")


def test_documents_dir_setting_and_default(tmp_path, monkeypatch) -> None:
    assert documents.documents_dir(str(tmp_path / "out")) == tmp_path / "out"
    monkeypatch.setenv("CHATFORGE_TEST_DOCS", str(tmp_path))
    assert documents.documents_dir(
        "%CHATFORGE_TEST_DOCS%\\x" if os.name == "nt" else "$CHATFORGE_TEST_DOCS/x"
    ) == (tmp_path / "x")
    import platformdirs

    monkeypatch.setattr(platformdirs, "user_documents_dir", lambda: str(tmp_path / "Docs"))
    assert documents.documents_dir("") == tmp_path / "Docs" / "ChatForge"
    monkeypatch.setattr(platformdirs, "user_documents_dir", lambda: "")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    assert documents.documents_dir("  ") == tmp_path / "me" / "Documents" / "ChatForge"


# --------------------------------------------------------------------------- #
# create_document
# --------------------------------------------------------------------------- #


def test_text_document_is_saved_and_described(tmp_path) -> None:
    folder = tmp_path / "docs"  # created on demand
    result = create_document("plan.md", "# Plan\n\n- step one\n", folder=str(folder))
    assert result.ok is True
    path = folder / "plan.md"
    assert path.read_bytes() == b"# Plan\n\n- step one\n"
    assert result.document == {"name": "plan.md", "path": str(path), "size": 19, "kind": "text"}
    assert "Saved plan.md (19 bytes)" in result.content
    assert result.summary == "Saved plan.md"


def test_never_overwrites(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
    first = create_document("notes.txt", "one", folder=str(tmp_path))
    second = create_document("notes.txt", "two", folder=str(tmp_path))
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert Path(first.document["path"]).name == "notes (2).txt"
    assert Path(second.document["path"]).name == "notes (3).txt"
    assert (tmp_path / "notes (3).txt").read_text(encoding="utf-8") == "two"


def test_csv_gets_a_bom_for_excel(tmp_path) -> None:
    result = create_document("table.csv", "name,city\nZoë,Porto\n", folder=str(tmp_path))
    data = (tmp_path / "table.csv").read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and data[3:].decode("utf-8") == "name,city\nZoë,Porto\n"
    assert result.document["kind"] == "data" and result.document["size"] == len(data)


def test_refusals_come_back_as_results(tmp_path, monkeypatch) -> None:
    r = create_document("x.pdf", "text", folder=str(tmp_path))
    assert r.ok is False and "cannot create .pdf" in r.content and r.document is None
    r = create_document("x.md", "   ", folder=str(tmp_path))
    assert r.ok is False and "empty" in r.content
    monkeypatch.setattr(documents, "MAX_DOCUMENT_BYTES", 10)
    r = create_document("x.md", "x" * 11, folder=str(tmp_path))
    assert r.ok is False and "5 MB" in r.content
    assert list(tmp_path.iterdir()) == []


def test_docx_is_a_valid_word_file(tmp_path) -> None:
    markdown = (
        "# Quarterly report\n\n"
        "Sales were **up 12%** in *Q3*, see `data.csv` and [the site](https://example.com).\n"
        "A second line.\n\n"
        "## Highlights\n\n"
        "- North & South <both>\n"
        "  - nested point\n"
        "1. first\n"
        "2. second\n\n"
        "### Table\n\n"
        "| Region | Total |\n|---|---:|\n| North | 12 |\n| South | 9 |\n\n"
        "```\ncode line\n```\n"
    )
    result = create_document("Report", markdown, "docx", folder=str(tmp_path))
    assert result.ok and result.document["kind"] == "docx"
    data = (tmp_path / "Report.docx").read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert {
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/_rels/document.xml.rels",
            "word/styles.xml",
            "word/numbering.xml",
        } <= names
        for name in names:
            ET.fromstring(zf.read(name))  # every part is well-formed XML
        doc = ET.fromstring(zf.read("word/document.xml"))
        types = zf.read("[Content_Types].xml").decode()
    assert "wordprocessingml.document.main+xml" in types
    paragraphs = doc.findall(f".//{W}body/{W}p")
    styles = [
        (
            p.find(f"{W}pPr/{W}pStyle").get(f"{W}val")
            if p.find(f"{W}pPr/{W}pStyle") is not None
            else None
        )
        for p in paragraphs
    ]
    assert styles[:3] == ["Heading1", None, "Heading2"]
    assert "Heading3" in styles and "Code" in styles
    bold = [r for r in doc.iter(f"{W}r") if r.find(f"{W}rPr/{W}b") is not None]
    assert any("".join(t.text or "" for t in r.iter(f"{W}t")) == "up 12%" for r in bold)
    numbered = [p for p in paragraphs if p.find(f"{W}pPr/{W}numPr") is not None]
    assert len(numbered) == 4  # two bullets (one nested) and two numbered items
    table = doc.find(f".//{W}tbl")
    assert table is not None and len(table.findall(f"{W}tr")) == 3
    # Read back with the attachment reader: the text survives the round trip.
    text = extract_text("Report.docx", data).text
    assert "# Quarterly report" in text and "North & South <both>" in text
    assert "the site (https://example.com)" in text and "Region | Total" in text


def test_numbered_lists_restart() -> None:
    data = markdown_to_docx("1. a\n2. b\n\nText\n\n1. c\n")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        doc = ET.fromstring(zf.read("word/document.xml"))
        numbering = ET.fromstring(zf.read("word/numbering.xml"))
    ids = [n.get(f"{W}val") for n in doc.iter(f"{W}numId")]
    assert ids == ["2", "2", "3"]  # a new list after the paragraph
    assert len(numbering.findall(f"{W}num")) == 3  # bullets + two numbered lists


def test_control_characters_never_break_the_xml() -> None:
    data = markdown_to_docx("bad \x0b char and \ud800 lone surrogate\tand a tab")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        ET.fromstring(zf.read("word/document.xml"))


# --------------------------------------------------------------------------- #
# Through the registry
# --------------------------------------------------------------------------- #


async def test_registry_tool_saves_into_the_configured_folder(tmp_path) -> None:
    registry = ToolRegistry({"documents_dir": str(tmp_path)})
    assert "create_document" in TOOL_NAMES
    assert "create_document" not in [
        s["function"]["name"] for s in registry.schemas(TOOL_NAMES, local=True)
    ]
    r = await registry.call(
        "create_document",
        json.dumps({"filename": "hello", "content": "Hi there", "format": "txt"}),
        enabled=["create_document"],
        max_chars=1500,
    )
    assert r.ok and (tmp_path / "hello.txt").read_text(encoding="utf-8") == "Hi there"
    assert r.document["name"] == "hello.txt"
    for args in ({"content": "x"}, {"filename": "a.md"}, {"filename": " ", "content": "x"}):
        bad = await registry.call(
            "create_document", json.dumps(args), enabled=["create_document"], max_chars=1500
        )
        assert bad.ok is False and "required" in bad.content


# --------------------------------------------------------------------------- #
# open / reveal checks
# --------------------------------------------------------------------------- #


def test_resolve_document_only_inside_the_folder(tmp_path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    inside = folder / "a.md"
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    assert resolve_document(folder, str(inside)) == inside.resolve()
    assert resolve_document(folder, "a.md") == inside.resolve()  # relative to the folder
    for bad in (str(outside), str(folder / ".." / "secret.txt"), "../secret.txt", str(folder)):
        with pytest.raises(AppError) as ei:
            resolve_document(folder, bad)
        assert ei.value.code == "bad_request", bad
    with pytest.raises(AppError) as ei:
        resolve_document(folder, str(folder / "gone.md"))
    assert ei.value.code == "not_found"
    with pytest.raises(AppError):
        resolve_document(folder, "")


def test_resolve_document_follows_links_out_of_the_folder(tmp_path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("x", encoding="utf-8")
    link = folder / "innocent.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin rights here")
    with pytest.raises(AppError) as ei:
        resolve_document(folder, str(link))
    assert ei.value.code == "bad_request"


def test_open_and_reveal_use_safe_commands(tmp_path, monkeypatch) -> None:
    started: list[tuple] = []
    popen: list = []
    monkeypatch.setattr(documents, "_WINDOWS", True)
    monkeypatch.setattr(os, "startfile", lambda *a: started.append(a), raising=False)
    monkeypatch.setattr(documents.subprocess, "Popen", lambda cmd, *a, **k: popen.append(cmd))
    doc = tmp_path / "report.docx"
    script = tmp_path / "tool.js"
    documents.open_document(doc)
    documents.open_document(script)  # a script must open in Notepad, never run
    documents.reveal_document(doc)
    assert started == [(str(doc),), ("notepad.exe", "open", f'"{script}"')]
    assert popen == [f'explorer /select,"{doc}"']
