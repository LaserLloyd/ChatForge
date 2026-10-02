"""``create_document`` tool: save a file the model wrote (a report, a spreadsheet, slides, a
table, a script) into the user's documents folder, so the chat can offer it like a download.

The folder is ``tools.documents_dir``; empty means ``ChatForge`` inside the user's Documents
folder, created on first use. The format comes from the file name's extension (or the
``format`` argument), and every writer uses only the standard library:

- ``.docx`` (Word): Markdown-style text -> WordprocessingML (:mod:`doc_docx`).
- ``.xlsx`` (Excel): the Markdown tables or CSV in the text, one sheet per table
  (:mod:`doc_xlsx`); text without a table is refused with a hint the model can act on.
- ``.pptx`` (PowerPoint): ``#`` title slide, a slide per ``##`` or ``---`` (:mod:`doc_pptx`).
- ``.csv`` / ``.tsv``: a Markdown table is converted; CSV text is written as it is. A
  ``.csv`` starts with a byte-order mark so Excel reads it as UTF-8.
- ``.html``: Markdown becomes a standalone page (:mod:`doc_html`); HTML is kept as it is.
- Other text formats (``.md``, ``.txt``, ``.json``, code ...) are written as UTF-8.

Files are never overwritten: a taken name gets `` (2)``, `` (3)`` ... .

:func:`resolve_document`, :func:`open_document`, :func:`reveal_document` and
:func:`save_copy` back the bridge's ``open_document`` / ``reveal_document`` /
``save_document``: only files inside the documents folder (after resolving links) are
opened, shown or copied out.
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from chatforge.attachments import kind_for
from chatforge.errors import AppError
from chatforge.logging_setup import get_logger
from chatforge.tools import doc_html, doc_pptx, doc_xlsx
from chatforge.tools import doc_markdown as md
from chatforge.tools.doc_docx import markdown_to_docx
from chatforge.tools.registry import ToolResult

log = get_logger(__name__)

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_NAME_CHARS = 120
FOLDER_NAME = "ChatForge"
#: Written as UTF-8 text.
TEXT_FORMATS = frozenset(
    {".md", ".txt", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml",
     ".py", ".js", ".ts", ".css", ".sql", ".ps1", ".sh"}
)  # fmt: skip
OFFICE_FORMATS = frozenset({".docx", ".xlsx", ".pptx"})
FORMATS = TEXT_FORMATS | OFFICE_FORMATS
#: ``format`` values that name a type in words rather than by extension.
_FORMAT_ALIASES = {
    "markdown": ".md",
    "text": ".txt",
    "plain": ".txt",
    "word": ".docx",
    "doc": ".docx",
    "excel": ".xlsx",
    "spreadsheet": ".xlsx",
    "workbook": ".xlsx",
    "xls": ".xlsx",
    "powerpoint": ".pptx",
    "presentation": ".pptx",
    "slides": ".pptx",
    "deck": ".pptx",
    "ppt": ".pptx",
    "web": ".html",
    "javascript": ".js",
    "typescript": ".ts",
    "python": ".py",
    "powershell": ".ps1",
    "bash": ".sh",
    "shell": ".sh",
}
#: Older or longer extensions written as the format the tool makes.
_UPGRADED = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
    ".markdown": ".md",
    ".text": ".txt",
}
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
     *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
)  # fmt: skip
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
#: Opened with their default app. Anything else (scripts above all: ``.js`` and ``.py``
#: would run, not open) opens in Notepad.
SAFE_TO_OPEN = frozenset(
    {".md", ".txt", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml",
     ".docx", ".xlsx", ".pptx", ".log"}
)  # fmt: skip
_WINDOWS = sys.platform == "win32"

#: Told to the model when an ``.xlsx`` has no table to hold.
XLSX_NEEDS_TABLE = (
    "there is no table in 'content'. For .xlsx write each table as a Markdown table "
    "(a '| Name | Total |' line, then '|---|---|', then one '| a | 1 |' line per row) or as "
    "CSV lines ('Name,Total'), with a '## Heading' above each table to name its sheet"
)
#: Told to the model when a ``.pptx`` has nothing to show.
PPTX_NEEDS_TEXT = (
    "there is nothing to put on a slide. For .pptx write Markdown: '# Deck title', then "
    "'## Slide title' and '- bullet' lines for each slide"
)
#: Save As filter names (pywebview allows letters, digits and spaces).
_TYPE_NAMES = {
    ".docx": "Word document",
    ".xlsx": "Excel workbook",
    ".pptx": "PowerPoint presentation",
    ".csv": "CSV file",
    ".tsv": "TSV file",
    ".md": "Markdown file",
    ".txt": "Text file",
    ".html": "Web page",
    ".htm": "Web page",
    ".json": "JSON file",
}


class ContentError(ValueError):
    """``content`` cannot make the requested kind of file; the message tells the model
    how to write it."""


# --------------------------------------------------------------------------- #
# Folder and names
# --------------------------------------------------------------------------- #


def default_documents_dir() -> Path:
    """``ChatForge`` in the user's Documents folder (where Windows keeps it, which may be
    redirected to OneDrive), else ``%USERPROFILE%\\Documents\\ChatForge``."""
    docs = ""
    try:
        import platformdirs

        docs = platformdirs.user_documents_dir()
    except Exception:  # noqa: BLE001 - fall back to the plain profile path
        docs = ""
    if docs:
        return Path(docs) / FOLDER_NAME
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(profile) / "Documents" / FOLDER_NAME


def documents_dir(configured: str | os.PathLike[str] | None = "") -> Path:
    """The documents folder: ``tools.documents_dir`` (``%VARS%`` and ``~`` expanded), or
    :func:`default_documents_dir` when that is empty. Not created here."""
    value = str(configured or "").strip()
    if not value:
        return default_documents_dir()
    return Path(os.path.expandvars(value)).expanduser()


def downloads_dir() -> Path:
    """The user's Downloads folder (where Windows keeps it), else ``%USERPROFILE%\\Downloads``,
    else the profile folder: where Save As starts."""
    try:
        import platformdirs

        found = Path(platformdirs.user_downloads_dir())
        if found.is_dir():
            return found
    except Exception:  # noqa: BLE001 - fall back to the plain profile path
        pass
    profile = Path(os.environ.get("USERPROFILE") or str(Path.home()))
    fallback = profile / "Downloads"
    return fallback if fallback.is_dir() else profile


def sanitize_filename(filename: str, fmt: str | None = None) -> str:
    """A safe file name with a supported extension. Raises ``ValueError`` (shown to the
    model) for an extension that cannot be written.

    Folders are dropped, characters Windows forbids become ``_``, reserved device names
    (``CON``, ``NUL``, ``COM1`` ...) get a leading ``_``, and the name is cut to 120
    characters. ``.doc``/``.xls``/``.ppt`` become ``.docx``/``.xlsx``/``.pptx``. Without a
    supported extension the ``format`` decides (default ``.md``).
    """
    name = re.split(r"[\\/]", str(filename or ""))[-1]
    name = _BAD_CHARS.sub("_", name).strip().strip(".").strip()
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = f".{ext.lower()}" if ext else ""
    ext = _UPGRADED.get(ext, ext)
    wanted = _format_ext(fmt)
    if ext not in FORMATS:
        if ext[1:].isalpha() and not wanted:
            raise ValueError(f"cannot create {ext} files; use one of: {', '.join(sorted(FORMATS))}")
        if not ext[1:].isalpha():
            stem = name  # "report.v2": the dot is part of the name, not a file type
        ext = wanted or ".md"
    stem = stem.strip().strip(".").strip() or "document"
    if stem.split(".")[0].strip().upper() in _RESERVED:
        stem = "_" + stem
    return stem[: MAX_NAME_CHARS - len(ext)].rstrip(" .") + ext


def _format_ext(fmt: str | None) -> str | None:
    if not isinstance(fmt, str) or not fmt.strip():
        return None
    key = fmt.strip().lower().lstrip(".")
    ext = _FORMAT_ALIASES.get(key, f".{key}")
    ext = _UPGRADED.get(ext, ext)
    return ext if ext in FORMATS else None


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _write_new(folder: Path, name: str, data: bytes) -> Path:
    """Write ``data`` to ``name`` in ``folder``, or to ``name (2)``, ``name (3)`` ... when
    it is taken. Exclusive creation: an existing file is never replaced."""
    stem, ext = os.path.splitext(name)
    for n in range(1, 1000):
        candidate = folder / (name if n == 1 else f"{stem} ({n}){ext}")
        try:
            with open(candidate, "xb") as fh:
                fh.write(data)
        except FileExistsError:
            continue
        return candidate
    raise OSError("too many files with this name")


# --------------------------------------------------------------------------- #
# Building the file
# --------------------------------------------------------------------------- #


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _delimited(content: str, ext: str) -> tuple[bytes, list[str]]:
    """``.csv``/``.tsv``: the first Markdown table (or a ```` ```csv ```` block inside other
    text) converted; plain CSV text kept as it is."""
    inner = md.unfence(content)
    tables = md.find_tables(md.parse(inner))
    markdown = [t for t in tables if t.markdown]
    fenced = [t for t in tables if t.source == "fence"]
    notes: list[str] = []
    table = markdown[0] if markdown else (fenced[0] if fenced else None)
    if table is not None:
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter="\t" if ext == ".tsv" else ",", lineterminator="\r\n")
        writer.writerows(md.table_text(table))
        body = buf.getvalue()
        if len(markdown) > 1:
            notes.append(
                f"Only the first of {len(markdown)} tables was written; use .xlsx to keep "
                "several tables (one sheet each)."
            )
    else:
        body = inner
    data = body.encode("utf-8", errors="replace")
    return (b"\xef\xbb\xbf" + data if ext == ".csv" else data), notes


def build_document(ext: str, content: str, title: str = "") -> tuple[bytes, str, list[str]]:
    """``(file bytes, what it holds, notes)`` for ``content`` as an ``ext`` file. "What it
    holds" is a short phrase such as ``"3 sheets: Sales, Costs, Notes"`` (or ``""``); notes
    say what was left out. Raises :class:`ContentError`."""
    if ext == ".docx":
        return markdown_to_docx(md.unfence(content, md.MARKDOWN_FENCES)), "", []
    if ext == ".xlsx":
        try:
            book = doc_xlsx.markdown_to_xlsx(content, title)
        except doc_xlsx.NoTableError:
            raise ContentError(XLSX_NEEDS_TABLE) from None
        shown = ", ".join(book.sheets[:6]) + (", ..." if len(book.sheets) > 6 else "")
        return book.data, f"{_plural(len(book.sheets), 'sheet')}: {shown}", book.notes
    if ext == ".pptx":
        try:
            deck = doc_pptx.markdown_to_pptx(md.unfence(content, md.MARKDOWN_FENCES), title)
        except ValueError:
            raise ContentError(PPTX_NEEDS_TEXT) from None
        return deck.data, _plural(deck.slides, "slide"), deck.notes
    if ext in (".csv", ".tsv"):
        data, notes = _delimited(content, ext)
        return data, "", notes
    if ext in (".html", ".htm") and not doc_html.looks_like_html(content):
        page = doc_html.markdown_to_html(md.unfence(content, md.MARKDOWN_FENCES), title)
        return page.encode("utf-8", errors="replace"), "", []
    return content.encode("utf-8", errors="replace"), "", []


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #


def create_document(
    filename: str, content: str, fmt: str | None = None, *, folder: str | os.PathLike[str] = ""
) -> ToolResult:
    """Save ``content`` as ``filename`` in the documents folder (``folder`` is the
    ``tools.documents_dir`` setting). Problems come back as ``ToolResult(ok=False)``."""
    try:
        name = sanitize_filename(filename, fmt)
    except ValueError as exc:
        return ToolResult(False, f"Invalid arguments for create_document: {exc}.", "bad file name")
    if not content.strip():
        return ToolResult(
            False, "Invalid arguments for create_document: 'content' is empty.", "empty document"
        )
    if len(content.encode("utf-8", errors="replace")) > MAX_DOCUMENT_BYTES:
        return ToolResult(
            False,
            "The document is larger than 5 MB. Write a shorter one or split it into parts.",
            "document too large",
        )
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    try:
        data, holds, notes = build_document(ext, content, stem)
    except ContentError as exc:
        return ToolResult(
            False, f"Invalid content for {name}: {exc}. Call create_document again.", "no content"
        )
    if len(data) > MAX_DOCUMENT_BYTES:
        return ToolResult(False, "The document is larger than 5 MB.", "document too large")
    target_dir = documents_dir(folder)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = _write_new(target_dir, name, data)
    except OSError as exc:
        log.warning("document_save_failed", error=type(exc).__name__)
        return ToolResult(
            False,
            f"The document could not be saved ({type(exc).__name__}). Tell the user.",
            "save failed",
        )
    size = len(data)
    log.info("document_saved", kind=ext, size=size)
    document = {"name": path.name, "path": str(path), "size": size, "kind": kind_for(path.name)}
    about = f"{_human_size(size)}, {holds}" if holds else _human_size(size)
    note = "".join(f" Note: {n}" for n in notes)
    return ToolResult(
        True,
        f"Saved {path.name} ({about}) in the user's documents folder. Tell the user its name; "
        f"they can open or download it from the chat.{note}",
        f"Saved {path.name}",
        document=document,
    )


# --------------------------------------------------------------------------- #
# Open / reveal / save a copy (bridge)
# --------------------------------------------------------------------------- #


def resolve_document(folder: Path, path: str | os.PathLike[str] | None) -> Path:
    """The real path of ``path`` if it is a file inside ``folder`` (links resolved), else
    an ``AppError`` the bridge returns as is."""
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        raise AppError("No document was given.", code="bad_request")
    target = Path(raw)
    if not target.is_absolute():
        target = folder / target
    try:
        real = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AppError(
            "That document no longer exists.",
            code="not_found",
            hint="It may have been moved, renamed or deleted.",
        ) from None
    root = folder.resolve(strict=False)
    if real == root or not real.is_relative_to(root):
        raise AppError(
            "Only files in the ChatForge documents folder can be opened from the chat.",
            code="bad_request",
            hint=f"The documents folder is {root}.",
        )
    if not real.is_file():
        raise AppError("That document no longer exists.", code="not_found")
    return real


def open_document(path: Path) -> None:
    """Open with the default app; scripts and unknown types open in Notepad instead, so
    opening never runs anything."""
    if not _WINDOWS:  # pragma: no cover - the app targets Windows
        subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
        return
    if path.suffix.lower() in SAFE_TO_OPEN:
        os.startfile(str(path))  # noqa: S606 - user-initiated, vetted path
    else:
        os.startfile("notepad.exe", "open", f'"{path}"')  # noqa: S606


def reveal_document(path: Path) -> None:
    """Show the file selected in Explorer."""
    if not _WINDOWS:  # pragma: no cover - the app targets Windows
        subprocess.Popen(["xdg-open", str(path.parent)])  # noqa: S603,S607
        return
    # A path cannot contain '"' on Windows, so the quoting below is safe.
    subprocess.Popen(f'explorer /select,"{path}"')  # noqa: S603


def save_file_types(name: str) -> tuple[str, ...]:
    """The Save As filters for ``name``: its own type first, then all files."""
    ext = os.path.splitext(name)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
        return ("All files (*.*)",)
    label = _TYPE_NAMES.get(ext, f"{ext[1:].upper()} file")
    return (f"{label} (*{ext})", "All files (*.*)")


def save_copy(source: Path, target: str | os.PathLike[str]) -> Path:
    """Copy ``source`` to ``target`` (a path the Save As dialog returned, which already
    asked before replacing a file). Written to a temporary name first, so a failed copy
    never leaves half a file. Raises ``AppError``."""
    dest = Path(target)
    if not dest.is_absolute() or not dest.name or dest.is_dir():
        raise AppError("Choose a file name to save the document as.", code="bad_request")
    try:
        if dest.exists() and os.path.samefile(source, dest):
            return dest  # saved over itself: nothing to do
    except OSError:
        pass
    temp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, dest)
    except PermissionError:
        _discard(temp)
        raise AppError(
            f"{dest.name} could not be saved there.",
            code="in_use",
            hint="Close it if it is open in another program, or choose another folder.",
        ) from None
    except OSError as exc:
        _discard(temp)
        raise AppError(
            f"{dest.name} could not be saved there ({type(exc).__name__}).",
            code="server",
            hint="Check that the folder exists and there is free space.",
        ) from None
    log.info("document_copied", kind=dest.suffix.lower(), size=dest.stat().st_size)
    return dest


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


__all__ = [
    "FORMATS",
    "MAX_DOCUMENT_BYTES",
    "ContentError",
    "build_document",
    "create_document",
    "default_documents_dir",
    "documents_dir",
    "downloads_dir",
    "markdown_to_docx",
    "open_document",
    "resolve_document",
    "reveal_document",
    "sanitize_filename",
    "save_copy",
    "save_file_types",
]
