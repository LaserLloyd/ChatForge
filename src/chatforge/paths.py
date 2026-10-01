"""Where ChatForge keeps its data (outside the repo).

# Adapted from StudioForge src/studioforge/config.py `default_data_dir` (MIT, LaserLloyd)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "ChatForge"
HOME_ENV = "CHATFORGE_HOME"
#: The home override from before the app was renamed (AI Chat -> ChatForge).
LEGACY_HOME_ENV = "AICHAT_HOME"


def home_overridden() -> bool:
    """True when the home folder comes from an environment variable."""
    return any((os.environ.get(n) or "").strip() for n in (HOME_ENV, LEGACY_HOME_ENV))


def app_home() -> Path:
    """``CHATFORGE_HOME`` (or the older ``AICHAT_HOME``) if set, else
    ``%LOCALAPPDATA%\\ChatForge`` (``~/.local/share/ChatForge`` elsewhere)."""
    # Adapted from StudioForge src/studioforge/config.py `default_data_dir` (MIT, LaserLloyd)
    env = os.environ.get(HOME_ENV) or os.environ.get(LEGACY_HOME_ENV)
    if env and env.strip():
        return Path(env.strip()).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base_path = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base_path / APP_NAME


@dataclass(frozen=True)
class Paths:
    """Every location the app reads or writes, derived from one home folder."""

    home: Path
    config_file: Path
    state_file: Path
    conversation_file: Path
    downloads_file: Path
    models_dir: Path
    runtime_dir: Path
    cache_dir: Path
    logs_dir: Path
    webview_dir: Path

    @classmethod
    def from_home(cls, home: Path | str) -> Paths:
        home = Path(home)
        return cls(
            home=home,
            config_file=home / "config.toml",
            state_file=home / "state.json",
            conversation_file=home / "conversation.json",
            downloads_file=home / "downloads.json",
            models_dir=home / "models",
            runtime_dir=home / "runtime",
            cache_dir=home / "cache",
            logs_dir=home / "logs",
            webview_dir=home / "webview",
        )

    @classmethod
    def default(cls) -> Paths:
        return cls.from_home(app_home())

    @property
    def app_log(self) -> Path:
        return self.logs_dir / "chatforge.log"

    @property
    def ovms_log(self) -> Path:
        return self.logs_dir / "ovms.log"

    def ensure_dirs(self) -> None:
        """Create the home folder and every directory under it (idempotent)."""
        for directory in (
            self.home,
            self.models_dir,
            self.runtime_dir,
            self.cache_dir,
            self.logs_dir,
            self.webview_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
