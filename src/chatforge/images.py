"""Pictures attached to a message: checked, cleaned, stored, and shown to models that see.

:func:`prepare` turns the bytes of a PNG, JPEG, GIF (first frame), BMP, WebP or TIFF (first
page) picture into a :class:`Picture` with Pillow; AVIF and HEIC/HEIF too when this Pillow
build reads them (AVIF is built in since Pillow 11.2; HEIC needs the optional
``pillow-heif`` package), else the user is told to export a JPEG. It

* checks that the bytes really are a picture (only the formats above are tried, so Pillow
  never hands a file to an outside program such as Ghostscript),
* refuses decompression bombs before anything is decoded (the file is at most
  :data:`attachments.MAX_FILE_BYTES`; a big JPEG is decoded at 1/2, 1/4 or 1/8 scale;
  anything still over :data:`MAX_PIXELS` is refused),
* applies the EXIF orientation and converts colour profiles to sRGB,
* shrinks the long side to :data:`MAX_SIDE` pixels and saves it again as JPEG (quality
  :data:`JPEG_QUALITY`), or PNG when it has real transparency, at most
  :data:`MAX_STORED_BYTES`, with **no metadata**: no EXIF (camera, GPS position, time),
  XMP, ICC or comments,
* and makes a ~:data:`THUMB_SIDE` px JPEG thumbnail ``data:`` URL for the popup, plus, for a
  picture it shrank or one with transparency, a sharp flat copy (up to :data:`OCR_SIDE` px,
  on white, or on black when its content is light; also without metadata) that stays in
  memory for reading text in it (OCR) when the message is sent.

A sent picture is stored in the app's data folder (``Paths.attachments_dir``) as
``<sha256>.jpg`` / ``.png`` (:func:`store`); the message's ``_attachments`` record keeps the
file name, the size in pixels, the media type and the thumbnail (``Picture.record``), never
the image itself. :func:`cleanup` removes the files no message refers to any more.

``chat.history`` puts a picture in the prompt as an ``image_url`` part whose URL is
``chatforge-image:<file>`` (:func:`ref_part`); :func:`inline` swaps those for ``data:`` URLs
just before the request is sent, or for a short note when the file is gone. A model that
cannot see pictures gets :func:`note` text instead.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import math
import os
import re
import struct
import threading
import time
import warnings
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatforge.attachments import (
    MAX_FILE_BYTES,
    PICTURE_EXTS,
    AttachmentError,
    Extracted,
    clean_name,
    extract_text,
    kind_for,
)
from chatforge.attachments import read_path as read_text_path
from chatforge.logging_setup import get_logger

log = get_logger(__name__)

#: The longest side a picture is stored (and sent) with, in pixels.
MAX_SIDE = 1568
#: The thumbnail's longest side, in pixels.
THUMB_SIDE = 160
JPEG_QUALITY = 85
THUMB_QUALITY = 70
#: A stored picture is made smaller (lower quality, then fewer pixels) until it fits.
MAX_STORED_BYTES = 1_500_000
#: The most pixels decoded for one picture (after a big JPEG's reduced-scale decode).
MAX_PIXELS = 50_000_000
#: The smallest long side the size cap shrinks a picture to.
MIN_SIDE = 320
#: The longest side of the copy text is read from (OCR): small print in a big screenshot
#: is unreadable at MAX_SIDE. Older Windows OCR takes at most 2600 pixels.
OCR_SIDE = 2600
OCR_QUALITY = 90
#: The URL scheme of a picture reference in a prompt (``chatforge-image:<file>``).
REF_SCHEME = "chatforge-image:"
#: Tokens a picture costs when its size is unknown (the estimate for a 1568 x 1176 one).
DEFAULT_IMAGE_TOKENS = 2400

HEIC_UNSUPPORTED = "HEIC photos cannot be read here. Export it as JPEG and attach that."
AVIF_UNSUPPORTED = "AVIF pictures cannot be read here. Save it as JPEG or PNG and attach that."

_EXT_FORMAT = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".jfif": "JPEG",
    ".gif": "GIF", ".bmp": "BMP", ".dib": "BMP", ".webp": "WEBP", ".tif": "TIFF",
    ".tiff": "TIFF", ".avif": "AVIF", ".heic": "HEIF", ".heif": "HEIF",
}  # fmt: skip
#: The Pillow formats tried, in this order; nothing else is ever opened.
_OPEN_FORMATS = ("JPEG", "PNG", "GIF", "BMP", "DIB", "WEBP", "TIFF", "AVIF", "HEIF")
_FILE_RE = re.compile(r"[0-9a-f]{64}\.(?:jpg|png)")

_heif_state: bool | None = None


# --------------------------------------------------------------------------- #
# Formats
# --------------------------------------------------------------------------- #


def _ext(name: str) -> str:
    return Path(str(name or "").lower()).suffix


def heif_supported() -> bool:
    """True when HEIC/HEIF can be read (the optional ``pillow-heif`` package)."""
    global _heif_state
    if _heif_state is None:
        try:
            import pillow_heif  # type: ignore[import-not-found]

            pillow_heif.register_heif_opener()
            _heif_state = True
        except Exception:  # noqa: BLE001 - missing or broken: HEIC is refused politely
            _heif_state = False
    return _heif_state


def avif_supported() -> bool:
    """True when this Pillow build reads AVIF."""
    try:
        from PIL import features

        return bool(features.check("avif"))
    except Exception:  # noqa: BLE001
        return False


def _formats() -> list[str]:
    from PIL import Image

    Image.init()
    if _heif_state is None:
        heif_supported()
    return [f for f in _OPEN_FORMATS if f in Image.OPEN]


_SIGNATURES: tuple[tuple[int, bytes], ...] = (
    (0, b"\x89PNG\r\n\x1a\n"),
    (0, b"\xff\xd8\xff"),
    (0, b"GIF87a"),
    (0, b"GIF89a"),
    (0, b"II*\x00"),
    (0, b"MM\x00*"),
)
_FTYP_BRANDS = (b"avif", b"avis", b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


def sniff(data: bytes) -> bool:
    """True when ``data`` starts like a picture this module reads."""
    head = bytes(data[:32])
    if any(head[at : at + len(sig)] == sig for at, sig in _SIGNATURES):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[:2] == b"BM" and len(head) >= 18:  # BMP: the info header is 12..124 bytes
        return struct.unpack_from("<I", head, 14)[0] in (12, 40, 52, 56, 64, 108, 124)
    return head[4:8] == b"ftyp" and head[8:12] in _FTYP_BRANDS


def is_picture(name: str, data: bytes | None = None) -> bool:
    """True for a picture this module should read: a picture extension, or (``data``
    given) a file whose name says nothing known and whose bytes look like a picture."""
    ext = _ext(name)
    if ext in PICTURE_EXTS:
        return True
    return data is not None and kind_for(clean_name(name)) is None and sniff(data)


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Picture:
    """A cleaned picture, ready to store and send."""

    data: bytes = field(repr=False)
    media_type: str
    width: int
    height: int
    #: A small JPEG ``data:`` URL for the popup.
    thumb: str = field(repr=False)
    #: ``<sha256 of data>.jpg`` / ``.png``: the name it is stored under.
    file: str = ""
    #: A sharper JPEG (long side up to :data:`OCR_SIDE`, no metadata) to read text from,
    #: when the picture was shrunk; kept in memory only, never stored.
    ocr_data: bytes | None = field(default=None, repr=False)

    def record(self) -> dict[str, Any]:
        """The picture's keys in a message's ``_attachments`` entry."""
        return {
            "file": self.file,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "thumb": self.thumb,
        }


def _damaged(ext: str) -> str:
    what = ext.lstrip(".").upper() if ext else ""
    tail = f" or not really a {what} file" if what else ""
    return f"The picture could not be read; it may be damaged{tail}."


def prepare(data: bytes, *, name: str = "") -> tuple[Picture, str | None]:
    """Check and clean one picture (see the module docstring): ``(picture, warning)``.
    Raises :class:`AttachmentError` with a message for the user."""
    from PIL import Image, UnidentifiedImageError

    if len(data) > MAX_FILE_BYTES:
        raise AttachmentError("The file is larger than 20 MB.", code="too_large")
    ext = _ext(name)
    wanted = _EXT_FORMAT.get(ext)
    if wanted == "HEIF" and not heif_supported():
        raise AttachmentError(HEIC_UNSUPPORTED, code="unsupported")
    if wanted == "AVIF" and not avif_supported():
        raise AttachmentError(AVIF_UNSUPPORTED, code="unsupported")
    warning: str | None = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data), formats=_formats())
            width, height = im.size
            if im.format in ("JPEG", "MPO") and max(width, height) > 2 * OCR_SIDE:
                # Decode a big photo at 1/2, 1/4 or 1/8 scale, still at least OCR_SIDE long.
                scale = OCR_SIDE / max(width, height)
                im.draft("RGB", (max(1, int(width * scale)), max(1, int(height * scale))))
            if im.size[0] * im.size[1] > MAX_PIXELS:
                raise AttachmentError(_too_big(width, height), code="too_large")
            if getattr(im, "is_animated", False):
                im.seek(0)
                # A camera's MPO (a JPEG with a depth or preview image after it) is one photo.
                if im.format == "TIFF":
                    warning = "Only the first page is used."
                elif im.format != "MPO":
                    warning = "Only the first frame of this animation is used."
            im.load()
            picture = _clean(im)
    except AttachmentError:
        raise
    except Image.DecompressionBombError:
        raise AttachmentError(
            "The picture has too many pixels to read safely.", code="too_large"
        ) from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError, struct.error,
            IndexError, KeyError, TypeError, MemoryError):  # fmt: skip
        heic = wanted == "HEIF" or data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1")
        if heic and not heif_supported():
            raise AttachmentError(HEIC_UNSUPPORTED, code="unsupported") from None
        raise AttachmentError(_damaged(ext)) from None
    return picture, warning


def _too_big(width: int, height: int) -> str:
    mp = MAX_PIXELS // 1_000_000
    return f"The picture is too large ({width:,} × {height:,} pixels). Attach one under {mp} megapixels."


def _to_srgb(im: Any) -> Any:
    """``im`` converted from its embedded colour profile to sRGB (unchanged when it has
    none, or the conversion is not possible)."""
    icc = im.info.get("icc_profile")
    if not icc or im.mode not in ("RGB", "RGBA", "CMYK"):
        return im
    try:
        from PIL import ImageCms

        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        target = ImageCms.createProfile("sRGB")
        mode = "RGBA" if im.mode == "RGBA" else "RGB"
        return ImageCms.profileToProfile(im, source, target, outputMode=mode) or im
    except Exception:  # noqa: BLE001 - a broken profile: keep the colours as they are
        return im


def _flatten_mode(im: Any) -> tuple[Any, bool]:
    """``(image, has_alpha)``: RGB, or RGBA when some pixel is not fully opaque."""
    if im.mode in ("I", "F") or im.mode.startswith("I;16"):
        im = im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    alpha = im.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in im.info
    if alpha:
        im = im.convert("RGBA")
        low, _high = im.getchannel("A").getextrema()
        if low < 255:
            return im, True
    return im.convert("RGB") if im.mode != "RGB" else im, False


def _clean(im: Any) -> Picture:
    from PIL import Image, ImageOps

    try:
        im = ImageOps.exif_transpose(im) or im
    except Exception:  # noqa: BLE001 - damaged EXIF: the picture itself is still fine
        log.info("picture_exif_unreadable")
    im = _to_srgb(im)
    im, alpha = _flatten_mode(im)
    backdrop = _backdrop(im) if alpha else WHITE
    ocr_data = None
    # Text is read from a flat, sharp copy: of a shrunk picture, and of a transparent one
    # (OCR would put it on black, losing dark text).
    if alpha or max(im.size) > MAX_SIDE:
        sharp = _flat(im, backdrop)
        if max(sharp.size) > OCR_SIDE:
            sharp = sharp.copy()
            sharp.thumbnail((OCR_SIDE, OCR_SIDE), Image.Resampling.LANCZOS)
        ocr_data = _encode(sharp, False, OCR_QUALITY)
        im = im.copy()
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    quality = JPEG_QUALITY
    while True:
        data = _encode(im, alpha, quality)
        if len(data) <= MAX_STORED_BYTES or max(im.size) <= MIN_SIDE:
            break
        if not alpha and quality > 60:
            quality -= 10
            continue
        w, h = im.size
        im = im.resize((max(1, int(w * 0.75)), max(1, int(h * 0.75))), Image.Resampling.LANCZOS)
    media_type = "image/png" if alpha else "image/jpeg"
    digest = hashlib.sha256(data).hexdigest()
    return Picture(
        data=data,
        media_type=media_type,
        width=im.size[0],
        height=im.size[1],
        thumb=_thumbnail(im, backdrop),
        file=f"{digest}{'.png' if alpha else '.jpg'}",
        ocr_data=ocr_data,
    )


WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def _backdrop(im: Any) -> tuple[int, int, int]:
    """What to put a transparent picture on so its content shows: white behind dark
    content, black behind light content (white text on a clear logo), judged from the
    pixels that are mostly opaque."""
    from PIL import ImageStat

    if im.mode != "RGBA":
        return WHITE
    mask = im.getchannel("A").point(lambda a: 255 if a >= 128 else 0)
    if mask.getbbox() is None:
        return WHITE
    return WHITE if ImageStat.Stat(im.convert("L"), mask=mask).mean[0] < 128 else BLACK


def _flat(im: Any, backdrop: tuple[int, int, int] = WHITE) -> Any:
    """``im`` as RGB, a transparent one put on ``backdrop``."""
    from PIL import Image

    if im.mode != "RGBA":
        return im.convert("RGB") if im.mode != "RGB" else im
    back = Image.new("RGB", im.size, backdrop)
    back.paste(im, mask=im.getchannel("A"))
    return back


def _encode(im: Any, png: bool, quality: int) -> bytes:
    """``im`` saved without any metadata (no EXIF, XMP, ICC, text chunks or comments)."""
    from PIL import Image

    bare = Image.new(im.mode, im.size)
    bare.paste(im)
    buf = io.BytesIO()
    if png:
        bare.save(buf, "PNG", optimize=True)
    else:
        bare.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _thumbnail(im: Any, backdrop: tuple[int, int, int] = WHITE) -> str:
    from PIL import Image

    thumb = im.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
    thumb = _flat(thumb, backdrop)  # JPEG has no transparency
    buf = io.BytesIO()
    thumb.convert("RGB").save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------- #
# Attaching
# --------------------------------------------------------------------------- #


def extract(name: str, data: bytes) -> Extracted:
    """An attached picture as an :class:`~chatforge.attachments.Extracted` of kind
    ``image`` (no text; the cleaned picture is ``.image``). Raises ``AttachmentError``."""
    name = clean_name(name)
    picture, warning = prepare(data, name=name)
    return Extracted(name, "image", "", 0, False, warning, image=picture)


def read_path(path: str | Path) -> tuple[Extracted, int]:
    """Read a picture picked in the file dialog: ``(extracted, size in bytes)``."""
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
    return extract(target.name, data), len(data)


def load_data(name: str, data: bytes, *, max_chars: int) -> Extracted:
    """A dropped or pasted file: a picture read here, anything else by
    ``attachments.extract_text``."""
    if is_picture(name, data):
        return extract(name, data)
    return extract_text(name, data, max_chars=max_chars)


def load_path(path: str | Path, *, max_chars: int) -> tuple[Extracted, int]:
    """A file picked in the file dialog: ``(extracted, size)``, a picture read here (also
    one whose name says nothing known but whose first bytes are a picture's), anything
    else by ``attachments.read_path``."""
    target = Path(path)
    picture = is_picture(target.name)
    if not picture and kind_for(clean_name(target.name)) is None:
        with contextlib.suppress(OSError), target.open("rb") as fh:
            picture = sniff(fh.read(32))
    if picture:
        return read_path(target)
    return read_text_path(target, max_chars=max_chars)


# --------------------------------------------------------------------------- #
# Storing
# --------------------------------------------------------------------------- #


def store(picture: Picture, folder: Path) -> Path:
    """Write ``picture`` to ``folder/<file>`` (atomically; a file already there with the
    same name holds the same bytes, so it is kept). Raises ``OSError``."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / picture.file
    if target.is_file() and target.stat().st_size == len(picture.data):
        return target
    tmp = folder / f"{picture.file}.{os.getpid()}.tmp"
    try:
        tmp.write_bytes(picture.data)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return target


def is_image_record(record: Any) -> bool:
    return isinstance(record, dict) and record.get("kind") == "image"


def stored_file(record: dict) -> str | None:
    """The stored file a picture record names, when it is a name this module made."""
    name = str(record.get("file") or "")
    return name if _FILE_RE.fullmatch(name) else None


def referenced(messages: Iterable[dict]) -> set[str]:
    """The stored picture files the user messages of ``messages`` refer to."""
    out: set[str] = set()
    for msg in messages:
        files = msg.get("_attachments") if isinstance(msg, dict) else None
        for rec in files if isinstance(files, list) else []:
            if is_image_record(rec) and (name := stored_file(rec)):
                out.add(name)
    return out


def cleanup(folder: Path | None, keep: set[str], *, min_age_s: float = 0.0) -> int:
    """Delete the stored pictures in ``folder`` that are not in ``keep`` and the temporary
    files left behind (a half-written picture, a copy made to read text from), returning
    how many went. Files newer than ``min_age_s`` stay."""
    if folder is None:
        return 0
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    removed = 0
    now = time.time()
    try:
        entries = list(folder.iterdir())
    except OSError:
        return 0
    for path in entries:
        name = path.name
        ours = _FILE_RE.fullmatch(name) or (name.endswith(".tmp") and _FILE_RE.match(name))
        if not ours or name in keep:
            continue
        try:
            if min_age_s and now - path.stat().st_mtime < min_age_s:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("picture_cleanup_failed", file=name, error=type(exc).__name__)
    if removed:
        log.info("pictures_removed", count=removed)
    return removed


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #


def image_tokens(width: Any, height: Any) -> int:
    """A conservative estimate of what one picture costs a vision model, in tokens: one
    per 28 x 28 pixel patch (Qwen-VL's rate; OpenAI's and MiniMax's are lower)."""
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return DEFAULT_IMAGE_TOKENS
    if w <= 0 or h <= 0:
        return DEFAULT_IMAGE_TOKENS
    return max(85, math.ceil(w / 28) * math.ceil(h / 28))


def describe(record: dict) -> str:
    """``"photo.jpg" (1600×1200)`` (the size only when it is known)."""
    name = str(record.get("name") or "image").replace('"', "'")
    w, h = record.get("width"), record.get("height")
    size = f" ({int(w)}×{int(h)})" if isinstance(w, int) and isinstance(h, int) else ""
    return f'"{name}"{size}'


#: Notes a model gets in place of a picture it is not shown (``{}``: :func:`describe`).
NOTE_BLIND = "[Image {} attached — this model cannot see images.]"
NOTE_BLIND_OCR = "[Image {} attached — this model cannot see images. Text recognised in it:]"
NOTE_EARLIER = "[Image {} was attached earlier; it is no longer shown to the model.]"
NOTE_LEFT_OUT = "[Image {} attached, but left out: it does not fit the model's context window.]"
NOTE_MISSING = "[Image {} is no longer available.]"
NOTE_SHOWN = "[Image {} attached below.]"


def ref_part(record: dict) -> dict:
    """The prompt's ``image_url`` part for a picture record, pointing at its stored file
    (:func:`inline` makes it a ``data:`` URL). ``_image`` holds what the estimate and the
    "missing" note need; :func:`inline` drops it."""
    return {
        "type": "image_url",
        "image_url": {"url": f"{REF_SCHEME}{stored_file(record) or ''}"},
        "_image": {
            "name": str(record.get("name") or "image"),
            "width": record.get("width"),
            "height": record.get("height"),
            "media_type": str(record.get("media_type") or "image/jpeg"),
        },
    }


def part_tokens(part: Any) -> int:
    """What one ``image_url`` content part costs (0 for any other part)."""
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return 0
    meta = part.get("_image")
    if isinstance(meta, dict):
        return image_tokens(meta.get("width"), meta.get("height"))
    return DEFAULT_IMAGE_TOKENS


_URL_CACHE_MAX = 32
_url_cache: OrderedDict[tuple[str, int, int, str], str] = OrderedDict()
_url_cache_lock = threading.Lock()


def _file_data_url(path: Path, media_type: str) -> str:
    """``data:`` URL for the picture at ``path``. Read and encoded once while the file is
    unchanged (names are content hashes, so that is almost always); the file is ``stat``-ed
    on every call, so one that was deleted raises ``OSError`` rather than coming from the
    cache, and one rewritten in place (new size or mtime) is read again."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns, media_type)
    with _url_cache_lock:
        url = _url_cache.get(key)
        if url is not None:
            _url_cache.move_to_end(key)
            return url
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    url = f"data:{media_type};base64,{encoded}"
    with _url_cache_lock:
        _url_cache[key] = url
        _url_cache.move_to_end(key)
        while len(_url_cache) > _URL_CACHE_MAX:
            _url_cache.popitem(last=False)
    return url


def inline(messages: list[dict], folder: Path | None) -> list[dict]:
    """``messages`` with each picture reference made a ``data:`` URL read from
    ``folder``, or a text note when the file is missing. Messages without one are
    returned as they are (not copied)."""
    cache: dict[str, str | None] = {}

    def data_url(name: str, media_type: str) -> str | None:
        if name not in cache:
            cache[name] = None
            if folder is not None and _FILE_RE.fullmatch(name):
                try:
                    cache[name] = _file_data_url(Path(folder) / name, media_type)
                except OSError:
                    log.warning("picture_missing", file=name)
        return cache[name]

    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list) or not any(_is_ref(p) for p in content):
            out.append(msg)
            continue
        parts: list[dict] = []
        for part in content:
            if not _is_ref(part):
                parts.append(part)
                continue
            meta = part.get("_image") if isinstance(part.get("_image"), dict) else {}
            name = str(part["image_url"]["url"])[len(REF_SCHEME) :]
            url = data_url(name, str(meta.get("media_type") or "image/jpeg"))
            if url is None:
                parts.append({"type": "text", "text": NOTE_MISSING.format(describe(meta))})
            else:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        out.append({**msg, "content": parts})
    return out


def _is_ref(part: Any) -> bool:
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return False
    url = part.get("image_url")
    return isinstance(url, dict) and str(url.get("url") or "").startswith(REF_SCHEME)


__all__ = [
    "AVIF_UNSUPPORTED",
    "HEIC_UNSUPPORTED",
    "MAX_SIDE",
    "NOTE_BLIND",
    "NOTE_BLIND_OCR",
    "NOTE_EARLIER",
    "NOTE_LEFT_OUT",
    "NOTE_MISSING",
    "NOTE_SHOWN",
    "REF_SCHEME",
    "Picture",
    "avif_supported",
    "cleanup",
    "describe",
    "extract",
    "heif_supported",
    "image_tokens",
    "inline",
    "is_image_record",
    "is_picture",
    "load_data",
    "load_path",
    "part_tokens",
    "prepare",
    "read_path",
    "ref_part",
    "referenced",
    "sniff",
    "store",
    "stored_file",
]
