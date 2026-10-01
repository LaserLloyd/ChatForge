"""Pytest configuration and shared fixtures."""

import keyring
import pytest

from tests.fakes.clock import FakeClock
from tests.fakes.keyring_backend import FakeKeyringBackend


@pytest.fixture
def chatforge_home(tmp_path, monkeypatch):
    """Set CHATFORGE_HOME to a temporary directory."""
    home_dir = tmp_path / "chatforge_home"
    home_dir.mkdir()
    monkeypatch.setenv("CHATFORGE_HOME", str(home_dir))
    return home_dir


@pytest.fixture
def fake_keyring():
    """Install an in-memory keyring backend and restore the previous one after the test."""
    backend = FakeKeyringBackend()
    previous_backend = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous_backend)


@pytest.fixture
def fake_clock():
    """Create a fake clock for testing."""
    return FakeClock()
