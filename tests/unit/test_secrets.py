"""secrets.py: keyring + env precedence, validation, no key in logs."""

import logging

import keyring
import pytest

from chatforge import secrets
from chatforge.errors import AppError
from chatforge.secrets import (
    SERVICE,
    InvalidKeyError,
    delete_api_key,
    get_api_key,
    key_status,
    set_api_key,
)

FAKE_KEY = "sk-test-0123456789abcdef"
OTHER_KEY = "sk-env-fedcba9876543210"


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)


def test_service_name():
    assert SERVICE == "ChatForge"


def test_set_and_get_from_keyring(fake_keyring):
    assert get_api_key("minimax", "MINIMAX_API_KEY") == (None, "none")
    set_api_key("minimax", FAKE_KEY)
    assert fake_keyring.get_password("ChatForge", "minimax") == FAKE_KEY
    assert get_api_key("minimax", "MINIMAX_API_KEY") == (FAKE_KEY, "keyring")


def test_key_is_stripped(fake_keyring):
    set_api_key("minimax", f"  {FAKE_KEY}\t ")
    assert fake_keyring.get_password("ChatForge", "minimax") == FAKE_KEY


def test_env_wins_over_keyring(fake_keyring, monkeypatch):
    set_api_key("minimax", FAKE_KEY)
    monkeypatch.setenv("MINIMAX_API_KEY", f" {OTHER_KEY} ")
    assert get_api_key("minimax", "MINIMAX_API_KEY") == (OTHER_KEY, "env")


def test_env_ignored_without_env_name_or_when_blank(fake_keyring, monkeypatch):
    set_api_key("minimax", FAKE_KEY)
    monkeypatch.setenv("MINIMAX_API_KEY", "   ")
    assert get_api_key("minimax", "MINIMAX_API_KEY") == (FAKE_KEY, "keyring")
    monkeypatch.setenv("MINIMAX_API_KEY", OTHER_KEY)
    assert get_api_key("minimax", None) == (FAKE_KEY, "keyring")
    assert get_api_key("minimax", "not a valid name") == (FAKE_KEY, "keyring")


def test_key_status_sources(fake_keyring, monkeypatch):
    assert key_status("minimax", "MINIMAX_API_KEY") == {
        "source": "none",
        "env_name": "MINIMAX_API_KEY",
        "env_overrides_saved": False,
    }
    set_api_key("minimax", FAKE_KEY)
    assert key_status("minimax", "MINIMAX_API_KEY") == {
        "source": "keyring",
        "env_name": "MINIMAX_API_KEY",
        "env_overrides_saved": False,
    }
    monkeypatch.setenv("MINIMAX_API_KEY", OTHER_KEY)
    assert key_status("minimax", "MINIMAX_API_KEY") == {
        "source": "env",
        "env_name": "MINIMAX_API_KEY",
        "env_overrides_saved": True,
    }


def test_env_only_does_not_claim_override(fake_keyring, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", OTHER_KEY)
    status = key_status("minimax", "MINIMAX_API_KEY")
    assert status["source"] == "env"
    assert status["env_overrides_saved"] is False


def test_key_status_never_contains_key(fake_keyring, monkeypatch):
    set_api_key("minimax", FAKE_KEY)
    monkeypatch.setenv("MINIMAX_API_KEY", OTHER_KEY)
    assert FAKE_KEY not in repr(key_status("minimax", "MINIMAX_API_KEY"))
    assert OTHER_KEY not in repr(key_status("minimax", "MINIMAX_API_KEY"))


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "\n", "abc\ndef", "abc\r\ndef", "abc\rdef", "ab\x00cd", "x" * 513, "abc\tdef"],
)
def test_rejects_bad_keys(fake_keyring, bad):
    with pytest.raises(InvalidKeyError) as exc:
        set_api_key("minimax", bad)
    assert isinstance(exc.value, AppError)
    assert isinstance(exc.value, ValueError)
    assert fake_keyring.get_password("ChatForge", "minimax") is None
    assert "x" * 20 not in str(exc.value)


def test_accepts_512_chars(fake_keyring):
    key = "k" * 512
    set_api_key("minimax", key)
    assert get_api_key("minimax", None)[0] == key


def test_delete(fake_keyring):
    assert delete_api_key("minimax") is False
    set_api_key("minimax", FAKE_KEY)
    assert delete_api_key("minimax") is True
    assert get_api_key("minimax", None) == (None, "none")
    assert delete_api_key("minimax") is False


def test_get_and_set_register_secret(fake_keyring, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(secrets, "register_secret", seen.append)
    set_api_key("minimax", FAKE_KEY)
    assert seen == [FAKE_KEY]
    get_api_key("minimax", None)
    assert seen[-1] == FAKE_KEY
    monkeypatch.setenv("MINIMAX_API_KEY", OTHER_KEY)
    get_api_key("minimax", "MINIMAX_API_KEY")
    assert seen[-1] == OTHER_KEY


def test_keyring_failure_reads_as_no_key(fake_keyring, monkeypatch, caplog):
    def boom(*_a):
        raise RuntimeError(f"backend exploded with {FAKE_KEY}")

    monkeypatch.setattr(keyring, "get_password", boom)
    with caplog.at_level(logging.WARNING):
        assert get_api_key("minimax", "MINIMAX_API_KEY") == (None, "none")
    assert FAKE_KEY not in caplog.text  # only the exception type is logged


def test_keyring_failure_on_set_is_an_app_error(fake_keyring, monkeypatch):
    def boom(*_a):
        raise keyring.errors.NoKeyringError(f"no backend {FAKE_KEY}")

    monkeypatch.setattr(keyring, "set_password", boom)
    with pytest.raises(AppError) as exc:
        set_api_key("minimax", FAKE_KEY)
    assert exc.value.code == "keyring_unavailable"
    assert FAKE_KEY not in str(exc.value)


def test_no_key_in_logs_from_secrets(fake_keyring, caplog):
    with caplog.at_level(logging.DEBUG):
        set_api_key("minimax", FAKE_KEY)
        get_api_key("minimax", "MINIMAX_API_KEY")
        key_status("minimax", "MINIMAX_API_KEY")
        delete_api_key("minimax")
    assert FAKE_KEY not in caplog.text
