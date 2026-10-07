"""Tiny static file server for the popup and settings pages (PLAN 0, 1.9, WS7 step 2).

Why not ``file://``: WebView2 blocks ES modules there. Why not pywebview's bottle server:
the Windows registry can map ``.js`` to ``text/plain`` (which breaks modules) and the
ui-theme bundle wants ``no-cache``. So: a stdlib ``ThreadingHTTPServer`` bound to
``127.0.0.1:0``, rooted at ``src/chatforge/web``, with

* an explicit MIME map (``.js`` is always ``text/javascript``),
* ``Cache-Control: no-cache`` on everything,
* the §1.9 CSP plus ``object-src 'none'`` (§7 item 6) and ``X-Content-Type-Options: nosniff``,
* a Host check against DNS rebinding: a request whose ``Host`` is not this server's own
  loopback address and port (``127.0.0.1:<port>``, ``localhost:<port>``) is answered 421;
  a missing ``Host`` is accepted only for HTTP/1.0,
* path confinement: every request resolves to a regular file strictly inside the root, or
  it is a 404. Query strings are ignored,
* each page's ``ui-theme.js`` tag carrying ChatForge's theme settings (desktop/theme.py).

``python -m chatforge.desktop.webserver [--port 8765]`` serves the pages the same way for
working on the UI in a normal browser (against ``dev-mock.js``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import posixpath
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from chatforge.desktop import theme

_log = logging.getLogger(__name__)

#: The web root inside the package.
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'; object-src 'none'"
)

#: Explicit MIME map. Anything else is served as ``application/octet-stream``.
MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
}

_INDEX = "index.html"


def content_type_for(path: str | os.PathLike[str]) -> str:
    """MIME type for a file name, from the explicit map only."""
    ext = os.path.splitext(str(path))[1].lower()
    return MIME_TYPES.get(ext, "application/octet-stream")


def resolve_request_path(root: Path, raw_path: str) -> Path | None:
    """Map a request target to a file under ``root``, or ``None`` when it must be refused.

    Pure and testable: decodes the path, drops the query string, collapses ``.``/``..``
    segments POSIX-style, then requires the resolved file to be a regular file strictly
    inside the resolved root. ``/`` serves ``index.html``. Symlinks or junctions leading
    out of the root are refused because the *resolved* path is what is checked.
    """
    path = urlsplit(raw_path).path
    path = unquote(path)
    if "\x00" in path or "\\" in path:
        return None
    # posixpath.normpath keeps a leading '..' only when it escapes the root; the
    # relative_to check below refuses that case regardless. Leading slashes are
    # collapsed first because normpath preserves a POSIX "//" prefix.
    norm = posixpath.normpath("/" + path.lstrip("/"))
    if norm == "/":
        norm = "/" + _INDEX
    rel = norm.lstrip("/")
    parts = [p for p in rel.split("/") if p]
    if any(p in ("..", ".") or p.startswith("~") for p in parts):
        return None
    try:
        root_resolved = root.resolve(strict=True)
        candidate = root_resolved.joinpath(*parts).resolve(strict=True)
        candidate.relative_to(root_resolved)
    except (OSError, ValueError, RuntimeError):
        return None
    if candidate == root_resolved or not candidate.is_file():
        return None
    return candidate


class _Handler(BaseHTTPRequestHandler):
    server_version = "ChatForgeStatic/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Set by StaticServer.
    root: Path = WEB_ROOT

    def allowed_hosts(self) -> frozenset[str]:
        port = int(self.server.server_address[1])
        host = str(self.server.server_address[0])
        return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"{host}:{port}"})

    def host_allowed(self) -> bool:
        """True for this server's own ``Host`` (any case); a missing one only on HTTP/1.0."""
        host = self.headers.get("Host")
        if host is None:
            return self.request_version == "HTTP/1.0"
        return host.strip().lower() in self.allowed_hosts()

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if self.host_allowed():
            return True
        body = b"Misdirected request"
        self.send_response(HTTPStatus.MISDIRECTED_REQUEST)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True
        return False

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
        _log.debug("static %s", format % args)

    def _headers(self, status: HTTPStatus, ctype: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()

    def _serve(self, *, head: bool) -> None:
        target = resolve_request_path(self.root, self.path)
        if target is None:
            body = b"Not found"
            self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", len(body))
            if not head:
                self.wfile.write(body)
            return
        try:
            data = target.read_bytes()
        except OSError:
            body = b"Not found"
            self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", len(body))
            if not head:
                self.wfile.write(body)
            return
        if target.suffix.lower() in (".html", ".htm"):
            data = theme.apply_to_page(data)
        self._headers(HTTPStatus.OK, content_type_for(target), len(data))
        if not head:
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._serve(head=False)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        self._serve(head=True)

    def _refuse(self) -> None:
        # Drain a small request body so the reply is not raced by a closed socket.
        with contextlib.suppress(ValueError, OSError):
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= 65536:
                self.rfile.read(length)
        body = b"Method not allowed"
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse


class StaticServer:
    """A loopback static server on a random port, running on its own daemon thread."""

    def __init__(self, root: Path | str = WEB_ROOT, host: str = "127.0.0.1", port: int = 0):
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"web root not found: {self.root}")
        handler = type("_RootedHandler", (_Handler,), {"root": self.root})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def start(self) -> StaticServer:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                kwargs={"poll_interval": 0.25},
                name="chatforge-static",
                daemon=True,
            )
            self._thread.start()
            _log.info("static server listening on %s (root %s)", self.base_url, self.root)
        return self

    def stop(self) -> None:
        if self._thread is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)
        self._thread = None

    def __enter__(self) -> StaticServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    """Serve the pages for UI work in a normal browser (they fall back to dev-mock.js)."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m chatforge.desktop.webserver")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = StaticServer(port=args.port).start()
    print(f"Serving {server.root} on {server.url('index.html')} (Ctrl+C stops)")
    try:
        while True:  # a sleep, not Event.wait(): only that lets Ctrl+C through on Windows
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
