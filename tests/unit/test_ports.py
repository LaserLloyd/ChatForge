"""Port selection for the OVMS child."""

from __future__ import annotations

import os
import socket

import pytest

from aichat.runtime import ports


def _hold(port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen()
    return sock


def test_free_port_is_bindable_and_silent():
    sock = _hold()
    port = sock.getsockname()[1]
    sock.close()
    assert ports.port_is_bindable(port)
    assert not ports.port_has_listener(port, timeout_s=0.2)


def test_held_port_is_not_bindable_and_has_listener():
    sock = _hold()
    try:
        port = sock.getsockname()[1]
        assert not ports.port_is_bindable(port)
        assert ports.port_has_listener(port, timeout_s=0.5)
    finally:
        sock.close()


def test_pick_free_port_skips_held_ports():
    first = _hold()
    base = first.getsockname()[1]
    first.close()
    held = _hold(base)
    try:
        picked = ports.pick_free_port(base, 20)
        assert picked != base
        assert base < picked < base + 20
    finally:
        held.close()


def test_pick_free_port_honours_exclude():
    sock = _hold()
    base = sock.getsockname()[1]
    sock.close()
    picked = ports.pick_free_port(base, 20, exclude={base})
    assert picked != base


def test_pick_free_port_raises_when_range_exhausted(monkeypatch):
    monkeypatch.setattr(ports, "port_is_bindable", lambda port, host="127.0.0.1": False)
    with pytest.raises(ports.NoFreePortError, match="18611-18613"):
        ports.pick_free_port(18611, 3)


def test_find_port_holder_never_raises(monkeypatch):
    def boom(kind="inet"):
        raise ports.psutil.AccessDenied()

    monkeypatch.setattr(ports.psutil, "net_connections", boom)
    holder = ports.find_port_holder(18611)
    assert holder.pid is None
    assert "unidentified" in holder.describe()


def test_find_port_holder_identifies_own_listener():
    sock = _hold()
    try:
        holder = ports.find_port_holder(sock.getsockname()[1])
    finally:
        sock.close()
    # net_connections can need elevation on some systems; when it works it names us.
    if holder.pid is not None:
        assert holder.pid == os.getpid()
        assert not holder.is_ovms
