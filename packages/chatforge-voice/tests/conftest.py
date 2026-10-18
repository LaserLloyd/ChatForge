"""Fakes for the whole suite: audio devices, speech engines, the chat port, ChatForge's extension API.

No test loads a model, opens a real audio device, uses the network or touches the user's folders.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from forge_voice_core import hostlink

from chatforge_voice import settings as vs
from chatforge_voice.controller import Deps


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith(
            ("CHATFORGE_VOICE_", "FORGE_VOICE_", "DISPATCH_VOICE_", "DISPATCH_SPEECH_")
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FORGE_VOICE_DATA_DIR", str(tmp_path / "fvc"))
    monkeypatch.setenv("CHATFORGE_HOME", str(tmp_path / "cf-home"))
    monkeypatch.setenv("CHATFORGE_VOICE_DATA_DIR", str(tmp_path / "voice-data"))
    hostlink.reset()
    yield
    hostlink.reset()


# --------------------------------------------------------------------------- audio devices


class FakeInputStream:
    def __init__(self, samplerate, channels, dtype, device, callback, **_):
        self.samplerate, self.callback, self.device = samplerate, callback, device
        self.started = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def feed(self, seconds: float, amp: int = 3000):
        """Deliver ``seconds`` of audio in 100 ms blocks, as the device would."""
        n = int(self.samplerate * 0.1)
        for _ in range(max(1, round(seconds * 10))):
            block = np.full((n, 1), amp, dtype="<i2")
            self.callback(block, n, None, None)


class FakeOutputStream:
    """Pulls audio like a sound card: a thread calls the callback in real time."""

    def __init__(self, samplerate, channels, dtype, device, callback, **_):
        self.samplerate, self.callback, self.device = samplerate, callback, device
        self.heard = bytearray()
        self.aborted = self.closed = self.running = False
        self._thread = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        n = 480
        while self.running:
            out = np.zeros((n, 1), dtype="<i2")
            self.callback(out, n, None, None)
            self.heard += out.tobytes()
            time.sleep(0.002)

    def stop(self):
        self.running = False

    def abort(self):
        self.aborted = True
        self.running = False

    def close(self):
        self.running = False
        self.closed = True


class FakeSD:
    """Stands in for the ``sounddevice`` module."""

    def __init__(self, devices=None, refuse_rates=()):
        self.devices = devices or [
            {
                "name": "Fake Mic",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000.0,
            },
            {
                "name": "Fake Speakers",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
        ]
        self.refuse_rates = set(refuse_rates)
        self.inputs: list[FakeInputStream] = []
        self.outputs: list[FakeOutputStream] = []

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return self.devices
        if device is None:
            device = 0 if kind == "input" else 1
        return self.devices[device]

    def _make(self, cls, store, samplerate, **kw):
        if samplerate in self.refuse_rates:
            raise OSError(f"Invalid sample rate {samplerate}")
        s = cls(samplerate=samplerate, **kw)
        store.append(s)
        return s

    def InputStream(self, samplerate, **kw):  # noqa: N802
        return self._make(FakeInputStream, self.inputs, samplerate, **kw)

    def OutputStream(self, samplerate, **kw):  # noqa: N802
        return self._make(FakeOutputStream, self.outputs, samplerate, **kw)


@pytest.fixture
def fake_sd():
    return FakeSD()


# --------------------------------------------------------------------------- engines


class FakeSTT:
    def __init__(self, text="hello there"):
        self.text = text
        self.calls: list[int] = []

    def transcribe(self, audio):
        self.calls.append(len(audio))
        return self.text


class FakeTTS:
    """A TTS engine for ``tts.speak``: 0.1 s of audio per word, instantly."""

    sample_rate = 24000

    def __init__(self):
        self.spoken: list[str] = []

    def stream(self, text, profile_id=None, stop=None):
        self.spoken.append(text)
        for _ in text.split():
            if stop is not None and stop.is_set():
                return
            yield np.full(2400, 500, dtype="<i2")


class FakePlayer:
    """Records what would be played; ``stop`` is immediate like the real one."""

    instances: list[FakePlayer] = []

    def __init__(self):
        self.started_rate = None
        self.frames: list[bytes] = []
        self.stopped = False
        self.finished = False
        FakePlayer.instances.append(self)

    def start(self, rate=None):
        self.started_rate = rate

    def write(self, pcm):
        if not self.stopped:
            self.frames.append(pcm)

    def finish(self, timeout=None):
        self.finished = True
        return not self.stopped

    def stop(self):
        self.stopped = True

    @property
    def audio_bytes(self):
        return sum(len(f) for f in self.frames)


class FakeRecorder:
    def __init__(self, seconds=1.0, fail=None, on_limit=None):
        self.seconds, self.fail, self.on_limit = seconds, fail, on_limit
        self.started = self.stopped = self.aborted = False

    def start(self):
        if self.fail:
            raise self.fail
        self.started = True

    def stop(self):
        self.stopped = True
        return b"\x10\x00" * int(16000 * self.seconds)

    def abort(self):
        self.aborted = True


class FakeChat:
    def __init__(self, rid="req-1", fail=False):
        self.rid, self.fail = rid, fail
        self.sent: list[str] = []
        self.cancelled: list[str] = []
        self.on_send = None  # hook: runs inside send(), like events racing the return

    def send(self, text):
        self.sent.append(text)
        if self.on_send:
            self.on_send(self.rid)
        if self.fail:
            return None
        return self.rid

    def cancel(self, rid):
        self.cancelled.append(rid)


def make_settings(**kw: Any) -> vs.VoiceEngineSettings:
    values = {"enabled": True}
    values.update(kw)
    return vs.build(vs.resolve(values, {}), Path("models-speech"))


def make_deps(*, stt=None, tts=None, recorder=None, missing=None, clock=time.monotonic) -> Deps:
    from forge_voice_core import tts as core_tts

    stt = stt or FakeSTT()
    tts = tts or FakeTTS()
    FakePlayer.instances.clear()
    return Deps(
        recorder_factory=lambda s, on_limit: recorder or FakeRecorder(on_limit=on_limit),
        player_factory=lambda s: FakePlayer(),
        stt_factory=lambda s: stt,
        tts_factory=lambda s: tts,
        speak=core_tts.speak,  # the real sentence splitter / normaliser around the fake engine
        missing=lambda s: list(missing or []),
        unload=lambda: None,
        clock=clock,
    )


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- ChatForge's extension API


class FakeAudioRegistry:
    def __init__(self):
        self.capture, self.playback = {}, {}

    def register_capture(self, name, factory):
        self.capture[name] = factory

    def register_playback(self, name, factory):
        self.playback[name] = factory


class FakeExtensionAPI:
    """Mirrors chatforge.extensions.ExtensionAPI (API_VERSION 1) as far as the add-on uses it."""

    api_version = 1

    def __init__(self, data_dir: Path, values: dict | None = None, chat=None):
        self.name = "voice"
        self.log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.values = values if values is not None else {}
        self.chat = chat or FakeChat()
        self.audio = FakeAudioRegistry()
        self.sections: list[dict] = []
        self.turn_cbs, self.event_cbs, self.startup_cbs, self.shutdown_cbs = [], [], [], []

    @property
    def settings(self):
        return dict(self.values)

    def register_settings_section(self, fields, *, title, section_id="main", description=""):
        self.sections.append(
            {"fields": list(fields), "title": title, "id": section_id, "description": description}
        )

    def on_turn(self, cb):
        self.turn_cbs.append(cb)

    def on_event(self, cb):
        self.event_cbs.append(cb)

    def on_startup(self, cb):
        self.startup_cbs.append(cb)

    def on_shutdown(self, cb):
        self.shutdown_cbs.append(cb)
