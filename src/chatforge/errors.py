"""Application error base class.

# Adapted from StudioForge src/studioforge/errors.py `StudioForgeError.to_payload` shape (MIT, LaserLloyd)
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """An error the UI can show: message, machine code, hint and a suggested action.

    Subclasses may set class-level ``code``/``hint``/``action`` defaults and add fields
    (``LLMError`` adds ``retryable``). ``action`` is one of ``add_key``, ``open_settings``,
    ``retry`` or ``None``.
    """

    code: str | None = None
    hint: str | None = None
    action: str | None = None

    def __init__(
        self,
        message: str,
        code: str | None = None,
        hint: str | None = None,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if hint is not None:
            self.hint = hint
        if action is not None:
            self.action = action
        self.details: dict[str, Any] = details or {}

    def to_payload(self) -> dict[str, Any]:
        """The ``error`` object of a bridge reply: ``{code, message, hint, action[, details]}``."""
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "action": self.action,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigError(AppError):
    """config.toml could not be parsed or a setting failed validation.

    ``details["errors"]`` maps a dotted field path to a human message.
    """

    code = "invalid_config"
