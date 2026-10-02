"""Bridge methods with pictures: attaching (dialog, drop, paste), which models can see
them, and what reaches the engine. The pictures are drawn by the tests."""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path

import pytest
import webview
from PIL import Image

from chatforge import attachments as att
from chatforge.config import load_config
from chatforge.desktop.bridge import Api, Services
from chatforge.desktop.core_loop import CoreLoop
from chatforge.paths import Paths
from tests.fakes.openai_server import fake_openai_server, json_reply
from tests.unit.test_bridge_files import Engine, FakePopup, FakeWindow


@pytest.fixture
def services(tmp_path: Path, fake_keyring, monkeypatch) -> Services:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    paths = Paths.from_home(tmp_path / "home")
    paths.ensure_dirs()
    return Services(paths, load_config(paths))


@pytest.fixture
def api(services: Services) -> Api:
    return Api(services)


@pytest.fixture
def loop():
    core = CoreLoop("test-images").start()
    yield core
    core.stop()


def png(size=(400, 300), colour=(30, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def data_url(data: bytes, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64," + base64.b64encode(data).decode()


def test_a_pasted_screenshot_becomes_a_picture_chip(api: Api, services: Services) -> None:
    raw = png()
    reply = api.attach_data("Pasted image 14.05.33.png", data_url(raw))
    assert reply["ok"] is True and reply["errors"] == []
    (view,) = reply["attachments"]
    assert view == {
        "id": view["id"],
        "name": "Pasted image 14.05.33.png",
        "kind": "image",
        "chars": 0,
        "size": len(raw),
        "truncated": False,
        "warning": None,
        "width": 400,
        "height": 300,
        "thumb": view["thumb"],
    }
    assert view["thumb"].startswith("data:image/jpeg;base64,")
    (pending,) = services.attachments.peek([view["id"]])
    assert pending.image is not None and pending.image.width == 400
    # Nothing is written to disk until the message is sent.
    assert list(services.paths.attachments_dir.iterdir()) == []


def test_a_picture_without_an_extension_is_recognised(api: Api) -> None:
    (view,) = api.attach_data("clipboard", data_url(png(), "application/octet-stream"))[
        "attachments"
    ]
    assert view["kind"] == "image"


def test_the_dialog_offers_and_reads_pictures(api: Api, services: Services, tmp_path) -> None:
    photo = tmp_path / "holiday.jpg"
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (200, 150, 90)).save(buf, "JPEG")
    photo.write_bytes(buf.getvalue())
    heic = tmp_path / "IMG_0003.HEIC"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)
    window = FakeWindow([str(photo), str(heic)])
    services.popup = FakePopup(window)
    reply = api.attach_files()
    (_kind, _multiple, filters) = window.calls[0]
    assert filters == att.DIALOG_FILE_TYPES and window.calls[0][0] == webview.FileDialog.OPEN
    assert "*.png" in filters[0] and "*.jpg" in filters[0]  # the filter the dialog opens with
    assert any(f.startswith("Pictures (") and "*.webp" in f for f in filters)
    (view,) = reply["attachments"]
    assert (view["name"], view["kind"], view["width"], view["height"]) == (
        "holiday.jpg",
        "image",
        1568,
        1045,
    )
    if not reply["errors"]:  # pillow-heif is installed here: HEIC reads
        return
    assert reply["errors"] == [
        {
            "name": "IMG_0003.HEIC",
            "message": "HEIC photos cannot be read here. Export it as JPEG and attach that.",
        }
    ]


def test_provider_views_say_which_models_see_pictures(api: Api, services: Services) -> None:
    views = {p["id"]: p for p in api.list_providers()["providers"]}
    assert views["openai"]["vision"] == {"gpt-4o-mini": True, "gpt-4o": True}
    assert views["deepseek"]["vision"] == {"deepseek-chat": False, "deepseek-reasoner": False}
    assert views["minimax"]["vision"]["MiniMax-M3"] is True
    assert views["minimax"]["vision"]["MiniMax-M2.7"] is False
    assert set(views["local-npu"]["vision"].values()) <= {False}
    reply = api.select_model("openai", "gpt-4o")
    assert reply["ok"] is True and reply["vision"] is True
    assert api.select_model("deepseek", "deepseek-chat")["vision"] is False


def test_send_message_hands_the_picture_to_the_engine(api: Api, services: Services, loop) -> None:
    services.loop = loop
    engine = services.engine = Engine()
    aid = api.attach_data("photo.png", data_url(png()))["attachments"][0]["id"]
    assert api.send_message("What is this?", [aid])["ok"] is True
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    (_text, _rid, files) = engine.calls[0]
    assert files[0].kind == "image" and files[0].image.media_type == "image/jpeg"
    assert att.as_record(files[0])["file"] == files[0].image.file


def test_refresh_models_keeps_what_the_server_says_about_vision(
    api: Api, services: Services
) -> None:
    listing = {
        "data": [
            {"id": "qwen-vl", "studioforge": {"kind": "chat", "vision": True}},
            {"id": "qwen-text", "studioforge": {"kind": "chat", "vision": False}},
        ]
    }
    with fake_openai_server(models_reply=json_reply(listing)) as srv:
        api._apply_patch({"providers": {"studioforge": {"base_url": srv.base_url}}})
        results = asyncio.run(api._refresh_models_async("studioforge"))
    assert results["studioforge"]["vision"] == {"qwen-vl": True, "qwen-text": False}
    spec = services.config.providers["studioforge"]
    assert spec.model_vision == {"qwen-vl": True, "qwen-text": False}
    view = next(p for p in api.list_providers()["providers"] if p["id"] == "studioforge")
    assert view["vision"]["qwen-vl"] is True and view["vision"]["qwen-text"] is False
