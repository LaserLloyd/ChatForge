"""Read attached files as text: a file attached to a message becomes context for the model.

``extract_text(name, data)`` turns one file into an :class:`Extracted`, using only the
standard library: plain text and code in the common encodings, HTML (its visible text),
Word ``.docx``, Excel ``.xlsx`` and PowerPoint ``.pptx`` (read straight from the XML parts
inside the zip), and PDF when the optional ``pypdf`` package is installed. Images, archives,
programs and old Office formats raise :class:`AttachmentError`, whose message the UI shows
as it is.

``kind`` is one of ``text``, ``code``, ``data``, ``html``, ``docx``, ``xlsx``, ``pptx`` or
``pdf``. Text longer than ``max_chars`` (``tools.attachment_max_chars``) is cut and marked
``truncated``.

:class:`AttachmentStore` keeps the files the user attached but has not sent yet, under
random ids, until ``send_message`` takes them. :func:`file_block` and :func:`user_content`
build what the model sees: the typed text, then one ``<file name="...">`` block per file.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import csv
import html
import io
import posixpath
import re
import secrets
import threading
import time
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from aichat.errors import AppError

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
DEFAULT_MAX_CHARS = 200_000
#: Attached files waiting to be sent; the oldest are forgotten past this many.
MAX_PENDING = 50
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

PDF_NEEDS_PYPDF = "Reading PDFs needs the pypdf package: py -3.12 -m uv add pypdf"
IMAGES_UNSUPPORTED = (
    "Images are not supported yet. Attach text, code, Word, Excel, PowerPoint or PDF files."
)

TEXT_EXTS = frozenset({".txt", ".text", ".md", ".markdown", ".rst", ".log", ".nfo", ".srt", ".vtt"})
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
OFFICE_KINDS = {".docx": "docx", ".docm": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx",
                ".pptx": "pptx", ".pptm": "pptx"}  # fmt: skip
IMAGE_EXTS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico", ".heic",
        ".heif", ".avif", ".psd", ".raw", ".cr2", ".nef", ".dng",
    }
)  # fmt: skip
#: Older formats with a newer equivalent this module reads: ``ext -> (program, new ext)``.
LEGACY_FORMATS = {
    ".doc": ("Word", ".docx"),
    ".xls": ("Excel", ".xlsx"),
    ".ppt": ("PowerPoint", ".pptx"),
    ".rtf": ("Word", ".docx"),
    ".odt": ("Word or LibreOffice", ".docx"),
    ".ods": ("Excel or LibreOffice", ".xlsx"),
    ".odp": ("PowerPoint or LibreOffice", ".pptx"),
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

#: The native file dialog's filters (pywebview syntax: ``Description (*.a;*.b)``).
DIALOG_FILE_TYPES: tuple[str, ...] = (
    "Supported files ({})".format(
        ";".join(
            f"*{ext}"
            for ext in sorted(
                TEXT_EXTS | CODE_EXTS | DATA_EXTS | HTML_EXTS | set(OFFICE_KINDS) | {".pdf"}
            )
        )
    ),
    "All files (*.*)",
)


class AttachmentError(AppError):
    """A file that cannot be attached; ``message`` is shown to the user as it is."""

    code = "bad_request"


@dataclass(frozen=True)
class Extracted:
    """The text of one attached file."""

    name: str
    kind: str
    text: str
    chars: int
    truncated: bool = False
    warning: str | None = None


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
    readers = {"docx": _docx, "xlsx": _xlsx, "pptx": _pptx, "pdf": _pdf, "html": _html}
    reader = readers.get(kind, _plain)
    try:
        text, warning = reader(data, limit)
    except AttachmentError:
        raise
    except (zipfile.BadZipFile, zlib.error, ET.ParseError, EOFError, KeyError, ValueError,
            IndexError, OSError):  # fmt: skip
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
    if ext in LEGACY_FORMATS:
        program, newer = LEGACY_FORMATS[ext]
        raise AttachmentError(
            f"{ext} files are not supported. Save it as {newer} in {program} and attach that.",
            code="unsupported",
        )
    if ext in BINARY_EXTS or looks_binary(data):
        what = f"{ext} files are" if ext else "This type of file is"
        raise AttachmentError(
            f"{what} not supported yet. Attach text, code, Word, Excel, PowerPoint or PDF files.",
            code="unsupported",
        )


def _damaged(kind: str) -> str:
    what = {"docx": "Word document", "xlsx": "Excel workbook", "pptx": "PowerPoint file"}.get(kind)
    if what is None:
        return "The file could not be read; it may be damaged."
    return f"The file could not be read: it is damaged, password-protected or not a real {what}."


def _plain(data: bytes, limit: int) -> tuple[str, str | None]:
    if looks_binary(data):
        raise AttachmentError("This file is not text, so it cannot be read.", code="unsupported")
    return decode_text(data), None


def _html(data: bytes, limit: int) -> tuple[str, str | None]:
    from aichat.tools import htmltext

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


# --- .pdf ------------------------------------------------------------------------------


def _pdf(data: bytes, limit: int) -> tuple[str, str | None]:
    """Page by page with ``pypdf`` (optional: not installed by default)."""
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
        """The bridge reply shape: ``{id, name, kind, chars, size, truncated, warning}``."""
        f = self.file
        return {
            "id": self.id,
            "name": f.name,
            "kind": f.kind,
            "chars": f.chars,
            "size": self.size,
            "truncated": f.truncated,
            "warning": f.warning,
        }


class AttachmentStore:
    """Files attached in the composer but not sent yet, by random, unguessable id.

    Thread-safe (pywebview calls the bridge on worker threads). Only the newest
    :data:`MAX_PENDING` are kept, so files attached and never sent cannot pile up.
    """

    def __init__(self, *, max_items: int = MAX_PENDING) -> None:
        self._items: dict[str, Pending] = {}
        self._max = max(1, int(max_items))
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, attachment_id: object) -> bool:
        return attachment_id in self._items

    def add(self, file: Extracted, size: int) -> Pending:
        item = Pending(f"att_{secrets.token_urlsafe(16)}", file, int(size), time.time())
        with self._lock:
            self._items[item.id] = item
            while len(self._items) > self._max:
                del self._items[next(iter(self._items))]
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


def as_record(file: Extracted | Mapping[str, Any]) -> dict[str, Any]:
    """The ``_attachments`` entry stored on a user message: ``{name, kind, chars,
    truncated, text}`` (the text is kept so follow-up questions work)."""
    if isinstance(file, Extracted):
        return {
            "name": file.name,
            "kind": file.kind,
            "chars": file.chars,
            "truncated": file.truncated,
            "text": file.text,
        }
    text = str(file.get("text") or "")
    return {
        "name": clean_name(file.get("name")),
        "kind": str(file.get("kind") or "text"),
        "chars": int(file.get("chars") or len(text)),
        "truncated": bool(file.get("truncated", False)),
        "text": text,
    }


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
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "NOTE_CUT",
    "NOTE_CUT_LOCAL",
    "PDF_NEEDS_PYPDF",
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
