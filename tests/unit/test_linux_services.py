"""The non-window Linux pieces: XDG autostart, the doctor's Linux rows and hotkey probe, the
documents-dir guard, the platform-aware runtime variant, the no-NPU device fallback, and the
launcher entry. ``tests/unit/conftest.py`` switches the host-dependent code off; the fixtures
here switch it on."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chatforge import app as app_mod
from chatforge import autostart, config, doctor
from chatforge.config import validate_config
from chatforge.desktop import hotkey, xutil
from chatforge.paths import Paths
from chatforge.runtime import manager as manager_mod
from chatforge.runtime import ovms_supervisor

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="exercises the Linux branch with POSIX path and HOME semantics; covered by the Ubuntu job",
)

ROOT = Path(__file__).resolve().parents[2]


# --- XDG autostart ---------------------------------------------------------------------


@pytest.fixture
def xdg(tmp_path, monkeypatch, chatforge_home):
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    return config_home


@POSIX_ONLY
def test_xdg_enable_writes_the_desktop_entry(xdg, chatforge_home):
    st = autostart.enable()
    entry = xdg / "autostart" / "chatforge.desktop"
    assert st.enabled and st.mechanism == "XDG autostart" and st.path == entry
    assert st.mode == "xdg-autostart"
    text = entry.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    for needed in (
        "Type=Application",
        "Name=ChatForge",
        "Terminal=false",
        "Hidden=false",
        "X-GNOME-Autostart-Delay=10",
    ):
        assert needed in lines
    exec_line = next(x for x in lines if x.startswith("Exec="))
    assert exec_line.endswith("-P -m chatforge --hidden") and sys.executable in exec_line
    icon = next(x for x in lines if x.startswith("Icon="))[5:]
    assert Path(icon).parent == chatforge_home and Path(icon).name == "app-icon-v2.png"
    assert Path(icon).is_file()
    assert not list((xdg / "autostart").glob("*.tmp"))


def test_xdg_enable_is_idempotent_and_takes_a_custom_command(xdg, chatforge_home):
    autostart.enable(["/opt/py/bin/python", "-m", "chatforge", "--hidden"], home=chatforge_home)
    autostart.enable(["/opt/py/bin/python", "-m", "chatforge", "--hidden"], home=chatforge_home)
    text = (xdg / "autostart" / "chatforge.desktop").read_text(encoding="utf-8")
    assert text.count("Exec=/opt/py/bin/python -m chatforge --hidden\n") == 1


def test_xdg_entry_without_an_icon_still_works(xdg, chatforge_home, monkeypatch):
    from chatforge.desktop import icon

    monkeypatch.setattr(icon, "write_app_png", lambda *_a: (_ for _ in ()).throw(OSError("ro")))
    autostart.enable()
    text = (xdg / "autostart" / "chatforge.desktop").read_text(encoding="utf-8")
    assert "Icon=" not in text and "Exec=" in text


@POSIX_ONLY
def test_xdg_config_home_falls_back_to_dot_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert autostart.xdg_autostart_dir() == tmp_path / ".config" / "autostart"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")  # the spec: relative is ignored
    assert autostart.xdg_autostart_dir() == tmp_path / ".config" / "autostart"


def test_exec_quoting_follows_the_desktop_entry_spec():
    assert autostart._exec_quote("/usr/bin/python3") == "/usr/bin/python3"
    assert autostart._exec_quote("/home/jo smith/py") == '"/home/jo smith/py"'
    assert autostart._exec_quote("100%") == "100%%"
    assert autostart._exec_quote('a"b$c`d') == '"a\\"b\\$c\\`d"'
    assert autostart._exec_quote("") == '""'


def test_xdg_status_reads_what_is_on_disk(xdg):
    assert autostart.status().enabled is False and autostart.status().path is None
    autostart.enable()
    st = autostart.status()
    assert st.enabled and st.detail == "" and st.mechanism == "XDG autostart"
    entry = st.path
    entry.write_text(entry.read_text().replace("Hidden=false", "Hidden=true"), encoding="utf-8")
    st = autostart.status()
    assert st.enabled is False and "Hidden=true" in st.detail
    entry.write_text(
        entry.read_text().replace("Hidden=true", "Hidden=false")
        + "X-GNOME-Autostart-enabled=false\n",
        encoding="utf-8",
    )
    # first value in the group wins: the one enable() wrote (true) is read first
    assert autostart.status().enabled is True
    entry.write_text(
        entry.read_text().replace("X-GNOME-Autostart-enabled=true\n", ""), encoding="utf-8"
    )
    st = autostart.status()
    assert st.enabled is False and "switched off" in st.detail


def test_xdg_status_flags_a_launcher_that_is_not_hidden_or_has_no_exec(xdg):
    entry = xdg / "autostart" / "chatforge.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nType=Application\nExec=/usr/bin/python3 -m chatforge\n")
    st = autostart.status()
    assert st.enabled and "--hidden" in st.detail
    entry.write_text("[Desktop Entry]\nType=Application\n")
    assert "no Exec" in autostart.status().detail


def test_xdg_disable_removes_the_entry(xdg):
    autostart.enable()
    st = autostart.disable()
    assert st.enabled is False and st.detail == "removed"
    assert not (xdg / "autostart" / "chatforge.desktop").exists()
    assert autostart.disable().detail == "was not enabled"


def test_xdg_write_failure_is_an_autostart_error(xdg, monkeypatch):
    (xdg).mkdir(parents=True)
    (xdg / "autostart").write_text("a file where the folder should be")
    with pytest.raises(autostart.AutostartError):
        autostart.enable()


def test_other_platforms_stay_unsupported(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: False)
    with pytest.raises(autostart.AutostartError):
        autostart.enable(["x"])
    assert autostart.status().enabled is False and autostart.disable().enabled is False


@POSIX_ONLY
def test_doctor_reads_the_xdg_entry_as_a_pass(xdg):
    st = autostart.enable()
    check = doctor.autostart_verdict(st)
    assert check.status == "pass" and "XDG autostart" in check.detail


# --- doctor: Linux rows ----------------------------------------------------------------


def test_display_verdicts():
    assert doctor.check_display({"DISPLAY": ":0"}).status == "pass"
    both = doctor.check_display({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"})
    assert both.status == "pass" and "XWayland" in both.detail
    assert doctor.check_display({"WAYLAND_DISPLAY": "wayland-0"}).status == "warn"
    assert doctor.check_display({}).status == "fail"


def test_webview_toolkit_verdicts():
    assert doctor.check_webview_toolkit("GTK 3 + WebKit2 4.1", None).detail.startswith("GTK")
    assert doctor.check_webview_toolkit(None, "PyQt6 6.7").status == "pass"
    missing = doctor.check_webview_toolkit(None, None)
    assert missing.status == "fail" and "gir1.2-webkit2-4.1" in missing.detail


def test_probe_toolkits_never_raises():
    gtk, qt = doctor.probe_toolkits()
    assert gtk is None or gtk.startswith("GTK")
    assert qt is None or isinstance(qt, str)


def test_probe_toolkits_finds_gtk_with_webkit_4_1(monkeypatch):
    import types

    seen: list[tuple] = []
    gi = types.ModuleType("gi")

    def require(name: str, version: str) -> None:
        seen.append((name, version))
        if (name, version) == ("WebKit2", "4.0"):
            raise ValueError("no 4.0")

    gi.require_version = require
    monkeypatch.setitem(sys.modules, "gi", gi)
    assert doctor.probe_toolkits()[0] == "GTK 3 + WebKit2 4.1"
    assert ("WebKit2", "4.0") not in seen  # 4.1 answered first


def test_probe_toolkits_falls_back_to_webkit_4_0(monkeypatch):
    import types

    gi = types.ModuleType("gi")

    def require(name: str, version: str) -> None:
        if (name, version) == ("WebKit2", "4.1"):
            raise ValueError("no 4.1")

    gi.require_version = require
    monkeypatch.setitem(sys.modules, "gi", gi)
    assert doctor.probe_toolkits()[0] == "GTK 3 + WebKit2 4.0"


def test_tray_host_hints():
    assert doctor.check_tray_host({"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "KDE"}).status == "pass"
    gnome = doctor.check_tray_host({"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"})
    assert gnome.status == "warn" and "AppIndicator" in gnome.detail
    assert (
        doctor.check_tray_host({"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "Unity"}).status == "pass"
    )
    chosen = doctor.check_tray_host({"PYSTRAY_BACKEND": "appindicator"})
    assert chosen.status == "pass" and "appindicator" in chosen.detail
    assert doctor.check_tray_host({}).status == "warn"


def test_npu_on_linux_is_a_node_or_a_cpu_warning():
    assert doctor.check_npu_linux(["/dev/accel/accel0"]).status == "pass"
    none = doctor.check_npu_linux([])
    assert none.status == "warn" and "CPU" in none.detail


def test_list_npu_nodes_globs_dev_accel(monkeypatch):
    import glob

    monkeypatch.setattr(glob, "glob", lambda pattern: ["/dev/accel/accel1", "/dev/accel/accel0"])
    assert doctor.list_npu_nodes() == ["/dev/accel/accel0", "/dev/accel/accel1"]


def test_app_running_wording_follows_the_platform():
    stray_linux = doctor.check_app_running(False, 1, [42], windows=False)
    assert "ovms pids" in stray_linux.detail and "kill 42" in stray_linux.detail
    assert "Task Manager" not in stray_linux.detail
    stray_win = doctor.check_app_running(False, 1, [42], windows=True)
    assert "ovms.exe" in stray_win.detail and "Task Manager" in stray_win.detail
    assert "ovms pids [7]" in doctor.check_app_running(True, 1, [7], windows=False).detail


@POSIX_ONLY
def test_run_checks_off_windows_has_linux_rows_and_no_windows_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "posix")
    monkeypatch.setattr(doctor, "probe_toolkits", lambda: ("GTK 3 + WebKit2 4.1", None))
    monkeypatch.setattr(doctor, "list_npu_nodes", lambda: [])
    monkeypatch.setattr(doctor, "probe_hotkey", lambda spec: "error:no display")
    monkeypatch.setattr(doctor, "local_model_checks", lambda *a, **k: [])
    monkeypatch.setattr(doctor, "app_is_running", lambda port: False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    checks = doctor.run_checks(Paths.from_home(tmp_path / "home"))
    names = [c.name for c in checks]
    for linux_row in ("Display", "Web view (pywebview)", "Tray host", "NPU device"):
        assert linux_row in names
    for windows_row in ("WebView2 runtime", "VC++ x64 runtime"):
        assert windows_row not in names
    assert next(c for c in checks if c.name == "Web view (pywebview)").status == "pass"
    assert next(c for c in checks if c.name == "NPU device").status == "warn"


# --- doctor: hotkey probe --------------------------------------------------------------


def test_hotkey_verdict_for_x11_and_copilot_outcomes():
    ok = doctor.check_hotkey("Ctrl+Alt+Space", "ok:X11 grab", False)
    assert ok.status == "pass" and "X11 grab" in ok.detail
    xw = doctor.check_hotkey("Ctrl+Alt+Space", "ok:X11 grab via XWayland", False)
    assert xw.status == "warn" and "chatforge --show" in xw.detail
    assert doctor.check_hotkey("Copilot", "ok", False).detail.startswith("Copilot key")
    assert doctor.check_hotkey("Copilot", "error:Could not hook", False).status == "warn"


def test_probe_reports_a_bad_spec(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "posix")
    assert doctor.probe_hotkey("nope").startswith("invalid:")


def test_probe_without_a_display_gives_the_shortcut_advice(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    outcome = doctor.probe_hotkey("Ctrl+Alt+Space")
    assert outcome == f"error:{hotkey.NO_X11_DISPLAY}"
    check = doctor.check_hotkey("Ctrl+Alt+Space", outcome, False)
    assert check.status == "warn" and "chatforge --show" in check.detail


class FakeThread:
    result: str | None = None
    stopped = 0

    def __init__(self, _on_press: Any) -> None:
        pass

    def start(self, spec: str) -> str | None:
        return FakeThread.result

    def stop(self) -> None:
        FakeThread.stopped += 1


def test_probe_grabs_and_releases_through_the_apps_own_thread(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "posix")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(hotkey, "HotkeyThread", FakeThread)
    FakeThread.result = None
    assert doctor.probe_hotkey("Ctrl+Alt+Space") == "ok:X11 grab"
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert doctor.probe_hotkey("Ctrl+Alt+Space") == "ok:X11 grab via XWayland"
    FakeThread.result = "Ctrl+Alt+Space is in use by another app. Choose a different combination."
    assert doctor.probe_hotkey("Ctrl+Alt+Space") == "in_use"
    FakeThread.result = "python-xlib is not available (No module)."
    assert doctor.probe_hotkey("Ctrl+Alt+Space").startswith("error:python-xlib")
    assert FakeThread.stopped == 4


def test_copilot_probe_off_windows_says_windows_only(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "posix")
    outcome = doctor.probe_hotkey("Copilot")
    assert outcome == f"error:{hotkey.COPILOT_WINDOWS_ONLY}"
    assert doctor.check_hotkey("Copilot", outcome, False).status == "warn"


def test_copilot_probe_on_windows_installs_and_removes_a_throwaway_hook(monkeypatch):
    import ctypes

    log: list[str] = []

    class FakeHook:
        def __init__(self, user32: Any, fire: Any) -> None:
            log.append("new")

        def install(self) -> int | None:
            log.append("install")
            return FakeHook.code

        def remove(self) -> None:
            log.append("remove")

    FakeHook.code = None
    monkeypatch.setattr(doctor.os, "name", "nt")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=object()), raising=False)
    monkeypatch.setattr(hotkey, "_CopilotHook", FakeHook)
    monkeypatch.setattr(hotkey, "_configure_user32", lambda _u: log.append("configure"))
    assert doctor.probe_hotkey("Copilot") == "ok"
    assert log == ["configure", "new", "install", "remove"]
    log.clear()
    FakeHook.code = 1428
    outcome = doctor.probe_hotkey("Copilot")
    assert outcome == "error:" + hotkey.describe_error("Copilot", 1428)
    assert "remove" not in log  # nothing was installed


# --- config ----------------------------------------------------------------------------


def test_documents_dir_may_not_be_the_xdg_autostart_folder(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    autostart_dir = tmp_path / ".config" / "autostart"
    for bad in (str(autostart_dir), str(autostart_dir / "sub"), "~/.config/autostart"):
        with caplog.at_level(logging.WARNING, logger="chatforge.config"):
            assert config.unsafe_documents_dir(bad) == "inside the Startup folder", bad
        assert validate_config({"tools": {"documents_dir": bad}}).tools.documents_dir == ""
    assert config.unsafe_documents_dir(str(tmp_path / ".config" / "autostart-notes")) is None
    custom = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))
    assert config.unsafe_documents_dir(str(custom / "autostart")) == "inside the Startup folder"
    assert config.unsafe_documents_dir(str(tmp_path / "Documents")) is None


def test_runtime_variant_defaults_to_python_off_on_linux(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert config.default_ovms_variant() == "python_off"
    assert validate_config({}).local.ovms_variant == "python_off"
    # An explicit choice is kept.
    assert (
        validate_config({"local": {"ovms_variant": "python_on"}}).local.ovms_variant == "python_on"
    )
    monkeypatch.setattr(config.sys, "platform", "win32")
    assert config.default_ovms_variant() == "python_on"
    assert validate_config({}).local.ovms_variant == "python_on"


# --- the no-NPU device fallback --------------------------------------------------------


def _manager(tmp_path, local: dict | None = None):
    from tests.unit.test_manager import QWEN, make_manager

    mgr, _sup, holder = make_manager(tmp_path, local=local)
    return mgr, holder, QWEN


def test_npu_without_a_device_node_runs_on_cpu_and_leaves_the_config(tmp_path, monkeypatch, caplog):
    mgr, holder, qwen = _manager(tmp_path)
    monkeypatch.setattr(manager_mod, "npu_unavailable_on_host", lambda device: device == "NPU")
    spec, fallback = mgr.plan(qwen)
    assert spec.device == "CPU"
    assert spec.cache_dir.name == "CPU-4096"  # the compile cache key is the CPU's
    assert fallback and fallback["from"] == "NPU" and fallback["to"] == "CPU"
    assert "accel0" in fallback["reason"]
    assert holder["cfg"].local.device == "NPU"  # not rewritten
    mgr.plan(qwen)
    mgr.plan(qwen)
    warnings = [r for r in caplog.records if "npu_unavailable_using_cpu" in r.getMessage()]
    assert len(warnings) <= 1  # one warning, not one per launch


def test_the_warning_is_logged_once(tmp_path, monkeypatch):
    mgr, _holder, qwen = _manager(tmp_path)
    monkeypatch.setattr(manager_mod, "npu_unavailable_on_host", lambda device: True)
    seen: list[str] = []
    monkeypatch.setattr(manager_mod.log, "warning", lambda event, **kw: seen.append(event))
    mgr.plan(qwen)
    mgr.plan(qwen)
    assert seen == ["npu_unavailable_using_cpu"]


def test_gpu_and_cpu_devices_are_not_touched_by_the_npu_check(tmp_path, monkeypatch):
    mgr, _holder, qwen = _manager(tmp_path, local={"device": "GPU"})
    monkeypatch.setattr(manager_mod, "npu_unavailable_on_host", lambda device: True)
    spec, fallback = mgr.plan(qwen)
    assert spec.device == "GPU" and fallback is None


def test_an_npu_host_keeps_the_npu(tmp_path, monkeypatch):
    mgr, _holder, qwen = _manager(tmp_path)
    monkeypatch.setattr(manager_mod, "npu_unavailable_on_host", lambda device: False)
    spec, fallback = mgr.plan(qwen)
    assert spec.device == "NPU" and fallback is None


def test_the_host_check_is_the_supervisors(tmp_path, monkeypatch):
    node = tmp_path / "accel0"
    # (conftest swaps the manager's name for a stub; the real one is the supervisor's)
    assert manager_mod.NPU_DEVICE_NODE == ovms_supervisor.NPU_DEVICE_NODE
    monkeypatch.setattr(ovms_supervisor.sys, "platform", "linux")
    monkeypatch.setattr(ovms_supervisor, "is_windows_host", lambda: False)
    assert ovms_supervisor.npu_unavailable_on_host("NPU", node=node) is True
    node.write_text("")
    assert ovms_supervisor.npu_unavailable_on_host("NPU", node=node) is False


# --- app.main on Linux -----------------------------------------------------------------


def test_main_prepares_the_linux_environment_first(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(xutil, "prefer_x11", lambda: calls.append("x11") or True)
    monkeypatch.setattr(xutil, "set_program_name", lambda name: calls.append(name) or True)
    monkeypatch.setattr(app_mod.single_instance, "acquire_single_instance", lambda: None)
    monkeypatch.setattr(app_mod.single_instance, "signal_existing", lambda: True)
    assert app_mod.main("hidden", paths=Paths.from_home(tmp_path / "home")) == 0
    assert calls == ["x11", "chatforge"]


# --- launcher --------------------------------------------------------------------------


def test_launcher_entry_is_a_valid_desktop_file():
    text = (ROOT / "launchers" / "chatforge.desktop").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    main_group = lines[: lines.index("")]
    fields = dict(line.split("=", 1) for line in main_group if "=" in line)
    assert fields["Type"] == "Application" and fields["Name"] == "ChatForge"
    assert fields["Exec"] == "chatforge --show" and fields["Terminal"] == "false"
    assert fields["StartupWMClass"] == app_mod.PROGRAM_NAME
    assert "Actions" in fields and "[Desktop Action Settings]" in lines
