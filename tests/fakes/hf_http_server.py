"""A real loopback HTTP server in front of :class:`tests.fakes.hf_api.FakeHfApi`.

``FakeHfApi`` answers ``httpx.MockTransport`` requests in memory. This puts the same
answers behind a socket, so the *real* ``httpx`` client, redirects, ``Range`` resumes and
the endpoint setting (``hf.endpoint``) are exercised end to end. It binds ``127.0.0.1:0``
only; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from tests.fakes.hf_api import FakeHfApi


def _make_handler(fake: FakeHfApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
            return

        def _serve(self, head_only: bool) -> None:
            host = self.headers.get("Host", "127.0.0.1")
            request = httpx.Request(
                self.command,
                f"http://{host}{self.path}",
                headers={k: v for k, v in self.headers.items()},
            )
            response = asyncio.run(fake.handler(request))
            response.read()
            body = response.content
            self.send_response(response.status_code)
            skip = {"content-length", "transfer-encoding", "connection"}
            for key, value in response.headers.items():
                if key.lower() not in skip:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._serve(False)

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(True)

    return Handler


@contextmanager
def serve_fake_hf(fake: FakeHfApi) -> Iterator[str]:
    """Serve ``fake`` on a loopback port; yields the endpoint URL (``http://127.0.0.1:N``)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(fake))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="fake-hf", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
