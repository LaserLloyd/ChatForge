"""Read attached files as text: a file attached to a message becomes context for the model.

``extract_text(name, data)`` turns one file into an :class:`Extracted`, using only the
standard library: plain text and code in the common encodings, HTML (its visible text),
Word ``.docx``, Excel ``.xlsx`` and PowerPoint ``.pptx`` (read straight from the XML parts
inside the zip), OpenDocument ``.odt``/``.ods``/``.odp`` (likewise), RTF, old Word ``.doc``
(from its OLE2 container and piece table), e-mail ``.eml`` and Outlook ``.msg``, and PDF
with ``pypdf`` (a required dependency). Old ``.xls`` and ``.ppt`` files (and the
rare ``.doc`` not read here) are converted by Microsoft Office itself when it is installed
(Windows, COM, hidden, read-only, macros off). Archives, programs, other old formats and
pictures raise :class:`AttachmentError`, whose message the UI shows as it is (the pictures
``chatforge.images`` reads never reach this module).

``kind`` is one of ``text``, ``code``, ``data``, ``html``, ``docx``, ``xlsx``, ``pptx``,
``odt``, ``ods``, ``odp``, ``rtf``, ``doc``, ``xls``, ``ppt``, ``eml``, ``msg`` or ``pdf``.
Text longer than ``max_chars`` (``tools.attachment_max_chars``) is cut and marked
``truncated``.

:class:`AttachmentStore` keeps the files the user attached but has not sent yet, under
random ids, until ``send_message`` takes them. :func:`file_block` and :func:`user_content`
build what the model sees: the typed text, then one ``<file name="...">`` block per file.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import codecs
import contextlib
import csv
import html
import io
import os
import posixpath
import re
import secrets
import struct
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from chatforge.errors import AppError

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
DEFAULT_MAX_CHARS = 200_000
#: Attached files waiting to be sent; the oldest are forgotten past this many.
MAX_PENDING = 50
#: ...and past this many bytes (a picture holds its cleaned image and a sharper copy to read
#: text from), the oldest are forgotten too; the newest file is always kept.
MAX_PENDING_BYTES = 64 * 1024 * 1024
#: One XML part of an Office file may not unpack to more than this (zip bombs)...
MAX_PART_BYTES = 50 * 1024 * 1024
#: ...and all the parts read from one file together not to more than this.
MAX_UNPACKED_BYTES = 150 * 1024 * 1024
#: The most XML elements read from one Office file. A 300-page Word document has a few
#: hundred thousand; the readers stop early once they have ``max_chars`` of text.
MAX_ELEMENTS = 3_000_000
#: The most elements inside one paragraph, cell or string (each is held whole until read).
MAX_KEPT_ELEMENTS = 250_000
#: The deepest XML nesting read (nested Word tables reach a few dozen levels).
MAX_DEPTH = 512
#: Excel's last column is XFD, the 16,384th.
MAX_XLSX_COLUMNS = 16_384
MAX_NAME_CHARS = 255

TOO_MUCH_DATA = "The file unpacks to too much data to read."
TOO_COMPLEX = "The file is too complex to read."

# ``pypdf`` is a required dependency; this only shows when its import fails (a broken install).
PDF_NEEDS_PYPDF = (
    "PDFs cannot be read because the pypdf package failed to load. "
    "Reinstall ChatForge to repair it."
)
IMAGES_UNSUPPORTED = (
    "This kind of picture cannot be read. Attach PNG, JPEG, GIF, BMP, WebP or TIFF pictures."
)
#: Pictures ``chatforge.images`` reads (AVIF and HEIC only when this Pillow build can).
PICTURE_EXTS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".bmp", ".dib", ".webp", ".tif",
        ".tiff", ".avif", ".heic", ".heif",
    }
)  # fmt: skip

TEXT_EXTS = frozenset(
    {
        ".txt", ".text", ".md", ".markdown", ".rst", ".log", ".nfo", ".srt", ".vtt", ".sbv",
        ".ass", ".ssa", ".lrc", ".ics", ".ical", ".ifb", ".vcs", ".vcf", ".vcard", ".tex",
        ".bib", ".org", ".adoc", ".asciidoc", ".wiki", ".textile", ".diff", ".patch",
    }
)  # fmt: skip
CODE_EXTS = frozenset(
    {
        ".py", ".pyw", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".scss",
        ".sass", ".less", ".sql", ".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".fish",
        ".bat", ".cmd", ".vbs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cs",
        ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".swift", ".lua", ".r", ".pl",
        ".pm", ".dart", ".scala", ".groovy", ".gradle", ".vb", ".fs", ".ex", ".exs", ".erl",
        ".hs", ".clj", ".jl", ".m", ".tf", ".hcl", ".vue", ".svelte", ".svg", ".ipynb",
        ".cmake", ".mk", ".dockerfile", ".gitignore", ".editorconfig",
    }
)  # fmt: skip
DATA_EXTS = frozenset(
    {
        ".json", ".jsonl", ".ndjson", ".geojson", ".csv", ".tsv", ".xml", ".xsd", ".xsl",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env", ".reg",
        ".plist", ".gpx", ".kml",
    }
)  # fmt: skip
HTML_EXTS = frozenset({".html", ".htm", ".xhtml"})
#: Office-type documents, each read by a reader of its own: ``ext -> kind``. Templates and
#: slide shows are read as what they hold.
OFFICE_KINDS = {
    ".docx": "docx", ".docm": "docx", ".dotx": "docx", ".dotm": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx", ".xltx": "xlsx", ".xltm": "xlsx",
    ".pptx": "pptx", ".pptm": "pptx", ".potx": "pptx", ".potm": "pptx", ".ppsx": "pptx",
    ".ppsm": "pptx",
    ".odt": "odt", ".ott": "odt", ".ods": "ods", ".ots": "ods", ".odp": "odp", ".otp": "odp",
    ".rtf": "rtf",
    ".doc": "doc", ".dot": "doc", ".xls": "xls", ".xlt": "xls", ".ppt": "ppt", ".pps": "ppt",
    ".pot": "ppt",
    ".eml": "eml", ".msg": "msg",
}  # fmt: skip
IMAGE_EXTS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico", ".heic",
        ".heif", ".avif", ".psd", ".raw", ".cr2", ".nef", ".dng",
    }
)  # fmt: skip
#: Old binary Office formats and their newer equivalent: ``ext -> (program, new ext)``.
#: A ``.doc`` is read here; the others need Microsoft Office installed (see ``_legacy``),
#: and this is the advice when it is not.
LEGACY_FORMATS = {
    ".doc": ("Word", ".docx"),
    ".xls": ("Excel", ".xlsx"),
    ".ppt": ("PowerPoint", ".pptx"),
}
BINARY_EXTS = frozenset(
    {
        ".exe", ".dll", ".sys", ".msi", ".msix", ".appx", ".bin", ".iso", ".img", ".zip",
        ".7z", ".rar", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".cab", ".jar", ".apk", ".mp3",
        ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".mp4", ".mkv", ".avi", ".mov",
        ".wmv", ".webm", ".db", ".sqlite", ".mdb", ".accdb", ".pst", ".ost", ".lnk", ".ttf",
        ".otf", ".woff", ".woff2", ".pyc", ".o", ".obj", ".lib", ".pdb", ".onnx", ".gguf",
        ".safetensors", ".pt", ".npz", ".npy", ".parquet", ".epub", ".mobi",
    }
)  # fmt: skip

#: Every extension read as a document: text, code, data, web pages, Office, PDF, e-mail.
DOCUMENT_EXTS = TEXT_EXTS | CODE_EXTS | DATA_EXTS | HTML_EXTS | set(OFFICE_KINDS) | {".pdf"}


def _file_filter(label: str, exts: Iterable[str]) -> str:
    """One file dialog filter, in pywebview's syntax: ``Label (*.a;*.b)``."""
    patterns = ";".join(f"*{ext}" for ext in sorted(exts))
    return f"{label} ({patterns})"


#: The native file dialog's filters; the first is the one it opens with.
DIALOG_FILE_TYPES: tuple[str, ...] = (
    _file_filter("Supported files", DOCUMENT_EXTS | PICTURE_EXTS),
    _file_filter("Documents", DOCUMENT_EXTS),
    _file_filter("Pictures", PICTURE_EXTS),
    "All files (*.*)",
)


class AttachmentError(AppError):
    """A file that cannot be attached; ``message`` is shown to the user as it is."""

    code = "bad_request"


@dataclass(frozen=True)
class Extracted:
    """The text of one attached file. A picture (``kind == "image"``) has no text; its
    cleaned image is ``image`` (a ``chatforge.images.Picture``)."""

    name: str
    kind: str
    text: str
    chars: int
    truncated: bool = False
    warning: str | None = None
    image: Any = field(default=None, repr=False, compare=False)


# --------------------------------------------------------------------------- #
# Names, kinds and decoding
# --------------------------------------------------------------------------- #


def clean_name(name: Any) -> str:
    """The file name without folders or control characters (``"file"`` when empty)."""
    text = re.split(r"[\\/]", str(name or ""))[-1]
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f").strip()
    return text[:MAX_NAME_CHARS] or "file"


_SPECIAL_NAMES = {"dockerfile": ".dockerfile", "makefile": ".mk"}


def _ext(name: str) -> str:
    """The lower-case extension; the whole name for dot-files such as ``.gitignore``."""
    lowered = name.lower()
    if lowered in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[lowered]
    suffix = Path(lowered).suffix
    return lowered if not suffix and lowered.startswith(".") else suffix


def kind_for(name: str) -> str | None:
    """The ``kind`` of a file name by its extension, or ``None`` when it is not known."""
    ext = _ext(name)
    if ext in OFFICE_KINDS:
        return OFFICE_KINDS[ext]
    if ext == ".pdf":
        return "pdf"
    if ext in HTML_EXTS:
        return "html"
    if ext in DATA_EXTS:
        return "data"
    if ext in CODE_EXTS:
        return "code"
    if ext in TEXT_EXTS:
        return "text"
    return None


def _utf16_without_bom(sample: bytes) -> str | None:
    """``utf-16-le``/``-be`` when ``sample`` looks like BOM-less UTF-16 text, else ``None``."""
    half = len(sample) // 2
    if half < 2:
        return None
    odd, even = sample[1::2].count(0), sample[0::2].count(0)
    if odd > half * 0.3 and even < half * 0.05:
        encoding = "utf-16-le"
    elif even > half * 0.3 and odd < half * 0.05:
        encoding = "utf-16-be"
    else:
        return None
    text = sample[: half * 2].decode(encoding, errors="replace")
    readable = sum(ch.isprintable() or ch in "\t\r\n" for ch in text)
    return encoding if readable >= 0.9 * len(text) else None


def looks_binary(data: bytes) -> bool:
    """True when ``data`` is not text: NUL bytes that BOM-less UTF-16 does not explain."""
    sample = data[:8192]
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    return b"\x00" in sample and _utf16_without_bom(sample) is None


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without a BOM), UTF-16 (BOM, or plainly UTF-16), else Windows-1252."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    utf16 = _utf16_without_bom(data[:8192])
    if utf16 is not None:
        return data.decode(utf16, errors="replace")
    return data.decode("cp1252", errors="replace")


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return re.sub(r"\n{4,}", "\n\n\n", text).strip("\n")


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


class _Out:
    """Collects extracted text and says when ``limit`` characters have been passed, so the
    readers can stop early on huge files."""

    def __init__(self, limit: int) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.limit = limit

    @property
    def full(self) -> bool:
        return self.size > self.limit

    def add(self, text: str) -> None:
        if not self.full and text:
            self.parts.append(text)
            self.size += len(text)

    def text(self) -> str:
        return "".join(self.parts)


def extract_text(name: str, data: bytes, *, max_chars: int = DEFAULT_MAX_CHARS) -> Extracted:
    """Read one attached file. Raises :class:`AttachmentError` with a user-facing message."""
    name = clean_name(name)
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentError("The file is larger than 20 MB.", code="too_large")
    limit = max(1, int(max_chars))
    kind = kind_for(name)
    ext = _ext(name)
    if kind is None:
        _refuse_unknown(ext, data)
        kind = "text"
    if kind in ("docx", "xlsx", "pptx") and data[:4] == _OLE_MAGIC[:4]:
        # A password-protected Office file, or an old one saved under a new name.
        kind = {"docx": "doc", "xlsx": "xls", "pptx": "ppt"}[kind]
    readers = {
        "docx": _docx, "xlsx": _xlsx, "pptx": _pptx, "pdf": _pdf, "html": _html,
        "odt": _odt, "ods": _ods, "odp": _odp, "rtf": _rtf, "doc": _doc, "xls": _xls,
        "ppt": _ppt, "eml": _eml, "msg": _msg,
    }  # fmt: skip
    reader = readers.get(kind, _plain)
    try:
        text, warning = reader(data, limit)
    except AttachmentError:
        raise
    except (zipfile.BadZipFile, zlib.error, ET.ParseError, EOFError, KeyError, ValueError,
            IndexError, OSError, struct.error):  # fmt: skip
        raise AttachmentError(_damaged(kind)) from None
    text = _tidy(text)
    if not text.strip():
        raise AttachmentError("There is no text in this file.")
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
        warning = f"Only the first {limit:,} characters are used."
    return Extracted(name, kind, text, len(text), truncated, warning)


def _refuse_unknown(ext: str, data: bytes) -> None:
    """Raise for a file type that is not read; return for unknown text files."""
    if ext in IMAGE_EXTS:
        raise AttachmentError(IMAGES_UNSUPPORTED, code="unsupported")
    if ext in BINARY_EXTS or looks_binary(data):
        what = f"{ext} files are" if ext else "This type of file is"
        raise AttachmentError(
            f"{what} not supported yet. Attach text, code, Word, Excel, PowerPoint or PDF files.",
            code="unsupported",
        )


_KIND_NAMES = {
    "docx": "Word document", "xlsx": "Excel workbook", "pptx": "PowerPoint file",
    "doc": "Word document", "xls": "Excel workbook", "ppt": "PowerPoint file",
    "odt": "OpenDocument text", "ods": "OpenDocument spreadsheet",
    "odp": "OpenDocument presentation", "msg": "Outlook message",
}  # fmt: skip


def _damaged(kind: str) -> str:
    what = _KIND_NAMES.get(kind)
    if what is None:
        return "The file could not be read; it may be damaged."
    return f"The file could not be read: it is damaged, password-protected or not a real {what}."


def _plain(data: bytes, limit: int) -> tuple[str, str | None]:
    if looks_binary(data):
        raise AttachmentError("This file is not text, so it cannot be read.", code="unsupported")
    return decode_text(data), None


def _html(data: bytes, limit: int) -> tuple[str, str | None]:
    from chatforge.tools import htmltext

    title, text = htmltext.extract(decode_text(data))
    return (f"{title}\n\n{text}" if title and title not in text[:200] else text), None


# --- zip parts -------------------------------------------------------------------------


class _Zip(zipfile.ZipFile):
    """An Office file's zip, with what is left of its budgets: the bytes all its parts may
    still unpack to (:data:`MAX_UNPACKED_BYTES`) and the XML elements still to be read
    (:data:`MAX_ELEMENTS`)."""

    def __init__(self, data: bytes) -> None:
        super().__init__(io.BytesIO(data))
        self.bytes_left = MAX_UNPACKED_BYTES
        self.elements_left = MAX_ELEMENTS


class _CappedReader:
    """A file-like reader over one part that refuses to unpack more than
    :data:`MAX_PART_BYTES`, or more than the file has left of its byte budget."""

    def __init__(self, fh: Any, zf: _Zip) -> None:
        self._fh = fh
        self._zf = zf
        self._left = MAX_PART_BYTES

    def read(self, size: int = -1) -> bytes:
        left = min(self._left, self._zf.bytes_left)
        want = left + 1 if size is None or size < 0 else min(size, left + 1)
        chunk = self._fh.read(want)
        self._left -= len(chunk)
        self._zf.bytes_left -= len(chunk)
        if self._left < 0 or self._zf.bytes_left < 0:
            raise AttachmentError(TOO_MUCH_DATA)
        return chunk


#: Finished children are dropped from their parent this many at a time: dropping them one
#: by one would shift the rest of the list (already parsed ahead) every time.
_DROP_BATCH = 256


def _iterparse(
    zf: _Zip,
    part: str,
    events: tuple[str, ...] = ("end",),
    keep: frozenset[str] = frozenset(),
) -> Iterator[tuple[str, ET.Element]]:
    """Stream one XML part as ``(event, element)`` pairs, in bounded memory.

    Once its "end" has been handled, an element is emptied and dropped from its parent,
    unless it is inside an element whose tag is in ``keep``: the readers look at those
    whole, at their "end", and they are dropped after that. Raises
    :class:`AttachmentError` past the file's element budget, :data:`MAX_KEPT_ELEMENTS`
    inside one kept element, or :data:`MAX_DEPTH`.
    """
    stack: list[ET.Element] = []  # the open elements, outermost first
    done: list[int] = []  # per open element: how many of its first children are finished
    kept = 0  # open elements whose tag is in ``keep``
    inside = 0  # elements started inside the outermost open kept element
    with zf.open(part) as fh:
        for event, el in ET.iterparse(_CappedReader(fh, zf), events=("start", "end")):
            if event == "start":
                zf.elements_left -= 1
                if kept:
                    inside += 1
                if zf.elements_left < 0 or inside > MAX_KEPT_ELEMENTS or len(stack) >= MAX_DEPTH:
                    raise AttachmentError(TOO_COMPLEX)
                stack.append(el)
                done.append(0)
                if el.tag in keep:
                    kept += 1
            else:
                stack.pop()
                done.pop()
                if el.tag in keep:
                    kept -= 1
            if event in events:
                yield event, el
            if event == "end" and not kept:
                inside = 0
                el.clear()
                if stack:
                    done[-1] += 1
                    if done[-1] >= _DROP_BATCH:
                        del stack[-1][: done[-1]]
                        done[-1] = 0


_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _rels(zf: _Zip, part: str, base: str) -> dict[str, str]:
    """``{relationship id: part name}`` from a ``.rels`` part (external targets skipped)."""
    if part not in zf.namelist():
        return {}
    out: dict[str, str] = {}
    for _, rel in _iterparse(zf, part):
        if rel.tag != f"{_PKG_REL}Relationship":
            continue
        target = rel.get("Target") or ""
        if not target or rel.get("TargetMode") == "External":
            continue
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            resolved = posixpath.normpath(posixpath.join(base, target))
        out[rel.get("Id") or ""] = resolved
    return out


def _numbered_parts(names: Iterable[str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    found = [(int(m.group(1)), n) for n in names if (m := rx.fullmatch(n))]
    return [n for _, n in sorted(found)]


def _open_zip(data: bytes) -> _Zip:
    return _Zip(data)


# --- .docx -----------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING_NAME = re.compile(r"heading\s*([1-6])$", re.IGNORECASE)


def _docx_styles(zf: _Zip) -> dict[str, str]:
    """``{styleId: prefix}``: ``"# "`` .. ``"###### "`` for headings (by the style's
    English name, which built-in styles keep in every language), ``"- "`` for list styles."""
    if "word/styles.xml" not in zf.namelist():
        return {}
    out: dict[str, str] = {}
    for _, style in _iterparse(zf, "word/styles.xml", keep=frozenset({f"{_W}style"})):
        if style.tag != f"{_W}style":
            continue
        sid = style.get(f"{_W}styleId") or ""
        name_el = style.find(f"{_W}name")
        name = (name_el.get(f"{_W}val") if name_el is not None else "") or sid
        m = _HEADING_NAME.match(name.strip())
        if m:
            out[sid] = "#" * int(m.group(1)) + " "
        elif name.strip().lower() == "title":
            out[sid] = "# "
        elif name.strip().lower().startswith(("list bullet", "list number")):
            out[sid] = "- "
    return out


def _docx_paragraph(p: ET.Element, styles: Mapping[str, str]) -> str:
    prefix = ""
    parts: list[str] = []
    for child in p:
        if child.tag == f"{_W}pPr":
            style = child.find(f"{_W}pStyle")
            sid = (style.get(f"{_W}val") if style is not None else "") or ""
            prefix = styles.get(sid, "")
            if not prefix and child.find(f"{_W}numPr") is not None:
                prefix = "- "
            continue
        for node in child.iter():
            tag = node.tag
            if tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif tag == f"{_W}tab":
                parts.append("\t")
            elif tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
            elif tag == f"{_W}noBreakHyphen":
                parts.append("-")
    text = "".join(parts).strip()
    return prefix + text if text else ""


_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _docx(data: bytes, limit: int) -> tuple[str, str | None]:
    """Paragraphs in order (headings as ``#``, list items as ``-``); table rows as
    ``cell | cell``."""
    out = _Out(limit)
    with _open_zip(data) as zf:
        styles = _docx_styles(zf)
        cells: list[list[str]] = []  # paragraphs of each open table cell (nested tables)
        rows: list[list[str]] = []  # cells of each open table row
        fallback = 0  # inside mc:Fallback, a second copy of a text box: skipped
        paragraph = frozenset({f"{_W}p"})
        for event, el in _iterparse(zf, "word/document.xml", ("start", "end"), paragraph):
            tag = el.tag
            if event == "start":
                if tag == f"{_W}tc":
                    cells.append([])
                elif tag == f"{_W}tr":
                    rows.append([])
                elif tag == _MC_FALLBACK:
                    fallback += 1
                continue
            if tag == _MC_FALLBACK:
                fallback -= 1
            elif tag == f"{_W}p" and fallback:
                el.clear()
            elif tag == f"{_W}p":
                line = _docx_paragraph(el, styles)
                if cells:
                    cells[-1].append(line)
                else:
                    out.add(line + "\n")
                el.clear()
            elif tag == f"{_W}tc" and cells:
                cell = " ".join(t.replace("\n", " ") for t in cells.pop() if t)
                if rows:
                    rows[-1].append(cell)
                el.clear()
            elif tag == f"{_W}tr" and rows:
                line = " | ".join(rows.pop())
                if cells:
                    cells[-1].append(line)
                elif line.strip(" |"):
                    out.add(line + "\n")
                el.clear()
            elif tag == f"{_W}tbl" and not cells:
                out.add("\n")
            if out.full:
                break
    return out.text(), None


# --- .xlsx -----------------------------------------------------------------------------

_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
#: Built-in number formats that show a date or time.
_DATE_FORMAT_IDS = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47})
_FORMAT_NOISE = re.compile(r'"[^"]*"|\\.|_.|\*.|\[[^\]]*\]')
_COLUMN = re.compile(r"([A-Za-z]+)")


def _rich_text(node: ET.Element | None) -> str:
    """The text of a shared or inline string (rich-text runs joined, phonetics left out)."""
    if node is None:
        return ""
    parts: list[str] = []
    for child in node:
        if child.tag == f"{_S}t":
            parts.append(child.text or "")
        elif child.tag == f"{_S}r":
            t = child.find(f"{_S}t")
            parts.append((t.text or "") if t is not None else "")
    return "".join(parts)


def _shared_strings(zf: _Zip) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    strings: list[str] = []
    for _, el in _iterparse(zf, "xl/sharedStrings.xml", keep=frozenset({f"{_S}si"})):
        if el.tag == f"{_S}si":
            strings.append(_rich_text(el))
            el.clear()
    return strings


def _is_date_format(code: str) -> bool:
    plain = _FORMAT_NOISE.sub("", code).lower()
    return "general" not in plain and re.search(r"[dmyhs]", plain) is not None


def _date_styles(zf: _Zip) -> set[int]:
    """Indexes of the cell formats (``c/@s``) that show a date or time."""
    if "xl/styles.xml" not in zf.namelist():
        return set()
    custom: dict[int, str] = {}  # numFmtId -> format code
    formats: list[int] = []  # the numFmtId of each cellXfs/xf, in order
    in_cell_xfs = False
    for event, el in _iterparse(zf, "xl/styles.xml", ("start", "end")):
        if el.tag == f"{_S}cellXfs":
            in_cell_xfs = event == "start"
        elif event == "end" and el.tag == f"{_S}numFmt":
            custom[int(el.get("numFmtId") or -1)] = el.get("formatCode") or ""
        elif event == "end" and el.tag == f"{_S}xf" and in_cell_xfs:
            formats.append(int(el.get("numFmtId") or 0))
    return {
        i
        for i, fmt in enumerate(formats)
        if fmt in _DATE_FORMAT_IDS or (fmt in custom and _is_date_format(custom[fmt]))
    }


def _excel_date(raw: str, date1904: bool) -> str:
    try:
        serial = float(raw)
    except ValueError:
        return raw
    if not 0 <= serial < 2_958_466:  # 9999-12-31
        return raw
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    moment = base + timedelta(seconds=round(serial * 86_400))
    if serial < 1 and not date1904:
        return moment.strftime("%H:%M:%S")
    if moment.hour == moment.minute == moment.second == 0:
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _number(raw: str) -> str:
    try:
        value = float(raw)
    except ValueError:
        return raw
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    short = f"{value:.15g}"
    return raw if "e" in short and "e" not in raw.lower() else short


def _cell_value(c: ET.Element, shared: list[str], dates: set[int], date1904: bool) -> str:
    kind = c.get("t") or "n"
    if kind == "inlineStr":
        return _rich_text(c.find(f"{_S}is"))
    v = c.find(f"{_S}v")
    raw = (v.text or "") if v is not None else ""
    if not raw:
        return ""
    if kind == "s":
        idx = int(raw)
        return shared[idx] if 0 <= idx < len(shared) else ""
    if kind == "b":
        return "TRUE" if raw.strip() == "1" else "FALSE"
    if kind in ("str", "e", "d"):
        return raw
    style = c.get("s")
    if style is not None and style.isdigit() and int(style) in dates:
        return _excel_date(raw, date1904)
    return _number(raw)


def _column_index(ref: str) -> int | None:
    """The 0-based column of a cell reference such as ``"AB12"`` (``None`` without one).

    A column past Excel's last (XFD) raises: a row is written out as wide as its last
    cell, so ``"ZZZZZ1"`` alone would be a row of 8 million empty fields.
    """
    m = _COLUMN.match(ref)
    if not m:
        return None
    index = 0
    for ch in m.group(1)[:4].upper():  # four letters are always past XFD
        index = index * 26 + (ord(ch) - 64)
    if index > MAX_XLSX_COLUMNS:
        raise AttachmentError(_damaged("xlsx"))
    return index - 1


def _workbook_sheets(zf: _Zip) -> tuple[list[tuple[str, str]], bool]:
    """``([(sheet name, part)], date1904)`` in workbook order."""
    names = set(zf.namelist())
    sheets: list[tuple[str, str]] = []
    date1904 = False
    if "xl/workbook.xml" in names:
        listed: list[tuple[str, str]] = []  # (sheet name, relationship id)
        for _, el in _iterparse(zf, "xl/workbook.xml"):
            if el.tag == f"{_S}workbookPr":
                date1904 = (el.get("date1904") or "").lower() in ("1", "true")
            elif el.tag == f"{_S}sheet":
                listed.append((el.get("name") or "", el.get(f"{_DOC_REL}id") or ""))
        rels = _rels(zf, "xl/_rels/workbook.xml.rels", "xl")
        for name, rel_id in listed:
            part = rels.get(rel_id)
            if part and part in names:
                sheets.append((name or posixpath.basename(part), part))
    if not sheets:
        parts = _numbered_parts(names, r"xl/worksheets/sheet(\d+)\.xml")
        sheets = [(f"Sheet{i}", p) for i, p in enumerate(parts, 1)]
    return sheets, date1904


def _xlsx(data: bytes, limit: int) -> tuple[str, str | None]:
    """Each sheet as ``## Sheet: name`` followed by its rows as CSV."""
    out = _Out(limit)
    with _open_zip(data) as zf:
        shared = _shared_strings(zf)
        dates = _date_styles(zf)
        sheets, date1904 = _workbook_sheets(zf)
        for title, part in sheets:
            out.add(f"## Sheet: {title}\n")
            row: dict[int, str] = {}
            next_col = 0
            wrote = False
            for _, el in _iterparse(zf, part, keep=frozenset({f"{_S}c"})):
                if el.tag == f"{_S}c":
                    col = _column_index(el.get("r") or "")
                    col = next_col if col is None else col
                    if col >= MAX_XLSX_COLUMNS:  # cells without a reference, past XFD
                        raise AttachmentError(_damaged("xlsx"))
                    next_col = col + 1
                    value = _cell_value(el, shared, dates, date1904)
                    if value != "":
                        row[col] = value
                    el.clear()
                elif el.tag == f"{_S}row":
                    if row:
                        buf = io.StringIO()
                        csv.writer(buf, lineterminator="\n").writerow(
                            [row.get(i, "") for i in range(max(row) + 1)]
                        )
                        out.add(buf.getvalue())
                        wrote = True
                    row, next_col = {}, 0
                    el.clear()
                    if out.full:
                        break
            out.add("\n" if wrote else "(empty)\n\n")
            if out.full:
                break
    return out.text(), None


# --- .pptx -----------------------------------------------------------------------------

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _slide_parts(zf: _Zip) -> list[str]:
    names = set(zf.namelist())
    slides: list[str] = []
    if "ppt/presentation.xml" in names:
        rel_ids = [
            el.get(f"{_DOC_REL}id") or ""
            for _, el in _iterparse(zf, "ppt/presentation.xml")
            if el.tag == f"{_P}sldId"
        ]
        rels = _rels(zf, "ppt/_rels/presentation.xml.rels", "ppt")
        for rel_id in rel_ids:
            part = rels.get(rel_id)
            if part and part in names:
                slides.append(part)
    return slides or _numbered_parts(names, r"ppt/slides/slide(\d+)\.xml")


def _pptx(data: bytes, limit: int) -> tuple[str, str | None]:
    """Each slide as ``## Slide n`` followed by its text, one paragraph per line."""
    out = _Out(limit)
    with _open_zip(data) as zf:
        for number, part in enumerate(_slide_parts(zf), 1):
            lines: list[str] = []
            for _, el in _iterparse(zf, part, keep=frozenset({f"{_A}p"})):
                if el.tag == f"{_A}p":
                    text = "".join(
                        (n.text or "") if n.tag == f"{_A}t" else "\n"
                        for n in el.iter()
                        if n.tag in (f"{_A}t", f"{_A}br")
                    ).strip()
                    if text:
                        lines.append(text)
                    el.clear()
            out.add(f"## Slide {number}\n" + ("\n".join(lines) or "(no text)") + "\n\n")
            if out.full:
                break
    return out.text(), None


# --- OpenDocument: .odt, .ods, .odp ----------------------------------------------------

PASSWORD_PROTECTED = "This file is password-protected. Remove the password and attach it again."
#: The most rows a ``.ods`` row repeats to (LibreOffice's last row).
MAX_ODS_ROWS = 1_048_576

_OD_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_OD_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_OD_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_OD_DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
_OD_PRES = "{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}"
_OD_SVG = "{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}"
_OD_MANIFEST = "{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}"
_OD_PARAGRAPHS = frozenset({f"{_OD_TEXT}p", f"{_OD_TEXT}h"})
_OD_CELLS = frozenset({f"{_OD_TABLE}table-cell", f"{_OD_TABLE}covered-table-cell"})
_OD_ITEMS = frozenset({f"{_OD_TEXT}list-item", f"{_OD_TEXT}list-header"})
#: Paragraphs inside these are not lines of their own: comments, tracked deletions,
#: speaker notes, and notes (written in brackets where they are anchored).
_OD_HIDDEN = frozenset(
    {f"{_OD_OFFICE}annotation", f"{_OD_TEXT}tracked-changes", f"{_OD_TEXT}note", f"{_OD_PRES}notes"}
)
#: Left out of a paragraph's text: comments, pictures' titles and descriptions.
_OD_NOT_TEXT = frozenset(
    {f"{_OD_OFFICE}annotation", f"{_OD_SVG}title", f"{_OD_SVG}desc", f"{_OD_OFFICE}binary-data"}
)
_OD_SPACE = re.compile(r"[ \t\r\n]+")
_OD_TIME = re.compile(r"-?PT(\d+)H(\d+)M(\d+)(?:[.,]\d+)?S")


def _od_count(value: str | None, most: int) -> int:
    """A count or level attribute as a number from 1 to ``most`` (1 when unreadable)."""
    try:
        return max(1, min(int(value or 1), most))
    except ValueError:
        return 1


def _od_inline(el: ET.Element, parts: list[str]) -> None:
    """Add the text inside a paragraph to ``parts``: white space collapsed as ODF does,
    ``text:s`` spaces, tabs and line breaks kept, a note as ``[mark: text]``."""
    if el.text:
        parts.append(_OD_SPACE.sub(" ", el.text))
    for child in el:
        tag = child.tag
        if tag == f"{_OD_TEXT}s":
            parts.append(" " * _od_count(child.get(f"{_OD_TEXT}c"), 1000))
        elif tag == f"{_OD_TEXT}tab":
            parts.append("\t")
        elif tag == f"{_OD_TEXT}line-break":
            parts.append("\n")
        elif tag == f"{_OD_TEXT}note":
            parts.append(_od_note(child))
        elif tag not in _OD_NOT_TEXT:
            _od_inline(child, parts)
        if child.tail:
            parts.append(_OD_SPACE.sub(" ", child.tail))


def _od_text(el: ET.Element) -> str:
    parts: list[str] = []
    _od_inline(el, parts)
    return "".join(parts).strip()


def _od_note(note: ET.Element) -> str:
    """A footnote or endnote as ``[mark: text]``."""
    mark = ""
    body: list[str] = []
    for child in note:
        if child.tag == f"{_OD_TEXT}note-citation":
            mark = "".join(child.itertext()).strip()
        elif child.tag == f"{_OD_TEXT}note-body":
            body.extend(_od_text(p) for p in child.iter() if p.tag in _OD_PARAGRAPHS)
    text = " ".join(t for t in body if t)
    if not text:
        return ""
    return f"[{mark}: {text}]" if mark else f"[{text}]"


def _od_content(zf: _Zip) -> str:
    """The part with the document (``content.xml``); refuses password-protected files,
    whose parts are encrypted."""
    names = set(zf.namelist())
    if "META-INF/manifest.xml" in names:
        for _, el in _iterparse(zf, "META-INF/manifest.xml"):
            if el.tag == f"{_OD_MANIFEST}encryption-data":
                raise AttachmentError(PASSWORD_PROTECTED)
    if "content.xml" not in names:
        raise KeyError("content.xml")
    return "content.xml"


def _odt(data: bytes, limit: int) -> tuple[str, str | None]:
    """Paragraphs in order (headings as ``#``, list items as ``-``), table rows as
    ``cell | cell``, notes in brackets; comments and tracked deletions left out."""
    out = _Out(limit)
    with _open_zip(data) as zf:
        part = _od_content(zf)
        hidden = 0  # open comments, notes and tracked changes
        lists = 0  # open lists (a nested list is indented)
        items: list[bool] = []  # per open list item: its bullet is still to be written
        cells: list[list[str]] = []  # paragraphs of each open table cell (nested tables)
        rows: list[list[str]] = []  # cells of each open table row
        for event, el in _iterparse(zf, part, ("start", "end"), _OD_PARAGRAPHS):
            tag = el.tag
            if event == "start":
                if tag in _OD_HIDDEN:
                    hidden += 1
                elif tag == f"{_OD_TEXT}list":
                    lists += 1
                elif tag in _OD_ITEMS:
                    items.append(tag == f"{_OD_TEXT}list-item")
                elif tag in _OD_CELLS:
                    cells.append([])
                elif tag == f"{_OD_TABLE}table-row":
                    rows.append([])
                continue
            if tag in _OD_HIDDEN:
                hidden -= 1
            elif tag in _OD_PARAGRAPHS:
                if hidden:
                    continue  # read with the paragraph around it (a note), or not at all
                line = _od_text(el)
                if line and tag == f"{_OD_TEXT}h":
                    line = "#" * _od_count(el.get(f"{_OD_TEXT}outline-level"), 6) + " " + line
                elif line and items:
                    line = "  " * (lists - 1) + ("- " if items[-1] else "  ") + line
                    items[-1] = False
                if cells:
                    cells[-1].append(line)
                elif line:
                    out.add(line + "\n")
                el.clear()  # a text box's paragraphs are not read again with the one around it
            elif tag == f"{_OD_TEXT}list":
                lists -= 1
            elif tag in _OD_ITEMS and items:
                items.pop()
            elif tag in _OD_CELLS and cells:
                cell = " ".join(t.replace("\n", " ") for t in cells.pop() if t)
                if rows:
                    rows[-1].append(cell)
            elif tag == f"{_OD_TABLE}table-row" and rows:
                row = rows.pop()
                while row and not row[-1]:
                    row.pop()
                line = " | ".join(row)
                if cells:
                    cells[-1].append(line)
                elif line:
                    out.add(line + "\n")
            elif tag == f"{_OD_TABLE}table" and not cells:
                out.add("\n")
            if out.full:
                break
    return out.text(), None


def _ods_value(cell: ET.Element) -> str:
    """A cell's value: numbers in full, dates as ``YYYY-MM-DD[ hh:mm:ss]``, times as
    ``hh:mm:ss``, booleans as ``TRUE``/``FALSE``, anything else as the text it shows."""
    kind = cell.get(f"{_OD_OFFICE}value-type")
    if kind in ("float", "percentage", "currency"):
        raw = cell.get(f"{_OD_OFFICE}value")
        if raw:
            return _number(raw)
    elif kind == "date":
        raw = cell.get(f"{_OD_OFFICE}date-value")
        if raw:
            return raw.removesuffix("T00:00:00").replace("T", " ")
    elif kind == "time":
        m = _OD_TIME.fullmatch(cell.get(f"{_OD_OFFICE}time-value") or "")
        if m:
            hours, minutes, seconds = (int(g) for g in m.groups())
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    elif kind == "boolean":
        raw = cell.get(f"{_OD_OFFICE}boolean-value")
        if raw:
            return "TRUE" if raw.strip().lower() == "true" else "FALSE"
    return "\n".join(t for t in (_od_text(p) for p in cell if p.tag in _OD_PARAGRAPHS) if t)


def _ods(data: bytes, limit: int) -> tuple[str, str | None]:
    """Each sheet as ``## Sheet: name`` followed by its rows as CSV, like ``.xlsx``. A
    repeated cell or row is written as often as it repeats, empty ones only move on; a
    value past column 16,384 (the last in Excel and LibreOffice) is damage."""
    out = _Out(limit)
    with _open_zip(data) as zf:
        part = _od_content(zf)
        depth = 0  # open tables: only the outermost one is a sheet
        row: dict[int, str] = {}
        col = 0
        wrote = False
        for event, el in _iterparse(zf, part, ("start", "end"), _OD_CELLS):
            tag = el.tag
            if tag == f"{_OD_TABLE}table":
                depth += 1 if event == "start" else -1
                if event == "start" and depth == 1:
                    title = el.get(f"{_OD_TABLE}name") or "Sheet"
                    out.add(f"## Sheet: {title}\n")
                    row, col, wrote = {}, 0, False
                elif event == "end" and depth == 0:
                    out.add("\n" if wrote else "(empty)\n\n")
            elif event == "end" and depth == 1 and tag in _OD_CELLS:
                repeated = el.get(f"{_OD_TABLE}number-columns-repeated")
                span = _od_count(repeated, MAX_XLSX_COLUMNS + 1)
                value = _ods_value(el)
                if value:
                    if col + span > MAX_XLSX_COLUMNS:
                        raise AttachmentError(_damaged("ods"))
                    row.update(dict.fromkeys(range(col, col + span), value))
                col += span
            elif event == "end" and depth == 1 and tag == f"{_OD_TABLE}table-row":
                if row:
                    buf = io.StringIO()
                    csv.writer(buf, lineterminator="\n").writerow(
                        [row.get(i, "") for i in range(max(row) + 1)]
                    )
                    line = buf.getvalue()
                    repeats = _od_count(el.get(f"{_OD_TABLE}number-rows-repeated"), MAX_ODS_ROWS)
                    for _ in range(repeats):
                        out.add(line)
                        if out.full:
                            break
                    wrote = True
                row, col = {}, 0
            if out.full:
                break
    return out.text(), None


def _odp(data: bytes, limit: int) -> tuple[str, str | None]:
    """Each slide as ``## Slide n`` followed by its text, one paragraph per line, like
    ``.pptx`` (speaker notes left out)."""
    out = _Out(limit)
    with _open_zip(data) as zf:
        part = _od_content(zf)
        hidden = 0
        number = 0
        lines: list[str] | None = None  # the open slide's paragraphs
        for event, el in _iterparse(zf, part, ("start", "end"), _OD_PARAGRAPHS):
            tag = el.tag
            if event == "start":
                if tag in _OD_HIDDEN:
                    hidden += 1
                elif tag == f"{_OD_DRAW}page":
                    number, lines = number + 1, []
                continue
            if tag in _OD_HIDDEN:
                hidden -= 1
            elif tag in _OD_PARAGRAPHS and not hidden:
                text = _od_text(el)
                if text and lines is not None:
                    lines.append(text)
                el.clear()
            elif tag == f"{_OD_DRAW}page" and lines is not None:
                out.add(f"## Slide {number}\n" + ("\n".join(lines) or "(no text)") + "\n\n")
                lines = None
                if out.full:
                    break
    return out.text(), None


# --- .rtf ------------------------------------------------------------------------------

#: The most control words, braces and text runs read from one RTF file.
MAX_RTF_TOKENS = 5_000_000

_RTF_TOKEN = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"  # a control word, its number, one space
    r"|\\'([0-9a-fA-F]{2})"  # a byte of text in the current code page
    r"|\\(.)"  # a control symbol
    r"|([{}])"
    r"|([^\\{}\r\n]+)"  # text
    r"|[\r\n]+",  # line breaks in the file are not text
    re.DOTALL,
)
#: Groups that are not text of the document: tables of fonts, colours, styles and lists,
#: document properties, pictures, objects, shapes' properties and fallback pictures,
#: field codes (their results are kept), bookmarks, index entries, headers, footers,
#: footnotes and comments.
_RTF_SKIP = frozenset(
    {
        "aftncn", "aftnsep", "aftnsepc", "annotation", "atnauthor", "atndate", "atnicn",
        "atnid", "atnparent", "atnref", "atntime", "atrfend", "atrfstart", "author",
        "bkmkend", "bkmkstart", "buptim", "category", "colorschememapping", "colortbl",
        "comment", "company", "creatim", "datafield", "datastore", "docvar", "doccomm",
        "falt", "filetbl", "fldinst", "fonttbl", "footer", "footerf", "footerl", "footerr",
        "footnote", "ftncn", "ftnsep", "ftnsepc", "generator", "header", "headerf",
        "headerl", "headerr", "info", "keywords", "latentstyles", "listoverridetable",
        "listpicture", "listtable", "manager", "nonshppict", "objdata", "object",
        "operator", "pgdsctbl", "pict", "pn", "pntxta", "pntxtb", "printim", "private",
        "protusertbl", "revtbl", "revtim", "rsidtbl", "shprslt", "sp", "stylesheet",
        "subject", "tc",
        "template", "themedata", "title", "txe", "userprops", "wgrffmtfilter", "xe",
        "xmlnstbl",
    }
)  # fmt: skip
_RTF_CHARS = {
    "par": "\n", "sect": "\n", "page": "\n", "line": "\n", "column": "\n", "tab": "\t",
    "cell": "\x1f", "nestcell": "\x1f", "row": "\n", "nestrow": "\n", "emdash": "\u2014",
    "endash": "\u2013", "bullet": "\u2022", "lquote": "\u2018", "rquote": "\u2019",
    "ldblquote": "\u201c", "rdblquote": "\u201d", "emspace": " ", "enspace": " ",
    "qmspace": " ",
}  # fmt: skip
_RTF_PARAGRAPH_ENDS = frozenset({"par", "sect", "page", "row", "cell", "nestcell", "nestrow"})
_RTF_SYMBOLS = {"\\": "\\", "{": "{", "}": "}", "~": " ", "_": "-", "\n": "\n", "\r": "\n"}
#: ``\fcharset`` numbers and the code pages their fonts' text is in.
_RTF_CHARSETS = {
    0: "cp1252", 77: "mac_roman", 128: "cp932", 129: "cp949", 130: "johab", 134: "gbk",
    136: "cp950", 161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255",
    178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250", 254: "cp437",
}  # fmt: skip


def _codec(name: str, fallback: str = "cp1252") -> str:
    """``name`` when Python has that codec, else ``fallback``."""
    try:
        return codecs.lookup(name).name
    except LookupError:
        return fallback


def _table_rows(text: str) -> str:
    """Table rows, whose cells end in ``\\x1f``, as ``cell | cell`` (RTF and ``.doc``)."""
    if "\x1f" not in text:
        return text
    lines = []
    for line in text.split("\n"):
        if "\x1f" in line:
            cells = [c.strip() for c in line.split("\x1f")]
            while cells and not cells[-1]:
                cells.pop()
            line = " | ".join(cells)
        lines.append(line)
    return "\n".join(lines)


def _rtf(data: bytes, limit: int) -> tuple[str, str | None]:
    """The text of an RTF document: paragraphs, line breaks and tabs kept, headings (by
    outline level) as ``#``, table rows as ``cell | cell``, text boxes where they are
    anchored, ``\\'hh`` bytes decoded in their font's code page, ``\\uN`` characters
    with their fallbacks skipped; everything that is not text left out."""
    body = data.lstrip(b" \t\r\n\x00").removeprefix(codecs.BOM_UTF8)
    if not body.startswith(b"{\\rtf"):
        return _plain(data, limit)  # a text file named .rtf
    src = body.decode("latin-1")
    out = _Out(limit)
    fonts: dict[int, str] = {}  # font number -> code page, from the font table
    ansi = "cp1252"
    default_font: int | None = None
    font: int | None = None  # the font being defined in the font table
    # Set per group, and put back when it closes: left out; an optional destination
    # announced (\*); the \uc fallback length; the code page; in the font table; the
    # paragraph's outline level (a heading).
    stack: list[tuple[bool, bool, int, str, bool, int | None]] = []
    skipped, optional, uc, codepage, fonttbl, level = False, False, 1, ansi, False, None
    skip = 0  # fallback characters still to skip after a \u character
    pending = bytearray()  # \'hh bytes, decoded together (a character may take two)
    starts = True  # no text yet in this paragraph

    def emit(text: str) -> None:
        nonlocal starts
        if starts and not text.isspace():
            starts = False
            if level is not None:
                out.add("#" * min(level + 1, 6) + " ")
        out.add(text)

    pos, end, tokens = 0, len(src), 0
    while pos < end and not out.full:
        m = _RTF_TOKEN.match(src, pos)
        if m is None:
            break  # a backslash at the very end
        pos = m.end()
        tokens += 1
        if tokens > MAX_RTF_TOKENS:
            raise AttachmentError(TOO_COMPLEX)
        word, number, hex_byte, symbol, brace, text = m.groups()
        if hex_byte is not None:
            if skip:
                skip -= 1
            elif not skipped:
                pending.append(int(hex_byte, 16))
            continue
        if word is None and symbol is None and brace is None and text is None:
            continue  # a line break in the file
        if pending:
            emit(pending.decode(codepage, "replace"))
            pending.clear()
        if brace == "{":
            if len(stack) >= MAX_DEPTH:
                raise AttachmentError(TOO_COMPLEX)
            stack.append((skipped, optional, uc, codepage, fonttbl, level))
            optional = False
            skip = 0
            continue
        if brace == "}":
            if not stack:
                break
            skipped, optional, uc, codepage, fonttbl, level = stack.pop()
            skip = 0
            if not stack:
                break  # the end of the document
            continue
        if skip:  # the fallback of a \u character: one character, \'hh or control word each
            if text is None:
                skip -= 1
                continue
            cut = min(skip, len(text))
            text, skip = text[cut:], skip - cut
            if not text:
                continue
        if text is not None:
            if not skipped:
                emit(text if text.isascii() else text.encode("latin-1").decode(codepage, "replace"))
            continue
        if symbol is not None:
            if symbol == "*":
                optional = True
            elif not skipped:
                emit(_RTF_SYMBOLS.get(symbol, ""))
            continue
        if optional:  # a destination readers may skip: all but a shape's text box are
            optional = False
            skipped = skipped or word != "shpinst"
        if word == "bin":  # binary data: skipped without reading it
            pos += max(0, int(number or 0))
        elif word in _RTF_SKIP:
            skipped, fonttbl = True, word == "fonttbl"
        elif fonttbl:
            if word == "f" and number:
                font = int(number)
            elif word == "fcharset" and number and font is not None:
                if int(number) in _RTF_CHARSETS:
                    fonts[font] = _RTF_CHARSETS[int(number)]
            elif word == "cpg" and number and font is not None:
                fonts[font] = _codec(f"cp{number}", ansi)
        elif word == "u" and number:
            if not skipped:
                emit(chr(int(number) & 0xFFFF))
            skip = uc
        elif word == "uc" and number:
            uc = max(0, min(int(number), 16))
        elif word == "f" and number:
            codepage = fonts.get(int(number), ansi)
        elif word == "plain":
            codepage = fonts.get(default_font, ansi) if default_font is not None else ansi
        elif word == "pard":
            level = None
        elif word == "outlinelevel":
            level = int(number) if number and 0 <= int(number) <= 8 else None
        elif word == "deff" and number:
            default_font = int(number)
        elif word == "ansicpg" and number:
            ansi = codepage = _codec("utf-8" if number == "65001" else f"cp{number}")
        elif word in ("mac", "pc", "pca"):
            ansi = codepage = {"mac": "mac_roman", "pc": "cp437", "pca": "cp850"}[word]
        elif not skipped and word in _RTF_CHARS:
            out.add(_RTF_CHARS[word])
            if word in _RTF_PARAGRAPH_ENDS:
                starts = True
    if pending:
        emit(pending.decode(codepage, "replace"))
    # \u numbers are UTF-16: a character past U+FFFF comes as two of them.
    text = out.text().encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return _table_rows(text), None


# --- OLE2 compound files: .doc, .xls, .ppt, .msg ----------------------------------------

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_LAST_SECTOR = 0xFFFFFFFA  # higher numbers end a chain or mark special sectors


class _Ole:
    """A read-only OLE2 compound file (MS-CFB), the container of old Word, Excel and
    PowerPoint files and of Outlook messages. Every sector chain is checked for loops
    and every stream is cut to the file's own size, so a damaged or hostile file cannot
    unpack to more than it holds. Raises ``ValueError`` or ``struct.error`` when broken."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(_OLE_MAGIC):
            raise ValueError("not an OLE2 file")
        major, _, shift, mini_shift = struct.unpack_from("<HHHH", data, 0x1A)
        if shift not in (9, 12) or mini_shift != 6:
            raise ValueError("unknown sector size")
        self._data = data
        self._size = 1 << shift
        self._count = len(data) // self._size - 1  # sectors in the file
        first_dir = struct.unpack_from("<I", data, 0x30)[0]
        self._cutoff, self._first_mini, _, first_difat = struct.unpack_from("<IIII", data, 0x38)
        # The FAT's own sectors: 109 listed in the header, more in a chain of DIFAT sectors.
        per = self._size // 4
        need = -(-self._count // per)  # FAT sectors enough to map every sector of the file
        fat_sectors = [s for s in struct.unpack_from("<109I", data, 0x4C) if s <= _OLE_LAST_SECTOR]
        seen: set[int] = set()
        sector = first_difat
        while sector <= _OLE_LAST_SECTOR and len(fat_sectors) < need:
            if sector in seen or sector >= self._count:
                raise ValueError("bad DIFAT chain")
            seen.add(sector)
            entries = struct.unpack_from(f"<{per}I", data, self._offset(sector))
            fat_sectors.extend(s for s in entries[:-1] if s <= _OLE_LAST_SECTOR)
            sector = entries[-1]
        self._fat: list[int] = []
        for s in fat_sectors[:need]:
            if s >= self._count:
                raise ValueError("FAT sector out of range")
            self._fat.extend(struct.unpack_from(f"<{per}I", data, self._offset(s)))
        self._mini: tuple[bytes, list[int]] | None = None
        self._streams: dict[str, tuple[int, int]] = {}  # lower-case path -> (start, size)
        self.storages: list[str] = []  # storage paths, ``/``-separated
        directory = self._chain_bytes(first_dir, len(data))
        entries_ = [
            struct.unpack_from("<64sHBBIII16sIQQIQ", directory, off)
            for off in range(0, len(directory) - 127, 128)
        ]
        if not entries_:
            raise ValueError("no directory")
        root = entries_[0]
        self._root = (root[11], root[12] if major >= 4 else root[12] & 0xFFFFFFFF)
        todo = [(root[6], "")]  # (entry, path of its storage)
        visited: set[int] = set()
        while todo:
            index, parent = todo.pop()
            if index >= len(entries_):
                continue  # 0xFFFFFFFF: no entry
            if index in visited:
                raise ValueError("directory loop")
            visited.add(index)
            raw, name_size, kind, _, left, right, child, *_, start, size = entries_[index]
            todo += [(left, parent), (right, parent)]
            name = raw[: max(0, min(name_size, 64) - 2)].decode("utf-16-le", "replace")
            path = f"{parent}/{name}" if parent else name
            if kind == 2:  # a stream
                self._streams[path.lower()] = (start, size if major >= 4 else size & 0xFFFFFFFF)
            elif kind == 1:  # a storage
                self.storages.append(path)
                todo.append((child, path))

    def _offset(self, sector: int) -> int:
        return (sector + 1) * self._size

    def _chain(self, start: int, table: list[int], count: int) -> Iterator[int]:
        seen: set[int] = set()
        sector = start
        while sector <= _OLE_LAST_SECTOR:
            if sector in seen or sector >= count or sector >= len(table):
                raise ValueError("bad sector chain")
            seen.add(sector)
            yield sector
            sector = table[sector]

    def _chain_bytes(self, start: int, size: int) -> bytes:
        parts: list[bytes] = []
        got = 0
        for sector in self._chain(start, self._fat, self._count):
            if got >= size:
                break
            offset = self._offset(sector)
            parts.append(self._data[offset : offset + self._size])
            got += self._size
        return b"".join(parts)[:size]

    def has(self, path: str) -> bool:
        return path.lower() in self._streams

    def read(self, path: str) -> bytes:
        """One stream (``KeyError`` when there is none)."""
        start, size = self._streams[path.lower()]
        size = min(size, len(self._data))
        if size >= self._cutoff:
            return self._chain_bytes(start, size)
        if self._mini is None:  # small streams are kept in 64-byte sectors of one stream
            stream = self._chain_bytes(*self._root)
            table = self._chain_bytes(self._first_mini, len(self._data))
            table = table[: len(table) // 4 * 4]
            self._mini = stream, [n for (n,) in struct.iter_unpack("<I", table)]
        stream, table = self._mini
        parts: list[bytes] = []
        for sector in self._chain(start, table, len(stream) // 64):
            if len(parts) * 64 >= size:
                break
            parts.append(stream[sector * 64 : sector * 64 + 64])
        return b"".join(parts)[:size]

    def streams(self) -> list[str]:
        """The paths of all streams, lower-case."""
        return list(self._streams)


class _Unreadable(Exception):
    """An old Office file that the readers here cannot read (Office itself may)."""


# --- .doc ------------------------------------------------------------------------------

_WORD_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f]")
_ASTRAL = re.compile("[\U00010000-\U0010ffff]")  # two UTF-16 units each
#: Word's control characters that stand for text; the others (pictures, note marks,
#: optional hyphens) are dropped. Paragraph and cell marks are handled apart.
_WORD_CHARS = {"\x0b": "\n", "\x0c": "\n", "\x0e": "\n", "\x1e": "-"}
#: Built-in styles by their language-independent number (``sti``): Heading 1-9,
#: Title, List Bullet and List Number (1-5).
_WORD_STYLES = {
    **{sti: "#" * min(sti, 6) + " " for sti in range(1, 10)},
    62: "# ",
    **dict.fromkeys((48, 49, *range(54, 62)), "- "),
}
#: The size of a Word property's operand by the property's top three bits.
_SPRM_SIZES = (1, 1, 2, 4, 2, 2, None, 3)
#: A paragraph's properties: ``(prefix, inside a table, ends a table row)``.
_NO_PAP = ("", False, False)


def _sprms(grpprl: bytes) -> Iterator[tuple[int, bytes]]:
    """The ``(sprm, operand)`` pairs of a list of Word properties (MS-DOC 2.2.5)."""
    i = 0
    while i + 2 <= len(grpprl):
        sprm = grpprl[i] | grpprl[i + 1] << 8
        i += 2
        size = _SPRM_SIZES[sprm >> 13]
        if size is None:  # variable: a size first
            if sprm == 0xD608 and i + 2 <= len(grpprl):  # sprmTDefTable: two bytes of it
                size = 1 + struct.unpack_from("<H", grpprl, i)[0]
            elif i >= len(grpprl) or (sprm == 0xC615 and grpprl[i] == 255):
                return  # cut short, or sprmPChgTabs' long form: nothing needed follows
            else:
                size = 1 + grpprl[i]
        yield sprm, grpprl[i : i + size]
        i += size


def _word_styles(word: bytes, table: bytes, fib: int) -> dict[int, str]:
    """``{style index: prefix}`` for the heading and list styles of the style sheet."""
    fc, lcb = struct.unpack_from("<iI", word, fib + 1 * 8)  # fcStshf
    stsh = table[fc : fc + lcb] if fc >= 0 else b""
    if len(stsh) < 4:
        return {}
    pos = 2 + struct.unpack_from("<H", stsh)[0]
    found: dict[int, str] = {}
    for istd in range(struct.unpack_from("<H", stsh, 2)[0]):
        if pos + 4 > len(stsh):
            break
        size = struct.unpack_from("<H", stsh, pos)[0]
        if size >= 2:
            sti = struct.unpack_from("<H", stsh, pos + 2)[0] & 0x0FFF
            if sti in _WORD_STYLES:
                found[istd] = _WORD_STYLES[sti]
        pos += 2 + size
    return found


def _word_papx(page: bytes, offset: int, styles: Mapping[int, str]) -> tuple[str, bool, bool]:
    """The properties of one paragraph, from its PAPX in a 512-byte FKP page."""
    if offset + 2 > len(page):
        return _NO_PAP
    cb = page[offset]
    if cb:
        papx = page[offset + 1 : offset + 2 * cb]
    else:
        papx = page[offset + 2 : offset + 2 + 2 * page[offset + 1]]
    if len(papx) < 2:
        return _NO_PAP
    prefix = styles.get(struct.unpack_from("<H", papx)[0], "")
    in_table = row_end = False
    for sprm, arg in _sprms(papx[2:]):
        on = any(arg)
        if sprm in (0x2416, 0x244B, 0x6649):  # sprmPFInTable, sprmPFInnerTableCell, sprmPItap
            in_table = in_table or on
        elif sprm in (0x2417, 0x244C):  # sprmPFTtp, sprmPFInnerTtp: a row's end mark
            row_end = row_end or on
        elif sprm == 0x460B and on and not prefix:  # sprmPIlfo: a list item
            prefix = "- "
    return prefix, in_table, row_end


class _WordParagraphs:
    """Paragraph properties by offset in the WordDocument stream, from the FKP pages
    its bin table (PlcBtePapx) lists. ``known`` is false when there are none."""

    def __init__(self, word: bytes, table: bytes, fib: int) -> None:
        styles = _word_styles(word, table, fib)
        fc, lcb = struct.unpack_from("<iI", word, fib + 13 * 8)  # fcPlcfBtePapx
        plc = table[fc : fc + lcb] if fc >= 0 else b""
        n = (len(plc) - 4) // 8
        pages: set[int] = set()
        if n > 0:
            pages = {pn & 0x3FFFFF for pn in struct.unpack_from(f"<{n}I", plc, 4 * (n + 1))}
        runs: list[tuple[int, int, tuple[str, bool, bool]]] = []
        for pn in sorted(pages):
            page = word[pn * 512 : pn * 512 + 512]
            count = page[511] if len(page) == 512 else 0
            if 17 * count + 4 > 511:  # (count + 1) offsets and count 13-byte entries
                continue
            fcs = struct.unpack_from(f"<{count + 1}I", page)
            for k in range(count):
                offset = 2 * page[4 * (count + 1) + 13 * k]
                pap = _word_papx(page, offset, styles) if offset else _NO_PAP
                runs.append((fcs[k], fcs[k + 1], pap))
        runs.sort()
        self._starts = [run[0] for run in runs]
        self._runs = runs
        self.known = bool(runs)

    def at(self, fc: int) -> tuple[str, bool, bool]:
        """The properties of the paragraph whose mark is at offset ``fc``."""
        i = bisect.bisect_right(self._starts, fc) - 1
        if i >= 0 and fc < self._runs[i][1]:
            return self._runs[i][2]
        return _NO_PAP


class _WordText:
    """Turns Word's text into lines: field codes left out (their results kept),
    headings and list items prefixed as the ``.docx`` reader does, a table row as cells
    ended by ``\\x1f`` (see :func:`_table_rows`)."""

    def __init__(self, out: _Out, paragraphs: _WordParagraphs) -> None:
        self.out = out
        self.paragraphs = paragraphs
        self.fields: list[bool] = []  # per open field: its result has started
        self.hidden = 0  # open fields still in their codes
        self.line: list[str] = []  # the open paragraph's text
        self.size = 0
        self.after_cell = False  # the last mark ended a table cell

    def feed(self, text: str, fc: int, width: int) -> None:
        """Add ``text``, which starts at offset ``fc`` of the stream, ``width`` bytes a
        character (a character past U+FFFF takes two UTF-16 units)."""
        pos = units = 0
        astral = width == 2 and _ASTRAL.search(text) is not None
        for m in _WORD_CONTROL.finditer(text):
            self._add(text[pos : m.start()])
            if astral:
                units += len(_ASTRAL.findall(text, pos, m.start()))
            pos = m.end()
            ch = m.group()
            if ch == "\x13":  # a field starts: its code
                if len(self.fields) >= MAX_DEPTH:
                    raise AttachmentError(TOO_COMPLEX)
                self.fields.append(False)
                self.hidden += 1
            elif ch == "\x14":  # the field's result
                if self.fields and not self.fields[-1]:
                    self.fields[-1] = True
                    self.hidden -= 1
            elif ch == "\x15":  # the field ends
                if self.fields and not self.fields.pop():
                    self.hidden -= 1
            elif self.hidden:
                continue
            elif ch in "\r\x07":
                self._mark(ch, fc + (m.start() + units) * width)
            else:
                self._add(_WORD_CHARS.get(ch, ""))
        self._add(text[pos:])

    def _add(self, text: str) -> None:
        if text and not self.hidden:
            self.line.append(text)
            self.size += len(text)
            if self.size > self.out.limit:  # one huge paragraph: written as it comes
                self.out.add("".join(self.line))
                self.line, self.size = [], 0

    def _mark(self, mark: str, fc: int) -> None:
        """A paragraph ends (``\\r``), or a table cell or row (``\\x07``)."""
        prefix, in_table, row_end = self.paragraphs.at(fc)
        text = "".join(self.line).strip()
        self.line, self.size = [], 0
        if mark == "\x07":
            # Without paragraph properties an empty cell right after a cell is taken for
            # the row's end, which is right unless a row ends in empty cells.
            if row_end or (not self.paragraphs.known and self.after_cell and not text):
                self.out.add("\n")
                self.after_cell = False
            else:
                self.out.add(text.replace("\n", " ") + "\x1f")
                self.after_cell = True
            return
        self.after_cell = False
        text = prefix + text if text else ""
        if in_table:  # one of a cell's paragraphs, before its last
            self.out.add(text + " " if text else "")
        else:
            self.out.add(text + "\n")

    def end_story(self) -> None:
        if self.line:
            self.out.add("".join(self.line).strip() + "\n")
        self.line, self.size, self.after_cell = [], 0, False
        self.fields.clear()
        self.hidden = 0
        self.out.add("\n")


def _word97(ole: _Ole, limit: int) -> str:
    """The text of a Word 97-2003 document, from its piece table: the body, then its
    footnotes, endnotes and text boxes. Raises :class:`_Unreadable` for other versions."""
    if not ole.has("WordDocument"):
        raise _Unreadable
    word = ole.read("WordDocument")
    if len(word) < 0x1AA:  # to the end of fcClx/lcbClx
        raise _Unreadable
    ident, version = struct.unpack_from("<HH", word, 0)
    flags = struct.unpack_from("<H", word, 0x0A)[0]
    if ident != 0xA5EC or version < 0xC1:  # Word 6 and 95 files are older
        raise _Unreadable
    if flags & 0x0100:  # fEncrypted
        raise AttachmentError(PASSWORD_PROTECTED)
    table_name = "1Table" if flags & 0x0200 else "0Table"
    if not ole.has(table_name):
        raise _Unreadable
    table = ole.read(table_name)
    # The FIB: 32 fixed bytes, then three arrays, each after its own length.
    pos = 32
    pos += 2 + 2 * struct.unpack_from("<H", word, pos)[0]
    longs = struct.unpack_from("<H", word, pos)[0]
    counts = pos + 2
    fib = counts + 4 * longs + 2  # the (fc, lcb) pairs
    if longs < 11 or struct.unpack_from("<H", word, fib - 2)[0] < 34:
        raise _Unreadable
    ccp = struct.unpack_from("<11i", word, counts)  # ccpText is [3], then the other stories
    text_n, notes_n, headers_n, comments_n, endnotes_n, boxes_n = (
        ccp[3], ccp[4], ccp[5], ccp[7], ccp[8], ccp[9]
    )  # fmt: skip
    endnotes_at = text_n + notes_n + headers_n + comments_n
    stories = [
        (0, text_n),
        (text_n, notes_n),
        (endnotes_at, endnotes_n),
        (endnotes_at + endnotes_n, boxes_n),
    ]
    fc_clx, lcb_clx = struct.unpack_from("<iI", word, fib + 33 * 8)
    clx = table[fc_clx : fc_clx + lcb_clx] if fc_clx >= 0 else b""
    i = 0
    while i + 3 <= len(clx) and clx[i] == 1:  # formatting (Prc), before the piece table
        i += 3 + max(0, struct.unpack_from("<h", clx, i + 1)[0])
    if i + 5 > len(clx) or clx[i] != 2:
        raise _Unreadable
    plc = clx[i + 5 : i + 5 + struct.unpack_from("<I", clx, i + 1)[0]]
    n = (len(plc) - 4) // 12
    if n < 1:
        raise _Unreadable
    cps = struct.unpack_from(f"<{n + 1}i", plc)
    pieces = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
        compressed = bool(fc & 0x40000000)  # 8-bit text (Windows-1252), else UTF-16
        fc &= 0x3FFFFFFF
        pieces.append((cps[k], cps[k + 1], fc // 2 if compressed else fc, compressed))
    out = _Out(limit)
    text = _WordText(out, _WordParagraphs(word, table, fib))
    budget = len(word) + limit  # characters: a real file stores each one once
    for first, count in stories:
        if count <= 0:
            continue
        for cp0, cp1, fc, compressed in pieces:
            lo, hi = max(first, cp0), min(first + count, cp1)
            if lo >= hi:
                continue
            budget -= hi - lo
            if budget < 0:
                raise AttachmentError(TOO_COMPLEX)
            if compressed:
                at = fc + lo - cp0
                text.feed(word[at : at + hi - lo].decode("cp1252", "replace"), at, 1)
            else:
                at = fc + 2 * (lo - cp0)
                text.feed(word[at : at + 2 * (hi - lo)].decode("utf-16-le", "replace"), at, 2)
            if out.full:
                break
        text.end_story()
        if out.full:
            break
    return _table_rows(out.text())


def _xls_check(ole: _Ole) -> None:
    """Refuse a workbook Excel would ask a password for (it would wait for one)."""
    name = next((n for n in ("Workbook", "Book") if ole.has(n)), None)
    if name is None:
        raise AttachmentError(_damaged("xls"))
    stream = ole.read(name)
    pos = 0
    for _ in range(64):  # FILEPASS follows the first BOF record, before the sheet list
        if pos + 4 > len(stream):
            break
        record, size = struct.unpack_from("<HH", stream, pos)
        if record == 0x002F:  # FILEPASS
            raise AttachmentError(PASSWORD_PROTECTED)
        if record in (0x000A, 0x0085):  # EOF, BOUNDSHEET
            break
        pos += 4 + size


def _ppt_check(ole: _Ole) -> None:
    """Refuse a presentation PowerPoint would ask a password for."""
    if not ole.has("PowerPoint Document"):
        raise AttachmentError(_damaged("ppt"))
    if ole.has("EncryptedSummary"):
        raise AttachmentError(PASSWORD_PROTECTED)
    if ole.has("Current User"):
        user = ole.read("Current User")
        if len(user) >= 16 and struct.unpack_from("<I", user, 12)[0] == 0xF3D1C4DF:
            raise AttachmentError(PASSWORD_PROTECTED)


# --- Office itself, for old formats (Windows) ------------------------------------------

#: How long Word, Excel or PowerPoint may take to open and convert one file.
OFFICE_TIMEOUT = 90.0
#: How long an Office that was closed may take to end before it is killed.
OFFICE_EXIT_WAIT = 30.0
#: Old formats Office converts: ``kind -> (COM program, its process, new extension)``.
_OFFICE = {
    "doc": ("Word.Application", "winword.exe", ".docx"),
    "xls": ("Excel.Application", "excel.exe", ".xlsx"),
    "ppt": ("PowerPoint.Application", "powerpnt.exe", ".pptx"),
}
#: Tried on a protected file so Office fails at once instead of asking for a password.
_NO_PASSWORD = "chatforge-no-password"
_office_lock = threading.Lock()


def _office_installed(prog_id: str) -> bool:
    """True on Windows when Office's ``prog_id`` (``"Word.Application"``...) is registered."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{prog_id}\\CLSID"):
            return True
    except OSError:
        return False


def _office_procs(image: str, pids: Iterable[int] | None = None) -> list[Any]:
    """The running ``image`` processes (``"excel.exe"``...) that were started for COM
    (``-Embedding``): all of them, or those among ``pids``."""
    import psutil

    if pids is None:
        procs = [
            p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == image
        ]
    else:
        procs = []
        for pid in pids:
            with contextlib.suppress(psutil.Error):
                procs.append(psutil.Process(pid))
    found = []
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            if proc.name().lower() == image and "-embedding" in " ".join(proc.cmdline()).lower():
                found.append(proc)
    return found


def _office_pids(image: str) -> set[int]:
    return {proc.pid for proc in _office_procs(image)}


def _office_kill(image: str, pids: Iterable[int], wait: float = 0.0) -> None:
    """Kill the Office processes ``pids`` this started, after ``wait`` seconds for them
    to end by themselves."""
    import psutil

    procs = _office_procs(image, pids)
    if wait and procs:
        procs = psutil.wait_procs(procs, timeout=wait)[1]
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()


def _word_save(app: Any, src: str, dst: str) -> None:
    app.Visible = False
    app.DisplayAlerts = 0  # wdAlertsNone
    app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable: no macros run
    doc = app.Documents.Open(
        FileName=src, ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
        PasswordDocument=_NO_PASSWORD, PasswordTemplate=_NO_PASSWORD, Revert=False,
        WritePasswordDocument=_NO_PASSWORD, WritePasswordTemplate=_NO_PASSWORD,
        Visible=False, NoEncodingDialog=True,
    )  # fmt: skip
    try:
        doc.SaveAs2(FileName=dst, FileFormat=16, AddToRecentFiles=False)  # .docx
    finally:
        doc.Close(SaveChanges=0)


def _excel_save(app: Any, src: str, dst: str) -> None:
    app.Visible = False
    app.DisplayAlerts = False
    app.ScreenUpdating = False
    app.EnableEvents = False
    app.AskToUpdateLinks = False
    app.AutomationSecurity = 3
    book = app.Workbooks.Open(
        Filename=src, UpdateLinks=0, ReadOnly=True, Password=_NO_PASSWORD,
        WriteResPassword=_NO_PASSWORD, IgnoreReadOnlyRecommended=True, Notify=False,
        AddToMru=False,
    )  # fmt: skip
    try:
        book.SaveAs(Filename=dst, FileFormat=51)  # .xlsx
    finally:
        book.Close(SaveChanges=False)


def _powerpoint_save(app: Any, src: str, dst: str) -> None:
    # PowerPoint runs once per user: when it was open already, this is the user's own,
    # whose settings are put back.
    alerts, security = app.DisplayAlerts, app.AutomationSecurity
    app.DisplayAlerts = 1  # ppAlertsNone
    app.AutomationSecurity = 3
    try:
        # "file::password::" makes a protected file fail instead of asking for one.
        deck = app.Presentations.Open(
            FileName=f"{src}::{_NO_PASSWORD}::", ReadOnly=-1, Untitled=0, WithWindow=0
        )
        try:
            deck.SaveAs(FileName=dst, FileFormat=24)  # .pptx
        finally:
            deck.Close()
    finally:
        app.DisplayAlerts, app.AutomationSecurity = alerts, security


_OFFICE_SAVE = {"doc": _word_save, "xls": _excel_save, "ppt": _powerpoint_save}


#: COM errors in starting or reaching Office, not in the file: an Office still closing
#: after the last conversion, one that is slow to start or busy. Worth another try.
_OFFICE_RETRY = frozenset(
    {
        -2147023170,  # 0x800706BE RPC_S_CALL_FAILED
        -2147023174,  # 0x800706BA RPC_S_SERVER_UNAVAILABLE
        -2146959355,  # 0x80080005 CO_E_SERVER_EXEC_FAILURE
        -2147418111,  # 0x80010001 RPC_E_CALL_REJECTED
        -2147417846,  # 0x8001010A RPC_E_SERVERCALL_RETRYLATER
        -2147417848,  # 0x80010108 RPC_E_DISCONNECTED
    }
)
OFFICE_TRIES = 3
OFFICE_RETRY_PAUSE = 1.0


def _office_try(kind: str, src: str, dst: str, state: dict[str, Any]) -> tuple[Any, str] | None:
    """One try: start a hidden Word, Excel or PowerPoint, have it save ``src`` as ``dst``
    and close it again. ``None`` when it did, else the error's HRESULT and words (words
    only: the error's frames would keep Office's objects, and Office, alive)."""
    import win32com.client

    prog_id, image, _ = _OFFICE[kind]
    app = None
    started: set[int] = set()
    try:
        app = win32com.client.DispatchEx(prog_id)
        started = _office_pids(image) - state["before"]
        state["pids"] |= started
        _OFFICE_SAVE[kind](app, src, dst)
        return None
    except Exception as exc:  # noqa: BLE001 - COM raises com_error and others
        details = getattr(exc, "excepinfo", None) or ()
        return getattr(exc, "hresult", None), " ".join(str(part) for part in (exc, *details))
    finally:
        # Word and Excel start anew for each caller; PowerPoint is closed only when this
        # started it, never when it is the user's.
        if app is not None and (kind != "ppt" or started):
            with contextlib.suppress(Exception):
                app.Quit(*((0,) if kind == "doc" else ()))  # Word: wdDoNotSaveChanges


def _office_run(kind: str, src: str, dst: str, state: dict[str, Any]) -> None:
    """The conversion, on a thread of its own (for COM), tried again on errors in
    reaching Office (:data:`_OFFICE_RETRY`). ``state`` gets the processes it started
    (``pids``, against those running ``before``) and an ``error``; ``done`` is set once
    the file is saved, or could not be. What it started is ended after."""
    _, image, _ = _OFFICE[kind]
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception as exc:  # noqa: BLE001 - pywin32 missing or broken
        state["error"] = str(exc)
        state["done"].set()
        return
    failed = None
    try:
        deadline = time.monotonic() + OFFICE_TIMEOUT
        for _ in range(OFFICE_TRIES):
            failed = _office_try(kind, src, dst, state)
            if failed is None or failed[0] not in _OFFICE_RETRY or time.monotonic() > deadline:
                break
            time.sleep(OFFICE_RETRY_PAUSE)
        if failed is not None:
            state["error"] = failed[1]
    finally:
        state["done"].set()
        pythoncom.CoUninitialize()
    _office_kill(image, state["pids"], wait=OFFICE_EXIT_WAIT)


def _office_convert(kind: str, data: bytes) -> bytes:
    """``data`` (an old ``.doc``, ``.xls`` or ``.ppt``) saved by Office itself as
    ``.docx``, ``.xlsx`` or ``.pptx``, for the readers above. Office runs hidden, with
    macros, links and prompts off, on a copy in a temporary folder deleted after; an
    Office this started is closed again, or killed after :data:`OFFICE_TIMEOUT`. Raises
    :class:`_Unreadable` without Office (or pywin32, or Windows)."""
    prog_id, image, new_ext = _OFFICE[kind]
    if not _office_installed(prog_id):
        raise _Unreadable
    try:
        import pythoncom  # noqa: F401 - pywin32, a dependency on Windows
        import win32com.client  # noqa: F401
    except ImportError:
        raise _Unreadable from None
    program = prog_id.partition(".")[0]
    with (
        _office_lock,
        tempfile.TemporaryDirectory(prefix="chatforge-office-", ignore_cleanup_errors=True) as tmp,
    ):
        src = os.path.join(tmp, f"attachment.{kind}")
        dst = os.path.join(tmp, f"converted{new_ext}")
        with open(src, "wb") as fh:
            fh.write(data)
        state: dict[str, Any] = {
            "before": _office_pids(image),
            "pids": set(),
            "done": threading.Event(),
        }
        worker = threading.Thread(
            target=_office_run, args=(kind, src, dst, state), name="office-convert", daemon=True
        )
        worker.start()
        if not state["done"].wait(OFFICE_TIMEOUT):
            # Kill what this started, even when it hangs before it is known (pids).
            _office_kill(image, state["pids"] or _office_pids(image) - state["before"])
            worker.join(5)
            raise AttachmentError(
                f"{program} took too long to open this file. Save it as {new_ext} in "
                f"{program} and attach that."
            )
        error = state.get("error")
        if error is not None:
            if "password" in error.lower():
                raise AttachmentError(PASSWORD_PROTECTED)
            raise AttachmentError(
                f"{program} could not open this file. It may be damaged; if it opens in "
                f"{program}, save it as {new_ext} and attach that."
            )
        if os.path.getsize(dst) > MAX_UNPACKED_BYTES:
            raise AttachmentError(TOO_MUCH_DATA)
        with open(dst, "rb") as fh:
            return fh.read()


# --- .doc, .xls, .ppt ------------------------------------------------------------------


def _legacy(kind: str, data: bytes, limit: int) -> tuple[str, str | None]:
    """An old binary Word, Excel or PowerPoint file, or a file saved under that name.

    A ``.doc`` is read here (Word 97 and later); the rest, when Microsoft Office is
    installed, by Office converting it to the newer format. Encrypted files are refused
    before Office would ask for their password.
    """
    newer = {"doc": _docx, "xls": _xlsx, "ppt": _pptx}[kind]
    if data.startswith(b"PK\x03\x04"):  # really a .docx, .xlsx or .pptx
        return newer(data, limit)
    if data[:4] != _OLE_MAGIC[:4]:
        head = data[:4096].lstrip(b" \t\r\n").removeprefix(codecs.BOM_UTF8)
        if head.startswith(b"{\\rtf"):  # Word saves RTF under any name
            return _rtf(data, limit)
        if looks_binary(data):
            raise AttachmentError(_damaged(kind))
        if re.search(rb"<(html|table|body)\b", head[:2048], re.IGNORECASE):  # a web page
            return _html(data, limit)
        return _plain(data, limit)
    ole = _Ole(data)
    if ole.has("EncryptedPackage"):  # a password-protected .docx, .xlsx or .pptx
        raise AttachmentError(PASSWORD_PROTECTED)
    if kind == "doc":
        with contextlib.suppress(_Unreadable):
            return _word97(ole, limit), None
    elif kind == "xls":
        _xls_check(ole)
    else:
        _ppt_check(ole)
    program, new_ext = LEGACY_FORMATS[f".{kind}"]
    try:
        converted = _office_convert(kind, data)
    except _Unreadable:
        raise AttachmentError(
            f"This old {program} file can be read only when Microsoft Office is installed. "
            f"Save it as {new_ext} in {program} and attach that.",
            code="unsupported",
        ) from None
    return newer(converted, limit)


def _doc(data: bytes, limit: int) -> tuple[str, str | None]:
    return _legacy("doc", data, limit)


def _xls(data: bytes, limit: int) -> tuple[str, str | None]:
    return _legacy("xls", data, limit)


def _ppt(data: bytes, limit: int) -> tuple[str, str | None]:
    return _legacy("ppt", data, limit)


# --- e-mail: .eml, .msg ----------------------------------------------------------------

_MAIL_HEADERS = ("From", "To", "Cc", "Date", "Subject")


def _mail_text(headers: Iterable[tuple[str, str]], files: list[str], body: str) -> str:
    """``Name: value`` header lines, the attached files' names, a blank line, the body."""
    lines = [f"{name}: {value}" for name, value in headers if value]
    if files:
        lines.append("Attachments: " + ", ".join(files))
    return "\n".join(lines) + "\n\n" + (body.strip() or "(no text)")


def _mime_header(msg: Any, name: str) -> str:
    try:
        value = msg.get(name)
        return " ".join(str(value).split()) if value is not None else ""
    except Exception:  # noqa: BLE001 - a malformed header is left out
        return ""


def _mime_body(part: Any) -> str:
    try:
        content = part.get_content()
    except (LookupError, ValueError):  # an unknown charset
        content = part.get_payload(decode=True) or b""
    if isinstance(content, bytes):
        content = decode_text(content)
    if not isinstance(content, str):
        return ""
    if part.get_content_subtype() == "html":
        from chatforge.tools import htmltext

        return htmltext.extract(content)[1]
    return content


def _eml(data: bytes, limit: int) -> tuple[str, str | None]:
    """An e-mail (MIME): From, To, Cc, Date and Subject, the names of its attached
    files, then its plain-text body (or its HTML body as text)."""
    import email
    from email import policy

    try:
        msg = email.message_from_bytes(data, policy=policy.default)
        headers = [(name, _mime_header(msg, name)) for name in _MAIL_HEADERS]
        files = [
            clean_name(part.get_filename() or "attachment")
            for part in msg.walk()
            if part.is_attachment()
        ]
        body = msg.get_body(preferencelist=("plain", "html"))
        text = _mime_body(body) if body is not None else ""
    except RecursionError:  # parts nested thousands deep
        raise AttachmentError(TOO_COMPLEX) from None
    return _mail_text(headers, files, text), None


def _msg_string(ole: _Ole, storage: str, prop: int, codepage: str) -> str:
    """A string property (``__substg1.0_<id>001F`` UTF-16, or ``001E`` 8-bit)."""
    for suffix, codec in (("001F", "utf-16-le"), ("001E", codepage)):
        path = f"{storage}__substg1.0_{prop:04X}{suffix}"
        if ole.has(path):
            return ole.read(path).decode(codec, "replace").split("\x00", 1)[0].strip()
    return ""


def _filetime(value: bytes) -> str:
    ticks = int.from_bytes(value[:8], "little")
    if not ticks:
        return ""
    try:
        moment = datetime(1601, 1, 1) + timedelta(microseconds=ticks // 10)
    except OverflowError:
        return ""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _msg(data: bytes, limit: int) -> tuple[str, str | None]:
    """An Outlook message: From, To, Cc, Date and Subject, the names of its attached
    files, then its plain-text body (or its HTML body as text)."""
    ole = _Ole(data)
    if not any(path.startswith("__substg1.0_") for path in ole.streams()):
        raise AttachmentError(_damaged("msg"))
    props: dict[int, bytes] = {}  # fixed-size properties: tag -> 8-byte value
    if ole.has("__properties_version1.0"):
        stream = ole.read("__properties_version1.0")
        for pos in range(32, len(stream) - 15, 16):  # after a 32-byte header
            props[struct.unpack_from("<I", stream, pos)[0]] = stream[pos + 8 : pos + 16]
    cpid = props.get(0x3FFD0003) or props.get(0x3FDE0003)  # message / internet code page
    number = struct.unpack_from("<I", cpid)[0] if cpid else 0
    codepage = _codec("utf-8" if number == 65001 else f"cp{number}") if number else "cp1252"

    def string(prop: int, storage: str = "") -> str:
        return _msg_string(ole, storage, prop, codepage)

    sender = string(0x0C1A) or string(0x0042)  # sender, sent-representing name
    address = next((a for a in (string(0x5D01), string(0x0C1F), string(0x0065)) if "@" in a), "")
    if sender and address and address.lower() not in sender.lower():
        sender = f"{sender} <{address}>"
    sent = props.get(0x00390040) or props.get(0x0E060040)  # submitted, delivered
    headers = [
        ("From", sender or address),
        ("To", string(0x0E04)),
        ("Cc", string(0x0E03)),
        ("Date", _filetime(sent) if sent else ""),
        ("Subject", string(0x0037)),
    ]
    files: list[str] = []
    for storage in sorted(ole.storages):
        if storage.lower().startswith("__attach_version1.0_#"):
            # The long file name, the short one, else the name shown for it.
            names = (string(prop, f"{storage}/") for prop in (0x3707, 0x3704, 0x3001))
            files.append(clean_name(next((n for n in names if n), "attachment")))
    body = string(0x1000)
    if not body.strip():
        html_part = "__substg1.0_10130102"
        html_text = decode_text(ole.read(html_part)) if ole.has(html_part) else string(0x1013)
        if html_text.strip():
            from chatforge.tools import htmltext

            body = htmltext.extract(html_text)[1]
    return _mail_text(headers, files, body), None


# --- .pdf ------------------------------------------------------------------------------


def _pdf(data: bytes, limit: int) -> tuple[str, str | None]:
    """Page by page with ``pypdf`` (a required dependency; the import is lazy)."""
    try:
        import pypdf
    except ImportError:
        raise AttachmentError(PDF_NEEDS_PYPDF, code="unsupported") from None
    out = _Out(limit)
    empty = total = 0
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise AttachmentError(
                "This PDF is password-protected. Remove the password and attach it again."
            )
        total = len(reader.pages)
        for number, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if not text:
                empty += 1
                continue
            out.add(f"## Page {number}\n{text}\n\n")
            if out.full:
                break
    except AttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types for broken files
        raise AttachmentError(f"This PDF could not be read ({type(exc).__name__}).") from None
    if not out.parts:
        raise AttachmentError(
            "This PDF has no text to read. It may be scanned pages, and images are not "
            "supported yet."
        )
    warning = (
        f"{empty} of {total} pages have no text (scanned pages are not read)." if empty else None
    )
    return out.text(), warning


# --------------------------------------------------------------------------- #
# Loading (file dialog, drag-and-drop)
# --------------------------------------------------------------------------- #


def read_path(path: str | Path, *, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[Extracted, int]:
    """Read and extract a file picked in the file dialog: ``(extracted, size in bytes)``.
    The size is checked before the file is read."""
    target = Path(path)
    try:
        if not target.is_file():
            raise AttachmentError("The file was not found.", code="not_found")
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise AttachmentError("The file is larger than 20 MB.", code="too_large")
        data = target.read_bytes()
    except OSError:
        raise AttachmentError(
            "The file could not be opened. It may be open in another program."
        ) from None
    return extract_text(target.name, data, max_chars=max_chars), len(data)


def decode_base64(data: str) -> bytes:
    """Decode a dropped or pasted file (plain base64 or a ``data:`` URL). The size is
    checked before anything is decoded."""
    text = str(data or "").strip()
    if text.startswith("data:"):
        text = text.partition(",")[2]
    padding = len(text) - len(text.rstrip("="))
    if len(text) * 3 // 4 - padding > MAX_FILE_BYTES:
        raise AttachmentError("The file is larger than 20 MB.", code="too_large")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError("The file data could not be read.") from None


# --------------------------------------------------------------------------- #
# Pending attachments
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Pending:
    id: str
    file: Extracted
    size: int
    added: float

    def view(self) -> dict[str, Any]:
        """The bridge reply shape: ``{id, name, kind, chars, size, truncated, warning}``,
        plus ``{width, height, thumb}`` for a picture (``thumb``: a small JPEG ``data:``
        URL)."""
        f = self.file
        out = {
            "id": self.id,
            "name": f.name,
            "kind": f.kind,
            "chars": f.chars,
            "size": self.size,
            "truncated": f.truncated,
            "warning": f.warning,
        }
        if f.image is not None:
            out.update(width=f.image.width, height=f.image.height, thumb=f.image.thumb)
        return out


def _held_bytes(file: Extracted) -> int:
    """About how much memory a pending file holds: its text, or a picture's images."""
    picture = file.image
    if picture is None:
        return len(file.text)
    return len(picture.data) + len(getattr(picture, "ocr_data", None) or b"")


class AttachmentStore:
    """Files attached in the composer but not sent yet, by random, unguessable id.

    Thread-safe (pywebview calls the bridge on worker threads). Only the newest
    :data:`MAX_PENDING` are kept, holding at most :data:`MAX_PENDING_BYTES`, so files
    attached and never sent cannot pile up.
    """

    def __init__(self, *, max_items: int = MAX_PENDING, max_bytes: int = MAX_PENDING_BYTES) -> None:
        self._items: dict[str, Pending] = {}
        self._max = max(1, int(max_items))
        self._max_bytes = max(1, int(max_bytes))
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, attachment_id: object) -> bool:
        return attachment_id in self._items

    def add(self, file: Extracted, size: int) -> Pending:
        item = Pending(f"att_{secrets.token_urlsafe(16)}", file, int(size), time.time())
        with self._lock:
            self._items[item.id] = item
            held = sum(_held_bytes(i.file) for i in self._items.values())
            while len(self._items) > 1 and (len(self._items) > self._max or held > self._max_bytes):
                oldest = next(iter(self._items))
                held -= _held_bytes(self._items.pop(oldest).file)
        return item

    def peek(self, ids: Iterable[str]) -> list[Extracted]:
        """The files for ``ids`` in that order, left in the store. Raises
        :class:`AttachmentError` (``not_found``) when any id is unknown."""
        with self._lock:
            missing = [i for i in ids if i not in self._items]
            if missing:
                raise AttachmentError(
                    "An attached file is no longer available. Attach it again.",
                    code="not_found",
                )
            return [self._items[i].file for i in ids]

    def discard(self, ids: Iterable[str]) -> None:
        with self._lock:
            for i in ids:
                self._items.pop(i, None)

    def remove(self, attachment_id: str) -> bool:
        with self._lock:
            return self._items.pop(str(attachment_id), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# --------------------------------------------------------------------------- #
# What the model sees
# --------------------------------------------------------------------------- #

#: Where a file cut to fit the prompt ends (local NPU model / any other model).
NOTE_CUT_LOCAL = (
    "[file truncated to fit the local model; use a cloud or StudioForge model for long files]"
)
NOTE_CUT = "[file truncated to fit this model's prompt window]"


#: The extra ``_attachments`` keys of a picture (``chatforge.images``): the stored file's
#: name, its type and size in pixels, the popup's thumbnail, and text recognised in it.
IMAGE_RECORD_KEYS = ("file", "media_type", "width", "height", "thumb", "ocr")


def as_record(file: Extracted | Mapping[str, Any]) -> dict[str, Any]:
    """The ``_attachments`` entry stored on a user message: ``{name, kind, chars,
    truncated, text}`` (the text is kept so follow-up questions work). A picture adds
    ``{file, media_type, width, height, thumb}`` (see ``chatforge.images``)."""
    if isinstance(file, Extracted):
        out = {
            "name": file.name,
            "kind": file.kind,
            "chars": file.chars,
            "truncated": file.truncated,
            "text": file.text,
        }
        if file.image is not None:
            out.update(file.image.record())
        return out
    text = str(file.get("text") or "")
    out = {
        "name": clean_name(file.get("name")),
        "kind": str(file.get("kind") or "text"),
        "chars": int(file.get("chars") or len(text)),
        "truncated": bool(file.get("truncated", False)),
        "text": text,
    }
    if out["kind"] == "image":
        out.update({k: file[k] for k in IMAGE_RECORD_KEYS if k in file})
    return out


def file_block(name: str, text: str, note: str | None = None) -> str:
    """One ``<file name="...">`` block (with a leading blank line)."""
    body = text.rstrip("\n")
    if note:
        body = f"{body}\n{note}" if body else note
    return f'\n\n<file name="{html.escape(name, quote=True)}">\n{body}\n</file>'


def user_content(
    text: str, files: list[Mapping[str, Any]], limits: list[int] | None = None, note: str = ""
) -> str:
    """The typed text followed by one block per file. ``limits[i]`` cuts file ``i`` to that
    many characters, ending it with ``note``; a file cut when it was read says so too."""
    blocks: list[str] = []
    for i, f in enumerate(files):
        body = str(f.get("text") or "")
        limit = limits[i] if limits is not None else None
        mark: str | None = None
        if limit is not None and len(body) > limit:
            body, mark = body[: max(0, limit)].rstrip(), note or NOTE_CUT
        elif f.get("truncated"):
            mark = f"[file truncated: only the first {len(body):,} characters were read]"
        blocks.append(file_block(str(f.get("name") or "file"), body, mark))
    joined = "".join(blocks)
    return text + joined if text.strip() else joined.lstrip("\n")


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DIALOG_FILE_TYPES",
    "IMAGE_RECORD_KEYS",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "NOTE_CUT",
    "NOTE_CUT_LOCAL",
    "PDF_NEEDS_PYPDF",
    "PICTURE_EXTS",
    "AttachmentError",
    "AttachmentStore",
    "Extracted",
    "Pending",
    "as_record",
    "clean_name",
    "decode_base64",
    "decode_text",
    "extract_text",
    "file_block",
    "kind_for",
    "read_path",
    "user_content",
]
