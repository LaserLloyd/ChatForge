"""single_instance.py: mutex socket and the SHOW signal."""

import socket
import threading

import pytest

from aichat.single_instance import (
    SINGLE_INSTANCE_PORT,
    acquire_single_instance,
    signal_existing,
    start_server,
)


def test_default_port():
    assert SINGLE_INSTANCE_PORT == 47831


@pytest.fixture
def instance():
    sock = acquire_single_instance(port=0)
    assert sock is not None
    port = sock.getsockname()[1]
    shown = threading.Event()
    server = start_server(sock, shown.set)
    yield port, shown, server
    server.stop()


def test_second_acquire_fails_while_first_holds_port(instance):
    port, _shown, _server = instance
    assert acquire_single_instance(port=port) is None


def test_show_signal_invokes_callback(instance):
    port, shown, _server = instance
    assert signal_existing(port=port) is True
    assert shown.wait(3)


def test_signal_can_repeat(instance):
    port, _shown, _server = instance
    calls = []
    lock = threading.Event()

    def on_show():
        calls.append(1)
        if len(calls) == 3:
            lock.set()

    _server._on_show = on_show
    for _ in range(3):
        assert signal_existing(port=port)
    assert lock.wait(3)


def test_garbage_does_not_call_back_and_server_survives(instance):
    port, shown, _server = instance
    with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
        conn.sendall(b"HELLO\n")
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass  # connect and hang up without a word
    assert not shown.wait(0.3)
    assert signal_existing(port=port)
    assert shown.wait(3)


def test_callback_exception_does_not_kill_accept_loop(instance):
    port, _shown, server = instance
    seen = threading.Event()
    state = {"n": 0}

    def on_show():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")
        seen.set()

    server._on_show = on_show
    assert signal_existing(port=port)
    assert signal_existing(port=port)
    assert seen.wait(3)


def test_signal_without_instance_returns_false():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert signal_existing(port=port, timeout_s=0.5) is False


def test_stop_ends_accept_thread(tmp_path):
    sock = acquire_single_instance(port=0)
    assert sock is not None
    port = sock.getsockname()[1]
    server = start_server(sock, lambda: None)
    thread = server._thread
    assert thread is not None and thread.is_alive()
    server.stop()
    assert not thread.is_alive()
    assert signal_existing(port=port, timeout_s=0.5) is False
