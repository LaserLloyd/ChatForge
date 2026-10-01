"""Carry an AI Chat install over to ChatForge (the app's earlier name) on first start.

AI Chat kept its data in ``%LOCALAPPDATA%\\AIChat`` (or ``AICHAT_HOME``), its API keys
under the keyring service ``AIChat``, the files it wrote in ``Documents\\AI Chat``, and
started at sign-in from the Task Scheduler task "AI Chat" (which runs ``AIChat.vbs``).
Each step here runs only when the ChatForge counterpart does not exist yet, never fails
the start, and leaves the old item in place when it cannot be moved.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

LEGACY_APP_NAME = "AIChat"
LEGACY_HOME_ENV = "AICHAT_HOME"
LEGACY_KEY_SERVICE = "AIChat"
LEGACY_TASK_NAME = "AI Chat"
LEGACY_ENTRY_NAME = "AIChat"
LEGACY_DOCUMENTS = "AI Chat"


def legacy_home(home: Path) -> Path:
    """Where AI Chat kept the data that ``home`` now holds."""
    return home.with_name(LEGACY_APP_NAME)


def migrate_home(home: Path) -> Path:
    """Rename AI Chat's data folder to ``home`` (the default ChatForge home) when ``home``
    does not exist yet. Returns the folder to use: ``home``, or the old folder while it
    cannot be renamed (AI Chat still running), so settings are never lost."""
    old = legacy_home(home)
    if home.exists() or not old.is_dir():
        return home
    try:
        old.rename(home)
    except OSError as exc:
        _log.warning("could not move %s to %s (%s); using it in place", old, home, exc)
        return old
    _log.info("moved the AI Chat data folder to %s", home)
    return home


def migrate_documents(new: Path, conversation_file: Path) -> bool:
    """Rename ``Documents\\AI Chat`` to ``new`` (the default ChatForge documents folder)
    and point the saved conversation's document cards at it. True when it moved."""
    old = new.with_name(LEGACY_DOCUMENTS)
    if new.exists() or not old.is_dir():
        return False
    try:
        old.rename(new)
    except OSError as exc:
        _log.warning("could not move %s to %s (%s)", old, new, exc)
        return False
    _log.info("moved the AI Chat documents folder to %s", new)
    try:
        text = conversation_file.read_text(encoding="utf-8")
    except OSError:
        return True
    # Paths are stored JSON-escaped, and twice over inside a JSON tool result.
    once = [json.dumps(str(p))[1:-1] for p in (old, new)]
    twice = [json.dumps(s)[1:-1] for s in once]
    updated = text
    for before, after in (twice, once):
        updated = updated.replace(before, after)
    if updated != text:
        try:
            conversation_file.write_text(updated, encoding="utf-8")
        except OSError as exc:
            _log.warning("could not update document paths in %s (%s)", conversation_file, exc)
    return True


def migrate_key(service: str, provider_id: str, keyring: Any) -> str | None:
    """An API key AI Chat saved for ``provider_id``: copied to ``service`` (and removed from
    the old entry), then returned. ``None`` when there is none."""
    try:
        value = keyring.get_password(LEGACY_KEY_SERVICE, provider_id)
    except Exception:  # noqa: BLE001 - no keyring backend, locked store, ...
        return None
    if not value:
        return None
    try:
        keyring.set_password(service, provider_id, value)
    except Exception:  # noqa: BLE001 - keep the old entry; the key still works from it
        _log.warning("could not move the %s API key to the ChatForge entry", provider_id)
        return value
    with contextlib.suppress(Exception):  # a stale copy is harmless
        keyring.delete_password(LEGACY_KEY_SERVICE, provider_id)
    _log.info("moved the %s API key to the ChatForge entry", provider_id)
    return value


def delete_legacy_key(provider_id: str, keyring: Any) -> bool:
    """Remove AI Chat's copy of a key the user deletes. True if there was one."""
    try:
        keyring.delete_password(LEGACY_KEY_SERVICE, provider_id)
    except Exception:  # noqa: BLE001 - none saved
        return False
    return True


def migrate_autostart(home: Path) -> bool:
    """Replace AI Chat's sign-in task with ChatForge's. True when it did. Windows only."""
    from chatforge import autostart

    if os.name != "nt":
        return False
    try:
        code, _out = autostart._schtasks("/Query", "/TN", LEGACY_TASK_NAME)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - schtasks missing
        return False
    if code != 0:
        return False  # no AI Chat task: nothing to carry over
    try:
        autostart.enable(home=home)
    except Exception as exc:  # noqa: BLE001 - keep the old task rather than none
        _log.warning("could not create the ChatForge sign-in task (%s)", exc)
        return False
    try:
        autostart._schtasks("/Delete", "/TN", LEGACY_TASK_NAME, "/F")  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not remove the AI Chat sign-in task (%s)", exc)
    for name in (f"{LEGACY_ENTRY_NAME}.vbs", f"{LEGACY_ENTRY_NAME}-task.xml"):
        with contextlib.suppress(OSError):
            (home / name).unlink(missing_ok=True)
    _log.info("replaced the AI Chat sign-in task with the ChatForge one")
    return True
