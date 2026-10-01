"""Start AI Chat when the user logs in: a per-user Task Scheduler logon task (no admin).

# Adapted from StudioForge src/studioforge/core/autostart.py (MIT, LaserLloyd)

The task, named :data:`TASK_NAME`, runs ``wscript.exe "<home>\\AIChat.vbs"`` ten seconds
after sign-in. The ``.vbs`` launches ``pythonw -m aichat --hidden`` with the window
hidden: a ``.lnk`` would need COM (pywin32), and a ``.bat`` would flash a console at every
login. The task is registered from XML because ``schtasks /Create`` has no switch for
"run on battery", and a laptop that signs in unplugged must still start the app.

Older versions used a shim in the per-user Startup folder. It is still the fallback when
Task Scheduler refuses the task, :func:`enable` removes it when the task is created (the
two together launch the app twice, and the second launch opens the popup at sign-in), and
:func:`disable` removes both. :func:`status` reports what Windows has, not what we believe
we wrote.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from aichat.errors import AppError
from aichat.logging_setup import get_logger
from aichat.paths import app_home

log = get_logger(__name__)

ENTRY_NAME = "AIChat"
WINDOWS_SHIM = f"{ENTRY_NAME}.vbs"
TASK_NAME = "AI Chat"
TASK_DESCRIPTION = "Starts AI Chat (tray assistant) at sign-in."
#: Logon-trigger delay (ISO 8601), so the shell and network are up before the app starts.
TASK_DELAY = "PT10S"
TASK_XML_NAME = f"{ENTRY_NAME}-task.xml"
TASK_MECHANISM = "Task Scheduler"
STARTUP_MECHANISM = "Windows Startup folder"
SCHTASKS_TIMEOUT_S = 15.0

_TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class AutostartError(AppError):
    code = "autostart_failed"


@dataclass(frozen=True)
class AutostartStatus:
    enabled: bool
    mechanism: str
    path: Path | None
    detail: str = ""
    #: A Startup-folder shim found next to an enabled task (two launches at sign-in).
    duplicate: Path | None = None

    @property
    def mode(self) -> str:
        """``"task-scheduler"`` or ``"startup-folder"`` (the bridge's ``get_autostart.mode``)."""
        return "task-scheduler" if self.mechanism == TASK_MECHANISM else "startup-folder"

    def describe(self) -> str:
        state = "enabled" if self.enabled else "not enabled"
        where = f" ({self.path})" if self.path else ""
        suffix = f" - {self.detail}" if self.detail else ""
        return f"autostart {state} via {self.mechanism}{where}{suffix}"


@dataclass(frozen=True)
class TaskInfo:
    """The parts of the registered task that :func:`status` looks at."""

    enabled: bool
    command: str
    arguments: str

    @property
    def command_line(self) -> str:
        return f"{self.command} {self.arguments}".strip()

    @property
    def script(self) -> Path | None:
        """The ``.vbs`` the task runs, when its arguments are one ``.vbs`` path."""
        text = self.arguments.strip().strip('"').strip()
        return Path(text) if text.casefold().endswith(".vbs") else None


def _is_windows() -> bool:
    return os.name == "nt"


def startup_dir() -> Path:
    """Per-user Startup folder."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_shim() -> Path:
    """The legacy (and fallback) Startup-folder shim."""
    return startup_dir() / WINDOWS_SHIM


def task_script(home: Path | None = None) -> Path:
    """``<home>\\AIChat.vbs``: the script the logon task runs."""
    return (home if home is not None else app_home()) / WINDOWS_SHIM


def _system32(name: str) -> str:
    """A full System32 path, so a same-named program earlier on PATH is never run."""
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = Path(root) / "System32" / name
    return str(candidate) if candidate.is_file() else name


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


def build_shim(argv: Sequence[str], home: Path, *, for_task: bool = False) -> str:
    """The VBScript source (LF newlines) that runs ``argv`` hidden, in ``home``."""
    if for_task:
        header = (
            f"' Run at sign-in by the '{TASK_NAME}' task in Task Scheduler. Turn off\n"
            "' 'Start at login' (or run 'aichat autostart disable') to stop it; deleting\n"
            "' only this file makes the task fail at every sign-in.\n"
        )
    else:
        header = (
            "' Created by 'aichat autostart enable'. Delete this file (or run\n"
            "' 'aichat autostart disable') to stop AI Chat starting at login.\n"
        )
    return (
        header + 'Set shell = CreateObject("WScript.Shell")\n'
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


def _write_utf16(path: Path, text: str) -> None:
    """UTF-16 LE with BOM and CRLF newlines.

    The ONLY Unicode encoding the VBScript engine accepts: wscript.exe parses a .vbs as
    ANSI or as BOM-marked UTF-16 and nothing else; a UTF-8 BOM arrives as garbage
    characters ("Invalid character / 800A0408"). Task XML is UTF-16 too.
    ``write_bytes`` does no newline translation, hence the explicit CRLF.
    """
    path.write_bytes(codecs.BOM_UTF16_LE + text.replace("\n", "\r\n").encode("utf-16-le"))


def _write_script(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_utf16(path, text)
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeEncodeError (a lone surrogate in an NTFS path).
        raise AutostartError(f"Could not write {path}: {exc}") from exc


def _remove(path: Path) -> bool:
    """Delete ``path`` if it is there. Returns whether it existed."""
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        raise AutostartError(f"Could not remove {path}: {exc}") from exc
    return True


# --------------------------------------------------------------------------- #
# Task Scheduler
# --------------------------------------------------------------------------- #


def _schtasks(*args: str) -> tuple[int, str]:
    """Run ``schtasks.exe`` without a console window: ``(exit code, stdout + stderr)``.

    Raises ``OSError`` or ``subprocess.SubprocessError`` when it cannot run at all.
    schtasks writes in the console (OEM) code page even when its XML says UTF-16.
    """
    proc = subprocess.run(
        [_system32("schtasks.exe"), *args],
        capture_output=True,
        timeout=SCHTASKS_TIMEOUT_S,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    encoding = "oem" if _is_windows() else "utf-8"
    try:
        text = (proc.stdout + proc.stderr).decode(encoding, errors="replace")
    except LookupError:  # pragma: no cover - the "oem" codec exists on every Windows
        text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode, text


def _schtasks_message(output: str) -> str:
    """The first meaningful line of schtasks output (``ERROR: Access is denied.``)."""
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return "no output"


def parse_task_xml(text: str) -> TaskInfo | None:
    """The task's enabled flag and action from ``schtasks /Query /XML`` output."""
    # The declaration says UTF-16 but the text arrives already decoded; drop it.
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.strip(), count=1)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    enabled = (root.findtext("t:Settings/t:Enabled", "true", _TASK_NS) or "").strip()
    return TaskInfo(
        enabled=enabled.casefold() != "false",
        command=(root.findtext("t:Actions/t:Exec/t:Command", "", _TASK_NS) or "").strip(),
        arguments=(root.findtext("t:Actions/t:Exec/t:Arguments", "", _TASK_NS) or "").strip(),
    )


def query_task() -> TaskInfo | None:
    """The registered ``AI Chat`` task, or ``None`` when there is none.

    ``/XML`` rather than ``/FO LIST /V``: the list labels are translated on non-English
    Windows, the XML element names are not. Raises :class:`AutostartError` when schtasks
    cannot run or answers with something that is not task XML.
    """
    try:
        code, out = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutostartError(f"Could not query Task Scheduler: {exc}") from exc
    if code != 0:
        return None  # "ERROR: The system cannot find the file specified."
    info = parse_task_xml(out)
    if info is None:
        raise AutostartError(f"Task Scheduler returned unreadable XML for '{TASK_NAME}'.")
    return info


def _current_user() -> str | None:
    """``DOMAIN\\user`` for the signed-in user: the logon trigger fires only for them.

    A logon trigger without a user means "any user", which needs administrator rights.
    """
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        return None
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{user}" if domain else user


def build_task_xml(script: Path, *, user: str | None, wscript: str) -> str:
    """Task XML (LF newlines) for the logon task that runs ``wscript.exe "<script>"``.

    Mirrors what Windows exports for the task: battery-agnostic, no run-time limit, a
    second trigger while the app is already running is ignored.
    """
    user_xml = f"      <UserId>{escape(user)}</UserId>\n" if user else ""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>{escape(TASK_DESCRIPTION)}</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"{user_xml}"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "  </Settings>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        f"      <Delay>{TASK_DELAY}</Delay>\n"
        f"{user_xml}"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(wscript)}</Command>\n"
        f'      <Arguments>"{escape(str(script))}"</Arguments>\n'
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _create_task(script: Path, home: Path) -> None:
    """Register (or replace) the logon task. Raises :class:`AutostartError`."""
    xml_path = home / TASK_XML_NAME
    text = build_task_xml(script, user=_current_user(), wscript=_system32("wscript.exe"))
    _write_script(xml_path, text)
    try:
        code, out = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F")
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutostartError(f"Could not run schtasks: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            xml_path.unlink()
    if code != 0:
        raise AutostartError(f"Task Scheduler refused the task: {_schtasks_message(out)}")


def _delete_task() -> bool:
    """Delete the logon task. Returns whether there was one. Raises :class:`AutostartError`."""
    if query_task() is None:
        return False
    try:
        code, out = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutostartError(f"Could not run schtasks: {exc}") from exc
    if code != 0:
        raise AutostartError(
            f"Could not remove the '{TASK_NAME}' task: {_schtasks_message(out)}. "
            "Delete it in Task Scheduler."
        )
    return True


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def enable(argv: Sequence[str] | None = None, *, home: Path | None = None) -> AutostartStatus:
    """Create the logon task (and its ``.vbs``), then remove any Startup-folder shim.

    Idempotent (the task and the script are overwritten). When Task Scheduler refuses,
    the Startup-folder shim is written instead and the reason is in ``detail``.
    """
    if not _is_windows():
        raise AutostartError("Autostart is only supported on Windows.")
    home = home if home is not None else app_home()
    command = list(argv) if argv is not None else launch_argv()
    script = task_script(home)
    shim = startup_shim()
    _write_script(script, build_shim(command, home, for_task=True))
    try:
        _create_task(script, home)
    except AutostartError as exc:
        log.warning("autostart task failed; using the Startup folder", error=str(exc))
        try:
            startup_dir().mkdir(parents=True, exist_ok=True)
        except OSError as mkdir_exc:
            raise AutostartError(
                f"{exc} The Startup folder {startup_dir()} is not usable either: {mkdir_exc}"
            ) from mkdir_exc
        _write_script(shim, build_shim(command, home))
        log.info("autostart enabled", path=str(shim), mechanism=STARTUP_MECHANISM)
        return AutostartStatus(True, STARTUP_MECHANISM, shim, f"{exc} Used the Startup folder.")
    try:
        removed = _remove(shim)
    except AutostartError as exc:
        # The task is in place; a shim that will not go away only means a second launch.
        log.warning("could not remove the Startup-folder shim", error=str(exc))
        return AutostartStatus(True, TASK_MECHANISM, script, str(exc), duplicate=shim)
    log.info("autostart enabled", path=str(script), mechanism=TASK_MECHANISM, shim_removed=removed)
    return AutostartStatus(enabled=True, mechanism=TASK_MECHANISM, path=script)


def disable(*, home: Path | None = None) -> AutostartStatus:
    """Delete the logon task and the Startup-folder shim (no error if they are absent).

    The task's ``.vbs`` is removed only once the task is gone, so a task that could not be
    deleted never fires against a missing script.
    """
    if not _is_windows():
        return AutostartStatus(False, TASK_MECHANISM, None, "not supported on this platform")
    shim_removed = _remove(startup_shim())
    task_removed = _delete_task()
    with contextlib.suppress(AutostartError):
        _remove(task_script(home))
    log.info("autostart disabled", task_removed=task_removed, shim_removed=shim_removed)
    return AutostartStatus(
        enabled=False,
        mechanism=TASK_MECHANISM,
        path=None,
        detail="removed" if task_removed or shim_removed else "was not enabled",
    )


def _hidden_problem(path: Path) -> str | None:
    return None if "--hidden" in _read_shim(path) else "shim does not pass --hidden"


def status() -> AutostartStatus:
    """What Task Scheduler and the Startup folder actually hold.

    An enabled task wins; a Startup-folder shim next to it is reported as a duplicate
    (``duplicate`` and ``detail``). A disabled task does not count as enabled.
    """
    if not _is_windows():
        return AutostartStatus(False, TASK_MECHANISM, None, "not supported on this platform")
    shim = startup_shim()
    shim_present = shim.is_file()
    notes: list[str] = []
    try:
        task = query_task()
    except AutostartError as exc:
        task = None
        notes.append(str(exc))

    if task is not None and task.enabled:
        script = task.script
        if script is None:
            notes.append(f"the task runs {task.command_line}")
        elif not script.is_file():
            notes.append(f"{script} is missing")
        elif problem := _hidden_problem(script):
            notes.append(problem)
        if shim_present:
            notes.append(f"{shim} also starts AI Chat, so it is launched twice at sign-in")
        return AutostartStatus(
            enabled=True,
            mechanism=TASK_MECHANISM,
            path=script,
            detail="; ".join(notes),
            duplicate=shim if shim_present else None,
        )
    if task is not None:
        notes.append(f"the '{TASK_NAME}' task is disabled in Task Scheduler")
    if shim_present:
        if problem := _hidden_problem(shim):
            notes.insert(0, problem)
        return AutostartStatus(True, STARTUP_MECHANISM, shim, "; ".join(notes))
    return AutostartStatus(False, TASK_MECHANISM, None, "; ".join(notes))


def is_enabled() -> bool:
    return status().enabled
