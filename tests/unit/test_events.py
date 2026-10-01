"""EventSink: delta coalescing, batching and delivery."""

from __future__ import annotations

import json
import threading
import time

from chatforge.desktop.events import EventSink, batch_script, coalesce


def _delta(rid: str, **fields: str) -> dict:
    return {"type": "chat.delta", "request_id": rid, **fields}


def test_coalesce_merges_consecutive_deltas_of_one_request() -> None:
    events = [
        _delta("r1", content="Hel"),
        _delta("r1", content="lo"),
        _delta("r1", reasoning="think"),
        _delta("r1", content="!", reasoning="ing"),
    ]
    merged = coalesce(events)
    assert merged == [
        {"type": "chat.delta", "request_id": "r1", "content": "Hello!", "reasoning": "thinking"}
    ]


def test_coalesce_keeps_other_events_and_order() -> None:
    events = [
        {"type": "chat.start", "request_id": "r1"},
        _delta("r1", content="a"),
        _delta("r1", content="b"),
        {"type": "chat.phase", "request_id": "r1", "phase": "calling_tool"},
        _delta("r1", content="c"),
        {"type": "chat.done", "request_id": "r1"},
    ]
    merged = coalesce(events)
    assert [e["type"] for e in merged] == [
        "chat.start",
        "chat.delta",
        "chat.phase",
        "chat.delta",
        "chat.done",
    ]
    assert merged[1]["content"] == "ab"
    assert merged[3]["content"] == "c"


def test_coalesce_does_not_mix_requests() -> None:
    merged = coalesce(
        [_delta("r1", content="a"), _delta("r2", content="b"), _delta("r1", content="c")]
    )
    assert [(e["request_id"], e["content"]) for e in merged] == [
        ("r1", "a"),
        ("r2", "b"),
        ("r1", "c"),
    ]


def test_coalesce_does_not_mutate_input() -> None:
    first = _delta("r1", content="a")
    coalesce([first, _delta("r1", content="b")])
    assert first["content"] == "a"


def test_batch_script_is_guarded_and_carries_json() -> None:
    script = batch_script([{"type": "popup.shown"}, {"type": "chat.done", "request_id": "r"}])
    assert script.startswith("(function(evs){if(!window.__chatforge")
    assert "eval" not in script
    payload = script[script.index("})(") + 3 : -1]
    assert json.loads(payload) == [
        {"type": "popup.shown"},
        {"type": "chat.done", "request_id": "r"},
    ]


def _events_from(script: str) -> list[dict]:
    return json.loads(script[script.index("})(") + 3 : -1])


def test_sink_batches_within_window_and_coalesces() -> None:
    delivered: list[str] = []
    got = threading.Event()

    def deliver(script: str) -> None:
        delivered.append(script)
        got.set()

    sink = EventSink(deliver=deliver, coalesce_ms=50).start()
    try:
        sink.emit({"type": "chat.start", "request_id": "r1"})
        for piece in "hello":
            sink.emit(_delta("r1", content=piece))
        sink.emit({"type": "chat.done", "request_id": "r1"})
        assert got.wait(2)
        time.sleep(0.15)
    finally:
        sink.stop()
    events = [e for s in delivered for e in _events_from(s)]
    types = [e["type"] for e in events]
    assert types == ["chat.start", "chat.delta", "chat.done"]
    assert events[1]["content"] == "hello"
    assert sink.batches_sent == 1


def test_sink_flushes_separately_after_the_window() -> None:
    delivered: list[str] = []
    sink = EventSink(deliver=delivered.append, coalesce_ms=30).start()
    try:
        sink.emit(_delta("r1", content="a"))
        time.sleep(0.2)
        sink.emit(_delta("r1", content="b"))
        time.sleep(0.2)
    finally:
        sink.stop()
    assert len(delivered) == 2
    assert [_events_from(s)[0]["content"] for s in delivered] == ["a", "b"]


def test_sink_delivers_to_attached_windows_and_survives_a_dead_one() -> None:
    class Window:
        def __init__(self, fail: bool = False) -> None:
            self.scripts: list[str] = []
            self.fail = fail

        def run_js(self, script: str) -> None:
            if self.fail:
                raise RuntimeError("window gone")
            self.scripts.append(script)

    good, bad = Window(), Window(fail=True)
    sink = EventSink(coalesce_ms=10)
    sink.attach(bad)
    sink.attach(good)
    sink.attach(good)  # idempotent
    sink.dispatch([{"type": "popup.shown"}])
    assert len(good.scripts) == 1
    assert _events_from(good.scripts[0]) == [{"type": "popup.shown"}]
    sink.detach(bad)
    sink.detach(bad)  # no error
    assert sink.windows() == [good]


def test_malformed_events_are_dropped() -> None:
    delivered: list[str] = []
    sink = EventSink(deliver=delivered.append, coalesce_ms=10)
    sink.emit("nope")  # type: ignore[arg-type]
    sink.emit({"no": "type"})
    assert sink._queue.empty()  # noqa: SLF001


def test_emitter_helper_sets_type() -> None:
    delivered: list[str] = []
    sink = EventSink(deliver=delivered.append, coalesce_ms=10)
    cb = sink.emitter("runtime.status")
    cb({"state": "ready"}, model_id="m")
    sink.dispatch([sink._queue.get_nowait()])  # noqa: SLF001
    assert _events_from(delivered[0]) == [
        {"state": "ready", "model_id": "m", "type": "runtime.status"}
    ]
