"""``python -m aichat`` / ``aichat`` command line.

aichat [--hidden | --show | --settings]      run the tray app (default: show the popup)
       [--after-pid PID]                    ... once process PID has exited (Restart)
aichat runtime install|status               OVMS runtime
aichat autostart enable|disable|status      login autostart
aichat doctor                               environment check (WS9)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

#: How long ``--after-pid`` waits for the old process before starting anyway.
AFTER_PID_TIMEOUT_S = 90.0  # a slow quit (model unload + services) can take ~25 s


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aichat", description="AI Chat desktop assistant")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--hidden", action="store_true", help="start in the tray only")
    mode.add_argument("--show", action="store_true", help="start and show the popup (default)")
    mode.add_argument("--settings", action="store_true", help="start and open Settings")
    parser.add_argument(
        "--after-pid",
        type=int,
        metavar="PID",
        help=f"wait up to {AFTER_PID_TIMEOUT_S:.0f} s for process PID to exit first "
        "(the tray's Restart uses this)",
    )

    sub = parser.add_subparsers(dest="command")
    runtime = sub.add_parser("runtime", help="manage the local OVMS runtime")
    runtime.add_argument("action", choices=["install", "status"])
    auto = sub.add_parser("autostart", help="start AI Chat at login")
    auto.add_argument("action", choices=["enable", "disable", "status"])
    sub.add_parser("doctor", help="check the environment")
    return parser


def _cmd_runtime(action: str) -> int:
    from aichat.config import load_config
    from aichat.paths import Paths
    from aichat.runtime import ovms_install

    paths = Paths.default()
    paths.ensure_dirs()
    cfg = load_config(paths, recover=True)
    if action == "status":
        status = ovms_install.runtime_status(paths, cfg.local)
        for key, value in status.items():
            print(f"{key}: {value}")
        return 0 if status.get("installed") else 1

    last = {"line": ""}

    def progress(p: dict) -> None:
        status = p.get("status", "downloading")
        done = int(p.get("downloaded_bytes") or 0) / 1e6
        total = int(p.get("total_bytes") or 0) / 1e6
        line = f"{status}: {done:.1f} / {total:.1f} MB"
        if p.get("error"):
            line += f" ({p['error']})"
        if line != last["line"]:
            print(line, flush=True)
            last["line"] = line

    try:
        exe = asyncio.run(ovms_install.install(paths, cfg.local, progress, asyncio.Event()))
    except Exception as exc:  # noqa: BLE001
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    print(f"installed: {exe}")
    return 0


def _cmd_autostart(action: str) -> int:
    from aichat import autostart

    try:
        if action == "enable":
            status = autostart.enable()
        elif action == "disable":
            status = autostart.disable()
        else:
            status = autostart.status()
    except Exception as exc:  # noqa: BLE001
        print(f"autostart {action} failed: {exc}", file=sys.stderr)
        return 1
    print(status.describe())
    return 0


def _cmd_doctor() -> int:
    try:
        from aichat import doctor  # WS9 fills this in
    except ImportError:
        print("aichat doctor is not available yet (WS9).")
        return 2
    run = getattr(doctor, "main", None)
    if run is None:
        print("aichat doctor is not available yet (WS9).")
        return 2
    return int(run() or 0)


def wait_for_exit(pid: int, timeout_s: float = AFTER_PID_TIMEOUT_S) -> bool:
    """Wait (bounded) for process ``pid`` to exit. ``True`` once it is gone (or was never
    there, or is not the process that started us); ``False`` after ``timeout_s``.

    The tray's Restart starts the new copy just before the old one quits; the old one holds
    the single-instance port until it exits, so the new copy waits here first.
    """
    import psutil

    if pid <= 0 or pid == os.getpid():
        return True
    try:
        proc = psutil.Process(pid)
        # A pid the system already reused belongs to a process younger than this one.
        if proc.create_time() > psutil.Process().create_time():
            return True
        proc.wait(timeout=timeout_s)
    except psutil.TimeoutExpired:
        return False
    except (psutil.Error, ValueError, OSError):
        return True  # gone already, or not ours to watch
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "runtime":
        return _cmd_runtime(args.action)
    if args.command == "autostart":
        return _cmd_autostart(args.action)
    if args.command == "doctor":
        return _cmd_doctor()

    if args.after_pid is not None:
        wait_for_exit(args.after_pid)  # then start anyway: the port check decides

    from aichat.app import main as run_app

    mode = "hidden" if args.hidden else "settings" if args.settings else "show"
    return run_app(mode)


if __name__ == "__main__":
    sys.exit(main())
