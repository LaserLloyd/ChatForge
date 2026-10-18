"""Against ChatForge's REAL ``chatforge.extensions`` (skipped where ChatForge's add-on seams are absent).

Run it in an environment that has ChatForge with the extension seams installed (the ChatForge merge
job), or point PYTHONPATH at a tree that has them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

ext = pytest.importorskip("chatforge.extensions")

import chatforge_voice  # noqa: E402


class EP:
    def __init__(self, name, target):
        self.name, self._t = name, target
        self.dist = SimpleNamespace(version="0.1.0")

    def load(self):
        return self._t


@pytest.fixture
def host(tmp_path):
    store: dict = {}
    h = ext.ExtensionHost(
        app_version="0.2.0",
        get_config=lambda: SimpleNamespace(extensions={"voice": dict(store)}),
        home=tmp_path / "cf",
    )
    h.store = store
    h.chat.bind(send=lambda text: "req-1", cancel=lambda rid: None)
    yield h
    h.shutdown()


def test_the_addon_loads_through_the_real_host(host):
    (loaded,) = host.load([EP("voice", chatforge_voice.setup)])
    assert loaded.ok, loaded.error
    (section,) = host.sections
    assert section.title == "Voice (CPU)" and section.fields[0].key == "enabled"
    assert host.audio.capture_backends() == ["sounddevice"]
    assert host.audio.playback_backends() == ["sounddevice"]
    assert host.turn_callbacks and host.event_callbacks and host.wants_events


def test_card_values_validate_and_round_trip(host):
    host.load([EP("voice", chatforge_voice.setup)])
    clean = host.validate_values(
        "voice", {"enabled": True, "hotkey": "F9", "speak_replies": "always"}
    )
    assert clean == {"enabled": True, "hotkey": "F9", "speak_replies": "always"}
    for bad in ({"speed": 9}, {"speak_replies": "loud"}, {"nope": 1}):
        with pytest.raises(ext.ExtensionError):
            host.validate_values("voice", bad)
    host.store.update(clean)
    assert host.settings_for("voice")["hotkey"] == "F9"
    view = host.view()[0]
    assert view["ok"] and view["values"]["enabled"] is True


def test_events_and_turns_reach_the_controller(host):
    host.load([EP("voice", chatforge_voice.setup)])
    host.store["enabled"] = True
    host.startup()
    host.dispatch_event({"type": "chat.start", "request_id": "r"})
    host.dispatch_turn(
        ext.TurnInfo(
            request_id="r", user_text="", reply_text="Hi", provider="p", model="m", ok=True
        )
    )
    assert [e.ok for e in host.loaded] == [True]  # the guarded dispatch raised nothing


def test_a_missing_engine_package_shows_up_as_a_failed_addon_with_the_sentence(host, monkeypatch):
    monkeypatch.setattr(chatforge_voice, "core_problem", lambda: chatforge_voice.CORE_MISSING)
    (loaded,) = host.load([EP("voice", chatforge_voice.setup)])
    assert loaded.ok is False and "forge-voice-core" in loaded.error
    assert host.sections == []  # nothing half-registered
