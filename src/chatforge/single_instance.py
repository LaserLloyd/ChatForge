"""One running copy of ChatForge, and a way for a second launch to wake the first.

# Adapted from StudioForge src/studioforge/tray/tray_app.py `acquire_single_instance` (MIT, LaserLloyd)

The first instance binds ``127.0.0.1:47831`` (the mutex) and runs an accept thread. A
second instance fails to bind, connects, sends ``SHOW\\n`` and exits; the first invokes its
``on_show`` callback (show the popup).
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
from collections.abc import Callable

SINGLE_INSTANCE_HOST = "127.0.0.1"
SINGLE_INSTANCE_PORT = 47831
SHOW_MESSAGE = b"SHOW\n"

_log = logging.getLogger(__name__)


def acquire_single_instance(
    port: int = SINGLE_INSTANCE_PORT, host: str = SINGLE_INSTANCE_HOST
) -> socket.socket | None:
    """Bind the mutex socket; ``None`` when another instance already holds it.

    ``SO_REUSEADDR`` is deliberately *not* set: on Windows it would let a second process
    bind the same address and defeat the whole guard.
    """
    # Adapted from StudioForge src/studioforge/tray/tray_app.py `acquire_single_instance` (MIT, LaserLloyd)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.listen(4)
    except OSError:
        with contextlib.suppress(OSError):
            sock.close()
        return None
    return sock


def signal_existing(
    port: int = SINGLE_INSTANCE_PORT, host: str = SINGLE_INSTANCE_HOST, timeout_s: float = 2.0
) -> bool:
    """Ask the running instance to show itself. ``True`` if the message was delivered."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as conn:
            conn.sendall(SHOW_MESSAGE)
    except OSError:
        return False
    return True


class SingleInstanceServer:
    """Accept thread on the mutex socket; calls ``on_show`` for every ``SHOW`` message."""

    def __init__(self, sock: socket.socket, on_show: Callable[[], None]) -> None:
        self._sock = sock
        self._on_show = on_show
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._sock.getsockname()[1])

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._serve, name="chatforge-single-instance", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the accept thread and release the port."""
        self._stop.set()
        # Closing a listening socket does not always wake a blocked accept() on Windows;
        # a throwaway connection does.
        with contextlib.suppress(OSError):
            host, port = self._sock.getsockname()[:2]
            socket.create_connection((host, port), timeout=0.5).close()
        with contextlib.suppress(OSError):
            self._sock.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            if self._stop.is_set():
                with contextlib.suppress(OSError):
                    conn.close()
                return
            try:
                self._handle(conn)
            except Exception:  # noqa: BLE001 - a bad client must never kill the accept loop
                _log.exception("single-instance client handling failed")

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(2.0)
            data = b""
            while b"\n" not in data and len(data) < 64:
                try:
                    chunk = conn.recv(64)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
        if data.strip().upper() == b"SHOW":
            try:
                self._on_show()
            except Exception:  # noqa: BLE001
                _log.exception("on_show callback failed")


def start_server(sock: socket.socket, on_show: Callable[[], None]) -> SingleInstanceServer:
    """Start the accept thread on an already-acquired mutex socket."""
    server = SingleInstanceServer(sock, on_show)
    server.start()
    return server
