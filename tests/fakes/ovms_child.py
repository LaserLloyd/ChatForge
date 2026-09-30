"""A stdlib stand-in for ``ovms.exe``, for supervisor/manager/engine tests.

Run it the way the supervisor runs OVMS, with the real flag names::

    python tests/fakes/ovms_child.py --rest_port 18611 --model_name M --model_path P ...

Use it from tests through :func:`command_prefix`, which is what
``OvmsSupervisor(command_prefix=...)`` expects (the supervisor drops its own
``exe`` and appends the OVMS arguments)::

    sup = OvmsSupervisor(exe, command_prefix=ovms_child.command_prefix())
    spec = LaunchSpec(..., extra_args=["--fake_load_s", "0.2"])

It reproduces what WS1 measured on the real OVMS 2026.4 (docs/RUNTIME-NOTES.md):

* ``/v2/health/live`` 200 as soon as REST is up; ``/v2/health/ready`` 503 and
  ``/v1/config`` ``{}`` while loading, then 200 and ``"state": "AVAILABLE"``.
* ``/v3/models`` (and ``/v1/models``) empty while loading, then the model.
* ``/v3/chat/completions`` (and ``/v1/...``) stream in OVMS's chunk shape: no
  initial ``role`` delta, a tool call as two chunks (``id``/``type``/``name``,
  then ``arguments``), ``finish_reason: "tool_calls"``, a final ``choices: []``
  chunk carrying ``usage`` when ``stream_options.include_usage`` is set, then
  ``[DONE]``. ``n > 1``, empty ``messages`` and an over-long prompt answer 400
  with OVMS's texts; a wrong model name, or any chat request while loading,
  answers 404 ``Mediapipe graph definition with requested name is not found``.
* ``/v3/tokenize`` ``{"model", "text"}`` -> ``{"tokens": [...]}``.
* A busy port prints ``Bind address failed`` and exits 1.

Fake-only flags (after the OVMS ones, via ``LaunchSpec.extra_args``), each also
settable with an env var ``FAKE_OVMS_<NAME>``:

``--fake_load_s S``        seconds of "compiling" before AVAILABLE (default 0.3)
``--fake_rest_delay_s S``  seconds before the REST server starts listening
``--fake_fail_load``       log LOADING_PRECONDITION_FAILED and exit 1 after loading
``--fake_crash_after_s S`` once ready, exit with ``--fake_exit_code`` after S seconds
``--fake_exit_code N``     exit code for a crash (default 3221226505 = 0xC0000409)
``--fake_grandchild``      spawn a sleeping grandchild (kill-tree tests)
``--fake_script PATH``     JSON list of scripted replies, served in order (last repeats):
                           ``{"content": "..."}`` or ``{"tool_calls": [{"name", "arguments"}]}``
                           (optional ``"reasoning"``, ``"finish_reason"``, ``"status"``+``"error"``)
``--fake_record PATH``     append every chat request body as a JSON line
``--fake_chunk_delay_s S`` sleep between streamed chunks (default 0)
``--fake_pidfile PATH``    write "<pid> <grandchild pid>" once started

Without a script it answers deterministically: a tool result -> ``"Tool said: <content>"``;
tools offered and the user mentions date/time -> a ``current_datetime`` call;
"2+2" -> ``"4"``; otherwise ``"Echo: <last user text>"``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()

OVERFLOW_ERROR = (
    "Mediapipe execution failed. MP status - INVALID_ARGUMENT: CalculatorGraph::Run() failed: \n"
    'Calculator::Process() for node "LLMExecutor" failed: Input length exceeds the maximum '
    "allowed length"
)
NOT_FOUND_ERROR = "Mediapipe graph definition with requested name is not found"
EMPTY_MESSAGES_ERROR = (
    "Mediapipe execution failed. MP status - INVALID_ARGUMENT: CalculatorGraph::Run() failed: \n"
    'Calculator::Process() for node "LLMExecutor" failed: Messages array cannot be empty'
)
N_ERROR = (
    "Mediapipe execution failed. MP status - INVALID_ARGUMENT: CalculatorGraph::Run() failed: \n"
    'Calculator::Process() for node "LLMExecutor" failed: n value cannot be greater than best_of'
)


def command_prefix() -> list[str]:
    """``[python, <this file>]`` -- pass as ``OvmsSupervisor(command_prefix=...)``."""
    return [sys.executable, str(HERE)]


def _log(msg: str, level: str = "info") -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}.000][{os.getpid()}][serving][{level}][fake_ovms] {msg}", flush=True)


def _env_default(name: str, default: Any) -> Any:
    return os.environ.get(f"FAKE_OVMS_{name.upper()}", default)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--rest_port", type=int, default=0)
    p.add_argument("--rest_bind_address", default="127.0.0.1")
    p.add_argument("--model_name", default="fake/model")
    p.add_argument("--model_path", default=".")
    p.add_argument("--target_device", default="NPU")
    p.add_argument("--max_prompt_len", type=int, default=4096)
    p.add_argument("--cache_dir", default=None)
    p.add_argument("--tool_parser", default=None)
    p.add_argument("--reasoning_parser", default=None)
    p.add_argument("--fake_load_s", type=float, default=float(_env_default("load_s", 0.3)))
    p.add_argument(
        "--fake_rest_delay_s", type=float, default=float(_env_default("rest_delay_s", 0))
    )
    p.add_argument(
        "--fake_fail_load", action="store_true", default=bool(_env_default("fail_load", ""))
    )
    p.add_argument(
        "--fake_crash_after_s", type=float, default=float(_env_default("crash_after_s", -1))
    )
    p.add_argument("--fake_exit_code", type=int, default=int(_env_default("exit_code", 3221226505)))
    p.add_argument(
        "--fake_grandchild", action="store_true", default=bool(_env_default("grandchild", ""))
    )
    p.add_argument("--fake_script", default=_env_default("script", None))
    p.add_argument("--fake_record", default=_env_default("record", None))
    p.add_argument(
        "--fake_chunk_delay_s", type=float, default=float(_env_default("chunk_delay_s", 0))
    )
    p.add_argument("--fake_pidfile", default=_env_default("pidfile", None))
    args, _unknown = p.parse_known_args(argv)
    return args


class FakeOvms:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.ready = threading.Event()
        self.created = int(time.time())
        self._script: list[dict[str, Any]] = []
        self._script_i = 0
        self._lock = threading.Lock()
        if args.fake_script:
            self._script = json.loads(Path(args.fake_script).read_text(encoding="utf-8"))

    # --- reply selection -------------------------------------------------

    def next_reply(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._script:
                reply = self._script[min(self._script_i, len(self._script) - 1)]
                self._script_i += 1
                return dict(reply)
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            return {"content": f"Tool said: {last.get('content', '')}"}
        text = str(last.get("content") or "")
        if body.get("tools") and any(w in text.lower() for w in ("date", "time", "today")):
            return {"tool_calls": [{"name": "current_datetime", "arguments": "{}"}]}
        if "2+2" in text.replace(" ", ""):
            return {"content": "4"}
        return {"content": f"Echo: {text}"}

    def prompt_tokens(self, body: dict[str, Any]) -> int:
        chars = sum(len(str(m.get("content") or "")) for m in body.get("messages") or [])
        chars += len(json.dumps(body.get("tools") or []))
        return max(1, chars // 3)


def make_handler(fake: FakeOvms) -> type[BaseHTTPRequestHandler]:
    args = fake.args

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *a: Any) -> None:  # quiet
            return

        def _send(self, status: int, payload: Any, ctype: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _models(self) -> dict[str, Any]:
            data = []
            if fake.ready.is_set():
                data = [
                    {
                        "id": args.model_name,
                        "object": "model",
                        "created": fake.created,
                        "owned_by": "OVMS",
                    }
                ]
            return {"data": data, "object": "list"}

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/v2/health/live":
                self._send(200, b"", "text/plain")
            elif path == "/v2/health/ready":
                if fake.ready.is_set():
                    self._send(200, b"", "text/plain")
                else:
                    self._send(503, {"error": "Server is not ready"})
            elif path == "/v1/config":
                if not fake.ready.is_set():
                    self._send(200, {})
                else:
                    self._send(
                        200,
                        {
                            args.model_name: {
                                "model_version_status": [
                                    {
                                        "version": "1",
                                        "state": "AVAILABLE",
                                        "status": {"error_code": "OK", "error_message": "OK"},
                                    }
                                ]
                            }
                        },
                    )
            elif path in ("/v3/models", "/v1/models"):
                self._send(200, self._models())
            else:
                self._send(400, {"error": "Invalid request URL"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                self._send(400, {"error": "The file is not valid json"})
                return
            if "model" not in body:
                self._send(
                    412,
                    {"error": "The file is not valid json - model field is missing in JSON body"},
                )
                return
            if path.endswith("/tokenize") and path.startswith("/v3"):
                text = body.get("text")
                if text is None:
                    self._send(400, {"error": "text field is required"})
                elif isinstance(text, list):
                    self._send(
                        200, {"tokens": [[hash(w) % 150000 for w in t.split()] for t in text]}
                    )
                else:
                    self._send(200, {"tokens": [hash(w) % 150000 for w in str(text).split()]})
                return
            if path not in ("/v3/chat/completions", "/v1/chat/completions"):
                self._send(400, {"error": "Invalid request URL"})
                return
            if body.get("model") != args.model_name or not fake.ready.is_set():
                # Real OVMS answers exactly this both for a wrong name and while loading.
                self._send(404, {"error": NOT_FOUND_ERROR})
                return
            if args.fake_record:
                with open(args.fake_record, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(body) + "\n")
            if not body.get("messages"):
                self._send(400, {"error": EMPTY_MESSAGES_ERROR})
                return
            if int(body.get("n") or 1) > 1:
                self._send(400, {"error": N_ERROR})
                return
            prompt_tokens = fake.prompt_tokens(body)
            if prompt_tokens > args.max_prompt_len:
                self._send(400, {"error": OVERFLOW_ERROR})
                return
            reply = fake.next_reply(body)
            if "status" in reply:
                self._send(int(reply["status"]), {"error": reply.get("error", "scripted error")})
                return
            if body.get("stream"):
                self._stream(body, reply, prompt_tokens)
            else:
                self._complete(reply, prompt_tokens)

        # --- chat bodies ---------------------------------------------------

        def _tool_calls(self, reply: dict[str, Any]) -> list[dict[str, Any]]:
            out = []
            for tc in reply.get("tool_calls") or []:
                out.append(
                    {
                        "id": tc.get("id") or uuid.uuid4().hex[:9],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc.get("arguments", "{}")},
                    }
                )
            return out

        def _finish(self, reply: dict[str, Any]) -> str:
            if reply.get("finish_reason"):
                return str(reply["finish_reason"])
            return "tool_calls" if reply.get("tool_calls") else "stop"

        def _usage(self, prompt_tokens: int, reply: dict[str, Any]) -> dict[str, int]:
            text = str(reply.get("content") or "") + str(reply.get("reasoning") or "")
            completion = max(1, len(text.split()) + 4 * len(reply.get("tool_calls") or []))
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion,
                "total_tokens": prompt_tokens + completion,
            }

        def _complete(self, reply: dict[str, Any], prompt_tokens: int) -> None:
            content = str(reply.get("content") or "")
            if reply.get("reasoning"):
                content = f"<think>{reply['reasoning']}</think>{content}"
            self._send(
                200,
                {
                    "choices": [
                        {
                            "finish_reason": self._finish(reply),
                            "index": 0,
                            "logprobs": None,
                            "message": {
                                "content": content,
                                "role": "assistant",
                                "tool_calls": self._tool_calls(reply),
                            },
                        }
                    ],
                    "created": int(time.time()),
                    "model": args.model_name,
                    "object": "chat.completion",
                    "usage": self._usage(prompt_tokens, reply),
                },
            )

        def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {
                "choices": [
                    {"index": 0, "logprobs": None, "delta": delta, "finish_reason": finish}
                ],
                "created": int(time.time()),
                "model": args.model_name,
                "object": "chat.completion.chunk",
                "usage": None,
            }

        def _stream(self, body: dict[str, Any], reply: dict[str, Any], prompt_tokens: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def emit(obj: Any) -> None:
                data = obj if isinstance(obj, str) else json.dumps(obj)
                payload = f"data: {data}\n\n".encode()
                self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
                if args.fake_chunk_delay_s:
                    time.sleep(args.fake_chunk_delay_s)

            try:
                if reply.get("reasoning"):
                    # OVMS emits the qwen3 parser's output as delta.reasoning_content.
                    for word in str(reply["reasoning"]).split(" "):
                        emit(self._chunk({"reasoning_content": word + " "}))
                content = str(reply.get("content") or "")
                if content:
                    pieces = [content[i : i + 4] for i in range(0, len(content), 4)]
                    for piece in pieces:
                        emit(self._chunk({"content": piece}))
                for index, tc in enumerate(self._tool_calls(reply)):
                    emit(
                        self._chunk(
                            {
                                "tool_calls": [
                                    {
                                        "id": tc["id"],
                                        "type": "function",
                                        "index": index,
                                        "function": {"name": tc["function"]["name"]},
                                    }
                                ]
                            }
                        )
                    )
                    emit(
                        self._chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "function": {"arguments": tc["function"]["arguments"]},
                                    }
                                ]
                            }
                        )
                    )
                emit(self._chunk({}, self._finish(reply)))
                if (body.get("stream_options") or {}).get("include_usage"):
                    emit(
                        {
                            "choices": [],
                            "created": int(time.time()),
                            "model": args.model_name,
                            "object": "chat.completion.chunk",
                            "usage": self._usage(prompt_tokens, reply),
                        }
                    )
                emit("[DONE]")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (ConnectionError, OSError):
                return

    return Handler


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    fake = FakeOvms(args)
    grandchild = None
    if args.fake_grandchild:
        grandchild = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if args.fake_pidfile:
        Path(args.fake_pidfile).write_text(
            f"{os.getpid()} {grandchild.pid if grandchild else ''}".strip(), encoding="utf-8"
        )
    _log("OpenVINO Model Server (fake) starting")
    if args.fake_rest_delay_s > 0:
        time.sleep(args.fake_rest_delay_s)
    _log(f"Binding REST server to address: {args.rest_bind_address}:{args.rest_port}")
    try:
        server = ThreadingHTTPServer((args.rest_bind_address, args.rest_port), make_handler(fake))
    except OSError:
        print(
            f"FATAL , Bind address failed at {args.rest_bind_address}:{args.rest_port}", flush=True
        )
        _log(f"Failed to start REST server - at {args.rest_bind_address}:{args.rest_port}", "error")
        return 1
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _log(f"REST server listening on port {args.rest_port}")
    _log(f"Graph config created in memory from model_path: {args.model_path}")
    time.sleep(max(0.0, args.fake_load_s))
    if args.fake_fail_load:
        _log(
            f"Mediapipe: {args.model_name} state changed to: LOADING_PRECONDITION_FAILED "
            "after handling: ValidationFailedEvent:",
            "error",
        )
        server.shutdown()
        return 1
    fake.ready.set()
    _log(
        f"Mediapipe: {args.model_name} state changed to: AVAILABLE after handling: "
        "ValidationPassedEvent:"
    )
    try:
        if args.fake_crash_after_s >= 0:
            time.sleep(args.fake_crash_after_s)
            _log("fake crash", "error")
            code = args.fake_exit_code
            if code > 0x7FFFFFFF:  # NTSTATUS as a signed C int for _exit()
                code -= 1 << 32
            os._exit(code)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        if grandchild is not None:
            grandchild.kill()


if __name__ == "__main__":
    sys.exit(main())
