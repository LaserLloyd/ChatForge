"""``chatforge-voice``: the command line of the add-on (models, devices, the Voice panel).

Everything but ``panel`` works without ChatForge running. Imports of the engine package happen inside
the commands, so ``chatforge-voice --version`` and ``--help`` work even when it is missing, and the
other commands then say what to install.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from collections.abc import Sequence

from . import __version__, core_problem


def _boot() -> int | None:
    """Install the host link for CLI use. Returns an exit code when voice cannot run."""
    problem = core_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    from forge_voice_core import hostlink

    from . import settings
    from .host import ChatForgeHost
    from .runtime import cli_data_dir

    hostlink.install(ChatForgeHost(cli_data_dir(), lambda: settings.from_prefs({})))
    return None


def _cmd_status(_: argparse.Namespace) -> int:
    if (rc := _boot()) is not None:
        return rc
    from . import hotkey, settings
    from .models_card import ModelsService

    s = settings.from_prefs({})
    st = ModelsService(lambda: s).status()
    print(f"chatforge-voice {__version__}")
    print(f"voice models: {'installed' if st['ready'] else 'NOT installed'}")
    for item in st["missing"]:
        print(f"  missing: {item}")
    print(
        f"push-to-talk key: {'supported' if hotkey.supported() else 'Windows only; use the panel'}"
    )
    return 0


def _cmd_devices(_: argparse.Namespace) -> int:
    from .audio_io import AudioError, list_devices

    try:
        for d in list_devices():
            kind = ("in " if d["inputs"] else "") + ("out" if d["outputs"] else "")
            print(f"{d['index']:>3}  {kind:<7} {d['name']}")
    except AudioError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    if (rc := _boot()) is not None:
        return rc
    from forge_voice_core import fetch_job, models

    from . import messages, settings
    from .models_card import ModelsService

    s = settings.from_prefs({})
    svc = ModelsService(lambda: s, job=fetch_job.FetchJob())
    if args.action == "status":
        print(json.dumps({k: v for k, v in svc.status().items() if k != "job"}, indent=2))
        print(f"folder: {models.root()}")
        return 0
    if args.action == "cancel":
        print(
            "Cancel a running download with Ctrl+C in the terminal that runs it, or in the panel."
        )
        return 0
    if not args.yes:
        print(
            f"This downloads about {messages.DOWNLOAD_SIZE_MB / 1000:.1f} GB of voice models into "
            f"{models.root()}.\n{messages.ATTRIBUTION}"
        )
        if input("Download now? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    res = svc.start(confirm=True)
    if not res["ok"]:
        print(res["message"], file=sys.stderr)
        return 1
    try:
        while (st := svc.job.status())["state"] == "running":
            print(f"\r{st['percent']:5.1f}%  {st['file'][:50]:<50}", end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        svc.cancel()
        svc.job.join(30)
        print("\ncancelled")
        return 130
    st = svc.job.status()
    print(f"\r{st['state']}: {st['error'] or 'ok'}".ljust(70))
    return 0 if st["state"] == "done" else 1


def _cmd_panel(args: argparse.Namespace) -> int:
    from .panel import PANEL_FILE
    from .runtime import cli_data_dir

    info_path = cli_data_dir() / PANEL_FILE
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}/#t={info['token']}"
    except (OSError, ValueError, KeyError):
        print(
            "The Voice panel is not running. Start ChatForge (with the voice add-on installed) first.",
            file=sys.stderr,
        )
        return 1
    if args.print_url:
        print(url)
    else:
        webbrowser.open(url)
        print("Opened the Voice panel in your browser.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chatforge-voice", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"chatforge-voice {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="are the voice models installed?").set_defaults(fn=_cmd_status)
    sub.add_parser("devices", help="list audio devices").set_defaults(fn=_cmd_devices)
    m = sub.add_parser("models", help="show or download the voice models (about 1.4 GB)")
    m.add_argument("action", choices=("status", "download", "cancel"), nargs="?", default="status")
    m.add_argument("--yes", action="store_true", help="do not ask before downloading")
    m.set_defaults(fn=_cmd_models)
    pn = sub.add_parser("panel", help="open the Voice panel of the running ChatForge")
    pn.add_argument(
        "--print-url", action="store_true", help="print the address instead of opening it"
    )
    pn.set_defaults(fn=_cmd_panel)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
