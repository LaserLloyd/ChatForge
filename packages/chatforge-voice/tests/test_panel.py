"""The loopback Voice panel: its guards and its calls (real sockets on 127.0.0.1, random port)."""

from __future__ import annotations

import http.client
import json

import pytest
from conftest import FakeChat, FakeSTT, FakeTTS, make_deps, make_settings
from forge_voice_core import fetch_job

from chatforge_voice.controller import VoiceController
from chatforge_voice.models_card import ModelsService
from chatforge_voice.panel import PANEL_FILE, PanelServer


@pytest.fixture
def env(tmp_path):
    state = {"s": make_settings()}
    deps = make_deps(stt=FakeSTT(), tts=FakeTTS())
    ctl = VoiceController(FakeChat(), lambda: state["s"], deps)
    models = ModelsService(
        lambda: state["s"],
        job=fetch_job.FetchJob(run=lambda **kw: 0),
        sysreq_check=lambda: [],
        models_root=lambda: tmp_path,
        free_bytes=lambda p: 10**12,
    )
    panel = PanelServer(ctl, models, tmp_path / "data")
    panel.start()
    yield panel, state
    panel.stop()
    ctl.shutdown()


def call(panel, method, path, body=None, *, token=True, host=None, origin=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", panel.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{panel.port}"}
    if token is True:
        headers["X-Voice-Token"] = panel.token
    elif token:
        headers["X-Voice-Token"] = token
    if origin:
        headers["Origin"] = origin
    payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def test_it_listens_on_loopback_only(env):
    panel, _ = env
    assert panel._httpd.server_address[0] == "127.0.0.1" and panel.port > 0


def test_the_page_and_its_assets_are_served_with_a_strict_csp(env):
    panel, _ = env
    for path, ctype in (
        ("/", "text/html"),
        ("/panel.js", "text/javascript"),
        ("/panel.css", "text/css"),
    ):
        resp, data = call(panel, "GET", path, token=False)
        assert resp.status == 200 and resp.getheader("Content-Type").startswith(ctype) and data
        assert "default-src 'none'" in resp.getheader("Content-Security-Policy")
        assert resp.getheader("Cache-Control") == "no-store"
    assert b"innerHTML" not in call(panel, "GET", "/panel.js", token=False)[1]


def test_the_api_needs_the_token(env):
    panel, _ = env
    assert call(panel, "GET", "/api/status", token=False)[0].status == 401
    assert call(panel, "GET", "/api/status", token="wrong")[0].status == 401
    assert call(panel, "GET", "/api/status")[0].status == 200


def test_a_foreign_host_header_is_refused_dns_rebinding(env):
    panel, _ = env
    assert call(panel, "GET", "/api/status", host="evil.example:80")[0].status == 403
    assert call(panel, "GET", "/", host="evil.example", token=False)[0].status == 403
    assert call(panel, "GET", "/api/status", host=f"localhost:{panel.port}")[0].status == 200


def test_a_foreign_origin_is_refused(env):
    panel, _ = env
    assert call(panel, "POST", "/api/stop", {}, origin="https://evil.example")[0].status == 403
    assert (
        call(panel, "POST", "/api/stop", {}, origin=f"http://127.0.0.1:{panel.port}")[0].status
        == 200
    )


def test_status_shows_voice_and_models(env):
    panel, _ = env
    _, data = call(panel, "GET", "/api/status")
    body = json.loads(data)
    assert body["voice"]["state"] == "idle" and body["voice"]["enabled"] is True
    assert body["models"]["ready"] is False and "attribution" in body["models"]


def test_talk_is_refused_while_voice_is_off(env):
    panel, state = env
    state["s"] = make_settings(enabled=False)
    resp, data = call(panel, "POST", "/api/talk/start", {})
    assert resp.status == 409 and "Voice is off" in json.loads(data)["message"]


def test_talk_start_is_refused_politely_without_models(env):
    panel, _ = env
    panel.controller.deps.missing = lambda s: ["model x"]
    resp, data = call(panel, "POST", "/api/talk/start", {})
    body = json.loads(data)
    assert resp.status == 200 and body["ok"] is False and "Voice panel" in body["voice"]["message"]


def test_models_download_requires_the_explicit_confirmation(env):
    panel, _ = env
    resp, data = call(panel, "POST", "/api/models/download", {"confirm": "yes"})
    assert resp.status == 409 and json.loads(data)["code"] == "confirm_required"
    resp, data = call(panel, "POST", "/api/models/download", {"confirm": True})
    assert resp.status == 200 and json.loads(data) == {"ok": True}


def test_bad_bodies_and_unknown_calls(env):
    panel, _ = env
    assert call(panel, "POST", "/api/models/download", raw="{not json")[0].status == 400
    assert call(panel, "POST", "/api/models/download", raw="x" * 5000)[0].status == 413
    assert call(panel, "POST", "/api/nope", {})[0].status == 404
    assert call(panel, "GET", "/nope", token=False)[0].status == 404
    assert call(panel, "GET", "/api/stop")[0].status == 405


def test_the_connection_file_is_written_for_the_cli_and_removed_on_stop(env, tmp_path):
    panel, _ = env
    info = json.loads((tmp_path / "data" / PANEL_FILE).read_text("utf-8"))
    assert info["port"] == panel.port and info["token"] == panel.token
    assert panel.url == f"http://127.0.0.1:{panel.port}/#t={panel.token}"
    panel.stop()
    assert not (tmp_path / "data" / PANEL_FILE).exists()
