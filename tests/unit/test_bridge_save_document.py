"""Bridge ``save_document`` (Download on a document card): the native Save As dialog of
the calling window, in Downloads, then a copy (dialog mocked)."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
import webview

from chatforge.config import load_config
from chatforge.desktop.bridge import Api, Services
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
def downloads(tmp_path: Path, monkeypatch) -> Path:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setattr(documents, "downloads_dir", lambda: folder)
    return folder


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    result = documents.create_document(
        "Plan.docx", "# Plan\n\n- one\n- two", folder=str(tmp_path / "docs")
    )
    return Path(result.document["path"])


class SaveWindow:
    """A window whose Save As dialog returns ``result`` (pywebview gives a tuple on Windows)."""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[int, dict]] = []

    def create_file_dialog(self, dialog_type, **kwargs):
        self.calls.append((dialog_type, kwargs))
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


def _leftovers(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.endswith(".part")]


def test_save_copies_to_the_chosen_file(api, services, doc, downloads) -> None:
    target = downloads / "Plan.docx"
    window = SaveWindow((str(target),))
    popup = services.popup = FakePopup(window)
    reply = api.save_document(str(doc))
    assert reply == {"ok": True, "path": str(target)}
    assert target.read_bytes() == doc.read_bytes()
    assert doc.exists()  # a copy, not a move
    ((kind, kwargs),) = window.calls
    assert kind == webview.FileDialog.SAVE
    assert kwargs == {
        "directory": str(downloads),
        "save_filename": "Plan.docx",
        "file_types": ("Word document (*.docx)", "All files (*.*)"),
    }
    assert popup.max_suspended == 1 and popup.suspended == 0  # no blur-hide under the dialog
    assert _leftovers(downloads) == []


def test_cancel_writes_nothing(api, services, doc, downloads) -> None:
    for nothing in (None, (), []):
        services.popup = FakePopup(SaveWindow(nothing))
        assert api.save_document(str(doc)) == {"ok": True, "cancelled": True}
    assert list(downloads.iterdir()) == []


def test_a_plain_string_from_the_dialog_also_works(api, services, doc, tmp_path) -> None:
    target = tmp_path / "elsewhere" / "Renamed.docx"
    target.parent.mkdir()
    services.popup = FakePopup(SaveWindow(str(target)))  # other platforms return a str
    assert api.save_document(str(doc)) == {"ok": True, "path": str(target)}
    assert target.read_bytes() == doc.read_bytes()


def test_replaces_a_file_the_dialog_confirmed(api, services, doc, downloads) -> None:
    target = downloads / "Plan.docx"
    target.write_bytes(b"old")
    services.popup = FakePopup(SaveWindow((str(target),)))
    assert api.save_document(str(doc))["ok"] is True
    assert target.read_bytes() == doc.read_bytes()
    assert _leftovers(downloads) == []


def test_saving_over_itself_changes_nothing(api, services, doc) -> None:
    before = doc.read_bytes()
    services.popup = FakePopup(SaveWindow((str(doc),)))
    assert api.save_document(str(doc)) == {"ok": True, "path": str(doc)}
    assert doc.read_bytes() == before
    assert _leftovers(doc.parent) == []


def test_refuses_files_outside_the_documents_folder(api, services, tmp_path, downloads) -> None:
    (tmp_path / "docs").mkdir(exist_ok=True)
    secret = tmp_path / "home" / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    window = SaveWindow((str(downloads / "secret.txt"),))
    services.popup = FakePopup(window)
    for bad in (str(secret), str(tmp_path / "docs" / ".." / "home" / "secret.txt"), ""):
        reply = api.save_document(bad)
        assert reply["ok"] is False and reply["error"]["code"] == "bad_request", bad
    gone = api.save_document(str(tmp_path / "docs" / "gone.docx"))
    assert gone["error"]["code"] == "not_found"
    assert window.calls == []  # the dialog never opened
    assert list(downloads.iterdir()) == []


def test_no_window_and_write_failures(api, services, doc, downloads, tmp_path, monkeypatch) -> None:
    assert api.save_document(str(doc))["error"]["code"] == "server"  # no window at all
    # A folder that is gone by the time the copy runs.
    services.popup = FakePopup(SaveWindow((str(tmp_path / "missing" / "Plan.docx"),)))
    reply = api.save_document(str(doc))
    assert reply["ok"] is False and reply["error"]["code"] == "server"
    assert "Plan.docx could not be saved there" in reply["error"]["message"]
    # The file is open in Excel or Word: Windows refuses to replace it.
    services.popup = FakePopup(SaveWindow((str(downloads / "Plan.docx"),)))

    def locked(src, dst):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(os, "replace", locked)
    reply = api.save_document(str(doc))
    assert reply["error"]["code"] == "in_use" and "another program" in reply["error"]["hint"]
    assert list(downloads.iterdir()) == []  # the temporary copy was removed


def test_uses_the_window_that_called(api, services, doc, downloads) -> None:
    settings_window = SaveWindow((str(downloads / "x.docx"),))
    popup_window = SaveWindow(None)
    popup = services.popup = FakePopup(popup_window)
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
    reply = ns["make"](settings_window, lambda: api.save_document(str(doc)))()
    assert reply["ok"] is True
    assert len(settings_window.calls) == 1 and popup_window.calls == []
    assert popup.max_suspended == 0  # not the popup: nothing to suspend


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.xlsx", ("Excel workbook (*.xlsx)", "All files (*.*)")),
        ("a.pptx", ("PowerPoint presentation (*.pptx)", "All files (*.*)")),
        ("a.csv", ("CSV file (*.csv)", "All files (*.*)")),
        ("a.py", ("PY file (*.py)", "All files (*.*)")),
        ("noext", ("All files (*.*)",)),
    ],
)
def test_save_file_types_are_valid_dialog_filters(name: str, expected) -> None:
    from webview.util import parse_file_type

    types = documents.save_file_types(name)
    assert types == expected
    for t in types:
        parse_file_type(t)  # pywebview raises for a filter it cannot use


def test_downloads_dir_falls_back_to_the_profile(tmp_path, monkeypatch) -> None:
    import platformdirs

    monkeypatch.setattr(platformdirs, "user_downloads_dir", lambda: str(tmp_path / "nope"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert documents.downloads_dir() == tmp_path  # no Downloads folder: the profile
    (tmp_path / "Downloads").mkdir()
    assert documents.downloads_dir() == tmp_path / "Downloads"
    real = tmp_path / "Real Downloads"
    real.mkdir()
    monkeypatch.setattr(platformdirs, "user_downloads_dir", lambda: str(real))
    assert documents.downloads_dir() == real
