"""The Models card: what is missing, what the download costs, and the download itself.

A thin layer over forge-voice-core's ``fetch_job`` (one background job with progress and cancel),
``sysreq`` (operating-system programs) and ``settings.missing()``. The job is injectable so tests run
a fake body instead of downloading 1.4 GB.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import messages

MIN_FREE_GB = 2.5


def _friendly(missing: list[str]) -> list[str]:
    """The engine package words its list for a different app; say it in this add-on's terms."""
    out: list[str] = []
    for item in missing:
        if item.startswith("dispatch-voice models"):
            continue  # the same thing is already listed model by model
        if item.startswith("model "):
            out.append(item.replace("model ", "voice model: ", 1))
        elif item.startswith("python package"):
            out.append(item + " (reinstall chatforge-voice)")
        else:
            out.append(item)
    return out or (["voice models"] if missing else [])


class ModelsService:
    def __init__(
        self,
        settings: Callable[[], Any],
        *,
        job: Any = None,
        sysreq_check: Callable[[], list[dict]] | None = None,
        models_root: Callable[[], Path] | None = None,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self._settings = settings
        self._job = job
        self._sysreq = sysreq_check
        self._root = models_root
        self._free = free_bytes or (lambda p: shutil.disk_usage(p).free)

    @property
    def job(self) -> Any:
        if self._job is None:
            from forge_voice_core import fetch_job

            self._job = fetch_job.JOB
        return self._job

    def _root_dir(self) -> Path:
        if self._root is not None:
            return self._root()
        from forge_voice_core import models

        return models.root()

    def _programs(self) -> list[dict]:
        if self._sysreq is not None:
            return self._sysreq()
        from forge_voice_core import sysreq

        return sysreq.check()

    def status(self) -> dict:
        s = self._settings()
        try:
            missing = _friendly(s.missing())
        except Exception as exc:  # noqa: BLE001 - e.g. numpy/onnxruntime failed to import
            missing = [f"the speech engines could not be checked ({type(exc).__name__})"]
        job = self.job.status()
        return {
            "ready": not missing,
            "missing": missing,
            "job": job,
            "download_mb": messages.DOWNLOAD_SIZE_MB,
            "ram_gb": messages.RAM_NEEDED_GB,
            "licences": list(messages.LICENCES),
            "attribution": messages.ATTRIBUTION,
            "programs": [
                {
                    "id": r["id"],
                    "found": bool(r["ok"]),
                    "what": r["what"],
                    "needed_for": r["required_for"],
                    "fix": r["fix"],
                    "needed_for_push_to_talk": False,
                }
                for r in self._programs()
            ],
            "notices": list(s.notices),
        }

    def start(self, *, confirm: bool = False) -> dict:
        """Begin the download. ``confirm`` is the person agreeing to the size and the licences."""
        if not confirm:
            return {
                "ok": False,
                "code": "confirm_required",
                "message": f"This downloads about {messages.DOWNLOAD_SIZE_MB / 1000:.1f} GB of voice "
                f"models. {messages.ATTRIBUTION} Confirm to start.",
            }
        root = self._root_dir()
        probe = root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free = self._free(probe)
        except OSError:
            free = None
        if free is not None and free < MIN_FREE_GB * 1024**3:
            return {
                "ok": False,
                "code": "disk_space",
                "message": f"Not enough free disk space: about {MIN_FREE_GB:g} GB is needed for the "
                f"download and its unpacking, {free / 1024**3:.1f} GB is free.",
            }
        if not self.job.start():
            return {"ok": False, "code": "running", "message": "A download is already running."}
        return {"ok": True}

    def cancel(self) -> dict:
        return {"ok": bool(self.job.cancel())}
