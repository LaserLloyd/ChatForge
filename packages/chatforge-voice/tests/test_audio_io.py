from __future__ import annotations

import time

import numpy as np
import pytest
from conftest import FakeSD

from chatforge_voice import audio_io as aio


def test_resample_halves_and_doubles_sample_counts():
    x = (np.sin(np.arange(4800) / 20) * 8000).astype("<i2").tobytes()
    assert len(aio.resample_pcm16(x, 48000, 16000)) == len(x) // 3
    assert len(aio.resample_pcm16(x, 16000, 48000)) == len(x) * 3
    assert aio.resample_pcm16(x, 16000, 16000) is x and aio.resample_pcm16(b"", 1, 2) == b""


def test_device_resolution(fake_sd):
    assert aio.resolve_device(fake_sd, "", "input") is None
    assert aio.resolve_device(fake_sd, "1", "output") == 1
    assert aio.resolve_device(fake_sd, "fake mic", "input") == 0
    assert aio.resolve_device(fake_sd, "speak", "output") == 1
    with pytest.raises(aio.AudioError):
        aio.resolve_device(fake_sd, "speak", "input")  # no input channels there
    assert [d["name"] for d in aio.list_devices(fake_sd)] == ["Fake Mic", "Fake Speakers"]


def test_recorder_captures_16k_pcm16_mono(fake_sd):
    rec = aio.Recorder(backend=fake_sd, device="mic")
    rec.start()
    stream = fake_sd.inputs[0]
    assert stream.samplerate == 16000 and stream.started and rec.recording
    stream.feed(1.0)
    pcm = rec.stop()
    assert len(pcm) == 16000 * 2 and stream.closed and not rec.recording
    assert np.frombuffer(pcm, "<i2")[0] == 3000
    assert rec.stop() == b""  # a second stop is harmless


def test_recorder_falls_back_to_the_device_rate_and_resamples():
    sd = FakeSD(refuse_rates={16000})
    rec = aio.Recorder(backend=sd)
    rec.start()
    assert sd.inputs[0].samplerate == 48000
    sd.inputs[0].feed(1.0)
    assert len(rec.stop()) == pytest.approx(16000 * 2, abs=4)


def test_recorder_stops_itself_at_the_limit(fake_sd):
    hit = []
    rec = aio.Recorder(backend=fake_sd, max_seconds=0.5, on_limit=lambda: hit.append(1))
    rec.start()
    fake_sd.inputs[0].feed(1.0)
    deadline = time.monotonic() + 2
    while not hit and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hit == [1]  # fired once, off the audio thread


def test_recorder_reports_an_unopenable_device():
    sd = FakeSD(refuse_rates={16000, 48000})
    with pytest.raises(aio.AudioError) as e:
        aio.Recorder(backend=sd).start()
    assert "could not be opened" in str(e.value)


def _wait(cond, t=3.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_player_plays_everything_then_closes(fake_sd):
    p = aio.Player(backend=fake_sd, prebuffer_s=0.05)
    p.start()
    frame = np.full(2400, 700, "<i2").tobytes()  # 0.1 s at 24 kHz
    for _ in range(5):
        p.write(frame)
    assert p.finish(timeout=5) is True
    out = fake_sd.outputs[0]
    assert out.closed and not out.aborted
    heard = np.frombuffer(bytes(out.heard), "<i2")
    assert (heard == 700).sum() == 2400 * 5  # every sample, nothing lost or repeated


def test_player_stop_is_immediate_and_drops_the_queue(fake_sd):
    p = aio.Player(backend=fake_sd, prebuffer_s=0.0)
    p.start()
    p.write(np.full(24000 * 20, 900, "<i2").tobytes())  # 20 s queued
    assert _wait(lambda: p.played_bytes > 0)
    p.stop()
    out = fake_sd.outputs[0]
    assert out.aborted and out.closed and p.buffered_s == 0 and p.stopped
    before = len(out.heard)
    p.write(b"\x01\x00" * 100)  # late frames from a cancelled reply
    assert p.buffered_s == 0
    time.sleep(0.05)
    assert len(out.heard) - before < 2000  # nothing more is played after the abort
    assert p.finish(timeout=1) is False


def test_player_resamples_when_the_device_refuses_24k():
    sd = FakeSD(refuse_rates={24000})
    p = aio.Player(backend=sd, prebuffer_s=0.0)
    p.start()
    assert sd.outputs[0].samplerate == 48000
    sd.outputs[0].running = False  # keep the fake card from draining the buffer
    p.write(np.full(2400, 500, "<i2").tobytes())
    assert p.buffered_s == pytest.approx(0.1, abs=0.01)
    p.stop()


def test_player_never_plays_before_the_prebuffer_fills(fake_sd):
    p = aio.Player(backend=fake_sd, prebuffer_s=0.5)
    p.start()
    p.write(np.full(2400, 800, "<i2").tobytes())  # only 0.1 s
    time.sleep(0.1)
    assert p.played_bytes == 0
    p.stop()


def test_a_missing_audio_library_is_a_sentence_not_a_crash(monkeypatch):
    import builtins

    real = builtins.__import__

    def blocked(name, *a, **k):
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(aio.AudioError) as e:
        aio.Recorder().start()
    assert "audio library" in str(e.value)
