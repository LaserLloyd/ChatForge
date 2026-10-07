"""The static server: MIME types, CSP, nosniff, no-cache and path confinement."""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from chatforge.desktop.webserver import (
    CSP,
    WEB_ROOT,
    StaticServer,
    content_type_for,
    resolve_request_path,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    web = tmp_path / "web"
    (web / "static" / "js").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (web / "settings.html").write_text("<!doctype html>", encoding="utf-8")
    (web / "static" / "js" / "app.js").write_text("export const x = 1;", encoding="utf-8")
    (web / "static" / "style.css").write_text("body{}", encoding="utf-8")
    (web / "static" / "icon.svg").write_text("<svg/>", encoding="utf-8")
    (web / "static" / "data.bin").write_bytes(b"\x00\x01")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    return web


@pytest.fixture
def server(root: Path):
    with StaticServer(root) as srv:
        yield srv


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - loopback
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_js_is_text_javascript(server: StaticServer) -> None:
    status, headers, body = _get(server.url("static/js/app.js"))
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    assert body == b"export const x = 1;"


def test_headers_on_every_response(server: StaticServer) -> None:
    for path in ("index.html", "static/style.css", "does-not-exist.html"):
        _status, headers, _body = _get(server.url(path))
        assert headers["Cache-Control"] == "no-cache"
        assert headers["Content-Security-Policy"] == CSP
        assert headers["X-Content-Type-Options"] == "nosniff"


def test_csp_policy_contents() -> None:
    assert "object-src 'none'" in CSP
    assert "script-src 'self'" in CSP
    assert "frame-ancestors 'none'" in CSP
    assert "unsafe-eval" not in CSP


def test_mime_map(server: StaticServer) -> None:
    assert _get(server.url("static/style.css"))[1]["Content-Type"].startswith("text/css")
    assert _get(server.url("static/icon.svg"))[1]["Content-Type"] == "image/svg+xml"
    assert _get(server.url("static/data.bin"))[1]["Content-Type"] == "application/octet-stream"
    assert _get(server.url("index.html"))[1]["Content-Type"].startswith("text/html")
    assert content_type_for("x.mjs").startswith("text/javascript")


def test_root_serves_index_and_query_is_ignored(server: StaticServer) -> None:
    assert _get(server.base_url + "/")[0] == 200
    status, headers, _ = _get(server.url("static/js/app.js?v=13"))
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")


def test_missing_and_directories_are_404(server: StaticServer) -> None:
    assert _get(server.url("nope.html"))[0] == 404
    assert _get(server.url("static"))[0] == 404
    assert _get(server.url("static/"))[0] == 404


def test_path_confinement(server: StaticServer, root: Path) -> None:
    # urllib normalises '..' itself, so send the raw request over a socket.
    import socket

    def raw(target: str) -> int:
        with socket.create_connection((server.host, server.port), timeout=5) as sock:
            sock.sendall(
                f"GET {target} HTTP/1.0\r\nHost: {server.host}:{server.port}\r\n\r\n".encode()
            )
            data = sock.recv(4096)
        return int(data.split(b" ")[1])

    assert raw("/../secret.txt") == 404
    assert raw("/static/../../secret.txt") == 404
    assert raw("/%2e%2e/secret.txt") == 404
    assert raw("/..%5csecret.txt") == 404
    assert raw("/static/js/app.js") == 200


def test_resolve_request_path_pure(root: Path) -> None:
    assert resolve_request_path(root, "/") == (root / "index.html").resolve()
    assert (
        resolve_request_path(root, "/static/js/app.js?v=1") == (root / "static/js/app.js").resolve()
    )
    assert resolve_request_path(root, "/../secret.txt") is None
    assert resolve_request_path(root, "/static/..%2F..%2Fsecret.txt") is None
    assert resolve_request_path(root, "/static\\js\\app.js") is None
    assert resolve_request_path(root, "/static") is None
    assert resolve_request_path(root, "/nope") is None


def test_only_get_and_head(server: StaticServer) -> None:
    req = urllib.request.Request(server.url("index.html"), data=b"x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310
    assert exc_info.value.code == 405
    head = urllib.request.Request(server.url("index.html"), method="HEAD")
    with urllib.request.urlopen(head, timeout=5) as resp:  # noqa: S310
        assert resp.status == 200
        assert resp.read() == b""


def test_real_web_root_exists_and_serves_modules() -> None:
    assert (WEB_ROOT / "index.html").is_file()
    with StaticServer(WEB_ROOT) as srv:
        status, headers, _ = _get(srv.url("static/js/bridge.js"))
        assert status == 200
        assert headers["Content-Type"].startswith("text/javascript")
        assert _get(srv.url("index.html"))[0] == 200
        assert _get(srv.url("settings.html"))[0] == 200


def test_missing_root_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        StaticServer(tmp_path / "missing")


# --- Host header (DNS rebinding) --------------------------------------------------------


def _raw(server: StaticServer, request: bytes) -> bytes:
    import socket

    with socket.create_connection((server.host, server.port), timeout=5) as sock:
        sock.sendall(request)
        chunks = []
        while data := sock.recv(65536):
            chunks.append(data)
    return b"".join(chunks)


def _status(reply: bytes) -> int:
    return int(reply.split(b" ", 2)[1])


def test_host_header_must_be_loopback_and_this_port(server: StaticServer) -> None:
    def get(host: str | None, version: str = "1.1") -> int:
        lines = [f"GET /index.html HTTP/{version}"]
        if host is not None:
            lines.append(f"Host: {host}")
        lines.append("Connection: close")
        return _status(_raw(server, ("\r\n".join(lines) + "\r\n\r\n").encode()))

    assert get(f"127.0.0.1:{server.port}") == 200
    assert get(f"LOCALHOST:{server.port}") == 200
    assert get("evil.example") == 421
    assert get(f"evil.example:{server.port}") == 421
    assert get("127.0.0.1:1") == 421
    assert get("127.0.0.1") == 421  # no port: not this server's origin
    assert get(f"127.0.0.1.evil.example:{server.port}") == 421
    assert get(None, "1.1") == 421
    assert get(None, "1.0") == 200  # HTTP/1.0 may omit Host


def test_misdirected_host_is_refused_for_every_method(server: StaticServer) -> None:
    for method in ("GET", "HEAD", "POST", "OPTIONS"):
        reply = _raw(
            server,
            f"{method} /index.html HTTP/1.1\r\nHost: evil.example\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n".encode(),
        )
        assert _status(reply) == 421, method
        if method != "HEAD":
            assert b"index.html" not in reply.split(b"\r\n\r\n", 1)[1]
