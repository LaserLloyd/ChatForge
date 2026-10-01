"""autostart.py: the Task Scheduler logon task, its .vbs, and the Startup-folder fallback.

schtasks is never run: every test swaps ``autostart._schtasks`` for :class:`FakeSchtasks`,
an in-memory task store that answers like the real tool.
"""

import codecs
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from chatforge import autostart

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

#: What ``schtasks /Query /TN "ChatForge" /XML`` prints for a task Windows registered itself
#: (decoded console output: the declaration still says UTF-16, lines end in CR CR LF).
WINDOWS_EXPORT = (
    '<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
    '<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\r\n'
    "  <RegistrationInfo>\r\r\n"
    "    <Description>Starts ChatForge (tray assistant) at sign-in.</Description>\r\r\n"
    "    <URI>\\ChatForge</URI>\r\r\n"
    "  </RegistrationInfo>\r\r\n"
    '  <Principals><Principal id="Author"><UserId>S-1-5-21-1-2-3-1001</UserId>'
    "<LogonType>InteractiveToken</LogonType></Principal></Principals>\r\r\n"
    "  <Settings>\r\r\n"
    "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\r\n"
    "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\r\r\n"
    "    <IdleSettings><Duration>PT10M</Duration></IdleSettings>\r\r\n"
    "  </Settings>\r\r\n"
    "  <Triggers><LogonTrigger><Delay>PT10S</Delay><UserId>PC\\user</UserId>"
    "</LogonTrigger></Triggers>\r\r\n"
    '  <Actions Context="Author">\r\r\n'
    "    <Exec>\r\r\n"
    "      <Command>C:\\WINDOWS\\System32\\wscript.exe</Command>\r\r\n"
    '      <Arguments>"{script}"</Arguments>\r\r\n'
    "    </Exec>\r\r\n"
    "  </Actions>\r\r\n"
    "</Task>\r\r\n"
)


class FakeSchtasks:
    """``schtasks /Query|/Create|/Delete`` against one in-memory task."""

    def __init__(self) -> None:
        self.task_xml: str | None = None
        self.calls: list[tuple[str, ...]] = []
        self.create_error: str | None = None
        self.delete_error: str | None = None
        self.raise_exc: Exception | None = None

    def __call__(self, *args: str) -> tuple[int, str]:
        self.calls.append(args)
        if self.raise_exc is not None:
            raise self.raise_exc
        verb = args[0]
        assert args[1:3] == ("/TN", "ChatForge")
        if verb == "/Query":
            assert args[3:] == ("/XML",)
            if self.task_xml is None:
                return 1, "ERROR: The system cannot find the file specified.\r\r\n"
            return 0, self.task_xml
        if verb == "/Create":
            assert args[3] == "/XML" and args[5:] == ("/F",)
            if self.create_error:
                return 1, self.create_error
            raw = Path(args[4]).read_bytes()
            assert raw.startswith(codecs.BOM_UTF16_LE)
            self.task_xml = raw[2:].decode("utf-16-le")
            return 0, 'SUCCESS: The scheduled task "ChatForge" has successfully been created.'
        if verb == "/Delete":
            assert args[3:] == ("/F",)
            if self.delete_error:
                return 1, self.delete_error
            self.task_xml = None
            return 0, 'SUCCESS: The scheduled task "ChatForge" was successfully deleted.'
        raise AssertionError(f"unexpected schtasks call {args}")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def schtasks(monkeypatch) -> FakeSchtasks:
    fake = FakeSchtasks()
    monkeypatch.setattr(autostart, "_schtasks", fake)
    return fake


@pytest.fixture
def appdata(tmp_path, monkeypatch, chatforge_home, schtasks):
    roaming = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("USERDOMAIN", "PC")
    monkeypatch.setenv("USERNAME", "jo")
    monkeypatch.setattr(autostart, "_is_windows", lambda: True)
    return roaming


def _shim_path(appdata: Path) -> Path:
    return (
        appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "ChatForge.vbs"
    )


def _script_path(chatforge_home: Path) -> Path:
    return chatforge_home.resolve() / "ChatForge.vbs"


def _decode(path: Path) -> str:
    raw = path.read_bytes()
    assert raw.startswith(codecs.BOM_UTF16_LE)
    return raw[2:].decode("utf-16-le")


def _task(xml: str) -> ET.Element:
    return ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", xml.strip()))


def _put_shim(appdata: Path, text: str = 'shell.Run "x -m chatforge --hidden", 0, False') -> Path:
    path = _shim_path(appdata)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
    return path


def test_startup_dir_uses_appdata(appdata):
    assert autostart.startup_dir() == _shim_path(appdata).parent


# --- enable -----------------------------------------------------------------------------


def test_enable_writes_utf16_le_bom_vbs_in_home(appdata, chatforge_home, schtasks):
    status = autostart.enable(["C:\\Py\\pythonw.exe", "-m", "chatforge", "--hidden"])
    path = _script_path(chatforge_home)
    assert status.enabled and status.path == path
    assert status.mechanism == "Task Scheduler" and status.mode == "task-scheduler"
    raw = path.read_bytes()
    assert raw[:2] == b"\xff\xfe"
    text = raw[2:].decode("utf-16-le")
    assert "--hidden" in text
    assert "-m chatforge" in text
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert 'CreateObject("WScript.Shell")' in text
    assert f'shell.CurrentDirectory = "{chatforge_home.resolve()}"' in text
    assert ", 0, False" in text  # hidden window, do not wait
    assert "\ufeff" not in text  # exactly one BOM
    assert "Task Scheduler" in text  # the header says what runs it
    assert not _shim_path(appdata).exists()


def test_enable_registers_a_battery_safe_logon_task(appdata, chatforge_home, schtasks):
    autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"])
    assert schtasks.verbs() == ["/Create"]
    xml_file = Path(schtasks.calls[0][4])
    assert xml_file.parent == chatforge_home.resolve()
    assert not xml_file.exists()  # the XML is only a vehicle for /Create
    root = _task(schtasks.task_xml)
    settings = root.find("t:Settings", NS)
    assert settings.findtext("t:DisallowStartIfOnBatteries", None, NS) == "false"
    assert settings.findtext("t:StopIfGoingOnBatteries", None, NS) == "false"
    assert settings.findtext("t:ExecutionTimeLimit", None, NS) == "PT0S"
    assert settings.findtext("t:MultipleInstancesPolicy", None, NS) == "IgnoreNew"
    trigger = root.find("t:Triggers/t:LogonTrigger", NS)
    assert trigger.findtext("t:Delay", None, NS) == "PT10S"
    assert trigger.findtext("t:UserId", None, NS) == "PC\\jo"  # only this user, no admin
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal.findtext("t:LogonType", None, NS) == "InteractiveToken"
    assert principal.findtext("t:UserId", None, NS) == "PC\\jo"
    command = root.findtext("t:Actions/t:Exec/t:Command", None, NS)
    assert command.lower().endswith("wscript.exe")
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", None, NS)
    assert arguments == f'"{_script_path(chatforge_home)}"'


def test_enable_removes_a_startup_folder_shim(appdata, chatforge_home):
    shim = _put_shim(appdata)
    status = autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"])
    assert status.mechanism == "Task Scheduler" and status.duplicate is None
    assert not shim.exists()
    assert autostart.status().duplicate is None


def test_enable_falls_back_to_startup_folder_when_task_is_refused(
    appdata, chatforge_home, schtasks
):
    schtasks.create_error = "ERROR: Access is denied.\r\r\n"
    status = autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"])
    shim = _shim_path(appdata)
    assert status.enabled and status.path == shim
    assert status.mechanism == "Windows Startup folder" and status.mode == "startup-folder"
    assert "Access is denied" in status.detail and "Startup folder" in status.detail
    text = _decode(shim)
    assert "-m chatforge --hidden" in text and "Created by" in text
    assert not list(chatforge_home.glob("*.xml"))
    reported = autostart.status()
    assert reported.enabled and reported.mechanism == "Windows Startup folder"


def test_enable_falls_back_when_schtasks_cannot_run(appdata, schtasks):
    schtasks.raise_exc = FileNotFoundError("schtasks.exe")
    status = autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"])
    assert status.mode == "startup-folder" and _shim_path(appdata).is_file()
    assert "Could not run schtasks" in status.detail


def test_default_argv_is_pythonw_module_hidden(appdata, chatforge_home):
    argv = autostart.launch_argv()
    assert argv[1:] == ["-m", "chatforge", "--hidden"]
    autostart.enable()
    text = _decode(_script_path(chatforge_home))
    assert "-m chatforge --hidden" in text


def test_paths_with_spaces_are_double_quoted(appdata, chatforge_home):
    autostart.enable(["C:\\Program Files\\Py\\pythonw.exe", "-m", "chatforge", "--hidden"])
    text = _decode(_script_path(chatforge_home))
    assert '""C:\\Program Files\\Py\\pythonw.exe"" -m chatforge --hidden' in text


def test_non_ascii_home_survives(appdata, tmp_path, schtasks):
    home = tmp_path / "Jos\u00e9 & \u4e2d"
    autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"], home=home)
    script = home / "ChatForge.vbs"
    assert f'shell.CurrentDirectory = "{home}"' in _decode(script)
    root = _task(schtasks.task_xml)  # "&" is escaped, the name survives UTF-16
    assert root.findtext("t:Actions/t:Exec/t:Arguments", None, NS) == f'"{script}"'
    assert autostart.status().path == script


def test_enable_is_idempotent_and_overwrites(appdata, chatforge_home, schtasks):
    autostart.enable(["a.exe", "-m", "chatforge", "--hidden"])
    autostart.enable(["b.exe", "-m", "chatforge", "--hidden"])
    text = _decode(_script_path(chatforge_home))
    assert "b.exe" in text and "a.exe" not in text
    assert schtasks.verbs() == ["/Create", "/Create"]  # /F replaces the task
    assert [p.name for p in chatforge_home.iterdir() if p.suffix in {".vbs", ".xml"}] == [
        "ChatForge.vbs"
    ]


# --- status / disable -----------------------------------------------------------------


def test_status_and_disable(appdata, chatforge_home, schtasks):
    s = autostart.status()
    assert s.enabled is False and s.path is None and s.mode == "task-scheduler"
    assert "not enabled" in s.describe()
    autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"])
    s = autostart.status()
    assert s.enabled is True and s.path == _script_path(chatforge_home)
    assert s.detail == "" and s.duplicate is None
    assert "enabled via Task Scheduler" in s.describe()
    assert autostart.is_enabled() is True
    d = autostart.disable()
    assert d.enabled is False and d.detail == "removed"
    assert schtasks.task_xml is None and "/Delete" in schtasks.verbs()
    assert not _script_path(chatforge_home).exists()
    assert autostart.status().enabled is False
    assert autostart.disable().detail == "was not enabled"


def test_disable_removes_task_and_startup_shim(appdata, chatforge_home):
    autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"])
    shim = _put_shim(appdata)
    autostart.disable()
    assert not shim.exists()
    status = autostart.status()
    assert status.enabled is False and status.duplicate is None


def test_disable_only_startup_shim(appdata, schtasks):
    shim = _put_shim(appdata)
    assert autostart.disable().detail == "removed"
    assert not shim.exists()
    assert "/Delete" not in schtasks.verbs()  # no task to delete


def test_disable_keeps_the_script_when_the_task_cannot_be_deleted(
    appdata, chatforge_home, schtasks
):
    autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"])
    shim = _put_shim(appdata)
    schtasks.delete_error = "ERROR: Access is denied.\r\r\n"
    with pytest.raises(autostart.AutostartError) as err:
        autostart.disable()
    assert "Access is denied" in str(err.value)
    # The task still fires, so its script must still be there; the shim is gone.
    assert _script_path(chatforge_home).is_file()
    assert not shim.exists()


def test_status_reports_a_duplicate_startup_shim(appdata, chatforge_home):
    autostart.enable(["pythonw.exe", "-m", "chatforge", "--hidden"])
    shim = _put_shim(appdata)
    s = autostart.status()
    assert s.enabled and s.mechanism == "Task Scheduler"
    assert s.duplicate == shim
    assert "twice" in s.detail and str(shim) in s.detail


def test_status_reads_a_task_windows_registered(appdata, tmp_path, schtasks):
    script = tmp_path / "Local" / "ChatForge" / "ChatForge.vbs"
    script.parent.mkdir(parents=True)
    script.write_bytes(
        codecs.BOM_UTF16_LE + 'shell.Run "p -m chatforge --hidden"'.encode("utf-16-le")
    )
    schtasks.task_xml = WINDOWS_EXPORT.replace("{script}", str(script))
    s = autostart.status()
    assert s.enabled and s.mode == "task-scheduler"
    assert s.path == script and s.detail == ""


def test_status_flags_a_missing_task_script(appdata, tmp_path, schtasks):
    missing = tmp_path / "gone" / "ChatForge.vbs"
    schtasks.task_xml = WINDOWS_EXPORT.replace("{script}", str(missing))
    s = autostart.status()
    assert s.enabled and s.path == missing
    assert "missing" in s.detail


def test_status_disabled_task_is_not_enabled(appdata, tmp_path, schtasks):
    script = tmp_path / "ChatForge.vbs"
    xml = WINDOWS_EXPORT.replace("{script}", str(script))
    schtasks.task_xml = xml.replace("<Settings>", "<Settings><Enabled>false</Enabled>")
    s = autostart.status()
    assert s.enabled is False and "disabled" in s.detail
    shim = _put_shim(appdata)
    s = autostart.status()
    assert s.enabled and s.mechanism == "Windows Startup folder" and s.path == shim
    assert "disabled" in s.detail and s.duplicate is None


def test_status_when_task_scheduler_cannot_be_queried(appdata, schtasks):
    schtasks.raise_exc = OSError("no schtasks")
    s = autostart.status()
    assert s.enabled is False and "Could not query Task Scheduler" in s.detail


def test_status_flags_shim_without_hidden(appdata):
    _put_shim(appdata, 'shell.Run "x -m chatforge", 0, False')
    s = autostart.status()
    assert s.enabled and "--hidden" in s.detail


def test_status_reads_legacy_utf8_bom_shim(appdata):
    path = _shim_path(appdata)
    path.parent.mkdir(parents=True)
    path.write_bytes(codecs.BOM_UTF8 + b'shell.Run "x -m chatforge --hidden", 0, False')
    assert autostart.status().detail == ""


def test_parse_task_xml_rejects_non_xml():
    assert autostart.parse_task_xml("ERROR: something") is None


def test_schtasks_runner_hides_the_console(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: nope\r\r\n")

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    code, out = autostart._schtasks("/Query", "/TN", "ChatForge", "/XML")
    assert code == 1 and "ERROR: nope" in out
    assert seen["argv"][0].lower().endswith("schtasks.exe")
    assert seen["argv"][1:] == ["/Query", "/TN", "ChatForge", "/XML"]
    assert seen["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert seen["timeout"] and seen["check"] is False


def test_unsupported_platform(monkeypatch, tmp_path, schtasks):
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    with pytest.raises(autostart.AutostartError):
        autostart.enable(["x"])
    assert autostart.status().enabled is False
    assert autostart.disable().enabled is False
    assert not any(tmp_path.iterdir())
    assert schtasks.calls == []
