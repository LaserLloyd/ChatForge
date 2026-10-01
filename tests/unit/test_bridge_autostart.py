"""Api.get_autostart / set_autostart: mode, home and config, with autostart.py faked."""

from __future__ import annotations

from pathlib import Path

import pytest

from chatforge import autostart
from chatforge.config import load_config
from chatforge.desktop.bridge import Api, Services
from chatforge.paths import Paths


@pytest.fixture
def api(tmp_path: Path, fake_keyring) -> Api:
    paths = Paths.from_home(tmp_path / "home")
    paths.ensure_dirs()
    return Api(Services(paths, load_config(paths)))


def test_get_autostart_reports_task_scheduler_and_duplicate(api, monkeypatch, tmp_path):
    script = tmp_path / "home" / "ChatForge.vbs"
    shim = tmp_path / "Startup" / "ChatForge.vbs"
    status = autostart.AutostartStatus(True, "Task Scheduler", script, "twice", duplicate=shim)
    monkeypatch.setattr(autostart, "status", lambda: status)
    reply = api.get_autostart()
    assert reply["ok"] and reply["enabled"] is True
    assert reply["mode"] == "task-scheduler" and reply["mechanism"] == "Task Scheduler"
    assert reply["path"] == str(script) and reply["duplicate"] == str(shim)


def test_get_autostart_reports_startup_folder(api, monkeypatch, tmp_path):
    shim = tmp_path / "Startup" / "ChatForge.vbs"
    status = autostart.AutostartStatus(True, "Windows Startup folder", shim)
    monkeypatch.setattr(autostart, "status", lambda: status)
    reply = api.get_autostart()
    assert reply["mode"] == "startup-folder" and reply["duplicate"] is None


def test_set_autostart_passes_the_app_home_both_ways(api, monkeypatch):
    calls: list[tuple[str, Path | None]] = []
    home = api._s.paths.home

    def enable(argv=None, *, home=None):
        calls.append(("enable", home))
        return autostart.AutostartStatus(True, "Task Scheduler", home / "ChatForge.vbs")

    def disable(*, home=None):
        calls.append(("disable", home))
        return autostart.AutostartStatus(False, "Task Scheduler", None, "removed")

    monkeypatch.setattr(autostart, "enable", enable)
    monkeypatch.setattr(autostart, "disable", disable)
    on = api.set_autostart(True)
    assert on["ok"] and on["enabled"] and on["mode"] == "task-scheduler"
    assert api._s.config.startup.autostart is True
    off = api.set_autostart(False)
    assert off["ok"] and off["enabled"] is False and off["detail"] == "removed"
    assert api._s.config.startup.autostart is False
    assert calls == [("enable", home), ("disable", home)]


def test_set_autostart_error_is_a_clean_reply(api, monkeypatch):
    def enable(argv=None, *, home=None):
        raise autostart.AutostartError("Could not write X: disk full")

    monkeypatch.setattr(autostart, "enable", enable)
    reply = api.set_autostart(True)
    assert reply["ok"] is False and "disk full" in reply["error"]["message"]
