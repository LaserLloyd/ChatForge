"""``chatforge doctor``: an environment health check with a pass/warn/fail table.

Every probe is wrapped so a broken machine still gets a full table; the pure parts
(thresholds, version parsing, table rendering, exit code) are unit-tested. Nothing here
prints a secret: the MiniMax checks report ``True``/``False`` only.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import platform
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Status = Literal["pass", "warn", "fail"]

#: WebView2 Evergreen runtime client id (the ``pv`` value holds the version).
WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_SUBKEYS: tuple[str, ...] = (
    rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}",
    rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}",
)

MIN_PYTHON = (3, 12)
DISK_WARN_BYTES = 10 * 1024**3
DISK_FAIL_BYTES = 2 * 1024**3
NPU_DEVICE_NAME = "AI Boost"
MINIMAX_PROVIDER = "minimax"
MINIMAX_ENV = "MINIMAX_API_KEY"
COPILOT_KEY = "Copilot"  # hotkey.COPILOT, without importing the Windows module at load
#: The Intel NPU's device node with the Linux driver (``intel_vpu``).
LINUX_NPU_NODES = "/dev/accel/accel*"
DOCTOR_HOTKEY_ID = 0xA1C8  # distinct from the app's HOTKEY_ID
ERROR_HOTKEY_ALREADY_REGISTERED = 1409


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "fail"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #


def check_python(version: tuple[int, ...] = sys.version_info[:3]) -> Check:
    text = ".".join(str(p) for p in version[:3])
    if tuple(version[:2]) >= MIN_PYTHON:
        return Check("Python", "pass", f"{text} ({sys.executable})")
    return Check("Python", "fail", f"{text}; ChatForge needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")


def parse_webview2_version(value: object) -> str | None:
    """The ``pv`` registry value as a version string; ``None`` when absent or ``0.0.0.0``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text == "0.0.0.0":
        return None
    return text


def check_webview2(version: str | None) -> Check:
    if version:
        return Check("WebView2 runtime", "pass", version)
    return Check(
        "WebView2 runtime",
        "fail",
        "not found; install the Evergreen runtime from "
        "https://developer.microsoft.com/microsoft-edge/webview2/",
    )


def check_vcredist(present: bool, version: str | None) -> Check:
    if present:
        return Check("VC++ x64 runtime", "pass", version or "installed")
    return Check(
        "VC++ x64 runtime",
        "fail",
        "missing; run: winget install --id Microsoft.VCRedist.2015+.x64 -e (needs admin)",
    )


def check_npu(device_names: list[str] | None) -> Check:
    if device_names is None:
        return Check(
            "NPU device", "warn", f"could not enumerate PnP devices for '{NPU_DEVICE_NAME}'"
        )
    matches = [n for n in device_names if NPU_DEVICE_NAME.casefold() in n.casefold()]
    if matches:
        return Check("NPU device", "pass", matches[0])
    return Check("NPU device", "fail", f"no '{NPU_DEVICE_NAME}' device; check the Intel NPU driver")


def check_display(environ: Any = None) -> Check:
    """Linux: a display to open windows on. Wayland needs XWayland (``DISPLAY`` too): the
    popup is placed at a screen position and the hotkey is an X11 grab."""
    env = os.environ if environ is None else environ
    x11, wayland = env.get("DISPLAY"), env.get("WAYLAND_DISPLAY")
    if x11 and wayland:
        return Check("Display", "pass", f"Wayland ({wayland}) with XWayland ({x11}); using X11")
    if x11:
        return Check("Display", "pass", f"X11 ({x11})")
    if wayland:
        return Check(
            "Display",
            "warn",
            f"Wayland ({wayland}) without XWayland (DISPLAY unset): the popup cannot be "
            "placed and there is no hotkey; install XWayland",
        )
    return Check("Display", "fail", "no DISPLAY or WAYLAND_DISPLAY: not in a desktop session")


def check_webview_toolkit(gtk: str | None, qt: str | None) -> Check:
    """Linux: which toolkit pywebview can use. ``gtk``/``qt`` are a description when that
    toolkit imports, ``None`` when not."""
    if gtk:
        return Check("Web view (pywebview)", "pass", f"GTK: {gtk}")
    if qt:
        return Check("Web view (pywebview)", "pass", f"Qt: {qt}")
    return Check(
        "Web view (pywebview)",
        "fail",
        "neither GTK + WebKit2GTK nor Qt WebEngine imports; install python3-gi, "
        "gir1.2-gtk-3.0, gir1.2-webkit2-4.1 and create the venv with --system-site-packages "
        "(docs/SETUP-LINUX.md)",
    )


def check_tray_host(environ: Any = None) -> Check:
    """Linux: whether a tray icon will have somewhere to appear (a hint, not a probe)."""
    env = os.environ if environ is None else environ
    backend = env.get("PYSTRAY_BACKEND") or ("xorg" if env.get("DISPLAY") else "")
    desktop = env.get("XDG_CURRENT_DESKTOP", "")
    if not backend:
        return Check("Tray host", "warn", "no display for the tray icon")
    if "gnome" in desktop.lower() and "unity" not in desktop.lower():
        return Check(
            "Tray host",
            "warn",
            f"pystray backend {backend} on {desktop}: GNOME shows no tray icons without the "
            "AppIndicator and KStatusNotifierItem extension; ChatForge still runs, open it "
            "with the hotkey or `chatforge --show`",
        )
    return Check("Tray host", "pass", f"pystray backend {backend} ({desktop or 'desktop unknown'})")


def check_npu_linux(nodes: list[str]) -> Check:
    if nodes:
        return Check("NPU device", "pass", nodes[0])
    return Check(
        "NPU device",
        "warn",
        f"no {LINUX_NPU_NODES} node (Intel NPU driver not loaded): local models run on the CPU",
    )


def check_ovms(status: dict[str, Any]) -> Check:
    if not status.get("installed"):
        return Check("OVMS runtime", "fail", "not installed; run: chatforge runtime install")
    version = status.get("version") or "?"
    variant = status.get("variant") or "?"
    if not status.get("variant_matches", True):
        return Check(
            "OVMS runtime",
            "warn",
            f"{version} ({variant}) installed but config wants {status.get('wanted_variant')}",
        )
    return Check("OVMS runtime", "pass", f"{version} ({variant}) at {status.get('exe')}")


def check_model(model_id: str, record: Any) -> Check:
    if record is None:
        return Check(
            "Model", "fail", f"{model_id} is not on disk; download it in Settings > Models"
        )
    if not getattr(record, "complete", False):
        missing = ", ".join(getattr(record, "missing", []) or []) or "files"
        return Check("Model", "fail", f"{model_id} is incomplete: missing {missing}")
    size_gb = getattr(record, "size_bytes", 0) / 1e9
    source = getattr(record, "source", None) or "no sidecar"
    return Check("Model", "pass", f"{model_id} complete, {size_gb:.2f} GB, {source}")


def check_compiled(model_id: str, device: str, compiled: bool, expected_s: float | None) -> Check:
    short = model_id.rpartition("/")[2]
    if compiled:
        eta = f"; cached load ~{expected_s:.0f} s" if expected_s else ""
        return Check("Compiled for NPU", "pass", f"{short} has a warm {device} cache{eta}")
    eta = f"~{expected_s:.0f} s" if expected_s else "unknown"
    return Check(
        "Compiled for NPU",
        "warn",
        f"{short} has no {device} compile cache; the first load compiles ({eta})",
    )


@dataclass(frozen=True)
class CompileState:
    """One installed local model's compile cache, for the device it would load on."""

    model_id: str
    device: str
    warm: bool
    expected_s: float | None = None


def check_compiled_models(states: list[CompileState], *, precompile: bool = True) -> Check:
    """Every installed local model's cache: pass when all are warm."""
    name = "Compiled for NPU"
    if not states:
        return Check(name, "pass", "no local models installed")

    def label(s: CompileState) -> str:
        short = s.model_id.rpartition("/")[2]
        eta = f" ~{s.expected_s:.0f} s" if s.expected_s else ""
        return f"{short} on {s.device}{eta}"

    cold = [s for s in states if not s.warm]
    if not cold:
        return Check(name, "pass", "warm (cached load): " + ", ".join(label(s) for s in states))
    then = (
        "the app compiles them in the background when idle"
        if precompile
        else "their first load compiles (local.precompile is off)"
    )
    return Check(
        name, "warn", "not compiled yet (first load): " + ", ".join(map(label, cold)) + f"; {then}"
    )


def check_npu_models(
    device: str, verdicts: list[tuple[str, Any]], fallback_device: str = "GPU"
) -> Check:
    """Installed models that will not run correctly on the NPU (``NpuVerdict.ok is False``)."""
    name = "NPU-ready models"
    if "NPU" not in device.upper():
        return Check(name, "pass", f"local.device is {device}; not checked")
    if not verdicts:
        return Check(name, "pass", "no local models installed")
    bad = [(mid, v) for mid, v in verdicts if getattr(v, "ok", None) is False]
    if not bad:
        unknown = sum(getattr(v, "ok", None) is None for _mid, v in verdicts)
        extra = f" ({unknown} not verifiable from their files)" if unknown else ""
        return Check(name, "pass", f"all {len(verdicts)} can run on the NPU{extra}")
    where = (
        "refused (local.npu_fallback_device = none)"
        if fallback_device.lower() == "none"
        else f"runs on {fallback_device.upper()} instead"
    )
    parts = [f"{mid.rpartition('/')[2]}: {v.reason} -> {where}" for mid, v in bad]
    return Check(name, "warn", "; ".join(parts))


def local_target_model(chat_model: str, installed: list[str]) -> str | None:
    """The local model the app loads: ``chat.model`` when installed, else the first one."""
    if chat_model in installed:
        return chat_model
    return installed[0] if installed else None


def check_keyring(backend_name: str, usable: bool) -> Check:
    if usable:
        return Check("Keyring backend", "pass", backend_name)
    return Check(
        "Keyring backend",
        "fail",
        f"{backend_name} is not usable; API keys cannot be saved (env vars still work)",
    )


def check_minimax_key(env_present: bool, keyring_present: bool) -> Check:
    detail = f"{MINIMAX_ENV} set: {env_present}; keyring entry: {keyring_present}"
    if env_present or keyring_present:
        return Check("MiniMax key", "pass", detail)
    return Check("MiniMax key", "warn", detail + " (enter it in Settings > Providers > MiniMax)")


def check_hotkey(spec: str, outcome: str, app_running: bool) -> Check:
    """``outcome`` is ``"ok"``, ``"ok:<how>"`` (``X11 grab``, ``X11 grab via XWayland``),
    ``"in_use"``, ``"invalid:<msg>"`` or ``"error:<msg>"``."""
    if outcome == "ok" and spec == COPILOT_KEY:
        return Check("Hotkey", "pass", "Copilot key: the keyboard hook can be installed")
    if outcome == "ok":
        return Check("Hotkey", "pass", f"{spec} is free")
    if outcome.startswith("ok:"):
        how = outcome[3:]
        if "XWayland" in how:
            return Check(
                "Hotkey",
                "warn",
                f"{spec} can be grabbed ({how}), but only while an X11 window has the focus; "
                "also bind a desktop shortcut to `chatforge --show`",
            )
        return Check("Hotkey", "pass", f"{spec} is free ({how})")
    if outcome == "in_use":
        if app_running:
            return Check("Hotkey", "pass", f"{spec} is held by the running ChatForge instance")
        return Check("Hotkey", "fail", f"{spec} is in use by another app; change it in Settings")
    kind, _, message = outcome.partition(":")
    status: Status = "fail" if kind == "invalid" else "warn"
    return Check("Hotkey", status, f"{spec}: {message or kind}")


def check_autostart(
    enabled: bool,
    path: Path | None,
    shim_text: str,
    expected_exe: str,
    *,
    mechanism: str = "Startup folder",
    duplicate: Path | None = None,
) -> Check:
    """``path`` is the ``ChatForge.vbs`` that runs at sign-in (``shim_text`` its source);
    ``duplicate`` a Startup-folder shim found next to an enabled logon task."""
    if not enabled:
        return Check(
            "Autostart", "warn", "not enabled; turn on 'Start at login' in the tray or Settings"
        )
    where = f"{mechanism} ({path})" if path else mechanism
    problems = []
    if not shim_text:
        problems.append("the launcher script is missing or empty")
    else:
        if "--hidden" not in shim_text:
            problems.append("shim does not pass --hidden")
        if expected_exe and expected_exe.casefold() not in shim_text.casefold():
            problems.append(f"shim does not point at {expected_exe}")
    if duplicate is not None:
        problems.append(f"{duplicate} also starts ChatForge, so it launches twice; delete it")
    if problems:
        return Check("Autostart", "warn", f"{where}: {'; '.join(problems)}")
    return Check("Autostart", "pass", f"enabled via {where}")


def check_disk(free: int | None, path: Path) -> Check:
    if free is None:
        return Check("Free disk", "warn", f"could not measure free space for {path}")
    text = f"{free / 1024**3:.1f} GB free on {path.drive or path}"
    if free < DISK_FAIL_BYTES:
        return Check("Free disk", "fail", text + " (under 2 GB; models and caches will fail)")
    if free < DISK_WARN_BYTES:
        return Check("Free disk", "warn", text + " (under 10 GB)")
    return Check("Free disk", "pass", text)


def check_app_running(
    running: bool, port: int, ovms_pids: list[int], *, windows: bool | None = None
) -> Check:
    win = os.name == "nt" if windows is None else windows
    name = "ovms.exe" if win else "ovms"
    if running:
        detail = f"yes (single-instance port {port} in use)"
        if ovms_pids:
            detail += f"; {name} pids {ovms_pids} (model loaded)"
        return Check("App running", "pass", detail)
    if ovms_pids:
        how = (
            "end them in Task Manager"
            if win
            else f"kill them (kill {' '.join(map(str, ovms_pids))})"
        )
        return Check(
            "App running", "warn", f"no, but stray {name} pids {ovms_pids} are alive; {how}"
        )
    return Check("App running", "pass", "no")


def render_table(checks: Iterable[Check]) -> str:
    rows = list(checks)
    width = max((len(c.name) for c in rows), default=4)
    lines = [f"{'CHECK'.ljust(width)}  STATUS  DETAIL", f"{'-' * width}  ------  ------"]
    for c in rows:
        lines.append(f"{c.name.ljust(width)}  {c.status.upper().ljust(6)}  {c.detail}")
    fails = sum(c.status == "fail" for c in rows)
    warns = sum(c.status == "warn" for c in rows)
    lines.append("")
    lines.append(
        f"{len(rows)} checks: {len(rows) - fails - warns} pass, {warns} warn, {fails} fail"
    )
    return "\n".join(lines)


def exit_code(checks: Iterable[Check]) -> int:
    return 0 if all(c.ok for c in checks) else 1


# --------------------------------------------------------------------------- #
# Probes (Windows, real machine)
# --------------------------------------------------------------------------- #


def read_webview2_version(
    reader: Callable[[int, str, str], object] | None = None,
) -> str | None:
    """Look for ``pv`` under HKLM and HKCU, both native and WOW6432Node."""
    if reader is None:
        if os.name != "nt":
            return None
        import winreg

        def reader(hive: int, subkey: str, value: str) -> object:
            try:
                with winreg.OpenKey(hive, subkey) as handle:
                    return winreg.QueryValueEx(handle, value)[0]
            except OSError:
                return None

        hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    else:
        hives = (0x80000002, 0x80000001)
    for hive in hives:
        for subkey in WEBVIEW2_SUBKEYS:
            version = parse_webview2_version(reader(hive, subkey, "pv"))
            if version:
                return version
    return None


def list_npu_devices(timeout_s: float = 20.0) -> list[str] | None:
    """Present PnP device names matching the NPU, via PowerShell. ``None`` if it failed."""
    if os.name != "nt":
        return None
    command = (
        "Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -like '*"
        + NPU_DEVICE_NAME
        + "*' } | ForEach-Object { $_.FriendlyName + ' [' + $_.Status + ']' }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def list_npu_nodes() -> list[str]:
    """Linux: the ``/dev/accel/accel*`` nodes the Intel NPU driver creates."""
    import glob

    return sorted(glob.glob(LINUX_NPU_NODES))


def probe_toolkits() -> tuple[str | None, str | None]:
    """``(gtk, qt)``: a description of each toolkit pywebview could use here, else ``None``."""
    gtk: str | None = None
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        for webkit in ("4.1", "4.0"):
            try:
                gi.require_version("WebKit2", webkit)
            except ValueError:
                continue
            gtk = f"GTK 3 + WebKit2 {webkit}"
            break
    except Exception:  # noqa: BLE001 - no PyGObject, or no typelib
        gtk = None
    qt: str | None = None
    try:
        import qtpy
        from qtpy import QtWebEngineWidgets  # noqa: F401

        qt = f"{qtpy.API_NAME} {qtpy.QT_VERSION}"
    except Exception:  # noqa: BLE001
        qt = None
    return gtk, qt


def _probe_copilot() -> str:
    """Install and remove a throwaway ``WH_KEYBOARD_LL`` hook (Windows)."""
    from chatforge.desktop import hotkey

    if os.name != "nt":
        return f"error:{hotkey.COPILOT_WINDOWS_ONLY}"
    user32 = ctypes.windll.user32
    hotkey._configure_user32(user32)  # noqa: SLF001 - the same argtypes the app's hook uses
    probe = hotkey._CopilotHook(user32, lambda: None)  # noqa: SLF001
    code = probe.install()
    if code is not None:
        return f"error:{hotkey.describe_error(hotkey.COPILOT, code)}"
    probe.remove()
    return "ok"


def _probe_x11(spec: str) -> str:
    """Grab and release the key on the X server, through the app's own grabber."""
    from chatforge.desktop.hotkey import NO_X11_DISPLAY, HotkeyThread

    if not os.environ.get("DISPLAY"):
        return f"error:{NO_X11_DISPLAY}"
    thread = HotkeyThread(lambda: None)
    try:
        error = thread.start(spec)
    finally:
        thread.stop()
    if error is None:
        via = " via XWayland" if os.environ.get("WAYLAND_DISPLAY") else ""
        return f"ok:X11 grab{via}"
    return "in_use" if "in use" in error else f"error:{error}"


def probe_hotkey(spec: str) -> str:
    """Try the platform's hotkey registration, then release it. See :func:`check_hotkey`
    for outcomes: ``RegisterHotKey`` on Windows, a keyboard hook for the Copilot key, an
    ``XGrabKey`` on Linux."""
    from chatforge.config import parse_hotkey

    try:
        _mods, key = parse_hotkey(spec)
    except ValueError as exc:
        return f"invalid:{exc}"
    if key == COPILOT_KEY:
        return _probe_copilot()
    if os.name != "nt":
        return _probe_x11(spec)
    try:
        from chatforge.desktop.hotkey import MOD_NOREPEAT, to_win32

        mods, vk = to_win32(spec)
    except ValueError as exc:
        return f"invalid:{exc}"
    user32 = ctypes.windll.user32
    if user32.RegisterHotKey(None, DOCTOR_HOTKEY_ID, mods | MOD_NOREPEAT, vk):
        user32.UnregisterHotKey(None, DOCTOR_HOTKEY_ID)
        return "ok"
    code = ctypes.GetLastError()
    if code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return "in_use"
    return f"error:Windows error {code}"


def keyring_backend_status() -> tuple[str, bool]:
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        name = f"{type(backend).__module__}.{type(backend).__name__}"
        if isinstance(backend, fail.Keyring):
            return name, False
        # A read of an entry that never exists proves the store answers.
        keyring.get_password("ChatForge", "__doctor_probe__")
        return name, True
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({type(exc).__name__})", False


def app_is_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _shim_text(path: Path | None) -> str:
    if path is None:
        return ""
    from chatforge.autostart import _read_shim

    return _read_shim(path)


def autostart_verdict(status: Any) -> Check:
    """:func:`check_autostart` for an :class:`chatforge.autostart.AutostartStatus`."""
    from chatforge import autostart

    return check_autostart(
        status.enabled,
        status.path,
        _shim_text(status.path),
        autostart.launch_argv()[0],
        mechanism=status.mechanism,
        duplicate=status.duplicate,
    )


def _safe(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one broken probe must not hide the rest
        return Check(name, "warn", f"check failed: {type(exc).__name__}: {exc}")


def load_config_readonly(paths: Any) -> tuple[Any, Check]:
    """The config for the checks, without writing anything (doctor has no side effects).

    A missing ``config.toml`` is normal before the first run: defaults are used. An invalid
    one is reported (the app itself would rename it and reset).
    """
    from chatforge.config import load_config, validate_config
    from chatforge.errors import AppError

    if not paths.config_file.exists():
        return validate_config({}), Check(
            "Config", "warn", f"{paths.config_file} does not exist yet (first run writes it)"
        )
    try:
        return load_config(paths, recover=False), Check("Config", "pass", str(paths.config_file))
    except AppError as exc:
        return validate_config({}), Check("Config", "fail", f"{paths.config_file}: {exc}")


def local_model_checks(paths: Any, cfg: Any, *, catalog: Any = None) -> list[Check]:
    """The "Model", "NPU-ready models" and "Compiled for NPU" rows.

    They describe the *local* models even when a cloud provider is selected (then
    ``chat.model`` names a cloud model, which is never on disk). The device, spec and
    compile hash come from :class:`LocalModelManager`'s own planning, so the doctor
    sees exactly what a load would use; nothing is started.
    """
    from chatforge.models.npu_compat import npu_verdict
    from chatforge.runtime import compile_cache

    shared: dict[str, Any] = {}

    def local() -> tuple[Any, Any, list[Any]]:
        if not shared:
            from chatforge.models.catalog import Catalog
            from chatforge.models.registry import Registry
            from chatforge.runtime.manager import LocalModelManager

            cat = catalog if catalog is not None else Catalog.load()
            registry = Registry(
                paths.models_dir, paths.cache_dir, catalog=cat, state_file=paths.state_file
            )
            registry.scan()
            manager = LocalModelManager(
                None,  # type: ignore[arg-type] - planning only, never started
                get_config=lambda: cfg,
                paths=paths,
                registry=registry,
                catalog=cat,
                is_installed=lambda: True,
                precompile=False,
            )
            records = [r for r in registry.all() if r.complete]
            shared.update(registry=registry, manager=manager, records=records)
        return shared["registry"], shared["manager"], shared["records"]

    def model_check() -> Check:
        registry, _manager, records = local()
        provider = cfg.providers.get(cfg.chat.provider)
        if provider is None or provider.kind == "ovms":
            target: str | None = cfg.chat.model
        else:
            target = local_target_model(cfg.chat.model, [r.id for r in records])
            if target is None:
                return Check(
                    "Model", "pass", f"no local model installed (chat uses {cfg.chat.provider})"
                )
        return check_model(str(target), registry.get(str(target)))

    def npu_check() -> Check:
        _registry, manager, records = local()
        verdicts = [(r.id, npu_verdict(r.id, r.path, manager.catalog)) for r in records]
        return check_npu_models(cfg.local.device, verdicts, cfg.local.npu_fallback_device)

    def compiled_check() -> Check:
        _registry, manager, records = local()
        version = cfg.local.ovms_version
        states: list[CompileState] = []
        for rec in records:
            try:
                spec = manager.build_spec(rec.id)
            except Exception:  # noqa: BLE001 - e.g. refused on the NPU: nothing to compile
                continue
            expected = compile_cache.expected_load_s(
                spec.cache_dir,
                state_file=paths.state_file,
                key=compile_cache.compile_key(rec.id, spec.device, spec.max_prompt_len, version),
                ovms_version=version,
                compile_hash=manager.compile_hash(spec),
                model_path=spec.model_path,
            )
            states.append(
                CompileState(rec.id, spec.device, manager.is_warm(spec), round(expected, 1))
            )
        return check_compiled_models(states, precompile=cfg.local.precompile)

    return [
        _safe("Model", model_check),
        _safe("NPU-ready models", npu_check),
        _safe("Compiled for NPU", compiled_check),
    ]


def run_checks(paths: Any = None, cfg: Any = None) -> list[Check]:
    from chatforge import autostart, secrets
    from chatforge.models.diskspace import free_bytes
    from chatforge.paths import Paths
    from chatforge.runtime import ovms_install
    from chatforge.runtime.jobobject import find_processes
    from chatforge.single_instance import SINGLE_INSTANCE_PORT

    paths = paths or Paths.default()
    if cfg is None:
        cfg, config_check = load_config_readonly(paths)
    else:
        config_check = Check("Config", "pass", "provided")
    running = app_is_running(SINGLE_INSTANCE_PORT)

    def minimax_check() -> Check:
        spec = cfg.providers.get(MINIMAX_PROVIDER)
        env_name = spec.api_key_env if spec is not None else MINIMAX_ENV
        env_present = bool(secrets._env_key(env_name))  # noqa: SLF001 - true/false only
        keyring_present = secrets._keyring_get(MINIMAX_PROVIDER) is not None  # noqa: SLF001
        return check_minimax_key(env_present, keyring_present)

    if os.name == "nt":
        platform_rows = [
            _safe("WebView2 runtime", lambda: check_webview2(read_webview2_version())),
            _safe(
                "VC++ x64 runtime",
                lambda: check_vcredist(
                    ovms_install.vcredist_present(), ovms_install.vcredist_version()
                ),
            ),
            _safe("NPU device", lambda: check_npu(list_npu_devices())),
        ]
    else:  # vcredist is None off Windows: not applicable, so no WebView2/VC++/PnP rows
        platform_rows = [
            _safe("Display", check_display),
            _safe("Web view (pywebview)", lambda: check_webview_toolkit(*probe_toolkits())),
            _safe("Tray host", check_tray_host),
            _safe("NPU device", lambda: check_npu_linux(list_npu_nodes())),
        ]

    return [
        _safe("Python", check_python),
        config_check,
        *platform_rows,
        _safe("OVMS runtime", lambda: check_ovms(ovms_install.runtime_status(paths, cfg.local))),
        *local_model_checks(paths, cfg),
        _safe("Keyring backend", lambda: check_keyring(*keyring_backend_status())),
        _safe("MiniMax key", minimax_check),
        _safe("Hotkey", lambda: check_hotkey(cfg.ui.hotkey, probe_hotkey(cfg.ui.hotkey), running)),
        _safe("Autostart", lambda: autostart_verdict(autostart.status())),
        _safe("Free disk", lambda: check_disk(free_bytes(paths.home), paths.home)),
        _safe(
            "App running",
            lambda: check_app_running(running, SINGLE_INSTANCE_PORT, find_processes("ovms")),
        ),
    ]


# --------------------------------------------------------------------------- #
# Diagnostics: the redacted report the Settings page offers for bug reports
# --------------------------------------------------------------------------- #

DIAGNOSTIC_LOG_LINES = 50
_REDACTED = "***REDACTED***"
#: A config line whose name says it holds a secret. ``*_env`` names (``api_key_env``) are
#: environment variable *names*, which help a bug report and are not secrets.
_SECRET_LINE_RE = re.compile(
    r"(?im)^(?P<head>\s*[\w.\"'-]*(?:key|token|secret|password|passwd|authorization|cookie"
    r"|credential)[\w.\"'-]*\s*=\s*)(?P<value>.*)$"
)
_URL_CREDS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@\"']+@")
_URL_QUERY_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s\"'?#]+)\?[^\s\"']*")
#: A long unbroken run of key-looking characters (``sk-...``, a pasted token).
_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b")


def redact_text(text: str) -> str:
    """``text`` with registered secrets, bearer tokens, URL credentials and query strings,
    and key-like runs removed (the log scrubber plus the config-specific rules)."""
    from chatforge.logging_setup import _scrub_text  # noqa: PLC2701 - same package

    text = _scrub_text(text)
    text = _URL_CREDS_RE.sub(rf"\1{_REDACTED}@", text)
    text = _URL_QUERY_RE.sub(r"\1?" + _REDACTED, text)
    return _TOKEN_RE.sub(_REDACTED, text)


def redact_config_text(text: str) -> str:
    """config.toml contents with the value of every key-like setting replaced."""

    def line(match: re.Match[str]) -> str:
        head = match.group("head")
        name = head.split("=")[0].strip().strip("\"'").lower()
        if name.endswith(("_env", "tokens")):
            return match.group(0)
        return f"{head}{_REDACTED}"

    return redact_text(_SECRET_LINE_RE.sub(line, text))


def _active_ids(cfg: Any) -> list[str]:
    chat = cfg.chat
    lines = [f"active provider: {chat.provider}", f"active model: {chat.model}"]
    fallback = getattr(chat, "fallback_provider", "")
    if fallback:
        lines.append(f"fallback: {fallback} / {getattr(chat, 'fallback_model', '')}")
    return lines


def diagnostics_report(
    paths: Any,
    cfg: Any,
    runtime: dict[str, Any] | None = None,
    log_lines: Iterable[str] = (),
) -> str:
    """A plain-text report to paste into a bug report: versions, the runtime state, the
    active provider and model ids, config.toml and the last log lines, all redacted
    (no API key, no URL credentials). ``log_lines`` are formatted log lines; at most the
    last :data:`DIAGNOSTIC_LOG_LINES` are used."""
    from chatforge import __version__

    out = [
        "ChatForge diagnostics",
        f"app version: {__version__}",
        f"python: {platform.python_version()} ({platform.python_implementation()})",
        f"os: {platform.platform()}",
        "",
        "[runtime]",
    ]
    for key in ("state", "model_id", "device", "error"):
        if runtime and runtime.get(key) is not None:
            out.append(f"{key}: {redact_text(str(runtime[key]))}")
    out += ["", "[chat]", *_active_ids(cfg), "", "[config.toml]"]
    try:
        raw = Path(paths.config_file).read_text(encoding="utf-8")
        out.append(redact_config_text(raw).rstrip() or "(empty)")
    except OSError:
        out.append("(no config.toml)")
    out += ["", f"[last {DIAGNOSTIC_LOG_LINES} log lines]"]
    lines = [str(x) for x in log_lines][-DIAGNOSTIC_LOG_LINES:]
    out.extend(redact_text(line) for line in lines or ["(none)"])
    return "\n".join(out) + "\n"


def main() -> int:
    """Entry point used by ``chatforge doctor``. Prints the table; 1 on any FAIL."""
    from chatforge.paths import Paths

    paths = Paths.default()
    print(f"ChatForge doctor  (home: {paths.home})")
    print()
    checks = run_checks(paths)
    print(render_table(checks))
    return exit_code(checks)


if __name__ == "__main__":  # pragma: no cover
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
