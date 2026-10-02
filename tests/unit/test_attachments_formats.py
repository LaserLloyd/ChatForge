"""chatforge.attachments: OpenDocument, RTF, old binary Office files (read here, or by
Office itself through fakes of its COM objects), e-mail (.eml, .msg) and the dialog filter."""

from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
import threading
import types
import zipfile
from datetime import datetime
from typing import Any

import pytest

from chatforge import attachments as att
from chatforge.attachments import AttachmentError, extract_text, kind_for

OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
ODF_NS = (
    f'xmlns:office="{OFFICE_NS}" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
)
MANIFEST_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"


@pytest.fixture(autouse=True)
def no_office(monkeypatch) -> None:
    """Never start the real Office from a test (the ``office`` fixture fakes one)."""
    monkeypatch.setattr(att, "_office_installed", lambda prog_id: False)


def make_zip(parts: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# OpenDocument
# --------------------------------------------------------------------------- #


def odf(body: str, *, encrypted: bool = False, kind: str = "text") -> bytes:
    content = (
        f"<office:document-content {ODF_NS}><office:body><office:{kind}>{body}"
        f"</office:{kind}></office:body></office:document-content>"
    )
    entry = '<manifest:encryption-data manifest:checksum="x"/>' if encrypted else ""
    manifest = (
        f'<manifest:manifest xmlns:manifest="{MANIFEST_NS}"><manifest:file-entry '
        f'manifest:full-path="content.xml">{entry}</manifest:file-entry></manifest:manifest>'
    )
    return make_zip(
        {
            "mimetype": f"application/vnd.oasis.opendocument.{kind}",
            "content.xml": content,
            "META-INF/manifest.xml": manifest,
        }
    )


ODT_BODY = (
    "<text:tracked-changes><text:changed-region><text:deletion><text:p>Deleted words</text:p>"
    "</text:deletion></text:changed-region></text:tracked-changes>"
    '<text:h text:outline-level="2">Plan</text:h>'
    '<text:p>Spaces:<text:s text:c="3"/>three, tab<text:tab/>here, line<text:line-break/>'
    "break,\n   collapsed    white space.</text:p>"
    '<text:p>Claim<text:note text:note-class="footnote"><text:note-citation>1</text:note-citation>'
    "<text:note-body><text:p>Source text</text:p></text:note-body></text:note> stands."
    "<office:annotation><dc:creator>Reviewer</dc:creator><text:p>A comment</text:p>"
    "</office:annotation></text:p>"
    "<text:list><text:list-item><text:p>First</text:p><text:p>more of first</text:p>"
    "<text:list><text:list-item><text:p>Nested</text:p></text:list-item></text:list>"
    "</text:list-item><text:list-item><text:p>Second</text:p></text:list-item></text:list>"
    '<table:table table:name="T"><table:table-row>'
    "<table:table-cell><text:p>Name</text:p></table:table-cell>"
    "<table:table-cell><text:p>Qty</text:p></table:table-cell><table:table-cell/>"
    "</table:table-row><table:table-row>"
    "<table:table-cell><text:p>Bolt</text:p><text:p>M6</text:p></table:table-cell>"
    "<table:table-cell><text:p>5</text:p></table:table-cell></table:table-row></table:table>"
    "<text:p><draw:frame><draw:text-box><text:p>Boxed</text:p></draw:text-box></draw:frame>"
    "After box<draw:frame><draw:image><svg:title>Logo</svg:title><svg:desc>Alt text</svg:desc>"
    "</draw:image></draw:frame></text:p>"
)


def test_odt_headings_lists_tables_notes_and_text_boxes() -> None:
    out = extract_text("plan.odt", odf(ODT_BODY))
    assert out.kind == "odt"
    assert out.text.split("\n") == [
        "## Plan",
        "Spaces:   three, tab\there, line",
        "break, collapsed white space.",
        "Claim[1: Source text] stands.",
        "- First",
        "  more of first",
        "  - Nested",
        "- Second",
        "Name | Qty",
        "Bolt M6 | 5",
        "",
        "Boxed",
        "After box",
    ]
    for hidden in ("Deleted words", "A comment", "Reviewer", "Logo", "Alt text"):
        assert hidden not in out.text
    assert extract_text("plan.ott", odf(ODT_BODY)).text == out.text  # a template


ODS_BODY = (
    '<table:table table:name="Data">'
    '<table:table-column table:number-columns-repeated="1024"/>'
    '<table:table-row><table:table-cell office:value-type="string"><text:p>Name</text:p>'
    '</table:table-cell><table:table-cell office:value-type="string"><text:p>a,b</text:p>'
    '</table:table-cell><table:table-cell table:number-columns-repeated="1022"/></table:table-row>'
    "<table:table-row>"
    '<table:table-cell office:value-type="float" office:value="3.5"><text:p>3.50</text:p>'
    "</table:table-cell>"
    '<table:table-cell office:value-type="percentage" office:value="0.25"><text:p>25%</text:p>'
    "</table:table-cell>"
    '<table:table-cell office:value-type="date" office:date-value="2023-01-01">'
    "<text:p>01/01/23</text:p></table:table-cell>"
    '<table:table-cell office:value-type="date" office:date-value="2023-01-01T12:30:00"/>'
    '<table:table-cell office:value-type="time" office:time-value="PT12H05M09S"/>'
    '<table:table-cell office:value-type="boolean" office:boolean-value="true"/>'
    '<table:table-cell office:value-type="currency" office:value="0.30000000000000004">'
    "<text:p>0.30 EUR</text:p></table:table-cell></table:table-row>"
    '<table:table-row table:number-rows-repeated="2"><table:table-cell '
    'table:number-columns-repeated="2" office:value-type="string"><text:p>same</text:p>'
    "</table:table-cell></table:table-row>"
    '<table:table-row table:number-rows-repeated="1048000">'
    '<table:table-cell table:number-columns-repeated="1024"/></table:table-row>'
    '<table:table-row><table:covered-table-cell table:number-columns-repeated="3"/>'
    "<table:table-cell><text:p>far</text:p><office:annotation><text:p>a note</text:p>"
    "</office:annotation></table:table-cell></table:table-row>"
    "</table:table>"
    '<table:table table:name="Empty"><table:table-row table:number-rows-repeated="1048576">'
    '<table:table-cell table:number-columns-repeated="16384"/></table:table-row></table:table>'
)


def test_ods_sheets_as_csv_with_repeats() -> None:
    out = extract_text("scores.ods", odf(ODS_BODY, kind="spreadsheet"))
    assert out.kind == "ods"
    assert out.text.split("\n") == [
        "## Sheet: Data",
        'Name,"a,b"',
        "3.5,0.25,2023-01-01,2023-01-01 12:30:00,12:05:09,TRUE,0.3",
        "same,same",
        "same,same",
        ",,,far",
        "",
        "## Sheet: Empty",
        "(empty)",
    ]


def _ods_row(cells: str, rows: int = 1) -> bytes:
    body = (
        f'<table:table table:name="S"><table:table-row table:number-rows-repeated="{rows}">'
        f"{cells}</table:table-row></table:table>"
    )
    return odf(body, kind="spreadsheet")


def test_ods_repeats_are_capped() -> None:
    value = '<table:table-cell office:value-type="string"><text:p>x</text:p></table:table-cell>'
    wide = '<table:table-cell table:number-columns-repeated="{}" office:value-type="string">'
    wide += "<text:p>x</text:p></table:table-cell>"
    # 16,384 columns (Excel's and LibreOffice's last) are written out; past that is damage.
    assert extract_text("w.ods", _ods_row(wide.format(16_384))).text.split("\n")[1] == ",".join(
        ["x"] * 16_384
    )
    empty = '<table:table-cell table:number-columns-repeated="{}"/>'
    assert extract_text("w.ods", _ods_row(empty.format(16_383) + value)).text.endswith(",x")
    for cells in (wide.format(16_385), empty.format(16_384) + value):
        with pytest.raises(AttachmentError) as ei:
            extract_text("w.ods", _ods_row(cells))
        assert "not a real OpenDocument spreadsheet" in ei.value.message
    # A row repeated a million times stops at the character limit, not after a million.
    out = extract_text("tall.ods", _ods_row(value, rows=1_048_576), max_chars=1000)
    assert out.truncated and out.chars == 1000 and out.text.startswith("## Sheet: S\nx\nx\n")


def test_odp_slides_without_speaker_notes() -> None:
    body = (
        '<draw:page draw:name="p1"><draw:frame><draw:text-box><text:p>Opening</text:p>'
        "</draw:text-box></draw:frame><presentation:notes><draw:frame><draw:text-box>"
        "<text:p>Speaker notes</text:p></draw:text-box></draw:frame></presentation:notes>"
        "</draw:page>"
        '<draw:page draw:name="p2"><draw:frame><draw:text-box><text:list><text:list-item>'
        "<text:p>Point one</text:p></text:list-item><text:list-item><text:p>Point "
        "<text:span>two</text:span></text:p></text:list-item></text:list></draw:text-box>"
        '</draw:frame></draw:page><draw:page draw:name="p3"/>'
    )
    out = extract_text("deck.odp", odf(body, kind="presentation"))
    assert out.kind == "odp"
    assert out.text == (
        "## Slide 1\nOpening\n\n## Slide 2\nPoint one\nPoint two\n\n## Slide 3\n(no text)"
    )


@pytest.mark.parametrize(
    ("name", "what"),
    [
        ("a.odt", "OpenDocument text"),
        ("b.ods", "OpenDocument spreadsheet"),
        ("c.odp", "OpenDocument presentation"),
    ],
)
def test_damaged_and_locked_opendocument_files(name: str, what: str) -> None:
    for data in (b"not a zip", make_zip({"styles.xml": "<x/>"})):
        with pytest.raises(AttachmentError) as ei:
            extract_text(name, data)
        assert what in ei.value.message
    with pytest.raises(AttachmentError) as ei:
        extract_text(name, odf("<text:p>secret</text:p>", encrypted=True))
    assert ei.value.message == att.PASSWORD_PROTECTED


@pytest.mark.parametrize(
    ("limit", "value"), [("MAX_PART_BYTES", 300), ("MAX_ELEMENTS", 40), ("MAX_DEPTH", 5)]
)
def test_opendocument_limits(monkeypatch, limit: str, value: int) -> None:
    data = odf(ODT_BODY)
    monkeypatch.setattr(att, limit, value)
    with pytest.raises(AttachmentError) as ei:
        extract_text("big.odt", data)
    assert ei.value.message in (att.TOO_MUCH_DATA, att.TOO_COMPLEX)


# --------------------------------------------------------------------------- #
# RTF
# --------------------------------------------------------------------------- #

RTF = (
    rb"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\fswiss Arial;}{\f1\fcharset204 Times;}}"
    rb"{\colortbl;\red0\green0\blue0;}{\stylesheet{\s1 heading 1;}}"
    rb"{\info{\title Secret title}{\author Someone}}{\*\generator Writer;}" + b"\r\n"
    rb"\pard\outlinelevel0 Heading\par" + b"\r\n"
    rb"\pard Caf\'e9 \f1\'cf\'f0\'e8\'e2\'e5\'f2\f0  and \u8364? and {\uc2\u20013\'d6\'d0} end\par"
    rb"{\field{\*\fldinst HYPERLINK " + b'"http://example.com"'
    rb"}{\fldrslt link text}}\tab tabbed\line next\par"
    rb"{\pict\pngblip 89504e470d0a1a0a}{\*\shppict{\pict ffd8}}"
    rb"\trowd\intbl A1\cell B1\cell\row \trowd\intbl A2\cell\cell\row "
    rb"\pard a\{b\}c\\d\par"
    rb"{\footnote not shown}{\header not shown}\u-10179?\u-8704?\par"
    rb"{\shp{\*\shpinst{\sp{\sn shapeType}{\sv 202}}{\shptxt Boxed text\par}}"
    rb"{\shprslt fallback text}}"
    rb"\bin4 " + b"{{}}" + rb" after bin\par}"
)


def test_rtf_text_with_its_escapes_tables_and_destinations() -> None:
    out = extract_text("letter.rtf", RTF)
    assert out.kind == "rtf"
    assert out.text.split("\n") == [
        "# Heading",
        "Café Привет and € and 中 end",
        "link text\ttabbed",
        "next",
        "A1 | B1",
        "A2",
        "a{b}c\\d",
        "\U0001f600",
        "Boxed text",
        " after bin",
    ]
    for hidden in ("Arial", "Secret", "Someone", "Writer", "HYPERLINK", "89504e", "not shown",
                   "shapeType", "202", "fallback"):  # fmt: skip
        assert hidden not in out.text


def test_rtf_limits_and_plain_text_named_rtf(monkeypatch) -> None:
    deep = b"{\\rtf1 " + b"{" * 600 + b"x" + b"}" * 600 + b"}"
    with pytest.raises(AttachmentError) as ei:
        extract_text("deep.rtf", deep)
    assert ei.value.message == att.TOO_COMPLEX
    monkeypatch.setattr(att, "MAX_DEPTH", 1000)
    assert extract_text("deep.rtf", deep).text == "x"
    monkeypatch.setattr(att, "MAX_RTF_TOKENS", 100)
    with pytest.raises(AttachmentError) as ei:
        extract_text("busy.rtf", b"{\\rtf1 " + b"\\b x\\b0 " * 100 + b"}")
    assert ei.value.message == att.TOO_COMPLEX
    assert extract_text("notes.rtf", b"Just text\r\nin a .rtf").text == "Just text\nin a .rtf"
    long = b"{\\rtf1 " + b"word \\par " * 10_000 + b"}"
    out = extract_text("long.rtf", long, max_chars=100)
    assert out.truncated and out.chars == 100


# --------------------------------------------------------------------------- #
# OLE2 compound files (built here: a minimal writer)
# --------------------------------------------------------------------------- #

END, FREE, FATSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
DIR_ENTRY = struct.Struct("<64sHBBIII16sIQQIQ")  # one 128-byte directory entry


def make_ole(streams: dict[str, bytes]) -> bytes:
    """A minimal OLE2 compound file (version 3, 512-byte sectors) holding ``streams`` by
    ``"Storage/Stream"`` path: those under 4096 bytes in the mini stream, the rest in
    sectors of their own. Siblings are chained to the right (no red-black balancing)."""
    entries: list[dict[str, Any]] = [
        {"name": "Root Entry", "type": 5, "kids": [], "start": END, "size": 0}
    ]
    where = {"": 0}
    for path in streams:
        parts = path.split("/")
        for depth in range(1, len(parts) + 1):
            key = "/".join(parts[:depth])
            if key not in where:
                where[key] = len(entries)
                kind = 2 if depth == len(parts) else 1
                entry = {"name": parts[depth - 1], "type": kind, "kids": [], "start": END}
                entries.append({**entry, "size": 0})
                entries[where["/".join(parts[: depth - 1])]]["kids"].append(where[key])
    mini, minifat, big = bytearray(), [], []
    for path, data in streams.items():
        entry = entries[where[path]]
        entry["size"] = len(data)
        if len(data) >= 4096:
            big.append((entry, data))
        elif data:
            count = -(-len(data) // 64)
            entry["start"] = len(minifat)
            minifat += [len(minifat) + k + 1 for k in range(count - 1)] + [END]
            mini += data.ljust(count * 64, b"\0")
    right = {a: b for e in entries for a, b in zip(e["kids"], e["kids"][1:], strict=False)}

    def sectors(size: int) -> int:
        return -(-size // 512)

    regions = [  # (bytes, padding) in sector order after the FAT
        (b"", b""),  # the directory, filled in below
        (struct.pack(f"<{len(minifat)}I", *minifat), struct.pack("<I", FREE)),
        (bytes(mini), b"\0"),
        *((data, b"\0") for _, data in big),
    ]
    dir_n = sectors(128 * len(entries))
    counts = [dir_n] + [sectors(len(r[0])) for r in regions[1:]]
    fat_n = 1
    while fat_n * 128 < fat_n + sum(counts):
        fat_n += 1
    starts, at = [], fat_n
    for n in counts:
        starts.append(at if n else END)
        at += n
    entries[0]["start"], entries[0]["size"] = starts[2], len(mini)
    for (entry, _), start in zip(big, starts[3:], strict=True):
        entry["start"] = start

    def entry_bytes(i: int, e: dict[str, Any]) -> bytes:
        name = e["name"].encode("utf-16-le") + b"\0\0"
        child = e["kids"][0] if e["kids"] else FREE
        links = (FREE, right.get(i, FREE), child)  # left and right sibling, first child
        size = e["size"]
        return DIR_ENTRY.pack(name, len(name), e["type"], 1, *links, b"", 0, 0, 0, e["start"], size)

    unused = DIR_ENTRY.pack(b"", 0, 0, 0, FREE, FREE, FREE, b"", 0, 0, 0, 0, 0)
    directory = b"".join(entry_bytes(i, e) for i, e in enumerate(entries))
    regions[0] = (directory, unused)
    fat = [FATSECT] * fat_n
    for n in counts:
        fat += [len(fat) + k + 1 for k in range(n - 1)] + [END] if n else []
    fat += [FREE] * (fat_n * 128 - len(fat))
    # Version 3, little-endian, 512-byte sectors, 64-byte mini sectors; the FAT's size, the
    # directory, the mini stream cutoff, the mini FAT, no DIFAT sectors.
    fields = (0x3E, 3, 0xFFFE, 9, 6, b"", 0, fat_n, starts[0], 0, 4096, starts[1], counts[1])
    header = struct.pack("<8s16sHHHHH6sIIIIIIIII", att._OLE_MAGIC, b"", *fields, END, 0)
    header += struct.pack("<109I", *(list(range(fat_n)) + [FREE] * (109 - fat_n)))
    body = bytearray(header)
    body += struct.pack(f"<{len(fat)}I", *fat)
    for (data, pad), n in zip(regions, counts, strict=True):
        filler = pad * ((n * 512 - len(data)) // max(1, len(pad))) if pad else b""
        body += (data + filler).ljust(n * 512, b"\0")
    return bytes(body)


def test_ole_reader_small_and_large_streams_and_storages() -> None:
    big = bytes(range(256)) * 20  # 5120 bytes: sectors of its own
    data = make_ole({"Small": b"tiny", "Big": big, "Box/Inner": b"x" * 100, "Empty": b""})
    ole = att._Ole(data)
    assert ole.read("small") == b"tiny" and ole.read("Big") == big
    assert ole.read("Box/Inner") == b"x" * 100 and ole.read("Empty") == b""
    assert ole.storages == ["Box"] and ole.has("BOX/INNER") and not ole.has("Box")
    with pytest.raises(KeyError):
        ole.read("Missing")


def test_ole_reader_refuses_loops_and_garbage() -> None:
    data = bytearray(make_ole({"Big": b"y" * 5000}))
    # Point the directory's sector at itself in the FAT: a chain that never ends.
    first_dir = struct.unpack_from("<I", data, 0x30)[0]
    struct.pack_into("<I", data, 512 + 4 * first_dir, first_dir)
    with pytest.raises(ValueError):
        att._Ole(bytes(data))
    with pytest.raises(AttachmentError) as ei:
        extract_text("loop.doc", bytes(data))
    assert "not a real Word document" in ei.value.message
    with pytest.raises(AttachmentError) as ei:
        extract_text("short.doc", att._OLE_MAGIC[:4])  # a header cut short
    assert "not a real Word document" in ei.value.message


# --------------------------------------------------------------------------- #
# .doc: a hand-made Word 97 document
# --------------------------------------------------------------------------- #

IN_TABLE = struct.pack("<HB", 0x2416, 1)  # sprmPFInTable
ROW_END = IN_TABLE + struct.pack("<HB", 0x2417, 1)  # + sprmPFTtp
LISTED = struct.pack("<HH", 0x460B, 1)  # sprmPIlfo


def _fkp(runs: list[tuple[int, int, bytes]]) -> bytes:
    """A 512-byte PAPX FKP page: ``(fc start, fc end, istd + properties)`` per paragraph."""
    page = bytearray(512)
    count = len(runs)
    struct.pack_into(f"<{count + 1}I", page, 0, *[r[0] for r in runs], runs[-1][1])
    top = 511
    for k, (_, _, grpprl) in enumerate(runs):
        if len(grpprl) % 2:
            body = bytes([(len(grpprl) + 1) // 2]) + grpprl
        else:
            body = bytes([0, len(grpprl) // 2]) + grpprl
        top -= len(body)
        top -= top % 2
        page[top : top + len(body)] = body
        page[4 * (count + 1) + 13 * k] = top // 2
    page[511] = count
    return bytes(page)


#: Fields (code, then result; a field nested in the second's code), Word's special
#: hyphens and a line break.
FIELDS = (
    "A field: \x13 DATE \x142026\x15, nested \x13 IF \x13 =1 \x141\x15 = 1 \x14yes\x15,"
    " no\x1ebreak, soft\x1fhyphen, line\x0bbreak.\r"
)
#: The test document's paragraphs: (text with its mark, style index, properties), first
#: in 8-bit text, then in UTF-16. A character past U+FFFF takes two UTF-16 units: the
#: row's end mark after two of them is found only when they are counted.
DOC_EIGHT = [
    ("Heading\r", 1, b""),
    (FIELDS, 0, b""),
    ("Item\r", 0, LISTED),
    ("Styled item\r", 3, b""),
    ("a\x07", 0, IN_TABLE),
    ("\x07", 0, IN_TABLE),  # an empty cell
    ("\x07", 0, ROW_END),
    ("b\x07", 0, IN_TABLE),
    ("two\r", 0, IN_TABLE),  # a cell of two paragraphs
    ("lines\x07", 0, IN_TABLE),
    ("\x07", 0, ROW_END),
]
DOC_WIDE = [
    ("\x01Caf\u00e9 \u4e2d \U0001f600 end\r", 0, b""),
    ("\U0001f600\x07", 0, IN_TABLE),
    ("\x07", 0, ROW_END),
    ("Last\r", 2, b""),  # istd 2 is an empty style slot: no prefix
]
DOC_FOOTNOTE = "\x02 A footnote\r"


def make_doc(*, version: int = 0xC1, encrypted: bool = False, fkp: bool = True) -> bytes:
    """A Word 97 document: FIB, two text pieces (8-bit, then UTF-16), a footnote piece,
    paragraph properties in one FKP page, a style sheet and the piece table."""
    eight = "".join(p[0] for p in DOC_EIGHT).encode("cp1252")
    wide = "".join(p[0] for p in DOC_WIDE).encode("utf-16-le")
    note = DOC_FOOTNOTE.encode("cp1252")
    at8, at16, at_note, at_fkp = 1024, 1536, 1792, 2048
    word = bytearray(4608)  # over 4096 bytes: in sectors, not the mini stream
    word[at8 : at8 + len(eight)] = eight
    word[at16 : at16 + len(wide)] = wide
    word[at_note : at_note + len(note)] = note
    runs = []
    for start, paragraphs, codec in ((at8, DOC_EIGHT, "cp1252"), (at16, DOC_WIDE, "utf-16-le")):
        fc = start
        for text, istd, props in paragraphs:
            size = len(text.encode(codec))
            runs.append((fc, fc + size, struct.pack("<H", istd) + props))
            fc += size
    word[at_fkp : at_fkp + 512] = _fkp(runs)
    cp_eight, cp_wide = len(eight), len(wide) // 2
    stsh = struct.pack("<HHH", 4, 4, 10)  # cbStshi, then cstd (4 styles) and cbSTDBase
    for sti in (0, 1, None, 48):  # Normal, Heading 1, an empty slot, List Bullet
        stsh += struct.pack("<HH", 2, sti) if sti is not None else struct.pack("<H", 0)
    cps = [0, cp_eight, cp_eight + cp_wide, cp_eight + cp_wide + len(note)]
    pcds = [(at8 * 2) | 0x40000000, at16, (at_note * 2) | 0x40000000]
    plc = struct.pack("<4i", *cps) + b"".join(struct.pack("<HIH", 0, pc, 0) for pc in pcds)
    clx = struct.pack("<Bh", 1, 2) + b"\0\0" + struct.pack("<BI", 2, len(plc)) + plc
    bte = struct.pack("<IIi", at8, runs[-1][1], at_fkp // 512) if fkp else b""
    table = stsh + bte + clx
    flags = 0x0200 | (0x0100 if encrypted else 0)
    struct.pack_into("<HHHHHH", word, 0, 0xA5EC, version, 0, 0x0409, 0, flags)
    struct.pack_into("<H", word, 32, 14)  # csw: 14 shorts
    struct.pack_into("<H", word, 62, 22)  # cslw: 22 longs
    struct.pack_into("<ii", word, 64 + 12, cp_eight + cp_wide, len(note))  # ccpText, ccpFtn
    struct.pack_into("<H", word, 152, 93)  # cbRgFcLcb: 93 pairs
    pairs = 154
    struct.pack_into("<II", word, pairs + 1 * 8, 0, len(stsh))  # fcStshf
    struct.pack_into("<II", word, pairs + 13 * 8, len(stsh), len(bte))  # fcPlcfBtePapx
    struct.pack_into("<II", word, pairs + 33 * 8, len(stsh) + len(bte), len(clx))  # fcClx
    return make_ole({"WordDocument": bytes(word), "1Table": table, "\x05SummaryInformation": b"s"})


def test_doc_read_here_with_headings_lists_tables_and_fields() -> None:
    out = extract_text("report.doc", make_doc())
    assert out.kind == "doc"
    assert out.text.split("\n") == [
        "# Heading",
        "A field: 2026, nested yes, no-break, softhyphen, line",
        "break.",
        "- Item",
        "- Styled item",
        "a",
        "b | two lines",
        "Café 中 \U0001f600 end",
        "\U0001f600",
        "Last",
        "",
        "A footnote",
    ]
    assert extract_text("report.dot", make_doc()).text == out.text  # a template


def test_doc_without_paragraph_properties_guesses_rows() -> None:
    text = extract_text("plain.doc", make_doc(fkp=False)).text
    assert "Heading" in text.split("\n") and "Item" in text.split("\n")
    assert "b | two" in text  # cells still joined; an empty cell can end a row early


def test_doc_refusals() -> None:
    with pytest.raises(AttachmentError) as ei:
        extract_text("locked.doc", make_doc(encrypted=True))
    assert ei.value.message == att.PASSWORD_PROTECTED
    # Word 6/95 files have no piece table: only Office reads them, and it is not here.
    with pytest.raises(AttachmentError) as ei:
        extract_text("old.doc", make_doc(version=0x65))
    assert "Save it as .docx in Word and attach that" in ei.value.message
    assert ei.value.code == "unsupported"
    with pytest.raises(AttachmentError) as ei:
        extract_text("locked.docx", make_ole({"EncryptionInfo": b"i", "EncryptedPackage": b"p"}))
    assert ei.value.message == att.PASSWORD_PROTECTED
    with pytest.raises(AttachmentError) as ei:
        extract_text("noise.doc", bytes(range(256)) * 4)
    assert "not a real Word document" in ei.value.message


def test_files_saved_under_an_old_name_are_read_as_what_they_are() -> None:
    docx = make_zip(
        {
            "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Really docx</w:t></w:r></w:p>'
            "</w:body></w:document>"
        }
    )
    assert extract_text("a.doc", docx).text == "Really docx"
    assert extract_text("b.doc", b"{\\rtf1 Really RTF\\par}").text == "Really RTF"
    page = b"<html><body><table><tr><td>Web</td><td>export</td></tr></table></body></html>"
    assert "Web" in extract_text("c.xls", page).text
    assert extract_text("d.xls", b"name\tqty\nbolt\t5\n").text == "name\tqty\nbolt\t5"
    assert extract_text("e.docx", make_doc()).text.startswith("# Heading")  # .doc as .docx


# --------------------------------------------------------------------------- #
# .xls and .ppt: through Office, faked
# --------------------------------------------------------------------------- #


def make_xls(*, encrypted: bool = False) -> bytes:
    records = struct.pack("<HH", 0x0809, 4) + b"\0\x06\x05\0"  # BOF
    if encrypted:
        records += struct.pack("<HH", 0x002F, 6) + b"\x01\0\x01\0\x01\0"  # FILEPASS
    records += struct.pack("<HH", 0x0085, 4) + b"\0\0\0\0" + struct.pack("<HH", 0x000A, 0)
    return make_ole({"Workbook": records})


def make_ppt(*, encrypted: bool = False) -> bytes:
    token = 0xF3D1C4DF if encrypted else 0xE391C05F
    user = struct.pack("<HHIII", 0, 0x0FF6, 20, 20, token) + b"\0" * 12
    return make_ole({"PowerPoint Document": b"\0" * 40, "Current User": user})


def tiny_ooxml(kind: str) -> bytes:
    if kind == "xls":
        sheet = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Converted</t></is></c>'
            "</row></sheetData></worksheet>"
        )
        return make_zip({"xl/worksheets/sheet1.xml": sheet})
    if kind == "ppt":
        slide = (
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
            "<p:sp><p:txBody><a:p><a:r><a:t>Converted slide</a:t></a:r></a:p></p:txBody></p:sp>"
            "</p:spTree></p:cSld></p:sld>"
        )
        return make_zip({"ppt/slides/slide1.xml": slide})
    body = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    body += "<w:body><w:p><w:r><w:t>Converted doc</w:t></w:r></w:p></w:body></w:document>"
    return make_zip({"word/document.xml": body})


class FakeOffice:
    """Stands in for win32com/pythoncom and Word, Excel and PowerPoint: records what is
    set and called, and saves ``tiny_ooxml`` where the conversion asks."""

    def __init__(self) -> None:
        self.log: list[tuple[Any, ...]] = []
        self.running: set[int] = set()  # what _office_pids sees
        self.user_powerpoint = False  # PowerPoint already open: Dispatch starts nothing
        self.killed: list[tuple[str, set[int], float]] = []
        self.error: Exception | None = None  # raised by Open
        self.block: threading.Event | None = None  # Open waits for it
        self.dispatch_errors: list[Exception] = []  # raised by the next DispatchEx calls
        self.sources: list[str] = []

    def join(self) -> None:
        """Wait for the conversion thread, which closes Office after the file is read."""
        for thread in threading.enumerate():
            if thread.name == "office-convert":
                thread.join(10)

    def dispatch(self, prog_id: str) -> Any:
        self.log.append(("DispatchEx", prog_id))
        if self.dispatch_errors:
            raise self.dispatch_errors.pop(0)
        if not (prog_id.startswith("PowerPoint") and self.user_powerpoint):
            self.running.add(4242)
        return FakeApp(self, prog_id)

    def opened(self, src: str, kwargs: dict[str, Any]) -> None:
        self.log.append(("Open", kwargs))
        self.sources.append(src)
        with open(src, "rb") as fh:
            assert fh.read(4) == att._OLE_MAGIC[:4]  # a copy of the attached bytes
        if self.block is not None:
            self.block.wait(10)
        if self.error is not None:
            raise self.error


class FakeApp:
    def __init__(self, office: FakeOffice, prog_id: str) -> None:
        object.__setattr__(self, "_office", office)
        object.__setattr__(self, "_kind", {"W": "doc", "E": "xls", "P": "ppt"}[prog_id[0]])
        object.__setattr__(self, "DisplayAlerts", 2)
        object.__setattr__(self, "AutomationSecurity", 1)
        collection = types.SimpleNamespace(Open=self._open)
        for name in ("Documents", "Workbooks", "Presentations"):
            object.__setattr__(self, name, collection)

    def __setattr__(self, name: str, value: Any) -> None:
        self._office.log.append(("set", name, value))
        object.__setattr__(self, name, value)

    def _open(self, **kwargs: Any) -> Any:
        src = kwargs.get("FileName") or kwargs.get("Filename")
        src = src.split("::")[0]
        self._office.opened(src, kwargs)
        kind, log = self._kind, self._office.log

        def save(**kw: Any) -> None:
            log.append(("SaveAs", kw))
            target = kw.get("FileName") or kw.get("Filename")
            with open(target, "wb") as fh:
                fh.write(tiny_ooxml(kind))

        return types.SimpleNamespace(
            SaveAs=save, SaveAs2=save, Close=lambda **kw: log.append(("Close", kw))
        )

    def Quit(self, *args: Any) -> None:  # noqa: N802 - COM's name
        self._office.log.append(("Quit", *args))
        self._office.running.discard(4242)


@pytest.fixture
def office(monkeypatch) -> Any:
    fake = FakeOffice()
    client = types.ModuleType("win32com.client")
    client.DispatchEx = fake.dispatch
    package = types.ModuleType("win32com")
    package.client = client
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: fake.log.append(("CoInitialize",))
    pythoncom.CoUninitialize = lambda: fake.log.append(("CoUninitialize",))
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setattr(att, "_office_installed", lambda prog_id: True)
    monkeypatch.setattr(att, "_office_pids", lambda image: set(fake.running))

    def kill(image: str, pids: Any, wait: float = 0.0) -> None:
        fake.killed.append((image, set(pids), wait))
        if fake.block is not None:
            fake.block.set()

    monkeypatch.setattr(att, "_office_kill", kill)
    yield fake
    fake.join()  # before the fakes go


def _calls(fake: FakeOffice, name: str) -> list[tuple[Any, ...]]:
    return [entry for entry in fake.log if entry[0] == name]


def test_xls_converted_by_excel_hidden_read_only_without_macros(office) -> None:
    out = extract_text("budget.xls", make_xls())
    office.join()
    assert out.kind == "xls" and out.text == "## Sheet: Sheet1\nConverted"
    assert ("DispatchEx", "Excel.Application") in office.log
    [(_, opened)] = _calls(office, "Open")
    assert opened["ReadOnly"] is True and opened["UpdateLinks"] == 0
    assert opened["Password"] == att._NO_PASSWORD  # fails instead of asking
    names = [entry[1] if entry[0] == "set" else entry[0] for entry in office.log]
    assert ("set", "AutomationSecurity", 3) in office.log  # macros off, before opening
    assert names.index("AutomationSecurity") < names.index("Open")
    assert ("set", "DisplayAlerts", False) in office.log
    [(_, saved)] = _calls(office, "SaveAs")
    assert saved["FileFormat"] == 51 and saved["Filename"].endswith("converted.xlsx")
    assert _calls(office, "Close") and _calls(office, "Quit")
    assert office.log[-1] == ("CoUninitialize",)
    assert office.killed == [("excel.exe", {4242}, att.OFFICE_EXIT_WAIT)]  # if it lingers
    # The copy Office opened was in a temporary folder that is gone.
    assert office.sources and not os.path.exists(os.path.dirname(office.sources[0]))


def test_ppt_and_old_doc_converted_by_powerpoint_and_word(office) -> None:
    assert extract_text("deck.ppt", make_ppt()).text == "## Slide 1\nConverted slide"
    office.join()
    [(_, opened)] = _calls(office, "Open")
    assert opened["FileName"].endswith("::" + att._NO_PASSWORD + "::")
    assert opened["WithWindow"] == 0 and opened["ReadOnly"] == -1
    assert _calls(office, "Quit")  # PowerPoint this started is closed
    office.log.clear()
    assert extract_text("old.doc", make_doc(version=0x65)).text == "Converted doc"  # Word 95
    office.join()
    [(_, opened)] = _calls(office, "Open")
    assert opened["ReadOnly"] is True and opened["PasswordDocument"] == att._NO_PASSWORD
    [(_, saved)] = _calls(office, "SaveAs")
    assert saved["FileFormat"] == 16
    assert _calls(office, "Quit") == [("Quit", 0)]  # Word: without saving anything
    office.log.clear()
    assert extract_text("new.doc", make_doc()).text.startswith("# Heading")
    assert not _calls(office, "DispatchEx")  # a Word 97 file never needs Office


def test_the_users_own_powerpoint_is_not_closed(office) -> None:
    office.user_powerpoint = True
    office.running = {77}
    assert extract_text("deck.pps", make_ppt()).text == "## Slide 1\nConverted slide"
    office.join()
    assert not _calls(office, "Quit") and not office.killed[0][1]
    # Its settings are put back as they were.
    assert office.log[-3:-1] == [("set", "DisplayAlerts", 2), ("set", "AutomationSecurity", 1)]


def test_office_conversion_errors(office) -> None:
    with pytest.raises(AttachmentError) as ei:
        extract_text("locked.xls", make_xls(encrypted=True))
    assert ei.value.message == att.PASSWORD_PROTECTED  # refused before Excel would wait
    with pytest.raises(AttachmentError) as ei:
        extract_text("locked.ppt", make_ppt(encrypted=True))
    assert ei.value.message == att.PASSWORD_PROTECTED
    assert not _calls(office, "DispatchEx")
    with pytest.raises(AttachmentError) as ei:
        extract_text("bad.xls", make_ole({"Other": b"x"}))
    assert "not a real Excel workbook" in ei.value.message

    class PasswordError(Exception):
        excepinfo = (0, "Microsoft Excel", "The password you supplied is not correct.", None)

    office.error = PasswordError("Exception occurred.")
    with pytest.raises(AttachmentError) as ei:
        extract_text("budget.xls", make_xls())
    assert ei.value.message == att.PASSWORD_PROTECTED
    office.error = RuntimeError("file is corrupt")
    with pytest.raises(AttachmentError) as ei:
        extract_text("budget.xls", make_xls())
    assert ei.value.message.startswith("Excel could not open this file.")
    office.join()
    assert len(_calls(office, "Quit")) == 2  # closed after each failure too


class ComError(Exception):
    """Like pywintypes.com_error: an HRESULT and words."""

    def __init__(self, hresult: int, text: str) -> None:
        super().__init__(hresult, text)
        self.hresult = hresult


def test_office_is_tried_again_when_it_cannot_be_reached(office, monkeypatch) -> None:
    monkeypatch.setattr(att, "OFFICE_RETRY_PAUSE", 0)
    # The Word or Excel of the last conversion still closing: a second try works.
    office.dispatch_errors = [ComError(-2147023170, "The remote procedure call failed.")]
    assert extract_text("budget.xls", make_xls()).text == "## Sheet: Sheet1\nConverted"
    office.join()
    assert len(_calls(office, "DispatchEx")) == 2
    office.log.clear()
    office.dispatch_errors = [ComError(-2146959355, "Server execution failed")] * 5
    with pytest.raises(AttachmentError) as ei:
        extract_text("budget.xls", make_xls())
    office.join()
    assert ei.value.message.startswith("Excel could not open this file.")
    assert len(_calls(office, "DispatchEx")) == att.OFFICE_TRIES
    # An error in the file itself is not tried again.
    office.log.clear()
    office.dispatch_errors = []
    office.error = ComError(-2146827284, "Excel cannot open the file.")
    with pytest.raises(AttachmentError):
        extract_text("budget.xls", make_xls())
    office.join()
    assert len(_calls(office, "DispatchEx")) == 1


def test_office_conversion_times_out_and_kills_what_it_started(office, monkeypatch) -> None:
    monkeypatch.setattr(att, "OFFICE_TIMEOUT", 0.2)
    office.block = threading.Event()  # Open hangs until the kill
    with pytest.raises(AttachmentError) as ei:
        extract_text("budget.xls", make_xls())
    assert ei.value.message == (
        "Excel took too long to open this file. Save it as .xlsx in Excel and attach that."
    )
    assert office.killed[0] == ("excel.exe", {4242}, 0.0)


def test_without_office_old_formats_are_refused_kindly() -> None:
    for name, data, advice in (
        ("budget.xls", make_xls(), "Save it as .xlsx in Excel and attach that."),
        ("deck.ppt", make_ppt(), "Save it as .pptx in PowerPoint and attach that."),
    ):
        with pytest.raises(AttachmentError) as ei:
            extract_text(name, data)
        assert ei.value.message.endswith(advice) and ei.value.code == "unsupported"
        assert "Microsoft Office" in ei.value.message


def test_office_is_only_looked_for_on_windows(monkeypatch) -> None:
    monkeypatch.undo()  # the real _office_installed, which only reads the registry
    if sys.platform == "win32":
        assert att._office_installed("ChatForge.NoSuchProgram") is False
    monkeypatch.setattr(sys, "platform", "linux")
    assert att._office_installed("Word.Application") is False


def test_importing_attachments_loads_no_com() -> None:
    code = (
        "import sys, chatforge.attachments; "
        "print(sorted(m for m in ('win32com', 'pythoncom', 'psutil') if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "[]"


# --------------------------------------------------------------------------- #
# E-mail
# --------------------------------------------------------------------------- #

EML = b"""From: Ann Example <ann@example.com>
To: Bob <bob@example.com>
Cc: carol@example.com
Date: Tue, 02 Jan 2024 10:30:00 +0000
Subject: =?utf-8?q?Caf=C3=A9_menu?=
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b1"

--b1
Content-Type: multipart/alternative; boundary="b2"

--b2
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: quoted-printable

Lunch at noon =E2=80=94 see the menu.
--b2
Content-Type: text/html; charset=utf-8

<p>Lunch at <b>noon</b></p>
--b2--
--b1
Content-Type: application/pdf; name="menu.pdf"
Content-Disposition: attachment; filename="menu.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--b1--
"""


def test_eml_headers_body_and_attachment_names() -> None:
    out = extract_text("lunch.eml", EML)
    assert out.kind == "eml"
    assert out.text == (
        "From: Ann Example <ann@example.com>\nTo: Bob <bob@example.com>\n"
        "Cc: carol@example.com\nDate: Tue, 02 Jan 2024 10:30:00 +0000\nSubject: Café menu\n"
        "Attachments: menu.pdf\n\nLunch at noon — see the menu."
    )


def test_eml_html_only_odd_charsets_and_deep_nesting() -> None:
    html_only = (
        b"Subject: Hi\nContent-Type: text/html\n\n<p>Hello <b>there</b></p><script>x</script>"
    )
    assert extract_text("a.eml", html_only).text == "Subject: Hi\n\nHello there"
    odd = b"Subject: Odd\nContent-Type: text/plain; charset=x-unknown\n\ncaf\xc3\xa9"
    assert extract_text("b.eml", odd).text == "Subject: Odd\n\ncafé"
    assert extract_text("c.eml", b"Subject: Empty\n\n").text == "Subject: Empty\n\n(no text)"
    depth = 1500
    nested = b"Subject: deep\n" + b"".join(
        b'Content-Type: multipart/mixed; boundary="b%d"\n\n--b%d\n' % (i, i) for i in range(depth)
    )
    with pytest.raises(AttachmentError) as ei:
        extract_text("deep.eml", nested + b"Content-Type: text/plain\n\nhi\n")
    assert ei.value.message == att.TOO_COMPLEX


def _props(*entries: tuple[int, bytes]) -> bytes:
    """A message's ``__properties_version1.0``: a 32-byte header, then 16 bytes each."""
    return b"\0" * 32 + b"".join(
        struct.pack("<II", tag, 6) + v.ljust(8, b"\0") for tag, v in entries
    )


def make_msg(*, html: bool = False) -> bytes:
    ticks = (datetime(2024, 1, 2, 10, 30) - datetime(1601, 1, 1)).total_seconds() * 10**7
    sent = int(ticks).to_bytes(8, "little")  # a FILETIME
    streams = {
        "__properties_version1.0": _props(
            (0x00390040, sent), (0x3FFD0003, struct.pack("<I", 1252))
        ),
        "__substg1.0_0037001F": "Lunch".encode("utf-16-le"),
        "__substg1.0_0C1A001F": "Ann Example".encode("utf-16-le"),
        "__substg1.0_5D01001F": "ann@example.com".encode("utf-16-le"),
        "__substg1.0_0E04001F": "Bob".encode("utf-16-le") + b"\0\0",
        "__attach_version1.0_#00000000/__substg1.0_3707001F": "menu.pdf".encode("utf-16-le"),
        "__attach_version1.0_#00000001/__substg1.0_3001001E": b"notes",
    }
    if html:
        streams["__substg1.0_10130102"] = b"<html><body><p>Hi <b>there</b></p></body></html>"
    else:
        streams["__substg1.0_1000001E"] = "Café at noon".encode("cp1252")
    return make_ole(streams)


def test_msg_outlook_message() -> None:
    out = extract_text("lunch.msg", make_msg())
    assert out.kind == "msg"
    assert out.text == (
        "From: Ann Example <ann@example.com>\nTo: Bob\nDate: 2024-01-02 10:30 UTC\n"
        "Subject: Lunch\nAttachments: menu.pdf, notes\n\nCafé at noon"
    )
    assert extract_text("html.msg", make_msg(html=True)).text.endswith("\n\nHi there")
    with pytest.raises(AttachmentError) as ei:
        extract_text("bad.msg", make_ole({"Other": b"x"}))
    assert "not a real Outlook message" in ei.value.message


# --------------------------------------------------------------------------- #
# Kinds and the file dialog
# --------------------------------------------------------------------------- #


def test_kinds_of_the_new_formats() -> None:
    for name, kind in {
        "a.odt": "odt", "a.ott": "odt", "a.ods": "ods", "a.ots": "ods", "a.odp": "odp",
        "a.otp": "odp", "a.rtf": "rtf", "a.doc": "doc", "a.dot": "doc", "a.xls": "xls",
        "a.xlt": "xls", "a.ppt": "ppt", "a.pps": "ppt", "a.pot": "ppt", "a.eml": "eml",
        "a.msg": "msg", "a.dotx": "docx", "a.xltm": "xlsx", "a.ppsx": "pptx", "a.ics": "text",
        "a.vcf": "text", "a.ass": "text", "a.tex": "text", "a.patch": "text",
    }.items():  # fmt: skip
        assert kind_for(name) == kind, name
    assert extract_text("cal.ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR").kind == "text"


def test_dialog_filters_list_documents_apart() -> None:
    from webview.util import parse_file_type

    for entry in att.DIALOG_FILE_TYPES:
        parse_file_type(entry)
    supported, documents = att.DIALOG_FILE_TYPES[0], att.DIALOG_FILE_TYPES[1]
    assert supported.startswith("Supported files (") and documents.startswith("Documents (")
    for ext in ("odt", "ods", "odp", "rtf", "doc", "xls", "ppt", "eml", "msg", "docx", "pdf"):
        assert f"*.{ext};" in supported and f"*.{ext};" in documents
    assert att.DIALOG_FILE_TYPES[-1] == "All files (*.*)"
