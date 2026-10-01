"""``create_document`` tool: save a file the model wrote (a report, a table, a script) into
the user's documents folder, so the chat can offer it like a download.

The folder is ``tools.documents_dir``; empty means ``AI Chat`` inside the user's Documents
folder, created on first use. The format comes from the file name's extension (or the
``format`` argument): text formats are written as UTF-8, ``.docx`` is built from
Markdown-style text (headings, paragraphs, bullet and numbered lists, tables, bold, italic
and code) as minimal WordprocessingML, with only the standard library. Files are never
overwritten: a taken name gets `` (2)``, `` (3)`` ... .

:func:`resolve_document`, :func:`open_document` and :func:`reveal_document` back the
bridge's ``open_document`` / ``reveal_document``: only files inside the documents folder
(after resolving links) are opened or shown.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from aichat.attachments import kind_for
from aichat.errors import AppError
from aichat.logging_setup import get_logger
from aichat.tools.registry import ToolResult

log = get_logger(__name__)

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_NAME_CHARS = 120
FOLDER_NAME = "AI Chat"
#: Written as UTF-8 text.
TEXT_FORMATS = frozenset(
    {".md", ".txt", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml",
     ".py", ".js", ".ts", ".css", ".sql", ".ps1", ".sh"}
)  # fmt: skip
FORMATS = TEXT_FORMATS | {".docx"}
#: ``format`` values that name a type in words rather than by extension.
_FORMAT_ALIASES = {
    "markdown": ".md",
    "text": ".txt",
    "plain": ".txt",
    "word": ".docx",
    "javascript": ".js",
    "typescript": ".ts",
    "python": ".py",
    "powershell": ".ps1",
    "bash": ".sh",
    "shell": ".sh",
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
     ".docx", ".log"}
)  # fmt: skip
_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- #
# Folder and names
# --------------------------------------------------------------------------- #


def default_documents_dir() -> Path:
    """``AI Chat`` in the user's Documents folder (where Windows keeps it, which may be
    redirected to OneDrive), else ``%USERPROFILE%\\Documents\\AI Chat``."""
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
    text = str(configured or "").strip()
    if not text:
        return default_documents_dir()
    return Path(os.path.expandvars(text)).expanduser()


def sanitize_filename(filename: str, fmt: str | None = None) -> str:
    """A safe file name with a supported extension. Raises ``ValueError`` (shown to the
    model) for an extension that cannot be written.

    Folders are dropped, characters Windows forbids become ``_``, reserved device names
    (``CON``, ``NUL``, ``COM1`` ...) get a leading ``_``, and the name is cut to 120
    characters. Without a supported extension the ``format`` decides (default ``.md``).
    """
    name = re.split(r"[\\/]", str(filename or ""))[-1]
    name = _BAD_CHARS.sub("_", name).strip().strip(".").strip()
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = f".{ext.lower()}" if ext else ""
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
    ext = os.path.splitext(name)[1].lower()
    text = content.encode("utf-8", errors="replace")
    if len(text) > MAX_DOCUMENT_BYTES:
        return ToolResult(
            False,
            "The document is larger than 5 MB. Write a shorter one or split it into parts.",
            "document too large",
        )
    if ext == ".docx":
        data = markdown_to_docx(content)
    elif ext == ".csv":
        data = b"\xef\xbb\xbf" + text  # a BOM, so Excel reads the CSV as UTF-8
    else:
        data = text
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
    return ToolResult(
        True,
        f"Saved {path.name} ({_human_size(size)}) in the user's documents folder. Tell the "
        "user its name; they can open it from the chat.",
        f"Saved {path.name}",
        document=document,
    )


# --------------------------------------------------------------------------- #
# Open / reveal (bridge)
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
            "Only files in the AI Chat documents folder can be opened from the chat.",
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


# --------------------------------------------------------------------------- #
# Markdown -> .docx (minimal WordprocessingML)
# --------------------------------------------------------------------------- #

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

_CONTENT_TYPES = (
    _XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
    "</Types>"
)
_PACKAGE_RELS = (
    _XML_HEAD
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_DOCUMENT_RELS = (
    _XML_HEAD
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/styles" Target="styles.xml"/>'
    f'<Relationship Id="rId2" Type="{_REL_NS}/numbering" Target="numbering.xml"/>'
    "</Relationships>"
)


def _heading_style(sid: str, name: str, level: int, size: int) -> str:
    return (
        f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="320" w:after="120"/>'
        f'<w:outlineLvl w:val="{level}"/></w:pPr>'
        f'<w:rPr><w:b/><w:bCs/><w:color w:val="1F3864"/><w:sz w:val="{size}"/>'
        f'<w:szCs w:val="{size}"/></w:rPr></w:style>'
    )


_BORDER = 'w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"'
_STYLES = (
    _XML_HEAD + f'<w:styles xmlns:w="{_W_NS}">'
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US"/>'
    "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
    '<w:spacing w:after="160" w:line="259" w:lineRule="auto"/>'
    "</w:pPr></w:pPrDefault></w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    + _heading_style("Heading1", "heading 1", 0, 32)
    + _heading_style("Heading2", "heading 2", 1, 28)
    + _heading_style("Heading3", "heading 3", 2, 24)
    + '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="60"/>'
    '<w:ind w:left="720"/><w:contextualSpacing/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:ind w:left="720"/></w:pPr>'
    '<w:rPr><w:i/><w:iCs/><w:color w:val="404040"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>'
    '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
    '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
    '<w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>'
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>'
    "<w:tblPr><w:tblBorders>"
    f"<w:top {_BORDER}/><w:left {_BORDER}/><w:bottom {_BORDER}/><w:right {_BORDER}/>"
    f"<w:insideH {_BORDER}/><w:insideV {_BORDER}/>"
    '</w:tblBorders><w:tblCellMar><w:left w:w="108" w:type="dxa"/>'
    '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
    "</w:styles>"
)

_BULLETS = ("•", "◦", "▪")  # • ◦ ▪
_NUMBER_FORMATS = (("decimal", "%1."), ("lowerLetter", "%2."), ("lowerRoman", "%3."))
_LIST_LEVELS = 3
_BULLET_NUM_ID = 1


def _numbering(starts: list[int]) -> str:
    """``numbering.xml``: numId 1 is the bullet list; each numbered list gets its own
    numId (2, 3, ...) so its numbering restarts at its first number."""

    def level(i: int, fmt: str, text: str) -> str:
        indent = 720 * (i + 1)
        return (
            f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
            f'<w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{indent}" w:hanging="360"/></w:pPr></w:lvl>'
        )

    bullets = "".join(level(i, "bullet", _BULLETS[i]) for i in range(_LIST_LEVELS))
    numbers = "".join(level(i, *_NUMBER_FORMATS[i]) for i in range(_LIST_LEVELS))
    nums = f'<w:num w:numId="{_BULLET_NUM_ID}"><w:abstractNumId w:val="0"/></w:num>'
    for k, start in enumerate(starts):
        nums += (
            f'<w:num w:numId="{k + 2}"><w:abstractNumId w:val="1"/>'
            f'<w:lvlOverride w:ilvl="0"><w:startOverride w:val="{max(0, start)}"/>'
            "</w:lvlOverride></w:num>"
        )
    return (
        _XML_HEAD + f'<w:numbering xmlns:w="{_W_NS}">'
        '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>'
        f"{bullets}</w:abstractNum>"
        '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>'
        f"{numbers}</w:abstractNum>{nums}</w:numbering>"
    )


#: Characters XML 1.0 cannot hold (and lone surrogates, which UTF-8 cannot encode).
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
_INLINE = re.compile(
    r"(\*\*\*[^*\n]+?\*\*\*"  # ***bold italic***
    r"|\*\*[^\n]+?\*\*"  # **bold**
    r"|__[^_\n]+?__"  # __bold__
    r"|`[^`\n]+`"  # `code`
    r"|\*[^*\s](?:[^*\n]*?[^*\s])?\*"  # *italic*
    r"|(?<!\w)_[^_\s](?:[^_\n]*?[^_\s])?_(?!\w)"  # _italic_
    r"|\[[^\]\n]+\]\([^)\s]+\))"  # [text](url)
)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$|^\s*\|\s*:?-+:?\s*\|\s*$")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def _text(s: str) -> str:
    return escape(_INVALID_XML.sub("", s))


def _run(text: str, *, bold: bool = False, italic: bool = False, code: bool = False) -> str:
    if not text:
        return ""
    props = ""
    if code:
        props += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
    if bold:
        props += "<w:b/><w:bCs/>"
    if italic:
        props += "<w:i/><w:iCs/>"
    pieces = "<w:tab/>".join(
        f'<w:t xml:space="preserve">{_text(part)}</w:t>' if part else ""
        for part in text.split("\t")
    )
    return f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}{pieces}</w:r>"


def _inline(text: str, *, bold: bool = False) -> str:
    """Runs for one line of Markdown text: ``**bold**``, ``*italic*``, `` `code` `` and
    ``[text](url)`` (written as "text (url)")."""
    out: list[str] = []
    for i, token in enumerate(_INLINE.split(text)):
        if not token:
            continue
        if i % 2 == 0:
            out.append(_run(token, bold=bold))
        elif token.startswith("***"):
            out.append(_run(token[3:-3], bold=True, italic=True))
        elif token.startswith(("**", "__")):
            out.append(_run(token[2:-2], bold=True))
        elif token.startswith("`"):
            out.append(_run(token[1:-1], bold=bold, code=True))
        elif token.startswith("["):
            m = _LINK.fullmatch(token)
            label, url = (m.group(1), m.group(2)) if m else (token, "")
            out.append(_run(f"{label} ({url})" if url and url != label else label, bold=bold))
        else:
            out.append(_run(token[1:-1], bold=bold, italic=True))
    return "".join(out)


def _paragraph(runs: str, style: str | None = None, num: tuple[int, int] | None = None) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if num is not None:
        num_id, level = num
        props += f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>'
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{runs}</w:p>"


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _table(rows: list[list[str]]) -> str:
    cols = max(len(r) for r in rows)
    width = 9360 // cols  # the text width of a Letter page with 1" margins, in twips
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(cols))
    out = [
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
        f'<w:tblLook w:val="04A0"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
    ]
    for r, row in enumerate(rows):
        header = r == 0
        out.append("<w:tr><w:trPr><w:tblHeader/></w:trPr>" if header else "<w:tr>")
        for c in range(cols):
            cell = row[c] if c < len(row) else ""
            out.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
                f"{_paragraph(_inline(cell, bold=header))}</w:tc>"
            )
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def _body(markdown: str) -> tuple[str, list[int]]:
    """``(body XML, the first number of each numbered list)``."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    starts: list[int] = []
    current: int | None = None  # numId of the numbered list in progress

    def flush() -> None:
        if para:
            out.append(_paragraph("<w:r><w:br/></w:r>".join(_inline(p) for p in para)))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            current = None
            fence = stripped[:3]
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1
            out.extend(_paragraph(_run(c), "Code") for c in code or [""])
            continue
        if not stripped:
            flush()
            i += 1
            continue
        if (m := _HEADING.match(line)) is not None:
            flush()
            current = None
            level = min(len(m.group(1)), 3)
            out.append(_paragraph(_inline(m.group(2)), f"Heading{level}"))
        elif _RULE.match(line):
            flush()
            current = None
            out.append(
                '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
                'w:color="A6A6A6"/></w:pBdr></w:pPr></w:p>'
            )
        elif "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            current = None
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            out.append(_table(rows))
            continue
        elif (m := _BULLET.match(line)) is not None:
            flush()
            level = min(len(m.group(1).expandtabs(4)) // 2, _LIST_LEVELS - 1)
            if level == 0:
                current = None
            out.append(_paragraph(_inline(m.group(2)), "ListParagraph", (_BULLET_NUM_ID, level)))
        elif (m := _NUMBERED.match(line)) is not None:
            flush()
            level = min(len(m.group(1).expandtabs(4)) // 2, _LIST_LEVELS - 1)
            if current is None:
                starts.append(int(m.group(2)) if level == 0 else 1)
                current = len(starts) + 1
            out.append(_paragraph(_inline(m.group(3)), "ListParagraph", (current, level)))
        elif (m := _QUOTE.match(line)) is not None:
            flush()
            current = None
            out.append(_paragraph(_inline(m.group(1)), "Quote"))
        else:
            if not line[:1].isspace():
                current = None
            para.append(stripped)
        i += 1
    flush()
    if not out or out[-1].startswith("<w:tbl>"):
        out.append("<w:p/>")  # Word wants a paragraph after a table that ends the body
    return "".join(out), starts


def markdown_to_docx(markdown: str) -> bytes:
    """A Word document from Markdown-style text (see the module docstring)."""
    body, starts = _body(markdown)
    document = (
        _XML_HEAD + f'<w:document xmlns:w="{_W_NS}" xmlns:r="{_REL_NS}"><w:body>{body}'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>'
        "</w:sectPr></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _PACKAGE_RELS)
        zf.writestr("word/_rels/document.xml.rels", _DOCUMENT_RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", _STYLES)
        zf.writestr("word/numbering.xml", _numbering(starts))
    return buf.getvalue()


__all__ = [
    "FORMATS",
    "MAX_DOCUMENT_BYTES",
    "create_document",
    "default_documents_dir",
    "documents_dir",
    "markdown_to_docx",
    "open_document",
    "resolve_document",
    "reveal_document",
    "sanitize_filename",
]
