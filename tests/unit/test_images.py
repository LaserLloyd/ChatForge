"""chatforge.images: attached pictures are checked, cleaned, stored and sent.

Every picture here is drawn by the test (no real photos)."""

from __future__ import annotations

import base64
import hashlib
import io
import random
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from chatforge import images
from chatforge.attachments import AttachmentError, Extracted, as_record

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def encode(im: Image.Image, fmt: str, **params) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **params)
    return buf.getvalue()


def halves(size: tuple[int, int], left=(220, 20, 20), right=(20, 20, 220)) -> Image.Image:
    """Left half red, right half blue: shows which way a picture was turned."""
    im = Image.new("RGB", size, right)
    im.paste(Image.new("RGB", (size[0] // 2, size[1]), left), (0, 0))
    return im


def camera_exif(orientation: int = 1) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation
    exif[0x010F] = "TestCam"  # Make
    exif[0x0132] = "2026:01:02 03:04:05"  # DateTime
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    gps[3] = "W"
    gps[4] = (0.0, 7.0, 0.0)
    return exif.tobytes()


def png_claiming(width: int, height: int) -> bytes:
    """A tiny PNG whose header claims ``width`` x ``height`` pixels."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
        + struct.pack(">I", 0)
        + b"IEND"
        + struct.pack(">I", zlib.crc32(b"IEND"))
    )


def decode(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def picture(name: str = "photo.jpg", size=(800, 600)) -> images.Picture:
    pic, _warning = images.prepare(encode(halves(size), "JPEG"), name=name)
    return pic


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #


def test_a_photo_is_turned_upright_shrunk_and_stripped_of_metadata() -> None:
    raw = encode(halves((4000, 3000)), "JPEG", quality=92, exif=camera_exif(orientation=6))
    pic, warning = images.prepare(raw, name="IMG_0001.JPG")
    assert warning is None
    assert pic.media_type == "image/jpeg"
    # Orientation 6: shown turned a quarter clockwise, so it is portrait, and the long side
    # is at most MAX_SIDE.
    assert (pic.width, pic.height) == (1176, 1568)
    out = decode(pic.data)
    assert out.format == "JPEG" and out.size == (1176, 1568)
    top = out.getpixel((pic.width // 2, 40))
    bottom = out.getpixel((pic.width // 2, pic.height - 40))
    assert top[0] > 150 and top[2] < 100, "the left (red) half should now be at the top"
    assert bottom[2] > 150 and bottom[0] < 100
    # No camera data, no GPS position, no colour profile, no comments: nothing but pixels.
    assert dict(out.getexif()) == {}
    assert not {"exif", "icc_profile", "xmp", "comment", "photoshop"} & set(out.info)
    assert b"TestCam" not in pic.data and b"Exif" not in pic.data
    assert pic.file == f"{hashlib.sha256(pic.data).hexdigest()}.jpg"


def test_a_shrunk_picture_keeps_a_sharper_copy_to_read_text_from() -> None:
    raw = encode(halves((3000, 2000)), "PNG")
    pic, _ = images.prepare(raw, name="screenshot.png")
    assert (pic.width, pic.height) == (1568, 1045)
    sharp = decode(pic.ocr_data)
    assert sharp.format == "JPEG" and sharp.size == (2600, 1733)
    assert dict(sharp.getexif()) == {} and "icc_profile" not in sharp.info
    big = encode(halves((6000, 4000)), "JPEG", exif=camera_exif())
    sharp = decode(images.prepare(big, name="photo.jpg")[0].ocr_data)
    assert max(sharp.size) <= images.OCR_SIDE and b"TestCam" not in images.prepare(big)[0].ocr_data
    # Not shrunk: the stored copy is as sharp as it gets.
    assert images.prepare(encode(halves((800, 600)), "PNG"), name="s.png")[0].ocr_data is None


def test_text_in_a_transparent_picture_is_read_on_a_backdrop_that_shows_it() -> None:
    def logo(ink) -> images.Picture:
        im = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
        im.paste(Image.new("RGBA", (300, 60), ink), (50, 70))
        return images.prepare(encode(im, "PNG"), name="logo.png")[0]

    dark, light = logo((20, 20, 20, 255)), logo((250, 250, 250, 255))
    assert dark.media_type == light.media_type == "image/png"
    # OCR would put transparency on black: a flat copy is read instead, on white behind
    # dark ink and on black behind light ink.
    assert decode(dark.ocr_data).getpixel((5, 5)) == pytest.approx((255, 255, 255), abs=3)
    assert decode(light.ocr_data).getpixel((5, 5)) == pytest.approx((0, 0, 0), abs=3)
    assert decode(light.ocr_data).getpixel((200, 100))[0] > 200
    # The thumbnail too: white ink is not lost on white.
    thumb = decode(base64.b64decode(light.thumb.split(",", 1)[1]))
    assert thumb.getpixel((2, 2))[0] < 30


def test_pending_pictures_are_capped_by_size() -> None:
    from chatforge.attachments import AttachmentStore

    raw = encode(halves((2000, 1500)), "PNG")
    files = [images.extract(f"p{i}.png", raw) for i in range(3)]
    one = len(files[0].image.data) + len(files[0].image.ocr_data)
    store = AttachmentStore(max_bytes=int(one * 2.5))
    ids = [store.add(f, len(raw)).id for f in files]
    assert ids[0] not in store and ids[1] in store and ids[2] in store
    tiny = AttachmentStore(max_bytes=1)
    only = tiny.add(files[0], len(raw)).id
    assert only in tiny  # the newest file is always kept


def test_a_small_picture_keeps_its_size() -> None:
    pic, _ = images.prepare(encode(halves((640, 480)), "PNG"), name="small.png")
    assert (pic.width, pic.height) == (640, 480)
    assert pic.media_type == "image/jpeg"  # no transparency: JPEG


def test_real_transparency_keeps_png_opaque_alpha_does_not() -> None:
    clear = Image.new("RGBA", (300, 200), (0, 128, 255, 0))
    clear.paste(Image.new("RGBA", (100, 100), (255, 0, 0, 255)), (50, 50))
    pic, _ = images.prepare(encode(clear, "PNG"), name="logo.png")
    assert pic.media_type == "image/png" and pic.file.endswith(".png")
    out = decode(pic.data)
    assert out.mode == "RGBA" and out.getpixel((5, 5))[3] == 0
    solid = Image.new("RGBA", (300, 200), (0, 128, 255, 255))
    pic, _ = images.prepare(encode(solid, "PNG"), name="solid.png")
    assert pic.media_type == "image/jpeg" and pic.file.endswith(".jpg")
    # A palette picture with a transparent colour is transparency too.
    pal = Image.new("P", (40, 40), 0)
    pal.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    pal.paste(1, (10, 10, 30, 30))
    pic, _ = images.prepare(encode(pal, "GIF", transparency=0), name="icon.gif")
    assert pic.media_type == "image/png"


def test_an_animation_uses_its_first_frame() -> None:
    frames = [Image.new("RGB", (64, 48), c) for c in ((250, 0, 0), (0, 250, 0), (0, 0, 250))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100)
    pic, warning = images.prepare(buf.getvalue(), name="loop.gif")
    assert warning == "Only the first frame of this animation is used."
    r, g, b = decode(pic.data).getpixel((32, 24))
    assert r > 200 and g < 60 and b < 60


def test_a_cameras_mpo_photo_is_one_photo() -> None:
    first, second = Image.new("RGB", (400, 300), (200, 0, 0)), Image.new("RGB", (400, 300), "blue")
    buf = io.BytesIO()
    first.save(buf, "MPO", save_all=True, append_images=[second])
    pic, warning = images.prepare(buf.getvalue(), name="DSCF0001.JPG")
    assert warning is None and (pic.width, pic.height) == (400, 300)
    assert decode(pic.data).getpixel((10, 10))[0] > 150


@pytest.mark.parametrize(
    ("name", "make"),
    [
        ("scan.bmp", lambda: encode(halves((120, 80)), "BMP")),
        ("page.tiff", lambda: encode(halves((120, 80)), "TIFF")),
        ("shot.webp", lambda: encode(halves((120, 80)), "WEBP")),
        ("print.jpg", lambda: encode(Image.new("CMYK", (120, 80), (0, 255, 255, 0)), "JPEG")),
        ("depth.png", lambda: encode(Image.new("I;16", (120, 80), 40000), "PNG")),
        ("grey.png", lambda: encode(Image.new("L", (120, 80), 128), "PNG")),
    ],
)
def test_other_formats_and_modes_become_rgb_jpeg(name: str, make) -> None:
    pic, _ = images.prepare(make(), name=name)
    out = decode(pic.data)
    assert out.mode == "RGB" and out.size == (120, 80)


def test_16_bit_values_are_scaled_not_clipped() -> None:
    pic, _ = images.prepare(encode(Image.new("I;16", (32, 32), 32768), "PNG"), name="d.png")
    value = decode(pic.data).getpixel((16, 16))[0]
    assert 100 < value < 160  # half of the 16-bit range is mid-grey, not white


def test_avif_is_read_when_pillow_can() -> None:
    if not images.avif_supported():
        with pytest.raises(AttachmentError) as ei:
            images.prepare(b"\x00\x00\x00\x1cftypavif" + b"\x00" * 64, name="x.avif")
        assert ei.value.message == images.AVIF_UNSUPPORTED
        return
    pic, _ = images.prepare(encode(halves((200, 100)), "AVIF"), name="photo.avif")
    assert (pic.width, pic.height) == (200, 100) and pic.media_type == "image/jpeg"


def test_heic_without_its_plugin_says_to_export_a_jpeg(monkeypatch) -> None:
    monkeypatch.setattr(images, "_heif_state", False)
    for name in ("IMG_0002.HEIC", "photo.heif"):
        with pytest.raises(AttachmentError) as ei:
            images.prepare(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, name=name)
        assert ei.value.message == images.HEIC_UNSUPPORTED and ei.value.code == "unsupported"
    # A HEIC photo saved under another name is recognised by its bytes.
    with pytest.raises(AttachmentError) as ei:
        images.prepare(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, name="photo.jpg")
    assert ei.value.message == images.HEIC_UNSUPPORTED


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("photo.png", b"\x89PNG\r\n\x1a\n"),  # a signature and nothing else
        ("photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 40),
        ("photo.jpg", b"just some text, not a picture"),
        ("photo.gif", b""),
    ],
)
def test_damaged_or_fake_pictures_are_refused(name: str, data: bytes) -> None:
    with pytest.raises(AttachmentError) as ei:
        images.prepare(data, name=name)
    ext = name.rsplit(".", 1)[1].upper()
    assert ei.value.message == (
        f"The picture could not be read; it may be damaged or not really a {ext} file."
    )


def test_formats_pillow_would_hand_to_other_programs_are_never_opened(tmp_path: Path) -> None:
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    with pytest.raises(AttachmentError):
        images.prepare(eps, name="drawing.png")


def test_decompression_bombs_are_refused_before_decoding(monkeypatch) -> None:
    # Over Pillow's own limit: refused while the header is read.
    with pytest.raises(AttachmentError) as ei:
        images.prepare(png_claiming(30_000, 30_000), name="bomb.png")
    assert ei.value.code == "too_large" and "too many pixels" in ei.value.message
    # Over ours (lowered here): refused with the size, before decoding.
    monkeypatch.setattr(images, "MAX_PIXELS", 10_000)
    with pytest.raises(AttachmentError) as ei:
        images.prepare(png_claiming(200, 200), name="big.png")
    assert ei.value.message.startswith("The picture is too large (200 × 200 pixels)")


def test_a_big_jpeg_is_decoded_at_reduced_scale(monkeypatch) -> None:
    # 6000 x 4000 = 24 Mpx is over a 10 Mpx cap, but a JPEG is decoded at 1/2 or 1/4 scale.
    monkeypatch.setattr(images, "MAX_PIXELS", 10_000_000)
    pic, _ = images.prepare(encode(halves((6000, 4000)), "JPEG", quality=70), name="big.jpg")
    assert (pic.width, pic.height) == (1568, 1045)


def test_the_file_size_cap(monkeypatch) -> None:
    monkeypatch.setattr(images, "MAX_FILE_BYTES", 100)
    with pytest.raises(AttachmentError) as ei:
        images.prepare(encode(halves((64, 64)), "PNG") + b"\x00" * 200, name="a.png")
    assert ei.value.code == "too_large"


def test_the_stored_size_is_capped(monkeypatch) -> None:
    noise = Image.frombytes("RGB", (1200, 900), random.Random(7).randbytes(1200 * 900 * 3))
    monkeypatch.setattr(images, "MAX_STORED_BYTES", 150_000)
    pic, _ = images.prepare(encode(noise, "PNG"), name="noise.png")
    assert len(pic.data) <= 150_000 or max(pic.width, pic.height) <= images.MIN_SIDE


def test_the_thumbnail_is_a_small_jpeg_data_url() -> None:
    clear = Image.new("RGBA", (900, 300), (0, 0, 0, 0))
    pic, _ = images.prepare(encode(clear, "PNG"), name="wide.png")
    assert pic.thumb.startswith("data:image/jpeg;base64,")
    thumb = decode(base64.b64decode(pic.thumb.split(",", 1)[1]))
    assert thumb.format == "JPEG" and max(thumb.size) <= images.THUMB_SIDE
    assert thumb.getpixel((5, 5)) == pytest.approx((255, 255, 255), abs=3)  # on white
    assert len(pic.thumb) < 12_000


# --------------------------------------------------------------------------- #
# Attaching
# --------------------------------------------------------------------------- #


def test_extract_and_records() -> None:
    raw = encode(halves((800, 600)), "PNG")
    ex = images.extract("C:\\Users\\me\\Pictures\\shot.png", raw)
    assert isinstance(ex, Extracted)
    assert (ex.name, ex.kind, ex.text, ex.chars, ex.truncated) == (
        "shot.png",
        "image",
        "",
        0,
        False,
    )
    record = as_record(ex)
    assert record == {
        "name": "shot.png",
        "kind": "image",
        "chars": 0,
        "truncated": False,
        "text": "",
        "file": ex.image.file,
        "media_type": "image/jpeg",
        "width": 800,
        "height": 600,
        "thumb": ex.image.thumb,
    }
    # A stored record passed back in (a dict) keeps its picture keys.
    assert as_record({**record, "ocr": "Hello"}) == {**record, "ocr": "Hello"}


def test_what_counts_as_a_picture() -> None:
    png = encode(halves((20, 20)), "PNG")
    assert (
        images.is_picture("a.PNG") and images.is_picture("b.jpeg") and images.is_picture("c.heic")
    )
    assert not images.is_picture("notes.txt", png)  # a text name wins
    assert images.is_picture("photo", png)  # no extension: the bytes say
    assert images.is_picture("download.bin", png)
    assert not images.is_picture("photo", b"hello")
    assert images.sniff(encode(halves((20, 20)), "BMP")) and images.sniff(b"GIF89a....")
    assert images.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert images.sniff(b"\x00\x00\x00\x1cftypavif") and not images.sniff(b"BMxx")


def test_load_path_and_load_data_pick_the_reader(tmp_path: Path) -> None:
    jpeg = encode(halves((300, 200)), "JPEG")
    no_ext = tmp_path / "scan"
    no_ext.write_bytes(jpeg)
    ex, size = images.load_path(no_ext, max_chars=1000)
    assert ex.kind == "image" and size == len(jpeg)
    text = tmp_path / "notes.md"
    text.write_text("# hi", encoding="utf-8")
    ex, _ = images.load_path(text, max_chars=1000)
    assert ex.kind == "text" and ex.text == "# hi"
    assert images.load_data("p.jpg", jpeg, max_chars=1000).kind == "image"
    assert images.load_data("n.txt", b"hello", max_chars=1000).kind == "text"
    with pytest.raises(AttachmentError):
        images.load_path(tmp_path / "missing.png", max_chars=1000)


# --------------------------------------------------------------------------- #
# Storing and cleaning up
# --------------------------------------------------------------------------- #


def test_store_is_atomic_and_idempotent(tmp_path: Path) -> None:
    pic = picture()
    folder = tmp_path / "attachments"
    path = images.store(pic, folder)
    assert path == folder / pic.file and path.read_bytes() == pic.data
    mtime = path.stat().st_mtime_ns
    assert images.store(pic, folder) == path and path.stat().st_mtime_ns == mtime
    assert [p.name for p in folder.iterdir()] == [pic.file]  # no temporary file left


def test_cleanup_removes_only_unreferenced_pictures(tmp_path: Path) -> None:
    folder = tmp_path / "attachments"
    kept, dropped = picture(size=(300, 200)), picture(size=(200, 300))
    images.store(kept, folder)
    images.store(dropped, folder)
    (folder / f"{dropped.file}.123.tmp").write_bytes(b"partial")
    (folder / f"{kept.file}.ocr.tmp").write_bytes(b"left by a crash while reading text")
    (folder / "readme.txt").write_text("not ours", encoding="utf-8")
    messages = [
        {"role": "user", "content": "hi", "_attachments": [{"kind": "image", **kept.record()}]},
        {"role": "assistant", "content": "a picture"},
        {"role": "user", "content": "x", "_attachments": [{"kind": "text", "file": dropped.file}]},
    ]
    assert images.referenced(messages) == {kept.file}
    assert images.cleanup(folder, images.referenced(messages)) == 3
    assert sorted(p.name for p in folder.iterdir()) == sorted([kept.file, "readme.txt"])
    assert images.cleanup(tmp_path / "nowhere", set()) == 0
    assert images.cleanup(None, set()) == 0


def test_a_record_cannot_name_a_file_outside_the_folder() -> None:
    assert images.stored_file({"file": "..\\..\\secrets.txt"}) is None
    assert images.stored_file({"file": "a" * 64 + ".jpg"}) == "a" * 64 + ".jpg"


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #


def test_inline_reads_the_stored_file_or_leaves_a_note(tmp_path: Path) -> None:
    pic = picture()
    images.store(pic, tmp_path)
    record = {"name": "photo.jpg", "kind": "image", **pic.record()}
    gone = {**record, "name": "gone.jpg", "file": "b" * 64 + ".jpg"}
    plain = {"role": "user", "content": "no pictures"}
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Look"},
            images.ref_part(record),
            images.ref_part(gone),
        ],
    }
    out = images.inline([plain, msg], tmp_path)
    assert out[0] is plain  # untouched messages are not copied
    text, shown, missing = out[1]["content"]
    assert text == {"type": "text", "text": "Look"}
    url = "data:image/jpeg;base64," + base64.b64encode(pic.data).decode()
    assert shown == {"type": "image_url", "image_url": {"url": url}}  # no private keys left
    assert missing == {
        "type": "text",
        "text": '[Image "gone.jpg" (800×600) is no longer available.]',
    }


def _one_ref(tmp_path: Path, pic: images.Picture | None = None) -> tuple[dict, dict]:
    pic = pic or picture()
    images.store(pic, tmp_path)
    record = {"name": "photo.jpg", "kind": "image", **pic.record()}
    return record, {"role": "user", "content": [images.ref_part(record)]}


def test_inline_reads_an_unchanged_picture_from_disk_once(tmp_path: Path, monkeypatch) -> None:
    images._url_cache.clear()  # noqa: SLF001
    record, msg = _one_ref(tmp_path)
    reads: list[str] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    first = images.inline([msg], tmp_path)
    second = images.inline([msg], tmp_path)  # the next tool round
    assert first == second and len(reads) == 1


def test_inline_cache_never_resurrects_a_deleted_or_rewritten_file(tmp_path: Path) -> None:
    images._url_cache.clear()  # noqa: SLF001
    pic = picture()
    record, msg = _one_ref(tmp_path, pic)
    shown = images.inline([msg], tmp_path)[0]["content"][0]
    assert shown["type"] == "image_url"
    stored = tmp_path / record["file"]
    stored.write_bytes(b"changed on disk")  # same name, new size: read again
    again = images.inline([msg], tmp_path)[0]["content"][0]
    assert again["image_url"]["url"].endswith(base64.b64encode(b"changed on disk").decode())
    stored.unlink()
    gone = images.inline([msg], tmp_path)[0]["content"][0]
    assert gone["type"] == "text" and "no longer available" in gone["text"]


def test_inline_cache_is_capped(tmp_path: Path) -> None:
    images._url_cache.clear()  # noqa: SLF001
    for i in range(images._URL_CACHE_MAX + 5):  # noqa: SLF001
        f = tmp_path / f"{i}.bin"
        f.write_bytes(bytes([i]) * 4)
        images._file_data_url(f, "image/png")  # noqa: SLF001
    assert len(images._url_cache) == images._URL_CACHE_MAX  # noqa: SLF001


def test_image_tokens() -> None:
    assert images.image_tokens(1568, 1176) == 56 * 42
    assert images.image_tokens(10, 10) == 85
    assert images.image_tokens(None, 5) == images.DEFAULT_IMAGE_TOKENS
    assert images.part_tokens({"type": "text", "text": "x"}) == 0
    assert images.part_tokens({"type": "image_url", "image_url": {"url": "data:..."}}) == (
        images.DEFAULT_IMAGE_TOKENS
    )
