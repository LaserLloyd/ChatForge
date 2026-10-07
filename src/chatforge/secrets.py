"""API key storage: environment variable first, then the Windows Credential Manager.

Keyring service ``ChatForge``, username = provider id. Keys are never written to config,
logs or JS; every key that passes through here is registered with the log redactor.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal

import keyring
import keyring.errors

from chatforge import legacy
from chatforge.errors import AppError
from chatforge.logging_setup import register_secret

SERVICE = "ChatForge"
MAX_KEY_LEN = 512

KeySource = Literal["env", "keyring", "none"]

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

_log = logging.getLogger(__name__)


class InvalidKeyError(AppError, ValueError):
    """The key text is not acceptable (empty, multi-line, control characters, too long)."""

    code = "invalid_key"


class KeyringUnavailableError(AppError):
    """The OS credential store could not be used."""

    code = "keyring_unavailable"
    hint = "Set the provider's API key environment variable instead."
    action = "open_settings"


def _env_key(env_name: str | None) -> str | None:
    if not env_name or not _ENV_NAME_RE.match(env_name):
        return None
    value = os.environ.get(env_name, "").strip()
    return value or None


def _keyring_get(provider_id: str) -> str | None:
    try:
        value = keyring.get_password(SERVICE, provider_id)
    except Exception as exc:  # noqa: BLE001 - no backend / locked vault: behave as "no key"
        # Log the exception type only: some backends echo the secret in messages.
        _log.warning("keyring read failed for provider %s: %s", provider_id, type(exc).__name__)
        return None
    if value is None:
        # A key saved before the app was renamed moves to the ChatForge entry.
        value = legacy.migrate_key(SERVICE, provider_id, keyring)
    if value is None:
        return None
    value = value.strip()
    if value:
        register_secret(value)  # every read path, not only ``get_api_key``
    return value or None


def get_api_key(provider_id: str, env_name: str | None) -> tuple[str | None, KeySource]:
    """Resolve a provider's key. The env var (``env_name``) wins over the keyring."""
    env_value = _env_key(env_name)
    if env_value is not None:
        register_secret(env_value)
        return env_value, "env"
    saved = _keyring_get(provider_id)
    if saved is not None:
        register_secret(saved)
        return saved, "keyring"
    return None, "none"


def validate_key(key: str) -> str:
    """Return the stripped key, or raise :class:`InvalidKeyError`."""
    if not isinstance(key, str):
        raise InvalidKeyError("The API key must be text.")
    cleaned = key.strip()
    if not cleaned:
        raise InvalidKeyError("The API key is empty.", hint="Paste the key and try again.")
    if any(ch in cleaned for ch in "\r\n") or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise InvalidKeyError(
            "The API key contains a line break or control character.",
            hint="Paste only the key itself, on a single line.",
        )
    if len(cleaned) > MAX_KEY_LEN:
        raise InvalidKeyError(
            f"The API key is longer than {MAX_KEY_LEN} characters.",
            hint="Check that you copied only the key.",
        )
    return cleaned


def set_api_key(provider_id: str, key: str) -> None:
    """Validate and store a key in the keyring. The key is registered for log redaction."""
    cleaned = validate_key(key)
    register_secret(cleaned)
    try:
        keyring.set_password(SERVICE, provider_id, cleaned)
    except keyring.errors.KeyringError as exc:
        raise KeyringUnavailableError(
            f"Could not save the key in the credential store ({type(exc).__name__})."
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise KeyringUnavailableError(
            f"Could not save the key in the credential store ({type(exc).__name__})."
        ) from None


def delete_api_key(provider_id: str) -> bool:
    """Remove the saved key. ``True`` if one was deleted, ``False`` if there was none."""
    old = legacy.delete_legacy_key(provider_id, keyring)  # a copy from before the rename
    try:
        keyring.delete_password(SERVICE, provider_id)
    except keyring.errors.PasswordDeleteError:
        return old
    except Exception as exc:  # noqa: BLE001
        _log.warning("keyring delete failed for provider %s: %s", provider_id, type(exc).__name__)
        return old
    return True


def key_status(provider_id: str, env_name: str | None) -> dict[str, Any]:
    """``{"source", "env_name", "env_overrides_saved"}``; never contains the key itself."""
    _value, source = get_api_key(provider_id, env_name)
    overrides = source == "env" and _keyring_get(provider_id) is not None
    return {"source": source, "env_name": env_name, "env_overrides_saved": overrides}
