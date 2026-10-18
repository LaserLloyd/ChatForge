"""Registration with ChatForge's extension API (a fake of API_VERSION 1), the hooks, the lifecycle."""

from __future__ import annotations

import sys
import time
import types
from dataclasses import dataclass
from importlib import metadata
from types import SimpleNamespace

import pytest
from conftest import FakeChat, FakeExtensionAPI, make_deps, wait_for
from forge_voice_core import hostlink

import chatforge_voice
from chatforge_voice import runtime
from chatforge_voice.audio_io import Player, Recorder
from chatforge_voice.host import ChatForgeHost


@dataclass(frozen=True)
class _SettingField:
    """The shape of chatforge.extensions.SettingField (used only when ChatForge itself is not installed)."""

    key: str
    label: str
    type: str = "text"
    default: object = None
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""


@pytest.fixture
def ext_module(monkeypatch):
    """chatforge.extensions: the real module when ChatForge is importable, else a stand-in."""
    try:
        import chatforge.extensions as real

        return real
    except ImportError:
        pkg = types.ModuleType("chatforge")
        mod = types.ModuleType("chatforge.extensions")
        mod.SettingField = _SettingField
        pkg.extensions = mod
        monkeypatch.setitem(sys.modules, "chatforge", pkg)
        monkeypatch.setitem(sys.modules, "chatforge.extensions", mod)
        return mod


@pytest.fixture
def addon(tmp_path, ext_module):
    api = FakeExtensionAPI(tmp_path / "ext" / "voice", chat=FakeChat())
    down: set[int] = set()
    a = runtime.VoiceAddon(api, deps=make_deps(), is_down=down.__contains__, settings_ttl=0)
    a.register()
    a.down = down
    yield a, api
    a.stop()


def test_the_entry_point_is_declared_for_chatforge():
    eps = [e for e in metadata.entry_points(group="chatforge.extensions") if e.name == "voice"]
    assert eps and eps[0].value == "chatforge_voice:setup"
    assert eps[0].load() is chatforge_voice.setup
    assert chatforge_voice.setup.API_VERSION == 1


def test_register_adds_one_settings_card_with_the_voice_toggle(addon):
    _, api = addon
    (section,) = api.sections
    assert section["title"] == "Voice (CPU)" and "chatforge-voice panel" in section["description"]
    keys = [f.key for f in section["fields"]]
    assert keys[0] == "enabled" and "hotkey" in keys and "speak_replies" in keys
    enabled = section["fields"][0]
    assert (enabled.type, enabled.default, enabled.label) == ("bool", False, "Voice (CPU)")
    speak = next(f for f in section["fields"] if f.key == "speak_replies")
    assert speak.type == "choice" and "voice_only" in speak.choices
    speed = next(f for f in section["fields"] if f.key == "speed")
    assert (speed.minimum, speed.maximum) == (0.8, 1.3)


def test_register_wires_hooks_audio_backends_and_the_host_link(addon, tmp_path):
    a, api = addon
    assert api.turn_cbs == [a.controller.on_turn] and api.event_cbs == [a.controller.on_event]
    assert api.startup_cbs and api.shutdown_cbs
    assert isinstance(api.audio.capture["sounddevice"](), Recorder)
    assert isinstance(api.audio.playback["sounddevice"](), Player)
    link = hostlink.get()
    assert isinstance(link, ChatForgeHost) and link.data_dir() == tmp_path / "ext" / "voice"


def test_nothing_runs_until_startup(addon):
    a, _ = addon
    assert a.panel.port == 0 and a.watcher._thread is None and a._reaper is None


def test_startup_opens_the_panel_and_stop_closes_everything(addon, tmp_path):
    a, api = addon
    api.startup_cbs[0]()
    assert a.panel.port > 0 and (tmp_path / "ext" / "voice" / "panel.json").exists()
    assert a.watcher._thread is not None
    api.shutdown_cbs[0]()
    assert not (tmp_path / "ext" / "voice" / "panel.json").exists()
    assert type(hostlink.get()) is hostlink.HostLink  # the engine package is back to its default


def test_the_hold_key_drives_the_controller_and_follows_the_settings(addon):
    a, api = addon
    a.controller.deps.missing = lambda s: []
    api.values.update({"enabled": True, "hotkey": "F9"})
    api.startup_cbs[0]()
    a.down.add(0x78)
    assert wait_for(lambda: a.controller.state == "recording")
    a.down.clear()
    assert wait_for(lambda: a.controller.state in ("transcribing", "thinking"))
    assert wait_for(lambda: a.controller.chat.sent == ["hello there"])


def test_the_key_does_nothing_while_voice_is_off_and_works_without_a_restart_when_turned_on(addon):
    a, api = addon
    api.startup_cbs[0]()
    api.values.update({"hotkey": "F9"})
    a.down.add(0x78)
    time.sleep(0.15)
    assert a.controller.state == "idle"  # enabled is false
    a.controller.deps.missing = lambda s: []
    api.values["enabled"] = True
    assert wait_for(lambda: a.controller.state == "recording")


def test_an_invalid_hotkey_is_logged_and_disables_the_key_not_the_app(addon):
    a, api = addon
    api.values.update({"enabled": True, "hotkey": "ctrl+banana"})
    api.startup_cbs[0]()
    assert a.watcher.error and a.watcher._keys == ()


def test_setup_raises_a_clear_sentence_when_the_engine_package_is_missing(monkeypatch):
    monkeypatch.setattr(chatforge_voice, "core_problem", lambda: chatforge_voice.CORE_MISSING)
    with pytest.raises(RuntimeError) as e:
        chatforge_voice.setup(SimpleNamespace())
    assert "forge-voice-core" in str(e.value) and "uv pip install" in str(e.value)


def test_setup_registers_through_the_entry_point_function(tmp_path, ext_module, monkeypatch):
    api = FakeExtensionAPI(tmp_path / "x")
    chatforge_voice.setup(api)
    assert len(api.sections) == 1 and api.startup_cbs
    api.shutdown_cbs[0]()


def test_an_old_chatforge_without_add_on_support_gets_a_sentence(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "chatforge", None
    )  # import chatforge.extensions -> ImportError
    monkeypatch.delitem(sys.modules, "chatforge.extensions", raising=False)
    with pytest.raises(RuntimeError) as e:
        runtime.VoiceAddon(FakeExtensionAPI(tmp_path / "y")).register()
    assert "no add-on support" in str(e.value)
