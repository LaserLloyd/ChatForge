"""The push-to-talk loop: hold a key, speak, hear the answer.

    talk_start  -> Recorder (PCM16 mono 16 kHz)           state "recording"
    talk_stop   -> Parakeet transcribes (worker thread)    state "transcribing"
                -> chat.send(text): an ordinary message    state "thinking"
    chat.delta  -> SpeechSession: sentences -> Chatterbox  state "speaking"
                -> Player (immediate stop)                 state "idle"

The controller knows nothing about ChatForge's internals: it gets a chat port (``send`` / ``cancel``),
a settings getter and a :class:`Deps` bundle (recorder, player, speech engines), all replaced by fakes
in the tests. Engines load on first use, in worker threads, and are dropped again after the configured
idle time. ``on_event`` runs on ChatForge's core loop thread, so it only queues text.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("chatforge_voice.controller")

MIN_SPEECH_S = 0.35
BUSY = ("recording", "transcribing")


@dataclass
class Deps:
    """Everything the loop uses from the outside world."""

    recorder_factory: Callable[[Any, Callable[[], None]], Any]
    player_factory: Callable[[Any], Any]
    stt_factory: Callable[[Any], Any]  # -> engine with transcribe(float32 array) -> str
    tts_factory: Callable[[Any], Any]  # -> engine for tts.speak (sample_rate, stream)
    speak: Callable[
        ..., Any
    ]  # tts.speak: (chunks, engine=, stop=) -> async iterator of PCM16 bytes
    missing: Callable[[Any], list[str]]  # what stops voice from working (empty = ready)
    unload: Callable[[], None] = lambda: None
    clock: Callable[[], float] = time.monotonic


def default_deps() -> Deps:
    """The real thing: sounddevice and forge-voice-core's engines."""
    from forge_voice_core import profile_bridge, registry, stt, tts

    from .audio_io import Player, Recorder

    def unload() -> None:
        for fn in (profile_bridge.release_engine, registry.run_release_hooks):
            try:
                fn()
            except Exception:  # noqa: BLE001 - best effort
                log.exception("voice: releasing an engine failed")
        for mod in (stt, tts):
            with mod._ENGINES_LOCK:
                mod._ENGINES.clear()

    return Deps(
        recorder_factory=lambda s, on_limit: Recorder(
            device=s.input_device, max_seconds=s.max_record_s, on_limit=on_limit
        ),
        player_factory=lambda s: Player(device=s.output_device),
        stt_factory=stt.shared_engine,
        tts_factory=tts.shared_engine,
        speak=tts.speak,
        missing=lambda s: s.missing(),
        unload=unload,
    )


class SpeechSession:
    """Speaks one reply: text goes in with ``feed``, sentences come out of the speakers."""

    def __init__(self, ctl: VoiceController, request_id: str) -> None:
        self.ctl = ctl
        self.request_id = request_id
        self.q: queue.Queue[str | None] = queue.Queue()
        self.stop_event = threading.Event()
        self.player: Any = None
        self.fed = False
        self.ended = False
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self._main()), name="chatforge-voice-speak", daemon=True
        )
        self.thread.start()

    def feed(self, text: str) -> None:
        if text and not self.ended and not self.stop_event.is_set():
            self.fed = True
            self.q.put(text)

    def end(self, final_text: str = "") -> None:
        """The reply is complete. A reply that never streamed is spoken whole from ``final_text``."""
        if self.ended:
            return
        self.ended = True
        if not self.fed and final_text and not self.stop_event.is_set():
            self.fed = True
            self.q.put(final_text)
        self.q.put(None)

    def stop(self) -> None:
        self.ended = True
        self.stop_event.set()
        player = self.player
        if player is not None:
            player.stop()
        self.q.put(None)

    async def _chunks(self):
        while True:
            item = await asyncio.to_thread(self.q.get)
            if item is None:
                return
            yield item

    async def _main(self) -> None:
        ctl = self.ctl
        player = None
        try:
            engine = await asyncio.to_thread(ctl.tts_engine)
            if self.stop_event.is_set():
                return
            player = self.ctl.deps.player_factory(ctl.settings())
            self.player = player
            started = False
            async for pcm in ctl.deps.speak(self._chunks(), engine=engine, stop=self.stop_event):
                if self.stop_event.is_set():
                    break
                if not started:
                    player.start(getattr(engine, "sample_rate", 24_000))
                    started = True
                    ctl._set_state("speaking", "Speaking.")
                player.write(pcm)
            if started and not self.stop_event.is_set():
                await asyncio.to_thread(player.finish, 120)
        except Exception as exc:  # noqa: BLE001 - shown to the person, never raised into ChatForge
            log.exception("voice: speaking failed")
            ctl._fail("The spoken reply failed", exc)
        finally:
            if player is not None:
                player.stop()
            ctl._session_finished(self)


class VoiceController:
    def __init__(
        self,
        chat: Any,
        settings: Callable[[], Any],
        deps: Deps,
        *,
        on_status: Callable[[dict], None] | None = None,
    ) -> None:
        self.chat = chat
        self._settings = settings
        self.deps = deps
        self._on_status = on_status
        self._lock = threading.RLock()
        self._load_lock = threading.Lock()
        self.state = "idle"
        self.message = ""
        self.heard = ""
        self._recorder: Any = None
        self._record_started = 0.0
        self._stt: Any = None
        self._tts: Any = None
        self._last_used = deps.clock()
        self._voice_rids: set[str] = set()
        self._open_rids: set[str] = set()
        self._voice_pending = False
        self._sessions: dict[str, SpeechSession] = {}
        self._seen_rids: set[str] = set()
        self._ended: set[str] = set()  # replies already finished or stopped: never spoken twice
        self._jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._job_thread: threading.Thread | None = None
        self._closed = False

    # ------------------------------------------------------------------ settings and status

    def settings(self) -> Any:
        return self._settings()

    def status(self) -> dict:
        s = self.settings()
        with self._lock:
            return {
                "enabled": bool(s.enabled),
                "state": self.state if s.enabled else "off",
                "message": self.message,
                "heard": self.heard,
                "notices": list(s.notices),
                "hotkey": s.hotkey,
                "loaded": {"stt": self._stt is not None, "tts": self._tts is not None},
            }

    def _set_state(self, state: str, message: str = "") -> None:
        with self._lock:
            self.state, self.message = state, message
        self._last_used = self.deps.clock()
        if self._on_status is not None:
            try:
                self._on_status(self.status())
            except Exception:  # noqa: BLE001
                log.exception("voice: status listener failed")

    def _fail(self, what: str, exc: BaseException | None = None) -> None:
        detail = f": {exc}" if exc is not None and str(exc) else ""
        self._set_state("idle", f"{what}{detail}"[:240])

    # ------------------------------------------------------------------ the worker

    def _submit(self, job: Callable[[], None]) -> None:
        with self._lock:
            if self._job_thread is None:
                self._job_thread = threading.Thread(
                    target=self._job_loop, name="chatforge-voice-worker", daemon=True
                )
                self._job_thread.start()
        self._jobs.put(job)

    def _job_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception as exc:  # noqa: BLE001
                log.exception("voice: job failed")
                self._fail("Voice failed", exc)

    # ------------------------------------------------------------------ engines (lazy)

    def stt_engine(self) -> Any:
        with self._load_lock:
            if self._stt is None:
                self._set_state(
                    self.state, "Loading the speech recogniser (first use takes a while)."
                )
                self._stt = self.deps.stt_factory(self.settings())
            self._last_used = self.deps.clock()
            return self._stt

    def tts_engine(self) -> Any:
        with self._load_lock:
            if self._tts is None:
                self._set_state(self.state, "Loading the voice (first use takes a while).")
                self._tts = self.deps.tts_factory(self.settings())
            self._last_used = self.deps.clock()
            return self._tts

    def unload_models(self) -> None:
        with self._load_lock:
            self._stt = self._tts = None
            try:
                self.deps.unload()
            except Exception:  # noqa: BLE001
                log.exception("voice: unload failed")

    def maintain(self) -> None:
        """Called every few seconds: free the models when voice is off or has been idle long enough."""
        s = self.settings()
        if self._stt is None and self._tts is None:
            return
        with self._lock:
            busy = self.state != "idle" or bool(self._sessions)
        if busy:
            return
        idle = self.deps.clock() - self._last_used
        if not s.enabled or (s.idle_unload_minutes > 0 and idle > s.idle_unload_minutes * 60):
            self.unload_models()
            self._set_state("idle", "")

    # ------------------------------------------------------------------ talking

    def talk_start(self) -> bool:
        s = self.settings()
        if not s.enabled:
            self._set_state("idle", "Voice is off. Turn on Voice (CPU) in Settings.")
            return False
        with self._lock:
            if self.state == "recording":
                return True
            if self.state == "transcribing":
                self.message = "Still working on what you just said."
                return False
        missing = self.deps.missing(s)
        if missing:
            self._set_state(
                "idle", "The voice models are not downloaded yet. Open the Voice panel."
            )
            return False
        if self.state in ("thinking", "speaking"):
            self.stop_all()  # talking over a reply stops it (barge-in by hand)
        try:
            rec = self.deps.recorder_factory(s, self._limit_reached)
            rec.start()
        except Exception as exc:  # noqa: BLE001 - AudioError text is meant for people
            log.warning("voice: microphone failed: %s", type(exc).__name__)
            self._fail("Could not use the microphone", exc)
            return False
        with self._lock:
            self._recorder = rec
            self._record_started = self.deps.clock()
        self._set_state("recording", "Listening. Release the key when you are done.")
        return True

    def _limit_reached(self) -> None:
        self.talk_stop()

    def talk_stop(self) -> None:
        with self._lock:
            rec, self._recorder = self._recorder, None
            if rec is None or self.state != "recording":
                return
        pcm = rec.stop()
        seconds = len(pcm) / (2 * 16_000)
        if seconds < MIN_SPEECH_S:
            self._set_state("idle", "That was too short. Hold the key while you speak.")
            return
        self._set_state("transcribing", "Transcribing.")
        self._submit(lambda: self._transcribe_and_send(pcm))

    def talk_cancel(self) -> None:
        with self._lock:
            rec, self._recorder = self._recorder, None
        if rec is not None:
            rec.abort()
            self._set_state("idle", "")

    def _transcribe_and_send(self, pcm: bytes) -> None:
        import numpy as np

        try:
            engine = self.stt_engine()
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            text = str(engine.transcribe(audio) or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.exception("voice: transcription failed")
            self._fail("Could not transcribe that", exc)
            return
        if not text:
            self._set_state("idle", "I did not catch that.")
            return
        self.heard = text
        with self._lock:
            self._voice_pending = True
        try:
            rid = self.chat.send(text)
        except Exception as exc:  # noqa: BLE001
            rid, err = None, exc
        else:
            err = None
        with self._lock:
            self._voice_pending = False
            if not rid:
                pass
            else:
                self._voice_rids.add(rid)
                self._open_rids.add(rid)
        if not rid:
            self._fail("ChatForge did not take the message", err)
            return
        self._set_state("thinking", "Waiting for the answer.")

    # ------------------------------------------------------------------ ChatForge events

    def on_event(self, evt: dict[str, Any]) -> None:
        """Every event the page hears. Runs on the core loop: queue and return."""
        etype, rid = evt.get("type"), str(evt.get("request_id") or "")
        if not rid:
            return
        if etype == "chat.start":
            with self._lock:
                self._seen_rids.add(rid)
                if self._voice_pending and rid not in self._voice_rids:
                    self._voice_rids.add(rid)
                    self._voice_pending = False
                if rid in self._voice_rids:
                    self._open_rids.add(rid)
            return
        if etype == "chat.delta" and evt.get("content"):
            sess = self._session_for(rid)
            if sess is not None:
                sess.feed(str(evt["content"]))
        elif etype == "chat.done":
            self._reply_end(rid, str(evt.get("content") or ""), ok=True)
        elif etype == "chat.error":
            if evt.get("code") == "cancelled":
                self._abort_reply(rid)
            else:
                self._reply_end(rid, "", ok=False, message=str(evt.get("message") or ""))

    def on_turn(self, turn: Any) -> None:
        """A turn finished (also reported by ``chat.done``; ending twice is harmless)."""
        rid = str(turn.request_id)
        if turn.ok:
            self._reply_end(rid, str(turn.reply_text or ""), ok=True)
        elif turn.error_code == "cancelled":
            self._abort_reply(rid)
        else:
            self._reply_end(rid, "", ok=False, message="")

    def _wants_speech(self, rid: str) -> bool:
        mode = self.settings().speak_replies
        if not self.settings().enabled or mode == "off":
            return False
        return mode == "always" or rid in self._voice_rids

    def _session_for(self, rid: str) -> SpeechSession | None:
        with self._lock:
            sess = self._sessions.get(rid)
            if sess is not None:
                return sess
            if self._closed or rid in self._ended or not self._wants_speech(rid):
                return None
            sess = self._sessions[rid] = SpeechSession(self, rid)
            self._open_rids.add(rid)
            return sess

    def _mark_ended(self, rid: str) -> None:
        self._ended.add(rid)
        if len(self._ended) > 200:
            self._ended = {rid}

    def _reply_end(self, rid: str, final_text: str, *, ok: bool, message: str = "") -> None:
        with self._lock:
            if rid in self._ended:
                return
            known = rid in self._voice_rids or rid in self._sessions
            self._open_rids.discard(rid)
            sess = self._sessions.get(rid)
        if sess is None and ok and final_text and self._wants_speech(rid):
            sess = self._session_for(rid)
        with self._lock:
            self._mark_ended(rid)
        if sess is not None:
            sess.end(final_text if ok else "")
            return
        if known and self.state in ("thinking",):
            self._set_state("idle", "" if ok else (message or "ChatForge could not answer."))

    def _abort_reply(self, rid: str) -> None:
        with self._lock:
            sess = self._sessions.get(rid)
            self._open_rids.discard(rid)
            self._mark_ended(rid)
        if sess is not None:
            sess.stop()
        elif rid in self._voice_rids and self.state == "thinking":
            self._set_state("idle", "Stopped.")

    def _session_finished(self, sess: SpeechSession) -> None:
        with self._lock:
            self._sessions.pop(sess.request_id, None)
            self._voice_rids.discard(sess.request_id)
            idle = not self._sessions and self.state in ("speaking", "thinking")
        if idle:
            self._set_state("idle", "")

    # ------------------------------------------------------------------ stop

    def stop_all(self) -> None:
        """The Stop button: silence at once, drop what is queued, and stop the answer being written."""
        with self._lock:
            rec, self._recorder = self._recorder, None
            sessions = list(self._sessions.values())
            rids = set(self._open_rids)
        if rec is not None:
            rec.abort()
        for sess in sessions:
            sess.stop()
            rids.add(sess.request_id)
        with self._lock:
            for rid in rids:
                self._mark_ended(rid)
        for rid in rids:
            try:
                self.chat.cancel(rid)
            except Exception:  # noqa: BLE001
                log.exception("voice: cancel failed")
        with self._lock:
            self._open_rids.clear()
            self._voice_pending = False
        self._set_state("idle", "Stopped.")

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self.stop_all()
        self._jobs.put(None)
        self.unload_models()
