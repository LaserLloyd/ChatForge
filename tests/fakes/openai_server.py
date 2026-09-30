"""A scripted, stdlib-only fake OpenAI-compatible server for tests.

Usage::

    from tests.fakes.openai_server import fake_openai_server, sse, text_chunks

    with fake_openai_server(sse(text_chunks("Hello there", 3))) as srv:
        client = OpenAICompatClient(srv.base_url, "sk-test", quirks=GenericQuirks())
        ...
        body = srv.chat_requests[-1].json      # what the client sent

Every ``POST …/chat/completions`` pops the next scripted reply (a :class:`Reply`, or
a callable ``(CapturedRequest) -> Reply``). With the queue empty, ``default_reply`` is
used (a short "ok" stream). ``GET …/models`` serves ``models`` (``None`` means 404).
Requests are captured in ``srv.requests`` (method, path, headers, raw body, parsed JSON).

Ready-made scenarios are the ``scenario_*`` functions at the bottom.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --------------------------------------------------------------------------- #
# Replies
# --------------------------------------------------------------------------- #


@dataclass
class Reply:
    """One scripted HTTP answer.

    ``kind == "sse"``: ``events`` are written as ``data: <json>\\n\\n`` frames (a str
    event is written verbatim after ``data: ``, so ``"[DONE]"`` works). ``split_bytes``
    writes each frame in pieces of that many bytes, flushing between them, so a frame
    straddles reads. ``stall_after`` stops writing after that many events and holds
    the connection open for ``stall_s`` seconds (or until the server stops).
    ``kind == "raw"``: ``body`` is written as-is with ``content_type``.
    """

    kind: str = "sse"
    status: int = 200
    events: list[Any] = field(default_factory=list)
    body: bytes = b""
    content_type: str = "text/event-stream"
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0  # before the status line
    event_delay_s: float = 0.0  # between events
    split_bytes: int | None = None
    stall_after: int | None = None
    stall_s: float = 30.0
    done: bool = True  # append ``data: [DONE]``


def sse(events: list[Any], **kw: Any) -> Reply:
    return Reply(kind="sse", events=list(events), **kw)


def json_reply(obj: Any, status: int = 200, **kw: Any) -> Reply:
    return Reply(
        kind="raw",
        status=status,
        body=json.dumps(obj).encode(),
        content_type="application/json",
        **kw,
    )


def raw_reply(body: bytes | str, status: int = 200, content_type: str = "text/plain", **kw: Any):
    data = body.encode() if isinstance(body, str) else body
    return Reply(kind="raw", status=status, body=data, content_type=content_type, **kw)


# --------------------------------------------------------------------------- #
# Chunk builders
# --------------------------------------------------------------------------- #


def chunk(
    delta: dict[str, Any] | None = None,
    *,
    finish_reason: str | None = None,
    model: str = "fake-model",
    usage: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One ``chat.completion.chunk``."""
    obj: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        obj["usage"] = usage
    obj.update(extra)
    return obj


def usage_chunk(prompt: int = 10, completion: int = 5, model: str = "fake-model") -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def text_chunks(
    text: str, pieces: int = 3, *, finish_reason: str = "stop", model: str = "fake-model"
) -> list[dict]:
    """``text`` split into ``pieces`` content deltas (mid-word), a finish chunk, usage."""
    n = max(1, pieces)
    size = max(1, -(-len(text) // n))
    parts = [text[i : i + size] for i in range(0, len(text), size)] or [""]
    out = [chunk({"role": "assistant", "content": parts[0]}, model=model)]
    out += [chunk({"content": p}, model=model) for p in parts[1:]]
    out.append(chunk({}, finish_reason=finish_reason, model=model))
    out.append(usage_chunk(completion=max(2, len(parts)), model=model))
    return out


def tool_call_chunks(
    calls: list[tuple[str, str, str]], *, content: str = "", model: str = "fake-model"
) -> list[dict]:
    """Tool calls ``[(id, name, arguments_json), ...]`` streamed the awkward way: the
    name split in two, arguments in 3 pieces, and calls interleaved by index."""
    out: list[dict] = []
    if content:
        out.append(chunk({"role": "assistant", "content": content}, model=model))
    for i, (cid, name, _args) in enumerate(calls):
        half = max(1, len(name) // 2)
        out.append(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": cid,
                            "type": "function",
                            "function": {"name": name[:half], "arguments": ""},
                        }
                    ]
                },
                model=model,
            )
        )
        out.append(
            chunk({"tool_calls": [{"index": i, "function": {"name": name[half:]}}]}, model=model)
        )
    # Arguments interleaved across indexes, 3 pieces each.
    pieces = []
    for i, (_cid, _name, args) in enumerate(calls):
        size = max(1, -(-len(args) // 3))
        pieces.append([(i, args[j : j + size]) for j in range(0, len(args), size)])
    for step in range(max((len(p) for p in pieces), default=0)):
        for p in pieces:
            if step < len(p):
                i, frag = p[step]
                out.append(
                    chunk(
                        {"tool_calls": [{"index": i, "function": {"arguments": frag}}]},
                        model=model,
                    )
                )
    out.append(chunk({}, finish_reason="tool_calls", model=model))
    out.append(usage_chunk(model=model))
    return out


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #


@dataclass
class CapturedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    json: Any


ReplySource = Reply | Callable[[CapturedRequest], Reply]


def _default_reply() -> Reply:
    return sse(text_chunks("ok", 1))


class FakeOpenAIServer:
    """Threaded fake. Use :func:`fake_openai_server` rather than constructing directly."""

    def __init__(
        self,
        replies: tuple[ReplySource, ...] = (),
        *,
        models: Sequence[str] | None = ("fake-model",),
        models_reply: Reply | None = None,
        prefix: str = "/v1",
        default_reply: Callable[[], Reply] = _default_reply,
    ) -> None:
        self.models = list(models) if models is not None else None
        self.models_reply = models_reply
        self.prefix = prefix.rstrip("/")
        self.default_reply = default_reply
        self.requests: list[CapturedRequest] = []
        self._queue: deque[ReplySource] = deque(replies)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- public API -------------------------------------------------------- #

    @property
    def base_url(self) -> str:
        assert self._httpd is not None, "server not started"
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}{self.prefix}"

    def enqueue(self, *replies: ReplySource) -> None:
        with self._lock:
            self._queue.extend(replies)

    @property
    def chat_requests(self) -> list[CapturedRequest]:
        with self._lock:
            return [r for r in self.requests if r.path.endswith("/chat/completions")]

    @property
    def last_body(self) -> Any:
        reqs = self.chat_requests
        return reqs[-1].json if reqs else None

    def start(self) -> FakeOpenAIServer:
        server = self

        class Handler(_Handler):
            fake = server

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- internals --------------------------------------------------------- #

    def _record(self, req: CapturedRequest) -> None:
        with self._lock:
            self.requests.append(req)

    def _next_chat_reply(self, req: CapturedRequest) -> Reply:
        with self._lock:
            src = self._queue.popleft() if self._queue else None
        if src is None:
            return self.default_reply()
        return src(req) if callable(src) else src


class _Handler(BaseHTTPRequestHandler):
    fake: FakeOpenAIServer
    protocol_version = "HTTP/1.0"  # close-delimited bodies; SSE needs no length

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        return

    def _capture(self) -> CapturedRequest:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        req = CapturedRequest(
            method=self.command,
            path=self.path,
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
            json=parsed,
        )
        self.fake._record(req)
        return req

    def do_GET(self) -> None:
        req = self._capture()
        if req.path.rstrip("/").endswith("/models"):
            if self.fake.models_reply is not None:
                self._send(self.fake.models_reply)
            elif self.fake.models is None:
                self._send(json_reply({"error": {"message": "not found"}}, status=404))
            else:
                data = [{"id": m, "object": "model"} for m in self.fake.models]
                self._send(json_reply({"object": "list", "data": data}))
            return
        self._send(json_reply({"error": {"message": "not found"}}, status=404))

    def do_POST(self) -> None:
        req = self._capture()
        if req.path.rstrip("/").endswith("/chat/completions"):
            self._send(self.fake._next_chat_reply(req))
            return
        self._send(json_reply({"error": {"message": "not found"}}, status=404))

    def _send(self, reply: Reply) -> None:
        try:
            if reply.delay_s:
                self.fake._stop.wait(reply.delay_s)
            self.send_response(reply.status)
            ctype = reply.content_type if reply.kind == "raw" else "text/event-stream"
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            for k, v in reply.headers.items():
                self.send_header(k, v)
            if reply.kind == "raw":
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)
                self.wfile.flush()
                return
            self.end_headers()
            self.wfile.flush()
            for i, ev in enumerate(reply.events):
                if reply.stall_after is not None and i >= reply.stall_after:
                    self.fake._stop.wait(reply.stall_s)
                    return
                payload = ev if isinstance(ev, str) else json.dumps(ev)
                self._write_frame(f"data: {payload}\n\n".encode(), reply.split_bytes)
                if reply.event_delay_s:
                    time.sleep(reply.event_delay_s)
            if reply.stall_after is not None and reply.stall_after >= len(reply.events):
                self.fake._stop.wait(reply.stall_s)
                return
            if reply.done:
                self._write_frame(b"data: [DONE]\n\n", None)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _write_frame(self, frame: bytes, split: int | None) -> None:
        if not split:
            self.wfile.write(frame)
            self.wfile.flush()
            return
        for i in range(0, len(frame), split):
            self.wfile.write(frame[i : i + split])
            self.wfile.flush()
            time.sleep(0.002)


@contextmanager
def fake_openai_server(
    *replies: ReplySource,
    models: list[str] | None = ["fake-model"],  # noqa: B006 - copied in the constructor
    models_reply: Reply | None = None,
    prefix: str = "/v1",
    default_reply: Callable[[], Reply] = _default_reply,
) -> Iterator[FakeOpenAIServer]:
    """Start a fake server on ``127.0.0.1:0``. Yields it (``.base_url``, ``.requests``,
    ``.chat_requests``, ``.last_body``, ``.enqueue()``); stops it on exit."""
    srv = FakeOpenAIServer(
        replies,
        models=models,
        models_reply=models_reply,
        prefix=prefix,
        default_reply=default_reply,
    ).start()
    try:
        yield srv
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
# Scenarios (PLAN §5 "LLM client")
# --------------------------------------------------------------------------- #


def scenario_split_content(text: str = "Hello, world! Streaming works.") -> Reply:
    """Content split mid-word, each SSE frame also split across TCP writes."""
    return sse(text_chunks(text, 5), split_bytes=7)


def scenario_tool_calls_split() -> Reply:
    return sse(
        tool_call_chunks(
            [
                ("call_a", "current_datetime", "{}"),
                ("call_b", "web_search", '{"query": "npu news", "max_results": 3}'),
            ]
        )
    )


def scenario_reasoning_content(reasoning: str = "Let me think.", answer: str = "42") -> Reply:
    return sse(
        [
            chunk({"role": "assistant", "reasoning_content": reasoning[:5]}),
            chunk({"reasoning_content": reasoning[5:]}),
            chunk({"content": answer}),
            chunk({}, finish_reason="stop"),
            usage_chunk(),
        ]
    )


def scenario_reasoning_details(answer: str = "Done.") -> Reply:
    """MiniMax ``reasoning_split`` shape: ``reasoning_details`` merged by index."""
    detail = {"type": "reasoning.text", "id": "reasoning-text-1", "format": "MiniMax-response-v1"}
    return sse(
        [
            chunk(
                {
                    "role": "assistant",
                    "reasoning_details": [{**detail, "index": 0, "text": "Step "}],
                }
            ),
            chunk({"reasoning_details": [{**detail, "index": 0, "text": "one."}]}),
            chunk({"content": answer}),
            chunk({}, finish_reason="stop"),
            usage_chunk(),
        ],
    )


def scenario_inline_think(answer: str = "Paris.") -> Reply:
    """``<think>`` inline in content, with the tags split across chunks."""
    return sse(
        [
            chunk({"role": "assistant", "content": "<thi"}),
            chunk({"content": "nk>The capital"}),
            chunk({"content": " of France.</th"}),
            chunk({"content": f"ink>{answer}"}),
            chunk({}, finish_reason="stop"),
            usage_chunk(),
        ]
    )


def scenario_error_frame(message: str = "upstream exploded", code: Any = "server_error") -> Reply:
    return sse(
        [
            chunk({"role": "assistant", "content": "partial"}),
            {"error": {"message": message, "code": code}},
        ],
        done=False,
    )


def scenario_base_resp(status_code: int = 2049, msg: str = "invalid api key") -> Reply:
    """HTTP 200, a non-SSE JSON body carrying a MiniMax ``base_resp`` error."""
    return json_reply(
        {"id": "", "choices": None, "base_resp": {"status_code": status_code, "status_msg": msg}}
    )


def scenario_429(retry_after: str = "7") -> Reply:
    return json_reply(
        {"error": {"message": "Too many requests", "type": "rate_limit_error"}},
        status=429,
        headers={"Retry-After": retry_after},
    )


def scenario_stall(after: int = 1, stall_s: float = 30.0) -> Reply:
    """Sends ``after`` content chunks, then goes silent with the connection open."""
    return sse(text_chunks("Stalling here", 3), stall_after=after, stall_s=stall_s)
