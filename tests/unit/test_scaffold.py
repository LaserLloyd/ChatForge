"""Test that the basic scaffold is in place."""

import os

import pytest

import aichat


def test_aichat_import():
    """Test that aichat can be imported."""
    assert aichat is not None


def test_aichat_version():
    """Test that aichat has a version."""
    assert hasattr(aichat, "__version__")
    assert aichat.__version__ == "0.1.0"


def test_fake_keyring(fake_keyring):
    """Test the fake keyring fixture."""
    import keyring.errors

    # Test set and get
    fake_keyring.set_password("test_service", "user1", "secret123")
    assert fake_keyring.get_password("test_service", "user1") == "secret123"

    # Test get non-existent
    assert fake_keyring.get_password("test_service", "user2") is None

    # Test delete
    fake_keyring.delete_password("test_service", "user1")
    assert fake_keyring.get_password("test_service", "user1") is None

    # Test delete non-existent raises
    with pytest.raises(keyring.errors.PasswordDeleteError):
        fake_keyring.delete_password("test_service", "user1")


def test_fake_clock(fake_clock):
    """Test the fake clock fixture."""
    # Test initial time
    assert fake_clock.now == 0.0
    assert fake_clock() == 0.0

    # Test advance
    fake_clock.advance(10.5)
    assert fake_clock.now == 10.5
    assert fake_clock() == 10.5

    # Test multiple advances
    fake_clock.advance(5)
    assert fake_clock.now == 15.5


@pytest.mark.asyncio
async def test_fake_clock_async_sleep(fake_clock):
    """Test the fake clock async sleep."""
    assert fake_clock.now == 0.0
    await fake_clock.sleep(10)
    assert fake_clock.now == 10.0


def test_aichat_home(aichat_home):
    """Test the aichat_home fixture."""
    assert os.getenv("AICHAT_HOME") == str(aichat_home)
    assert aichat_home.exists()
    assert aichat_home.is_dir()
