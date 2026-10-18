"""The push-to-talk loop with fake audio, fake speech engines and a fake chat."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import (
    FakeChat,
    FakePlayer,
    FakeRecorder,
    FakeSTT,
    FakeTTS,
    make_deps,
    make_settings,
    wait_for,
)

from chatforge_voice.controller import VoiceController


class Rig:
    """A controller plus its fakes and a settable settings object."""

    def __init__(self, *, chat=None, stt=None, tts=None, recorder=None, missing=None, **settings):
        self.chat = chat or FakeChat()
        self.stt = stt or FakeSTT("what is the weather")
        self.tts = tts or FakeTTS()
        self.settings = make_settings(**settings)
        self.deps = make_deps(stt=self.stt, tts=self.tts, recorder=recorder, missing=missing)
        self.statuses: list[dict] = []
        self.ctl = VoiceController(
            self.chat, lambda: self.settings, self.deps, on_status=self.statuses.append
        )

    def say(self):
        """Hold the key, speak for a second, release."""
        assert self.ctl.talk_start()
        self.ctl.talk_stop()
        assert wait_for(lambda: self.chat.sent or self.ctl.state == "idle")

    def event(self, etype, rid="req-1", **kw):
        self.ctl.on_event({"type": etype, "request_id": rid, **kw})

    def reply(self, words="It is sunny today. Bring a hat.", rid="req-1"):
        self.event("chat.start", rid)
        for w in words.split(" "):
            self.event("chat.delta", rid, content=w + " ")
        self.event("chat.done", rid, content=words)

    def finished(self):
        return wait_for(lambda: self.ctl.state == "idle" and not self.ctl._sessions)


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.ctl.shutdown()


def test_the_whole_loop_speech_in_text_to_chat_reply_spoken(rig):
    rig.say()
    assert rig.chat.sent == ["what is the weather"] and rig.ctl.heard == "what is the weather"
    assert rig.stt.calls == [16000]  # one second of float32 audio
    assert wait_for(lambda: rig.ctl.state == "thinking")
    rig.reply()
    assert rig.finished()
    assert rig.tts.spoken == ["It is sunny today.", "Bring a hat."]  # sentence by sentence
    player = FakePlayer.instances[-1]
    assert player.started_rate == 24000 and player.audio_bytes > 0 and player.finished
    seen = []
    for st in rig.statuses:
        if st["state"] in ("recording", "transcribing", "thinking", "speaking") and (
            not seen or seen[-1] != st["state"]
        ):
            seen.append(st["state"])
    assert seen[:4] == ["recording", "transcribing", "thinking", "speaking"]


def test_speech_starts_before_the_reply_is_complete(rig):
    rig.say()
    rig.event("chat.start")
    rig.event("chat.delta", content="The first sentence is here. ")
    assert wait_for(lambda: rig.tts.spoken == ["The first sentence is here."])
    assert rig.ctl.state in ("speaking", "thinking")
    rig.event("chat.delta", content="And more")
    rig.event("chat.done", content="The first sentence is here. And more")
    assert rig.finished() and rig.tts.spoken[-1] == "And more"


def test_a_reply_that_never_streamed_is_spoken_from_the_final_text(rig):
    rig.say()
    rig.event("chat.start")
    rig.event("chat.done", content="Short answer.")
    assert rig.finished() and rig.tts.spoken == ["Short answer."]


def test_the_turn_hook_alone_is_enough_and_ending_twice_speaks_once(rig):
    rig.say()
    rig.event("chat.start")
    rig.event("chat.done", content="Only once.")
    rig.ctl.on_turn(
        SimpleNamespace(request_id="req-1", ok=True, reply_text="Only once.", error_code=None)
    )
    assert rig.finished()
    assert rig.tts.spoken == ["Only once."]


def test_reasoning_deltas_are_not_spoken(rig):
    rig.say()
    rig.event("chat.start")
    rig.event("chat.delta", reasoning="thinking hard about it. ")
    rig.event("chat.delta", content="The answer. ")
    rig.event("chat.done", content="The answer.")
    assert rig.finished() and rig.tts.spoken == ["The answer."]


def test_typed_messages_are_not_spoken_in_voice_only_mode(rig):
    rig.reply("A typed answer.", rid="typed-1")
    assert not rig.ctl._sessions and rig.tts.spoken == []


def test_always_mode_speaks_typed_turns_too():
    r = Rig(speak_replies="always")
    r.reply("A typed answer.", rid="typed-1")
    assert r.finished() and r.tts.spoken == ["A typed answer."]
    r.ctl.on_turn(
        SimpleNamespace(
            request_id="typed-1", ok=True, reply_text="A typed answer.", error_code=None
        )
    )
    r.ctl.on_event({"type": "chat.done", "request_id": "typed-1", "content": "A typed answer."})
    assert r.tts.spoken == ["A typed answer."]  # a late duplicate is ignored
    r.ctl.shutdown()


def test_off_mode_never_speaks():
    r = Rig(speak_replies="off")
    r.say()
    r.reply()
    assert wait_for(lambda: r.ctl.state == "idle") and r.tts.spoken == []
    r.ctl.shutdown()


def test_events_that_beat_the_send_return_are_still_claimed():
    chat = FakeChat()
    r = Rig(chat=chat)
    chat.on_send = lambda rid: r.event("chat.start", rid)  # the engine emits before send() returns
    r.say()
    r.event("chat.delta", content="Hello you. ")
    r.event("chat.done", content="Hello you.")
    assert r.finished() and r.tts.spoken == ["Hello you."]
    r.ctl.shutdown()


def test_stop_silences_at_once_and_cancels_the_answer(rig):
    rig.say()
    rig.event("chat.start")
    rig.event("chat.delta", content="A very long answer begins here. ")
    assert wait_for(lambda: FakePlayer.instances and FakePlayer.instances[-1].started_rate)
    rig.ctl.stop_all()
    player = FakePlayer.instances[-1]
    assert player.stopped and rig.chat.cancelled == ["req-1"]
    assert rig.ctl.state == "idle" and rig.ctl.message == "Stopped."
    n = len(player.frames)
    rig.event("chat.delta", content="More text after the stop. ")  # a straggler event
    rig.event("chat.done", content="x")
    assert rig.finished() and len(player.frames) == n
    assert rig.ctl.status()["state"] == "idle"


def test_the_popup_stop_button_cancelled_error_silences_speech(rig):
    rig.say()
    rig.event("chat.start")
    rig.event("chat.delta", content="Speaking now for a while. ")
    assert wait_for(lambda: FakePlayer.instances and FakePlayer.instances[-1].started_rate)
    rig.event("chat.error", code="cancelled", message="Stopped.")
    assert wait_for(lambda: FakePlayer.instances[-1].stopped)
    assert rig.finished()


def test_pressing_to_talk_while_a_reply_plays_stops_it(rig):
    rig.say()
    rig.event("chat.start")
    rig.event("chat.delta", content="Talking over me soon. ")
    assert wait_for(lambda: FakePlayer.instances and FakePlayer.instances[-1].started_rate)
    assert rig.ctl.talk_start()
    assert FakePlayer.instances[-1].stopped and rig.chat.cancelled == ["req-1"]
    assert rig.ctl.state == "recording"
    rig.ctl.talk_cancel()


def test_a_chat_error_is_reported_not_spoken(rig):
    rig.say()
    rig.event("chat.start")
    rig.event("chat.error", code="no_key", message="No key set.")
    assert wait_for(lambda: rig.ctl.state == "idle")
    assert "No key" in rig.ctl.message and rig.tts.spoken == []


def test_too_short_a_recording_is_dropped():
    r = Rig(recorder=FakeRecorder(seconds=0.1))
    assert r.ctl.talk_start()
    r.ctl.talk_stop()
    assert r.ctl.state == "idle" and "too short" in r.ctl.message and r.chat.sent == []
    r.ctl.shutdown()


def test_silence_or_unclear_audio_sends_nothing():
    r = Rig(stt=FakeSTT("   "))
    r.say()
    assert wait_for(lambda: r.ctl.message == "I did not catch that.") and r.chat.sent == []
    r.ctl.shutdown()


def test_voice_off_refuses_to_listen():
    r = Rig(enabled=False)
    assert r.ctl.talk_start() is False and "Voice is off" in r.ctl.message
    assert r.ctl.status()["state"] == "off"
    r.ctl.shutdown()


def test_missing_models_refuse_with_a_pointer_to_the_panel():
    r = Rig(missing=["model silero_vad.onnx"])
    assert r.ctl.talk_start() is False and "Voice panel" in r.ctl.message
    r.ctl.shutdown()


def test_microphone_failure_is_a_message_not_an_exception():
    r = Rig(recorder=FakeRecorder(fail=RuntimeError("Invalid device")))
    assert r.ctl.talk_start() is False and "microphone" in r.ctl.message
    assert r.ctl.state == "idle"
    r.ctl.shutdown()


def test_a_chat_that_refuses_the_message_is_reported():
    r = Rig(chat=FakeChat(fail=True))
    r.say()
    assert wait_for(lambda: "did not take the message" in r.ctl.message)
    r.ctl.shutdown()


def test_transcription_failure_is_reported():
    class Boom:
        def transcribe(self, audio):
            raise RuntimeError("onnx exploded")

    r = Rig(stt=Boom())
    r.say()
    assert wait_for(lambda: "Could not transcribe" in r.ctl.message)
    r.ctl.shutdown()


def test_a_tts_failure_ends_the_session_cleanly():
    class BadTTS(FakeTTS):
        def stream(self, text, profile_id=None, stop=None):
            raise RuntimeError("no voice")
            yield  # pragma: no cover

    r = Rig(tts=BadTTS())
    r.say()
    r.reply()
    assert r.finished() and "spoken reply failed" in r.ctl.message
    r.ctl.shutdown()


def test_the_recording_limit_releases_the_key_for_you():
    holder = {}

    def factory(s, on_limit):
        holder["rec"] = FakeRecorder(on_limit=on_limit)
        return holder["rec"]

    r = Rig()
    r.deps.recorder_factory = factory
    assert r.ctl.talk_start()
    holder["rec"].on_limit()  # the recorder hit max_record_s
    assert wait_for(lambda: r.chat.sent)
    r.ctl.shutdown()


def test_models_load_lazily_and_unload_when_idle_or_off():
    now = [1000.0]
    r = Rig(idle_unload_minutes=1)
    r.deps.clock = lambda: now[0]
    unloaded = []
    r.deps.unload = lambda: unloaded.append(1)
    assert r.ctl.status()["loaded"] == {"stt": False, "tts": False}
    r.say()
    assert r.ctl.status()["loaded"]["stt"]
    r.reply()
    assert r.finished() and r.ctl.status()["loaded"]["tts"]
    now[0] += 30
    r.ctl.maintain()
    assert r.ctl.status()["loaded"]["stt"]  # not idle long enough
    now[0] += 60
    r.ctl.maintain()
    assert r.ctl.status()["loaded"] == {"stt": False, "tts": False} and unloaded
    r.ctl.shutdown()


def test_switching_voice_off_frees_the_models_at_once():
    r = Rig()
    r.say()
    r.reply()
    assert r.finished() and r.ctl.status()["loaded"]["stt"]
    r.settings = make_settings(enabled=False)
    r.ctl.maintain()
    assert not r.ctl.status()["loaded"]["stt"]
    r.ctl.shutdown()


def test_notices_about_unsupported_settings_are_in_the_status():
    r = Rig(mode="hands_free")
    assert "not in this release" in r.ctl.status()["notices"][0]
    r.ctl.shutdown()
