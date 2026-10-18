"""The ``chatforge.extensions`` seams: loading, settings sections, turn and event hooks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chatforge.config import load_config
from chatforge.desktop.bridge import Api, Services
from chatforge.desktop.events import EventSink
from chatforge.extensions import (
    API_VERSION,
    DISABLE_ENV,
    ENTRY_POINT_GROUP,
    ExtensionError,
    ExtensionHost,
    SettingField,
    TurnInfo,
)
from chatforge.paths import Paths


class FakeEntryPoint:
    def __init__(self, name: str, target: Any, version: str = "1.2.3") -> None:
        self.name = name
        self._target = target
        self.dist = SimpleNamespace(version=version)

    def load(self) -> Any:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


@pytest.fixture
def paths(chatforge_home: Path) -> Paths:
    p = Paths.default()
    p.ensure_dirs()
    return p


@pytest.fixture
def host(paths: Paths) -> ExtensionHost:
    cfg = load_config(paths)
    return ExtensionHost(app_version="9.9.9", get_config=lambda: cfg, home=paths.home)


VOICE_FIELDS = (
    SettingField("enabled", "Voice on", "bool", default=False),
    SettingField("rate", "Speaking rate", "float", default=1.0, minimum=0.5, maximum=2.0),
    SettingField("engine", "Engine", "choice", default="stock", choices=("stock", "rig")),
    SettingField("device", "Microphone", "text", default=""),
    SettingField("threads", "Threads", "int", default=4, minimum=1, maximum=32),
)


def voice_setup(api) -> None:
    api.register_settings_section(VOICE_FIELDS, title="Voice", description="Talk to ChatForge")


def test_the_entry_point_group_name_is_the_documented_one() -> None:
    assert ENTRY_POINT_GROUP == "chatforge.extensions"
    assert API_VERSION == 1


def test_an_addon_is_loaded_and_its_section_registered(host: ExtensionHost) -> None:
    loaded = host.load([FakeEntryPoint("voice", voice_setup)])
    assert [(e.name, e.ok, e.version) for e in loaded] == [("voice", True, "1.2.3")]
    (section,) = host.sections
    assert (section.extension, section.id, section.title) == ("voice", "main", "Voice")
    assert section.defaults()["rate"] == 1.0
    view = host.view()
    assert view[0]["values"]["engine"] == "stock"
    assert view[0]["sections"][0]["fields"][1]["max"] == 2.0


def test_setup_may_be_an_attribute_of_the_entry_point_object(host: ExtensionHost) -> None:
    host.load([FakeEntryPoint("voice", SimpleNamespace(setup=voice_setup))])
    assert host.loaded[0].ok and host.sections


def test_a_broken_addon_is_skipped_and_the_others_load(host: ExtensionHost) -> None:
    def explodes(api) -> None:
        api.register_settings_section([SettingField("x", "X")], title="Half")
        raise RuntimeError("boom")

    host.load(
        [
            FakeEntryPoint("bad-import", ImportError("no module named foo")),
            FakeEntryPoint("exploder", explodes),
            FakeEntryPoint("voice", voice_setup),
            FakeEntryPoint("Bad Name!", voice_setup),
        ]
    )
    status = {e.name: (e.ok, e.error) for e in host.loaded}
    assert status["voice"][0] is True
    assert status["bad-import"][0] is False and "foo" in status["bad-import"][1]
    assert status["exploder"][0] is False and "boom" in status["exploder"][1]
    assert status["Bad Name!"][0] is False
    assert [s.extension for s in host.sections] == ["voice"]  # the half-done one was removed


def test_an_addon_for_another_api_version_is_refused(host: ExtensionHost) -> None:
    target = SimpleNamespace(setup=voice_setup, API_VERSION=API_VERSION + 1)
    host.load([FakeEntryPoint("future", target)])
    assert host.loaded[0].ok is False and "extension API" in host.loaded[0].error
    assert not host.sections


def test_safe_mode_loads_nothing(host: ExtensionHost, monkeypatch) -> None:
    monkeypatch.setenv(DISABLE_ENV, "1")
    host.load([FakeEntryPoint("voice", voice_setup)])
    assert host.loaded == [] and host.sections == []


def test_the_installed_group_is_scanned_when_no_list_is_given(host: ExtensionHost) -> None:
    host.load()  # nothing in this environment registers the group: no error, no add-on
    assert host.loaded == []


def test_section_rules(host: ExtensionHost) -> None:
    def twice(api) -> None:
        api.register_settings_section([SettingField("a", "A")], title="One")
        api.register_settings_section([SettingField("b", "B")], title="Again")  # same id

    host.load([FakeEntryPoint("dup", twice)])
    assert host.loaded[0].ok is False and "already registered" in host.loaded[0].error
    with pytest.raises(ExtensionError):
        SettingField("Bad-Key", "x")
    with pytest.raises(ExtensionError):
        SettingField("k", "x", "choice")  # a choice needs choices


def test_settings_overlay_the_config_table_and_survive_garbage(
    host: ExtensionHost, paths: Paths
) -> None:
    host.load([FakeEntryPoint("voice", voice_setup)])
    cfg = load_config(paths)
    cfg.extensions["voice"] = {"enabled": "yes", "rate": "1.5", "engine": "nonsense", "extra": 7}
    host.get_config = lambda: cfg
    s = host.settings_for("voice")
    assert s["enabled"] is True and s["rate"] == 1.5
    assert s["engine"] == "stock"  # invalid -> default
    assert s["extra"] == 7  # keys the add-on did not register are passed through


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("rate", 5),
        ("rate", "fast"),
        ("engine", "x"),
        ("threads", 0),
        ("nope", 1),
        ("enabled", "maybe"),
    ],
)
def test_validation_refuses_what_does_not_fit(host: ExtensionHost, key: str, value: Any) -> None:
    host.load([FakeEntryPoint("voice", voice_setup)])
    with pytest.raises(ExtensionError):
        host.validate_values("voice", {key: value})


def test_validation_returns_typed_values(host: ExtensionHost) -> None:
    host.load([FakeEntryPoint("voice", voice_setup)])
    assert host.validate_values("voice", {"enabled": "off", "rate": "0.75", "threads": "8"}) == {
        "enabled": False,
        "rate": 0.75,
        "threads": 8,
    }


def test_callbacks_are_isolated_from_each_other(host: ExtensionHost) -> None:
    got: list[Any] = []

    def good(api) -> None:
        api.on_turn(lambda t: got.append(("good-turn", t.reply_text)))
        api.on_event(lambda e: got.append(("good-event", e["type"])))
        api.on_startup(lambda: got.append("up"))
        api.on_shutdown(lambda: got.append("down"))

    def bad(api) -> None:
        def raiser(*_a: Any) -> None:
            raise RuntimeError("add-on bug")

        api.on_turn(raiser)
        api.on_event(raiser)
        api.on_startup(raiser)
        api.on_shutdown(raiser)

    host.load([FakeEntryPoint("bad", bad), FakeEntryPoint("good", good)])
    host.startup()
    host.dispatch_event({"type": "chat.delta"})
    host.dispatch_turn(TurnInfo("r1", "hi", "hello", "local-npu", "m", True))
    host.shutdown()
    # The bad add-on raised in every hook; the good one still heard everything.
    assert got == [
        "up",
        ("good-event", "chat.delta"),
        ("good-turn", "hello"),
        "down",
    ]
    assert host.wants_events is True


def test_audio_placeholders_register_and_create(host: ExtensionHost) -> None:
    def setup(api) -> None:
        api.audio.register_capture("mic", lambda rate=16000: ("mic", rate))
        api.audio.register_playback("spk", lambda: "speaker")

    host.load([FakeEntryPoint("voice", setup)])
    assert host.audio.capture_backends() == ["mic"] and host.audio.playback_backends() == ["spk"]
    assert host.audio.create_capture("mic", rate=8000) == ("mic", 8000)
    assert host.audio.create_playback("spk") == "speaker"
    with pytest.raises(ExtensionError):
        host.audio.create_capture("nope")


def test_chat_port_needs_the_app_to_bind_it(host: ExtensionHost) -> None:
    with pytest.raises(ExtensionError):
        host.chat.send("hello")
    sent: list[str] = []
    cancelled: list[str] = []
    host.chat.bind(lambda text: sent.append(text) or "req_9", cancelled.append)
    assert host.chat.send("hello") == "req_9" and sent == ["hello"]
    host.chat.cancel("req_9")
    assert cancelled == ["req_9"]


def test_data_dir_is_inside_the_home(host: ExtensionHost, paths: Paths) -> None:
    seen: list[Path] = []
    host.load([FakeEntryPoint("voice", lambda api: seen.append(api.data_dir))])
    assert seen[0] == paths.home / "extensions" / "voice" and seen[0].is_dir()


# --------------------------------------------------------------------------- through the bridge


@pytest.fixture
def wired(paths: Paths, fake_keyring) -> tuple[Api, Services, ExtensionHost, list[dict]]:
    cfg = load_config(paths)
    s = Services(paths, cfg)
    delivered: list[dict] = []
    s.events = EventSink(deliver=lambda _x: None, coalesce_ms=0)
    host = ExtensionHost(app_version="9.9.9", get_config=lambda: s.config, home=paths.home)
    s.extensions = host
    api = Api(s)
    host.load([FakeEntryPoint("voice", voice_setup)])
    return api, s, host, delivered


def test_get_extensions_lists_sections_and_values(wired) -> None:
    api, _s, _host, _ = wired
    reply = api.get_extensions()
    assert reply["ok"] is True
    (ext,) = reply["extensions"]
    assert ext["name"] == "voice" and ext["ok"] and ext["values"]["threads"] == 4


def test_set_extension_settings_validates_and_persists(wired, paths: Paths) -> None:
    api, s, _host, _ = wired
    reply = api.set_extension_settings("voice", {"enabled": True, "rate": 1.25})
    assert reply["ok"] is True and reply["values"]["rate"] == 1.25
    assert s.config.extensions["voice"] == {"enabled": True, "rate": 1.25}
    assert load_config(paths).extensions["voice"]["rate"] == 1.25  # in config.toml
    bad = api.set_extension_settings("voice", {"rate": 9})
    assert bad["ok"] is False and bad["error"]["code"] == "bad_request"
    unknown = api.set_extension_settings("ghost", {"a": 1})
    assert unknown["ok"] is False


def test_an_unregistered_table_survives_a_config_save(wired, paths: Paths) -> None:
    api, s, _host, _ = wired
    api.set_extension_settings("voice", {"enabled": True})
    api.update_settings({"chat": {"max_tool_rounds": 3}})  # an unrelated save
    assert load_config(paths).extensions["voice"]["enabled"] is True


def test_events_and_turns_reach_the_addon_through_the_bridge(wired) -> None:
    api, s, host, _ = wired
    heard: list[Any] = []
    turns: list[TurnInfo] = []
    host.event_callbacks.append(("t", heard.append))
    host.turn_callbacks.append(("t", turns.append))
    emit = api._report_reply_end("req_1", api._emit, "What is 2+2?")
    emit({"type": "chat.delta", "request_id": "req_1", "delta": "4"})
    emit({"type": "chat.done", "request_id": "req_1", "content": "Four.", "model": "m-x"})
    assert [e["type"] for e in heard] == ["chat.delta", "chat.done"]
    (turn,) = turns
    assert (turn.user_text, turn.reply_text, turn.model, turn.ok) == (
        "What is 2+2?",
        "Four.",
        "m-x",
        True,
    )
    emit2 = api._report_reply_end("req_2", api._emit, "again")
    emit2({"type": "chat.error", "request_id": "req_2", "code": "cancelled"})
    assert turns[-1].ok is False and turns[-1].error_code == "cancelled"


def test_a_failing_addon_does_not_break_the_turn(wired) -> None:
    api, _s, host, _ = wired

    def boom(_t: TurnInfo) -> None:
        raise RuntimeError("x")

    host.turn_callbacks.append(("boom", boom))
    seen: list[dict] = []
    emit = api._report_reply_end("req_1", seen.append, "hi")
    emit({"type": "chat.done", "request_id": "req_1", "content": "ok"})
    assert seen and seen[0]["type"] == "chat.done"
