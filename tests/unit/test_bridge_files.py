"""Bridge methods for attached files and saved documents (dialog and shell mocked)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
from pathlib import Path

import pytest
import webview

from chatforge import attachments as att
from chatforge.config import load_config
from chatforge.desktop.bridge import Api, Services
from chatforge.desktop.core_loop import CoreLoop
from chatforge.paths import Paths
from chatforge.tools import documents


@pytest.fixture
def services(tmp_path: Path, fake_keyring, monkeypatch) -> Services:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    paths = Paths.from_home(tmp_path / "home")
    paths.ensure_dirs()
    s = Services(paths, load_config(paths))
    s.config = s.config.model_copy(
        update={
            "tools": s.config.tools.model_copy(update={"documents_dir": str(tmp_path / "docs")})
        }
    )
    return s


@pytest.fixture
def api(services: Services) -> Api:
    return Api(services)


@pytest.fixture
def loop():
    core = CoreLoop("test-files").start()
    yield core
    core.stop()


class FakeWindow:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def create_file_dialog(self, dialog_type, allow_multiple=False, file_types=()):
        self.calls.append((dialog_type, allow_multiple, tuple(file_types)))
        return self.result


class FakePopup:
    def __init__(self, window) -> None:
        self.window = window
        self.suspended = 0
        self.max_suspended = 0

    @contextlib.contextmanager
    def suspend_blur(self):
        self.suspended += 1
        self.max_suspended = max(self.max_suspended, self.suspended)
        try:
            yield
        finally:
            self.suspended -= 1


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --------------------------------------------------------------------------- #
# attach_data / remove_attachment
# --------------------------------------------------------------------------- #


def test_attach_data_reply_shape(api: Api, services: Services) -> None:
    reply = api.attach_data("C:\\Users\\me\\notes.md", b64(b"# Notes\nbuy milk"))
    assert reply["ok"] is True and reply["errors"] == []
    (item,) = reply["attachments"]
    assert item == {
        "id": item["id"],
        "name": "notes.md",
        "kind": "text",
        "chars": 16,
        "size": 16,
        "truncated": False,
        "warning": None,
    }
    assert item["id"] in services.attachments
    reply = api.attach_data("pasted.txt", "data:text/plain;base64," + b64(b"hi"))
    assert reply["attachments"][0]["chars"] == 2


def test_attach_data_problems_are_error_entries(api: Api, services: Services, monkeypatch) -> None:
    reply = api.attach_data("photo.png", b64(b"\x89PNG\r\n\x1a\n"))  # only a signature
    assert reply["ok"] is True and reply["attachments"] == []
    assert reply["errors"] == [
        {
            "name": "photo.png",
            "message": "The picture could not be read; it may be damaged or not really a PNG file.",
        }
    ]
    reply = api.attach_data("layers.psd", b64(b"8BPS\x00\x01"))
    assert reply["errors"] == [{"name": "layers.psd", "message": att.IMAGES_UNSUPPORTED}]
    reply = api.attach_data("x.txt", "@@@ not base64 @@@")
    assert reply["errors"][0]["message"] == "The file data could not be read."
    monkeypatch.setattr(att, "MAX_FILE_BYTES", 4)
    reply = api.attach_data("big.txt", b64(b"12345"))
    assert reply["errors"] == [{"name": "big.txt", "message": "The file is larger than 20 MB."}]
    assert len(services.attachments) == 0


def test_attach_data_uses_the_configured_cap(api: Api, services: Services) -> None:
    services.config = services.config.model_copy(
        update={"tools": services.config.tools.model_copy(update={"attachment_max_chars": 1000})}
    )
    item = api.attach_data("long.txt", b64(b"a" * 5000))["attachments"][0]
    assert item["chars"] == 1000 and item["truncated"] is True and item["warning"]


def test_remove_attachment(api: Api, services: Services) -> None:
    aid = api.attach_data("a.txt", b64(b"text"))["attachments"][0]["id"]
    assert api.remove_attachment(aid) == {"ok": True, "removed": True}
    assert api.remove_attachment(aid) == {"ok": True, "removed": False}
    assert aid not in services.attachments


# --------------------------------------------------------------------------- #
# attach_files (native dialog mocked)
# --------------------------------------------------------------------------- #


def test_attach_files_opens_a_multi_select_dialog(api: Api, services: Services, tmp_path) -> None:
    good = tmp_path / "a.py"
    good.write_bytes(b"print('hi')\n")
    bad = tmp_path / "b.exe"
    bad.write_bytes(b"MZ\x90\x00")
    window = FakeWindow((str(good), str(bad)))
    popup = FakePopup(window)
    services.popup = popup
    reply = api.attach_files()
    assert window.calls == [(webview.FileDialog.OPEN, True, att.DIALOG_FILE_TYPES)]
    assert popup.max_suspended == 1 and popup.suspended == 0  # blur-hide off while it is up
    assert [a["name"] for a in reply["attachments"]] == ["a.py"]
    assert reply["attachments"][0]["kind"] == "code" and reply["attachments"][0]["size"] == 12
    assert (
        reply["errors"][0]["name"] == "b.exe" and "not supported" in reply["errors"][0]["message"]
    )


def test_attach_files_cancelled_and_without_a_window(api: Api, services: Services) -> None:
    assert api.attach_files()["error"]["code"] == "server"  # no window at all
    services.popup = FakePopup(FakeWindow(None))
    assert api.attach_files() == {"ok": True, "attachments": [], "errors": [], "cancelled": True}


def test_attach_files_caps_the_count(api: Api, services: Services, tmp_path) -> None:
    paths = []
    for i in range(12):
        p = tmp_path / f"f{i}.txt"
        p.write_text(f"file {i}", encoding="utf-8")
        paths.append(str(p))
    services.popup = FakePopup(FakeWindow(paths))
    reply = api.attach_files()
    assert len(reply["attachments"]) == 10 and len(services.attachments) == 10
    assert [e["name"] for e in reply["errors"]] == ["f10.txt", "f11.txt"]
    assert "Only 10 files" in reply["errors"][0]["message"]


def test_attach_files_uses_the_window_that_called(api: Api, services: Services, tmp_path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("x", encoding="utf-8")
    settings_window = FakeWindow([str(f)])
    popup_window = FakeWindow(None)
    services.popup = FakePopup(popup_window)
    # Stand-in for webview/util.py js_bridge_call: the call runs in a ``_call`` closure
    # that holds the calling window.
    ns: dict = {"__name__": "webview.util"}
    exec(
        "def make(window, fn):\n"
        "    def _call():\n"
        "        assert window is not None\n"
        "        return fn()\n"
        "    return _call\n",
        ns,
    )
    reply = ns["make"](settings_window, api.attach_files)()
    assert len(settings_window.calls) == 1 and popup_window.calls == []
    assert reply["attachments"][0]["name"] == "x.txt"


# --------------------------------------------------------------------------- #
# send_message with attachments
# --------------------------------------------------------------------------- #


class Engine:
    """Records each send and ends it with ``outcome`` (a chat.done or chat.error event)."""

    def __init__(self, outcome: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self.outcome = outcome or {"type": "chat.done"}
        self.done = asyncio.Event()

    async def send(self, text, request_id, emit, attachments=None):
        self.calls.append((text, request_id, attachments))
        emit({**self.outcome, "request_id": request_id})
        self.done.set()


def test_send_message_hands_the_files_to_the_engine(api: Api, services: Services, loop) -> None:
    services.loop = loop
    engine = services.engine = Engine()
    a = api.attach_data("a.txt", b64(b"alpha"))["attachments"][0]["id"]
    b = api.attach_data("b.txt", b64(b"beta"))["attachments"][0]["id"]
    reply = api.send_message("Compare", [b, a, b])
    assert reply["ok"] is True
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    text, _rid, files = engine.calls[0]
    assert text == "Compare" and [f.name for f in files] == ["b.txt", "a.txt"]
    assert files[0].text == "beta"
    assert len(services.attachments) == 0  # answered: the files are in the conversation now
    # A second send with the same ids fails cleanly: the files are gone.
    again = api.send_message("Again", [a])
    assert again["ok"] is False and again["error"]["code"] == "not_found"


def test_files_stay_attached_after_an_error_for_retry(api: Api, services: Services, loop) -> None:
    services.loop = loop
    engine = services.engine = Engine({"type": "chat.error", "code": "no_key"})
    aid = api.attach_data("a.txt", b64(b"alpha"))["attachments"][0]["id"]
    assert api.send_message("Read", [aid])["ok"] is True
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    assert aid in services.attachments  # Retry can send the same ids again
    engine.outcome = {"type": "chat.error", "code": "cancelled"}
    engine.done.clear()
    assert api.send_message("Read", [aid])["ok"] is True
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    assert aid not in services.attachments  # a stopped message keeps them in the history


def test_send_message_files_only_and_limits(api: Api, services: Services, loop) -> None:
    services.loop = loop
    engine = services.engine = Engine()
    aid = api.attach_data("a.txt", b64(b"alpha"))["attachments"][0]["id"]
    assert api.send_message("", None)["error"]["code"] == "bad_request"
    assert api.send_message("hi", "not-a-list-but-one-id")["error"]["code"] == "not_found"
    assert api.send_message("hi", 5)["error"]["code"] == "bad_request"
    many = [f"att_{i}" for i in range(11)]
    reply = api.send_message("hi", many)
    assert reply["error"]["code"] == "bad_request" and "10" in reply["error"]["message"]
    assert api.send_message("", [aid])["ok"] is True  # files alone are a message
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    assert engine.calls[0][0] == "" and engine.calls[0][2][0].name == "a.txt"


def test_send_message_without_files_keeps_the_old_engine_call(
    api: Api, services: Services, loop
) -> None:
    services.loop = loop
    seen: list[tuple] = []
    done = asyncio.Event()

    class OldEngine:  # no ``attachments`` parameter
        async def send(self, text, request_id, emit):
            seen.append((text, request_id))
            done.set()

    services.engine = OldEngine()
    assert api.send_message("hello")["ok"] is True
    assert api.send_message("again", [])["ok"] is True
    loop.run(asyncio.wait_for(done.wait(), 2))
    assert seen[0][0] == "hello"


def test_send_message_keeps_files_when_the_engine_is_missing(api: Api, services: Services) -> None:
    aid = api.attach_data("a.txt", b64(b"alpha"))["attachments"][0]["id"]
    assert api.send_message("hi", [aid])["error"]["code"] == "server"
    assert aid in services.attachments  # still there for a retry


# --------------------------------------------------------------------------- #
# open_document / reveal_document
# --------------------------------------------------------------------------- #


@pytest.fixture
def shell(monkeypatch) -> dict[str, list]:
    calls: dict[str, list] = {"startfile": [], "popen": []}
    monkeypatch.setattr(documents, "_WINDOWS", True)
    monkeypatch.setattr(os, "startfile", lambda *a: calls["startfile"].append(a), raising=False)
    monkeypatch.setattr(
        documents.subprocess, "Popen", lambda cmd, *a, **k: calls["popen"].append(cmd)
    )
    return calls


def test_open_and_reveal_a_saved_document(api: Api, tmp_path, shell) -> None:
    folder = tmp_path / "docs"
    result = documents.create_document("plan.md", "# Plan", folder=str(folder))
    path = result.document["path"]
    real = str(Path(path).resolve())
    assert api.open_document(path) == {"ok": True, "path": real}
    assert api.reveal_document(path) == {"ok": True, "path": real}
    assert shell["startfile"] == [(real,)]
    assert shell["popen"] == [f'explorer /select,"{real}"']


def test_open_document_refuses_files_outside_the_folder(api: Api, tmp_path, shell) -> None:
    (tmp_path / "docs").mkdir()
    outside = tmp_path / "home" / "config.toml"
    for bad in (
        str(outside),
        str(tmp_path / "docs" / ".." / "home" / "config.toml"),
        "C:\\Windows\\win.ini",
    ):
        reply = api.open_document(bad)
        assert reply["ok"] is False and reply["error"]["code"] in ("bad_request", "not_found"), bad
        assert api.reveal_document(bad)["ok"] is False
    reply = api.open_document(str(outside))
    assert reply["error"]["code"] == "bad_request"
    assert "documents folder" in reply["error"]["message"]
    gone = api.open_document(str(tmp_path / "docs" / "gone.md"))
    assert gone["error"]["code"] == "not_found"
    assert shell == {"startfile": [], "popen": []}
