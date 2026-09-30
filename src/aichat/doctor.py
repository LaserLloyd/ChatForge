"""``aichat doctor``: an environment health check with a pass/warn/fail table.

Every probe is wrapped so a broken machine still gets a full table; the pure parts
(thresholds, version parsing, table rendering, exit code) are unit-tested. Nothing here
prints a secret: the MiniMax checks report ``True``/``False`` only.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
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
    return Check("Python", "fail", f"{text}; AI Chat needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")


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


def check_ovms(status: dict[str, Any]) -> Check:
    if not status.get("installed"):
        return Check("OVMS runtime", "fail", "not installed; run: aichat runtime install")
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
    """``outcome`` is ``"ok"``, ``"in_use"``, ``"invalid:<msg>"`` or ``"error:<msg>"``."""
    if outcome == "ok":
        return Check("Hotkey", "pass", f"{spec} is free")
    if outcome == "in_use":
        if app_running:
            return Check("Hotkey", "pass", f"{spec} is held by the running AI Chat instance")
        return Check("Hotkey", "fail", f"{spec} is in use by another app; change it in Settings")
    kind, _, message = outcome.partition(":")
    status: Status = "fail" if kind == "invalid" else "warn"
    return Check("Hotkey", status, f"{spec}: {message or kind}")


def check_autostart(enabled: bool, path: Path | None, shim_text: str, expected_exe: str) -> Check:
    if not enabled:
        return Check(
            "Autostart", "warn", "not enabled; turn on 'Start at login' in the tray or Settings"
        )
    where = str(path) if path else "Startup folder"
    problems = []
    if "--hidden" not in shim_text:
        problems.append("shim does not pass --hidden")
    if expected_exe and expected_exe.casefold() not in shim_text.casefold():
        problems.append(f"shim does not point at {expected_exe}")
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


def check_app_running(running: bool, port: int, ovms_pids: list[int]) -> Check:
    if running:
        detail = f"yes (single-instance port {port} in use)"
        if ovms_pids:
            detail += f"; ovms.exe pids {ovms_pids} (model loaded)"
        return Check("App running", "pass", detail)
    if ovms_pids:
        return Check(
            "App running",
            "warn",
            f"no, but stray ovms.exe pids {ovms_pids} are alive; end them in Task Manager",
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


def probe_hotkey(spec: str) -> str:
    """Try ``RegisterHotKey`` then unregister. See :func:`check_hotkey` for outcomes."""
    if os.name != "nt":
        return "error:hotkeys are Windows-only"
    try:
        from aichat.desktop.hotkey import MOD_NOREPEAT, to_win32

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
        keyring.get_password("AIChat", "__doctor_probe__")
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
    from aichat.autostart import _read_shim

    return _read_shim(path)


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
    from aichat.config import load_config, validate_config
    from aichat.errors import AppError

    if not paths.config_file.exists():
        return validate_config({}), Check(
            "Config", "warn", f"{paths.config_file} does not exist yet (first run writes it)"
        )
    try:
        return load_config(paths, recover=False), Check("Config", "pass", str(paths.config_file))
    except AppError as exc:
        return validate_config({}), Check("Config", "fail", f"{paths.config_file}: {exc}")


def run_checks(paths: Any = None, cfg: Any = None) -> list[Check]:
    from aichat import autostart, secrets
    from aichat.models.diskspace import free_bytes
    from aichat.paths import Paths
    from aichat.runtime import compile_cache, ovms_install
    from aichat.runtime.jobobject import find_processes
    from aichat.single_instance import SINGLE_INSTANCE_PORT

    paths = paths or Paths.default()
    if cfg is None:
        cfg, config_check = load_config_readonly(paths)
    else:
        config_check = Check("Config", "pass", "provided")
    running = app_is_running(SINGLE_INSTANCE_PORT)
    model_id = cfg.chat.model
    device = cfg.local.device

    def model_check() -> Check:
        from aichat.models.catalog import Catalog
        from aichat.models.registry import Registry

        registry = Registry(
            paths.models_dir, paths.cache_dir, catalog=Catalog.load(), state_file=paths.state_file
        )
        registry.scan()
        return check_model(model_id, registry.get(model_id))

    def compiled_check() -> Check:
        cache_dir = compile_cache.cache_dir_for(
            paths.cache_dir, model_id, device, cfg.local.max_prompt_len
        )
        key = compile_cache.compile_key(
            model_id, device, cfg.local.max_prompt_len, cfg.local.ovms_version
        )
        compiled = compile_cache.is_compiled(cache_dir, ovms_version=cfg.local.ovms_version)
        expected = compile_cache.expected_load_s(
            cache_dir, state_file=paths.state_file, key=key, ovms_version=cfg.local.ovms_version
        )
        return check_compiled(model_id, device, compiled, expected)

    def minimax_check() -> Check:
        spec = cfg.providers.get(MINIMAX_PROVIDER)
        env_name = spec.api_key_env if spec is not None else MINIMAX_ENV
        env_present = bool(secrets._env_key(env_name))  # noqa: SLF001 - true/false only
        keyring_present = secrets._keyring_get(MINIMAX_PROVIDER) is not None  # noqa: SLF001
        return check_minimax_key(env_present, keyring_present)

    def autostart_check() -> Check:
        status = autostart.status()
        return check_autostart(
            status.enabled, status.path, _shim_text(status.path), autostart.launch_argv()[0]
        )

    return [
        _safe("Python", check_python),
        config_check,
        _safe("WebView2 runtime", lambda: check_webview2(read_webview2_version())),
        _safe(
            "VC++ x64 runtime",
            lambda: check_vcredist(
                ovms_install.vcredist_present(), ovms_install.vcredist_version()
            ),
        ),
        _safe("NPU device", lambda: check_npu(list_npu_devices())),
        _safe("OVMS runtime", lambda: check_ovms(ovms_install.runtime_status(paths, cfg.local))),
        _safe("Model", model_check),
        _safe("Compiled for NPU", compiled_check),
        _safe("Keyring backend", lambda: check_keyring(*keyring_backend_status())),
        _safe("MiniMax key", minimax_check),
        _safe("Hotkey", lambda: check_hotkey(cfg.ui.hotkey, probe_hotkey(cfg.ui.hotkey), running)),
        _safe("Autostart", autostart_check),
        _safe("Free disk", lambda: check_disk(free_bytes(paths.home), paths.home)),
        _safe(
            "App running",
            lambda: check_app_running(running, SINGLE_INSTANCE_PORT, find_processes("ovms.exe")),
        ),
    ]


def main() -> int:
    """Entry point used by ``aichat doctor``. Prints the table; 1 on any FAIL."""
    from aichat.paths import Paths

    paths = Paths.default()
    print(f"AI Chat doctor  (home: {paths.home})")
    print()
    checks = run_checks(paths)
    print(render_table(checks))
    return exit_code(checks)


if __name__ == "__main__":  # pragma: no cover
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
