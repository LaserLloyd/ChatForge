"""The Voice panel: a small page on 127.0.0.1 with the Models card, a mic button and Stop.

ChatForge's add-on seam today offers settings fields only (no buttons, no progress bars, no extra
popup controls), so the parts of M1 that need them live here, in a page served by the add-on:

* it listens on loopback only, on a random port, and the port and a random token are written to a
  private file (``panel.json``) that ``chatforge-voice panel`` reads to open the page;
* the token travels in the URL **fragment** (never sent to a server, never logged) and then in an
  ``X-Voice-Token`` header on every API call;
* ``Host`` must be the loopback address with this port (DNS-rebinding guard), a browser ``Origin`` must
  be this page, and responses carry a strict CSP and ``no-store``;
* nothing is served that holds a secret, and the talk endpoints refuse while Voice is switched off.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger("chatforge_voice.panel")

PANEL_FILE = "panel.json"
MAX_BODY = 4096
_ASSETS = {
    "/": ("panel.html", "text/html; charset=utf-8"),
    "/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
    "/panel.css": ("panel.css", "text/css; charset=utf-8"),
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _asset(name: str) -> bytes:
    return (resources.files("chatforge_voice") / "web" / name).read_bytes()


class PanelServer:
    def __init__(self, controller: Any, models: Any, data_dir: Path) -> None:
        self.controller = controller
        self.models = models
        self.data_dir = Path(data_dir)
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> int:
        if self._httpd is not None:
            return self.port
        panel = self

        class Handler(_Handler):
            owner = panel

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="chatforge-voice-panel", daemon=True
        )
        self._thread.start()
        self._write_info()
        return self.port

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        info = self.data_dir / PANEL_FILE
        with contextlib.suppress(OSError):
            info.unlink()

    def _write_info(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / PANEL_FILE
        path.write_text(
            json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}),
            encoding="utf-8",
        )
        try:
            from forge_voice_core import fsperm

            fsperm.restrict(path)
        except Exception:  # noqa: BLE001 - the file sits in the user's own data folder anyway
            log.debug("could not restrict %s", PANEL_FILE)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#t={self.token}"

    # ---- the API -----------------------------------------------------------
    def api(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        ctl, models = self.controller, self.models
        if method == "GET" and path == "/api/status":
            return 200, {"voice": ctl.status(), "models": models.status()}
        if method != "POST":
            return 405, {"ok": False, "message": "Not allowed."}
        if path in ("/api/talk/start", "/api/talk/stop") and not ctl.settings().enabled:
            return 409, {"ok": False, "message": "Voice is off. Turn on Voice (CPU) in Settings."}
        if path == "/api/talk/start":
            return 200, {"ok": bool(ctl.talk_start()), "voice": ctl.status()}
        if path == "/api/talk/stop":
            ctl.talk_stop()
            return 200, {"ok": True, "voice": ctl.status()}
        if path == "/api/stop":
            ctl.stop_all()
            return 200, {"ok": True, "voice": ctl.status()}
        if path == "/api/models/download":
            res = models.start(confirm=body.get("confirm") is True)
            return (200 if res.get("ok") else 409), res
        if path == "/api/models/cancel":
            return 200, models.cancel()
        return 404, {"ok": False, "message": "No such call."}


class _Handler(BaseHTTPRequestHandler):
    owner: PanelServer
    server_version = "ChatForgeVoice"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence the default stderr access log
        log.debug(fmt, *args)

    # ---- guards ------------------------------------------------------------
    def _host_ok(self) -> bool:
        port = self.owner.port
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.owner.port
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _token_ok(self) -> bool:
        sent = self.headers.get("X-Voice-Token", "")
        return hmac.compare_digest(sent.encode(), self.owner.token.encode())

    # ---- responses ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _handle(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        if not self._host_ok() or not self._origin_ok():
            self._json(403, {"ok": False, "message": "Forbidden."})
            return
        if method == "GET" and path in _ASSETS:
            name, ctype = _ASSETS[path]
            self._send(200, _asset(name), ctype)
            return
        if not path.startswith("/api/"):
            self._json(404, {"ok": False, "message": "Not found."})
            return
        if not self._token_ok():
            self._json(401, {"ok": False, "message": "Missing or wrong token."})
            return
        body: dict = {}
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self._json(413, {"ok": False, "message": "Body too large."})
                return
            if length:
                try:
                    parsed = json.loads(self.rfile.read(length) or b"{}")
                    body = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    self._json(400, {"ok": False, "message": "Bad JSON."})
                    return
        try:
            code, payload = self.owner.api(method, path, body)
        except Exception:  # noqa: BLE001
            log.exception("panel call failed")
            code, payload = 500, {"ok": False, "message": "Something went wrong."}
        self._json(code, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")
