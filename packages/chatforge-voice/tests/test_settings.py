from __future__ import annotations

from chatforge_voice import messages
from chatforge_voice import settings as vs


def test_the_schema_uses_the_chatforge_environment_prefix():
    names = {n for s in vs.SCHEMA.SETTINGS for n in s.env}
    assert "CHATFORGE_VOICE_ENABLED" in names and "CHATFORGE_VOICE_THREADS" in names
    assert not any(n.startswith("DISPATCH_VOICE_") for n in names)


def test_defaults_are_off_cpu_push_to_talk():
    v = vs.resolve({}, {})
    assert v["enabled"] is False and v["mode"] == "push_to_talk" and v["backend"] == "cpu"
    assert v["hotkey"] == vs.DEFAULT_HOTKEY and v["speak_replies"] == "voice_only"
    assert vs.unsupported(v) == []


def test_environment_beats_the_file_beats_the_default():
    prefs = {"enabled": False, "threads": 3, "speed": 1.2}
    assert vs.resolve(prefs, {})["threads"] == 3
    env = {"CHATFORGE_VOICE_THREADS": "5", "CHATFORGE_VOICE_ENABLED": "yes"}
    v = vs.resolve(prefs, env)
    assert v["threads"] == 5 and v["enabled"] is True and v["speed"] == 1.2


def test_a_bad_stored_value_falls_back_to_the_default():
    v = vs.resolve({"speed": 99, "speak_replies": "loudly", "max_record_s": "x"}, {})
    assert v["speed"] == 1.05 and v["speak_replies"] == "voice_only" and v["max_record_s"] == 30.0


def test_hands_free_is_understood_but_not_acted_on():
    v = vs.resolve({"mode": "hands_free"}, {})
    assert vs.unsupported(v) == [messages.NOT_IN_THIS_RELEASE_HANDS_FREE]
    assert vs.build(v).mode == "hands_free"  # reported; push-to-talk is what runs
    assert "not in this release" in messages.NOT_IN_THIS_RELEASE_HANDS_FREE


def test_remote_speech_is_refused_with_one_message_and_the_engines_stay_local():
    env = {
        "CHATFORGE_VOICE_STT_PROVIDER": "openai",
        "CHATFORGE_VOICE_STT_URL": "http://10.0.0.5:9000",
        "CHATFORGE_VOICE_HQ_URL": "http://10.0.0.5:9001",
    }
    v = vs.resolve({"backend": "speechforge"}, env)
    assert vs.unsupported(v) == [messages.NOT_IN_THIS_RELEASE_REMOTE]
    e = vs.build(v)
    assert (e.stt_provider, e.tts_provider, e.hq_url, e.stt_url) == ("cpu", "cpu", "", "")
    assert e.rig.enabled is False and e.tts_engine == "auto"
    assert e.notices == (messages.NOT_IN_THIS_RELEASE_REMOTE,)


def test_the_rig_switch_in_the_environment_does_not_turn_the_rig_on():
    v = vs.resolve({}, {"CHATFORGE_VOICE_RIG_ENABLED": "1"})
    assert vs.build(v).rig.enabled is False


def test_the_engine_settings_object_is_what_the_engines_read():
    e = vs.build(vs.resolve({"enabled": True}, {}))
    assert e.switched_on is True and e.forced is None
    assert e.stt_engine == "parakeet" and e.parakeet_dir.name == "parakeet-tdt-0.6b-v2"
    assert 2 <= e.threads <= 6 and 2 <= e.tts_threads <= 8


def test_the_settings_card_is_small_and_matches_the_schema():
    assert vs.CARD_KEYS[0] == "enabled"
    assert vs.SCHEMA.setting("enabled").label == "Voice (CPU)"
    assert set(vs.CARD_KEYS) <= set(vs.SCHEMA.BY_ID)
