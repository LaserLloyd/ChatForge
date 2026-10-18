"""Secrets in the operating system's credential store (Windows Credential Manager through ``keyring``).

``forge_voice_core`` asks its host for ``secret_store()`` (``get`` / ``set`` / ``delete``) and keeps
the optional GPU-rig token there first, so no token file is ever written. M1 does not use the rig, but
the store is wired now so the later milestone needs no change here. A store that cannot be reached
(no Secret Service on Linux, a locked vault) behaves as "nothing stored" and never raises into the
engines; only :meth:`set` reports failure, because losing a secret silently would be worse.
"""

from __future__ import annotations

import logging
from typing import Any

SERVICE = "ChatForge Voice"
log = logging.getLogger("chatforge_voice.secrets")


class KeyringSecretStore:
    def __init__(self, service: str = SERVICE, backend: Any = None) -> None:
        self.service = service
        self._backend = backend

    def _kr(self) -> Any:
        if self._backend is None:
            import keyring

            self._backend = keyring
        return self._backend

    def get(self, name: str) -> str | None:
        try:
            return self._kr().get_password(self.service, name) or None
        except Exception as exc:  # noqa: BLE001 - no backend / locked vault means "not stored"
            # The exception type only: some backends echo the secret in their message.
            log.warning("credential store read failed: %s", type(exc).__name__)
            return None

    def set(self, name: str, value: str) -> None:
        try:
            self._kr().set_password(self.service, name, value)
        except Exception as exc:
            raise RuntimeError(
                f"Could not save to the system credential store ({type(exc).__name__})."
            ) from None

    def delete(self, name: str) -> None:
        try:
            self._kr().delete_password(self.service, name)
        except Exception as exc:  # noqa: BLE001 - deleting what is not there is fine
            log.debug("credential store delete: %s", type(exc).__name__)
