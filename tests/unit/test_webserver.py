"""The static server: MIME types, CSP, nosniff, no-cache and path confinement."""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aichat.desktop.webserver import (
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
            sock.sendall(f"GET {target} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
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
