"""autostart.py: the Startup-folder VBS shim."""

import codecs
from pathlib import Path

import pytest

from aichat import autostart


@pytest.fixture
def appdata(tmp_path, monkeypatch, aichat_home):
    roaming = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setattr(autostart, "_is_windows", lambda: True)
    return roaming


def _shim_path(appdata: Path) -> Path:
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AIChat.vbs"


def _decode(path: Path) -> str:
    raw = path.read_bytes()
    assert raw.startswith(codecs.BOM_UTF16_LE)
    return raw[2:].decode("utf-16-le")


def test_startup_dir_uses_appdata(appdata):
    assert autostart.startup_dir() == _shim_path(appdata).parent


def test_enable_writes_utf16_le_bom_vbs(appdata, aichat_home):
    status = autostart.enable(["C:\\Py\\pythonw.exe", "-m", "aichat", "--hidden"])
    path = _shim_path(appdata)
    assert status.enabled and status.path == path
    raw = path.read_bytes()
    assert raw[:2] == b"\xff\xfe"
    text = raw[2:].decode("utf-16-le")
    assert "--hidden" in text
    assert "-m aichat" in text
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert 'CreateObject("WScript.Shell")' in text
    assert f'shell.CurrentDirectory = "{aichat_home.resolve()}"' in text
    assert ", 0, False" in text  # hidden window, do not wait
    assert "\ufeff" not in text  # exactly one BOM


def test_default_argv_is_pythonw_module_hidden(appdata):
    argv = autostart.launch_argv()
    assert argv[1:] == ["-m", "aichat", "--hidden"]
    autostart.enable()
    text = _decode(_shim_path(appdata))
    assert "-m aichat --hidden" in text


def test_paths_with_spaces_are_double_quoted(appdata):
    autostart.enable(["C:\\Program Files\\Py\\pythonw.exe", "-m", "aichat", "--hidden"])
    text = _decode(_shim_path(appdata))
    assert '""C:\\Program Files\\Py\\pythonw.exe"" -m aichat --hidden' in text


def test_non_ascii_home_survives(appdata, tmp_path):
    home = tmp_path / "Jos\u00e9 \u4e2d"
    autostart.enable(["pythonw.exe", "-m", "aichat", "--hidden"], home=home)
    assert f'shell.CurrentDirectory = "{home}"' in _decode(_shim_path(appdata))


def test_enable_is_idempotent_and_overwrites(appdata):
    autostart.enable(["a.exe", "-m", "aichat", "--hidden"])
    autostart.enable(["b.exe", "-m", "aichat", "--hidden"])
    text = _decode(_shim_path(appdata))
    assert "b.exe" in text and "a.exe" not in text
    assert len(list(_shim_path(appdata).parent.iterdir())) == 1


def test_status_and_disable(appdata):
    s = autostart.status()
    assert s.enabled is False and s.path is None
    assert "not enabled" in s.describe()
    autostart.enable(["pythonw.exe", "-m", "aichat", "--hidden"])
    s = autostart.status()
    assert s.enabled is True and s.path == _shim_path(appdata)
    assert s.detail == ""
    assert "enabled via Windows Startup folder" in s.describe()
    assert autostart.is_enabled() is True
    d = autostart.disable()
    assert d.enabled is False and d.detail == "removed"
    assert not _shim_path(appdata).exists()
    assert autostart.status().enabled is False
    assert autostart.disable().detail == "was not enabled"


def test_status_flags_shim_without_hidden(appdata):
    path = _shim_path(appdata)
    path.parent.mkdir(parents=True)
    path.write_bytes(codecs.BOM_UTF16_LE + 'shell.Run "x -m aichat", 0, False'.encode("utf-16-le"))
    s = autostart.status()
    assert s.enabled and "--hidden" in s.detail


def test_status_reads_legacy_utf8_bom_shim(appdata):
    path = _shim_path(appdata)
    path.parent.mkdir(parents=True)
    path.write_bytes(codecs.BOM_UTF8 + b'shell.Run "x -m aichat --hidden", 0, False')
    assert autostart.status().detail == ""


def test_unsupported_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    with pytest.raises(autostart.AutostartError):
        autostart.enable(["x"])
    assert autostart.status().enabled is False
    assert autostart.disable().enabled is False
    assert not any(tmp_path.iterdir())
