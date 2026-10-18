"""The ChatForge side of forge-voice-core's host seam (``forge_voice_core.hostlink.HostLink``).

Everything the engines ask their host for is answered from ChatForge's own data folder
(``<ChatForge home>/extensions/voice``), the add-on's settings and the system credential store.
Nothing here touches a user-level default of the engine package (no ``~/.config``, no
``%LOCALAPPDATA%\\forge-voice``).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_voice_core.hostlink import HostLink

from . import __version__
from .secrets_store import KeyringSecretStore

MODELS_ENV = "CHATFORGE_VOICE_MODELS_DIR"


class ChatForgeHost(HostLink):
    client_name = "chatforge-voice"
    client_version = __version__

    def __init__(
        self,
        data_dir: Path,
        settings: Callable[[], Any],
        secrets: Any | None = None,
    ) -> None:
        self._data = Path(data_dir)
        self._settings = settings
        self._secrets = secrets if secrets is not None else KeyringSecretStore()

    # folders ---------------------------------------------------------------
    def data_dir(self) -> Path:
        return self._data

    def models_root(self) -> Path:
        explicit = os.environ.get(MODELS_ENV, "").strip()
        return Path(explicit).expanduser() if explicit else self._data / "models"

    def voices_dir(self) -> Path:
        return self._data / "voices"

    def state_dir(self) -> Path:
        return self._data / "state"

    def legacy_token_path(self) -> Path:
        # There is no pre-ChatForge token file to read; point at a name that never exists
        # rather than at the engine package's home-folder default.
        return self.state_dir() / "no-legacy-token"

    # settings and identity ---------------------------------------------------
    def load_settings(self) -> Any:
        return self._settings()

    def settings_schema_json(self) -> dict:
        from .settings import SCHEMA

        return SCHEMA.schema_json()

    # credentials -------------------------------------------------------------
    def secret_store(self) -> Any:
        return self._secrets
