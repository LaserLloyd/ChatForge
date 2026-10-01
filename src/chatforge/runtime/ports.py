"""Loopback port selection for the OVMS child.

Adapted from StudioForge src/studioforge/core/ports.py (MIT, LaserLloyd):
``port_is_bindable``, ``port_has_listener`` and ``find_port_holder`` are lifted
with their platform reasoning intact. The watchdog, adoption, respawn and
LM Studio hint machinery are cut: ChatForge has exactly one child on one port.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterable
from dataclasses import dataclass

import psutil

#: OVMS always binds loopback only (no firewall prompt, which needs admin).
CHILD_HOST = "127.0.0.1"
#: First port tried for the OVMS REST server (PLAN §1.7: "first bindable from 18611").
DEFAULT_PORT_START = 18611
#: How many consecutive ports are tried before giving up.
DEFAULT_PORT_SPAN = 100


class NoFreePortError(RuntimeError):
    """Every port in the scanned range is taken."""


# Adapted from StudioForge src/studioforge/core/ports.py port_has_listener (MIT, LaserLloyd)
def port_has_listener(port: int, host: str = CHILD_HOST, timeout_s: float = 1.0) -> bool:
    """Whether something ACCEPTS connections on ``host:port`` right now.

    The complement of :func:`port_is_bindable`, and not the same question: on
    Windows a bind to ``127.0.0.1:port`` can succeed while another socket holds
    the wildcard address, so to learn whether a listener exists, connect to it.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            # TCP "simultaneous open": probing a silent port inside the ephemeral
            # range can hand the client that very port as its source, and the
            # socket connects to itself. That is not a listener.
            return sock.getsockname() != sock.getpeername()
    except OSError:
        return False


# Adapted from StudioForge src/studioforge/core/ports.py port_is_bindable (MIT, LaserLloyd)
def port_is_bindable(port: int, host: str = CHILD_HOST) -> bool:
    """Whether ``host:port`` can be bound right now by the server we will start.

    * **Windows** -- set ``SO_EXCLUSIVEADDRUSE``. Without it a second bind
      against a listener that set ``SO_REUSEADDR`` succeeds, so the probe would
      call a busy port free. The danger here is a false *free*.
    * **POSIX** -- set ``SO_REUSEADDR``, because a port whose previous listener
      left connections in ``TIME_WAIT`` is one a real server takes happily. The
      danger there is a false *busy*.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            with contextlib.suppress(OSError, AttributeError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            with contextlib.suppress(OSError, AttributeError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


@dataclass(slots=True)
class PortHolder:
    """Best-effort identity of whatever listens on a port."""

    pid: int | None = None
    name: str | None = None
    cmdline: str = ""

    @property
    def is_ovms(self) -> bool:
        return (self.name or "").lower() in ("ovms.exe", "ovms")

    def describe(self) -> str:
        if self.pid is None:
            return "an unidentified process"
        return f"{self.name or 'process'} (pid {self.pid})"


# Adapted from StudioForge src/studioforge/core/ports.py find_port_holder (MIT, LaserLloyd)
def find_port_holder(port: int) -> PortHolder:
    """Identify the process listening on ``port``. Never raises.

    ``psutil.net_connections`` may refuse without elevation; that degrades to
    an empty :class:`PortHolder`, never to a traceback.
    """
    holder = PortHolder()
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError, RuntimeError):
        return holder
    for conn in connections:
        laddr = conn.laddr
        if not laddr or getattr(laddr, "port", None) != port:
            continue
        if conn.status not in (psutil.CONN_LISTEN, psutil.CONN_NONE):
            continue
        holder.pid = conn.pid
        break
    if holder.pid is None:
        return holder
    try:
        proc = psutil.Process(holder.pid)
        holder.name = proc.name()
        with contextlib.suppress(psutil.Error):
            holder.cmdline = " ".join(proc.cmdline())
    except (psutil.Error, ValueError):
        pass
    return holder


def pick_free_port(
    start: int = DEFAULT_PORT_START,
    span: int = DEFAULT_PORT_SPAN,
    *,
    host: str = CHILD_HOST,
    exclude: Iterable[int] = (),
) -> int:
    """The first port in ``[start, start + span)`` that is bindable and silent.

    Both checks are needed (see :func:`port_has_listener`): bindable alone can
    be fooled by a wildcard listener on Windows. ``exclude`` lets a caller skip
    a port that just failed to bind inside the child.
    """
    skip = set(exclude)
    for port in range(start, min(start + span, 65536)):
        if port in skip:
            continue
        if port_is_bindable(port, host) and not port_has_listener(port, host, timeout_s=0.2):
            return port
    raise NoFreePortError(
        f"No free loopback port in {start}-{start + span - 1} for the local model server."
    )
