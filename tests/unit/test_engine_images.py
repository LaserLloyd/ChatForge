"""Attached pictures through the chat engine: stored, sent to models that see, read (OCR)
for models that do not, and cleaned up. The pictures are drawn by the tests."""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

from chatforge import images
from chatforge.attachments import Extracted
from chatforge.chat.engine import ChatEngine
from tests.fakes.openai_server import fake_openai_server, sse, text_chunks
from tests.unit.test_engine import Harness


def photo(name: str = "photo.png", colour=(200, 40, 40), size=(640, 480)) -> Extracted:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return images.extract(name, buf.getvalue())


def harness(tmp_path: Path, base_url: str, *, vision: bool, ocr=None) -> Harness:
    h = Harness(tmp_path, base_url)
    h.cfg.providers["fake"].vision_models = ["*"] if vision else []
    h.providers.replace_all(h.cfg.providers)
    h.engine = ChatEngine(
        providers=h.providers,
        tools=h.tools,
        get_config=lambda: h.cfg,
        conversation_file=h.paths.conversation_file,
        clock=lambda: 1_700_000_000.0,
        ocr=ocr,
    )
    return h


async def send_with(h: Harness, text: str, files: list) -> list[dict]:
    """``Harness.send`` with attached files; the events of this message."""
    start = len(h.events)
    await h.engine.send(text, f"r{start + 1}", h.events.append, attachments=files or None)
    return h.events[start:]


def stored(h: Harness) -> list[str]:
    folder = h.paths.attachments_dir
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


async def test_a_vision_model_sees_the_picture(tmp_path) -> None:
    pic = photo()
    with fake_openai_server(sse(text_chunks("A red square.", 1))) as srv:
        h = harness(tmp_path, srv.base_url, vision=True)
        await h.engine.send("What is this?", "r1", h.events.append, attachments=[pic])
        body = srv.chat_requests[0].json
    assert h.events[-1]["type"] == "chat.done"
    assert stored(h) == [pic.image.file]
    content = body["messages"][-1]["content"]
    assert content[0] == {
        "type": "text",
        "text": 'What is this?\n\n[Image "photo.png" (640×480) attached below.]',
    }
    expected = "data:image/jpeg;base64," + base64.b64encode(pic.image.data).decode()
    assert content[1] == {"type": "image_url", "image_url": {"url": expected}}
    # The conversation keeps the file's name and a thumbnail, never the picture itself.
    record = h.engine.conversation.messages[0]["_attachments"][0]
    assert record["file"] == pic.image.file and "data" not in record
    assert expected not in h.paths.conversation_file.read_text(encoding="utf-8")
    item = h.engine.conversation_items()[0]["attachments"][0]
    assert item == {
        "name": "photo.png",
        "kind": "image",
        "chars": 0,
        "truncated": False,
        "width": 640,
        "height": 480,
        "thumb": pic.image.thumb,
    }


async def test_a_blind_model_gets_a_note_and_the_text_read_once(tmp_path) -> None:
    calls: list[Path] = []

    def fake_ocr(path: Path) -> str:
        calls.append(path)
        assert path.read_bytes()  # the stored file is there to read
        return "  INVOICE 42\nTotal: 12.50  "

    replies = (sse(text_chunks("It is invoice 42.", 1)), sse(text_chunks("12.50", 1)))
    with fake_openai_server(*replies) as srv:
        h = harness(tmp_path, srv.base_url, vision=False, ocr=fake_ocr)
        events = await send_with(h, "Read this", [photo("scan.png")])
        second = await send_with(h, "And the total?", [])
        bodies = [r.json for r in srv.chat_requests]
    assert [e["phase"] for e in h.of("chat.phase", events)][:1] == ["reading_image"]
    assert not [e for e in h.of("chat.phase", second) if e["phase"] == "reading_image"]
    assert len(calls) == 1  # read once, kept on the record
    first = bodies[0]["messages"][-1]["content"]
    assert first == (
        "Read this\n\n"
        '[Image "scan.png" (640×480) attached — this model cannot see images. Text recognised '
        'in it:]\n\n<file name="scan.png">\nINVOICE 42\nTotal: 12.50\n</file>'
    )
    # The follow-up still carries the earlier picture's note and text.
    earlier = bodies[1]["messages"][1]["content"]
    assert isinstance(earlier, str) and "INVOICE 42" in earlier
    assert h.engine.conversation.messages[0]["_attachments"][0]["ocr"] == (
        "INVOICE 42\nTotal: 12.50"
    )


async def test_text_is_read_from_the_sharper_copy_of_a_shrunk_picture(tmp_path) -> None:
    seen: list[tuple[Path, tuple[int, int]]] = []

    def fake_ocr(path: Path) -> str:
        with Image.open(path) as im:
            seen.append((path, im.size))
        return "small print"

    big = photo("screen.png", size=(2560, 1440))
    assert big.image.ocr_data is not None
    with fake_openai_server(sse(text_chunks("ok", 1))) as srv:
        h = harness(tmp_path, srv.base_url, vision=False, ocr=fake_ocr)
        await send_with(h, "read it", [big])
    ((path, size),) = seen
    assert size == (2560, 1440)  # not the 1568 px copy the model would get
    # Read next to the stored picture (a crash leaves it for images.cleanup), then gone.
    assert path == h.paths.attachments_dir / f"{big.image.file}.ocr.tmp"
    assert not path.exists()
    assert stored(h) == [big.image.file]  # and only the stored picture remains


async def test_a_hung_reader_is_given_up_on_for_the_rest_of_the_message(
    tmp_path, monkeypatch
) -> None:
    from chatforge.chat import engine as engine_module

    monkeypatch.setattr(engine_module, "OCR_WAIT_S", 0.3)
    calls: list[Path] = []

    def slow_ocr(path: Path) -> str:
        calls.append(path)
        import time

        time.sleep(1.0)
        return "too late"

    pics = [photo("a.png", (200, 0, 0)), photo("b.png", (0, 200, 0))]
    with fake_openai_server(sse(text_chunks("ok", 1))) as srv:
        h = harness(tmp_path, srv.base_url, vision=False, ocr=slow_ocr)
        await send_with(h, "read them", pics)
        content = srv.chat_requests[0].json["messages"][-1]["content"]
    assert len(calls) == 1  # the second picture is not tried after a timeout
    records = h.engine.conversation.messages[0]["_attachments"]
    assert [r["ocr"] for r in records] == ["", ""]
    assert "too late" not in content and content.count("cannot see images") == 2


async def test_an_ocr_failure_leaves_a_plain_note(tmp_path) -> None:
    def broken(path: Path) -> str:
        raise RuntimeError("no OCR here")

    with fake_openai_server(sse(text_chunks("ok", 1))) as srv:
        h = harness(tmp_path, srv.base_url, vision=False, ocr=broken)
        await send_with(h, "hi", [photo()])
        content = srv.chat_requests[0].json["messages"][-1]["content"]
    assert content == 'hi\n\n[Image "photo.png" (640×480) attached — this model cannot see images.]'
    assert h.engine.conversation.messages[0]["_attachments"][0]["ocr"] == ""


async def test_regenerate_sends_the_picture_again(tmp_path) -> None:
    pic = photo()
    replies = (sse(text_chunks("one", 1)), sse(text_chunks("two", 1)))
    with fake_openai_server(*replies) as srv:
        h = harness(tmp_path, srv.base_url, vision=True)
        await send_with(h, "Describe it", [pic])
        await h.engine.regenerate("r2", h.events.append)
        bodies = [r.json for r in srv.chat_requests]
    assert h.events[-1]["type"] == "chat.done"
    assert bodies[0]["messages"][-1] == bodies[1]["messages"][-1]
    assert bodies[1]["messages"][-1]["content"][1]["type"] == "image_url"


async def test_pictures_no_message_needs_are_removed(tmp_path) -> None:
    first, second = photo("a.png", (10, 200, 10)), photo("b.png", (10, 10, 200))
    replies = [sse(text_chunks("ok", 1)) for _ in range(4)]
    with fake_openai_server(*replies) as srv:
        h = harness(tmp_path, srv.base_url, vision=True)
        await send_with(h, "first", [first])
        await send_with(h, "second", [second])
        assert stored(h) == sorted([first.image.file, second.image.file])
        # A restart keeps what the saved conversation refers to, and drops strays.
        stray = h.paths.attachments_dir / ("f" * 64 + ".jpg")
        stray.write_bytes(b"x")
        h.engine = ChatEngine(
            providers=h.providers,
            tools=h.tools,
            get_config=lambda: h.cfg,
            conversation_file=h.paths.conversation_file,
        )
        assert stored(h) == sorted([first.image.file, second.image.file])
        # Trimmed out of the conversation: gone after the next turn.
        h.engine.conversation.trim(2)
        await send_with(h, "third", [])
        assert stored(h) == [second.image.file]
    # New chat: nothing refers to the last one any more.
    h.engine.new_chat()
    assert stored(h) == []
