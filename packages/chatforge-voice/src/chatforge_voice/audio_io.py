"""Native capture and playback through ``sounddevice`` (PortAudio).

* :class:`Recorder` captures PCM16 little-endian mono at 16 kHz while a key is held.
* :class:`Player` plays int16 mono frames (24 kHz from Chatterbox) and can stop at once.

Both take a ``backend`` (the ``sounddevice`` module by default) so tests drive them with a fake
device, and both resample in software when a device refuses the wanted rate. Nothing here imports
``sounddevice`` until a stream is opened, so the add-on imports on a machine without PortAudio.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

MIC_RATE = 16_000
SPEAKER_RATE = 24_000
_BYTES = 2  # int16


class AudioError(RuntimeError):
    """The audio library or a device is unusable; the message is shown to the person."""


def load_backend() -> Any:
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise AudioError(
            "The audio library (sounddevice / PortAudio) could not be loaded: "
            f"{type(exc).__name__}. Reinstall chatforge-voice."
        ) from None
    return sd


def resample_pcm16(pcm: bytes, src: int, dst: int) -> bytes:
    """Linear-interpolation resampler for speech (a moving average first when going down)."""
    if src == dst or not pcm:
        return pcm
    import numpy as np

    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if dst < src:
        k = max(1, round(src / dst))
        if k > 1:
            x = np.convolve(x, np.full(k, 1.0 / k, dtype=np.float32), mode="same")
    n = int(len(x) * dst / src)
    if n <= 0:
        return b""
    y = np.interp(np.arange(n) * (src / dst), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def resolve_device(backend: Any, name: str, kind: str) -> int | None:
    """``""`` is the system default (None). A number is a device index. Otherwise a
    case-insensitive part of a device's name that has input (or output) channels."""
    name = (name or "").strip()
    if not name:
        return None
    if name.isdigit():
        return int(name)
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    wanted = name.lower()
    for i, dev in enumerate(backend.query_devices()):
        if dev.get(key, 0) > 0 and wanted in str(dev.get("name", "")).lower():
            return i
    raise AudioError(f"No {kind} device matches {name!r}. See `chatforge-voice devices`.")


def list_devices(backend: Any = None) -> list[dict]:
    sd = backend or load_backend()
    out = []
    for i, dev in enumerate(sd.query_devices()):
        out.append(
            {
                "index": i,
                "name": str(dev.get("name", "")),
                "inputs": int(dev.get("max_input_channels", 0)),
                "outputs": int(dev.get("max_output_channels", 0)),
            }
        )
    return out


def _open(backend: Any, factory_name: str, rates: list[int], **kw: Any) -> tuple[Any, int]:
    """Open a stream at the first rate the device accepts; returns ``(stream, rate)``."""
    last: Exception | None = None
    for rate in rates:
        try:
            return getattr(backend, factory_name)(samplerate=rate, **kw), rate
        except Exception as exc:  # noqa: BLE001 - PortAudioError: try the next rate
            last = exc
    raise AudioError(f"The audio device could not be opened ({type(last).__name__}: {last}).") from None


def _default_rate(backend: Any, device: int | None, kind: str) -> int | None:
    try:
        info = backend.query_devices(device, kind)
        return int(info["default_samplerate"])
    except Exception:  # noqa: BLE001
        return None


class Recorder:
    """Hold-to-talk capture. ``start()`` opens the microphone; ``stop()`` returns 16 kHz PCM16."""

    def __init__(
        self,
        *,
        backend: Any = None,
        device: str = "",
        max_seconds: float = 30.0,
        on_limit: Callable[[], None] | None = None,
    ) -> None:
        self._backend = backend
        self.device = device
        self.max_seconds = max_seconds
        self._on_limit = on_limit
        self._chunks: list[bytes] = []
        self._samples = 0
        self._rate = MIC_RATE
        self._stream: Any = None
        self._lock = threading.Lock()
        self._limit_fired = False

    @property
    def seconds(self) -> float:
        return self._samples / float(self._rate)

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        sd = self._backend or load_backend()
        self._backend = sd
        index = resolve_device(sd, self.device, "input")
        rates = [MIC_RATE]
        fallback = _default_rate(sd, index, "input")
        if fallback and fallback != MIC_RATE:
            rates.append(fallback)
        self._chunks, self._samples, self._limit_fired = [], 0, False
        stream, self._rate = _open(
            sd, "InputStream", rates, channels=1, dtype="int16", device=index,
            callback=self._callback,
        )
        try:
            stream.start()
        except Exception as exc:  # noqa: BLE001
            self._close(stream)
            raise AudioError(f"The microphone could not be started ({type(exc).__name__}).") from None
        self._stream = stream

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        data = bytes(indata)  # int16, one channel
        with self._lock:
            self._chunks.append(data)
            self._samples += len(data) // _BYTES
            hit = self._samples >= self.max_seconds * self._rate and not self._limit_fired
            if hit:
                self._limit_fired = True
        if hit and self._on_limit is not None:
            threading.Thread(target=self._on_limit, name="chatforge-voice-limit", daemon=True).start()

    @staticmethod
    def _close(stream: Any) -> None:
        for step in ("stop", "close"):
            try:
                getattr(stream, step)()
            except Exception:  # noqa: BLE001 - closing a dead stream
                pass

    def stop(self) -> bytes:
        """Close the microphone and return what was heard as 16 kHz PCM16 mono."""
        stream, self._stream = self._stream, None
        if stream is not None:
            self._close(stream)
        with self._lock:
            pcm = b"".join(self._chunks)
            self._chunks = []
        return resample_pcm16(pcm, self._rate, MIC_RATE)

    def abort(self) -> None:
        self.stop()


class Player:
    """Plays int16 mono frames; ``stop()`` silences it immediately and drops what is queued."""

    def __init__(
        self,
        *,
        backend: Any = None,
        device: str = "",
        rate: int = SPEAKER_RATE,
        prebuffer_s: float = 0.25,
    ) -> None:
        self._backend = backend
        self.device = device
        self.rate = rate
        self._out_rate = rate
        self._prebuffer = prebuffer_s
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._stream: Any = None
        self._closing = False
        self._stopped = False
        self._primed = False
        self.played_bytes = 0

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def started(self) -> bool:
        return self._stream is not None

    @property
    def buffered_s(self) -> float:
        with self._cv:
            return len(self._buf) / float(_BYTES * self._out_rate)

    def start(self, rate: int | None = None) -> None:
        """Open the speaker for frames at ``rate`` (default: the constructor's)."""
        if self._stream is not None or self._stopped:
            return
        if rate:
            self.rate = rate
        sd = self._backend or load_backend()
        self._backend = sd
        index = resolve_device(sd, self.device, "output")
        rates = [self.rate]
        fallback = _default_rate(sd, index, "output")
        if fallback and fallback != self.rate:
            rates.append(fallback)
        stream, self._out_rate = _open(
            sd, "OutputStream", rates, channels=1, dtype="int16", device=index,
            callback=self._callback,
        )
        try:
            stream.start()
        except Exception as exc:  # noqa: BLE001
            Recorder._close(stream)
            raise AudioError(f"The speaker could not be started ({type(exc).__name__}).") from None
        self._stream = stream

    def write(self, pcm: bytes) -> None:
        if self._stopped or not pcm:
            return
        if self._out_rate != self.rate:
            pcm = resample_pcm16(pcm, self.rate, self._out_rate)
        with self._cv:
            if not self._stopped:
                self._buf += pcm

    def _callback(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        need = frames * _BYTES
        with self._cv:
            if not self._primed and (
                len(self._buf) >= self._prebuffer * self._out_rate * _BYTES or self._closing
            ):
                self._primed = True
            take = bytes(self._buf[:need]) if self._primed else b""
            del self._buf[: len(take)]
            self.played_bytes += len(take)
            if self._closing and not self._buf:
                self._cv.notify_all()
        raw = take + bytes(need - len(take))
        try:
            outdata[:] = _as_frames(raw, outdata)
        except Exception:  # noqa: BLE001 - never raise inside the audio thread
            pass

    def finish(self, timeout: float | None = None) -> bool:
        """Let what is queued play out, then close. False if it was stopped (or timed out)."""
        with self._cv:
            self._closing = True
            ok = self._cv.wait_for(lambda: not self._buf or self._stopped, timeout)
            stopped = self._stopped
        self._close_stream(abort=False)
        return bool(ok) and not stopped

    def stop(self) -> None:
        """Silence now: the queue is emptied and the stream aborted, not drained."""
        with self._cv:
            self._stopped = True
            self._buf.clear()
            self._cv.notify_all()
        self._close_stream(abort=True)

    def _close_stream(self, *, abort: bool) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        for step in ("abort" if abort else "stop", "close"):
            try:
                getattr(stream, step)()
            except Exception:  # noqa: BLE001
                pass


def _as_frames(raw: bytes, outdata: Any) -> Any:
    """``raw`` shaped for the stream's output buffer (a numpy array, or a CFFI buffer)."""
    try:
        import numpy as np

        arr = np.frombuffer(raw, dtype="<i2")
        shape = getattr(outdata, "shape", None)
        return arr.reshape(shape) if shape else arr
    except ImportError:  # pragma: no cover - numpy always comes with the engines
        return raw
