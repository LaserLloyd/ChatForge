"""Start AI Chat when the user logs in (Windows Startup folder, no admin).

# Adapted from StudioForge src/studioforge/core/autostart.py (MIT, LaserLloyd)

A ``.vbs`` shim in the per-user Startup folder launches ``pythonw -m aichat --hidden``
with the window hidden. A ``.lnk`` would need COM (pywin32), and a ``.bat`` would flash a
console at every login. Everything is reversible by :func:`disable`, and :func:`status`
reports what is on disk rather than what we believe we wrote.
"""

from __future__ import annotations

import codecs
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from aichat.errors import AppError
from aichat.logging_setup import get_logger
from aichat.paths import app_home

log = get_logger(__name__)

ENTRY_NAME = "AIChat"
WINDOWS_SHIM = f"{ENTRY_NAME}.vbs"
MECHANISM = "Windows Startup folder"


class AutostartError(AppError):
    code = "autostart_failed"


@dataclass(frozen=True)
class AutostartStatus:
    enabled: bool
    mechanism: str
    path: Path | None
    detail: str = ""

    def describe(self) -> str:
        state = "enabled" if self.enabled else "not enabled"
        where = f" ({self.path})" if self.path else ""
        suffix = f" - {self.detail}" if self.detail else ""
        return f"autostart {state} via {self.mechanism}{where}{suffix}"


def _is_windows() -> bool:
    return os.name == "nt"


def startup_dir() -> Path:
    """Per-user Startup folder."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _tray_interpreter() -> str:
    """``pythonw.exe`` next to the running interpreter, so the app owns no console window.

    Never the ``aichat`` console script: that is a console-subsystem launcher, and a shim
    baked with it would run the app attached to a console.
    """
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else sys.executable


def launch_argv() -> list[str]:
    """The command the shim runs: ``[pythonw, "-m", "aichat", "--hidden"]``."""
    return [_tray_interpreter(), "-m", "aichat", "--hidden"]


def _quote_for_vbs(argv: Sequence[str]) -> str:
    # Quoting is doubled because the whole command is a VBScript string literal.
    parts = [f'""{part}""' if " " in part else part for part in argv]
    return " ".join(parts)


def _vbs_literal(text: str) -> str:
    return text.replace('"', '""')


def build_shim(argv: Sequence[str], home: Path) -> str:
    """The VBScript source (LF newlines) that runs ``argv`` hidden, in ``home``."""
    return (
        "' Created by 'aichat autostart enable'. Delete this file (or run\n"
        "' 'aichat autostart disable') to stop AI Chat starting at login.\n"
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.CurrentDirectory = "{_vbs_literal(str(home))}"\n'
        f'shell.Run "{_quote_for_vbs(argv)}", 0, False\n'
    )


def _read_shim(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw.startswith(codecs.BOM_UTF16_LE):
        return raw[len(codecs.BOM_UTF16_LE) :].decode("utf-16-le", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def enable(argv: Sequence[str] | None = None, *, home: Path | None = None) -> AutostartStatus:
    """Write ``AIChat.vbs`` into the Startup folder. Idempotent (overwrites)."""
    if not _is_windows():
        raise AutostartError("Autostart is only supported on Windows.")
    target = startup_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AutostartError(f"Could not create the Startup folder {target}: {exc}") from exc

    command = list(argv) if argv is not None else launch_argv()
    script = build_shim(command, home if home is not None else app_home())
    path = target / WINDOWS_SHIM
    try:
        # UTF-16 LE with BOM: the ONLY Unicode encoding the VBScript engine accepts.
        # wscript.exe parses a .vbs as ANSI or as BOM-marked UTF-16 and nothing else; a
        # UTF-8 BOM arrives as garbage characters ("Invalid character / 800A0408").
        # CRLF explicitly: write_bytes does no newline translation.
        payload = codecs.BOM_UTF16_LE + script.replace("\n", "\r\n").encode("utf-16-le")
        path.write_bytes(payload)
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeEncodeError (a lone surrogate in an NTFS path).
        raise AutostartError(f"Could not write {path}: {exc}") from exc
    log.info("autostart enabled", path=str(path))
    return AutostartStatus(enabled=True, mechanism=MECHANISM, path=path)


def disable() -> AutostartStatus:
    """Remove the shim (no error if it is absent)."""
    if not _is_windows():
        return AutostartStatus(False, MECHANISM, None, "not supported on this platform")
    path = startup_dir() / WINDOWS_SHIM
    existed = path.is_file()
    if existed:
        try:
            path.unlink()
        except OSError as exc:
            raise AutostartError(f"Could not remove {path}: {exc}") from exc
    log.info("autostart disabled", removed=existed)
    return AutostartStatus(
        enabled=False,
        mechanism=MECHANISM,
        path=None,
        detail="removed" if existed else "was not enabled",
    )


def status() -> AutostartStatus:
    """What is actually on disk."""
    if not _is_windows():
        return AutostartStatus(False, MECHANISM, None, "not supported on this platform")
    path = startup_dir() / WINDOWS_SHIM
    if not path.is_file():
        return AutostartStatus(enabled=False, mechanism=MECHANISM, path=None)
    body = _read_shim(path)
    detail = "" if "--hidden" in body else "shim does not pass --hidden"
    return AutostartStatus(enabled=True, mechanism=MECHANISM, path=path, detail=detail)


def is_enabled() -> bool:
    return status().enabled
