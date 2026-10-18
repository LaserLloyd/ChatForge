from __future__ import annotations

import json

from conftest import FakeSD

from chatforge_voice import audio_io, cli
from chatforge_voice.panel import PANEL_FILE


def test_status_without_models_says_so(capsys, tmp_path):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "NOT installed" in out and "chatforge-voice" in out


def test_the_cli_uses_the_chatforge_data_folder(tmp_path):
    from chatforge_voice.runtime import cli_data_dir

    assert cli_data_dir() == tmp_path / "voice-data"  # CHATFORGE_VOICE_DATA_DIR (the test's)


def test_the_data_folder_defaults_to_chatforge_home(monkeypatch):
    from chatforge_voice.runtime import cli_data_dir

    monkeypatch.delenv("CHATFORGE_VOICE_DATA_DIR")
    assert cli_data_dir().parts[-2:] == ("extensions", "voice")


def test_devices_lists_inputs_and_outputs(monkeypatch, capsys):
    monkeypatch.setattr(audio_io, "load_backend", lambda: FakeSD())
    assert cli.main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "Fake Mic" in out and "Fake Speakers" in out


def test_models_download_asks_and_can_be_declined(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["models", "download"]) == 1
    out = capsys.readouterr().out
    assert "1.4 GB" in out and "CC-BY-4.0" in out


def test_panel_prints_or_opens_the_running_panels_address(monkeypatch, capsys, tmp_path):
    assert cli.main(["panel", "--print-url"]) == 1  # ChatForge not running: no panel.json
    assert "not running" in capsys.readouterr().err
    data = tmp_path / "voice-data"
    data.mkdir()
    (data / PANEL_FILE).write_text(json.dumps({"port": 4242, "token": "tok"}), encoding="utf-8")
    assert cli.main(["panel", "--print-url"]) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:4242/#t=tok"
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    assert cli.main(["panel"]) == 0 and opened == ["http://127.0.0.1:4242/#t=tok"]
