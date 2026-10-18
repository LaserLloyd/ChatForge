"""First-run "Set up local AI": the runtime, the starter model, a check, one progress bar.

A new install has neither the OpenVINO Model Server runtime (139 MB) nor a model. This
module is the one-click path from there to a first chat. It only sequences pieces that
already exist and are tested on their own:

1. **runtime**: :func:`chatforge.runtime.ovms_install.install` (SHA-256 pinned archive);
2. **model**: the catalog's recommended model through :class:`chatforge.models.downloader.
   Downloader` (resumable, every file hashed against Hugging Face's own SHA-256);
3. **verify**: the registry rescans the folder and must call the model complete;
4. **ready**: the chat is pointed at the model (when the user had not picked another).

State and progress are one dict (:meth:`LocalSetup.snapshot`), pushed as ``setup.progress``
events. Cancel works at every stage and removes the partial files of the model; starting
again resumes what is left (the runtime download and the model both resume).

The device is decided by :mod:`chatforge.runtime.npu_detect`: with no working NPU the
snapshot says the model will run on the CPU (slower), which is exactly what the launch
path will do. Nothing here rewrites ``local.device``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from chatforge.logging_setup import get_logger
from chatforge.models import diskspace

log = get_logger(__name__)

#: Phases, in order. ``idle`` = nothing running; the others are the stage in progress or the
#: way a run ended.
PHASES = ("idle", "checking", "runtime", "model", "verifying", "done", "error", "cancelled")
RUNNING_PHASES = frozenset({"checking", "runtime", "model", "verifying"})
#: The runtime archive and what is left of the model must fit with this much to spare
#: (the extracted runtime is 357 MiB; downloads keep a ``.part`` file beside the result).
RUNTIME_EXTRACTED_BYTES = 360 * 1024**2
FREE_SPACE_MARGIN_BYTES = 2 * 1024**3
#: Used for the bar until the runtime archive's and the model's real sizes are known.
RUNTIME_ARCHIVE_BYTES = 138_798_816
DEFAULT_MODEL_BYTES = 936_000_000


class SetupError(Exception):
    """A setup step failed; the message is already user-facing."""

    def __init__(self, message: str, hint: str = "", code: str = "setup_failed") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


def _human_gb(n: float) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


class LocalSetup:
    """The "Set up local AI" state machine. Every method runs on the core loop."""

    def __init__(
        self,
        *,
        paths: Any,
        get_config: Callable[[], Any],
        catalog: Any,
        registry: Any,
        downloader: Any,
        emit: Callable[[dict[str, Any]], None],
        apply_patch: Callable[[dict[str, Any]], Any],
        install_runtime: Callable[..., Awaitable[Any]] | None = None,
        runtime_installed: Callable[[], bool] | None = None,
        probe_npu: Callable[[], Awaitable[Any]] | None = None,
        free_bytes: Callable[[Path], int | None] | None = None,
        on_runtime_installed: Callable[[], None] | None = None,
    ) -> None:
        self._paths = paths
        self._get_config = get_config
        self._catalog = catalog
        self._registry = registry
        self._downloader = downloader
        self._emit = emit
        self._apply_patch = apply_patch
        self._install_runtime = install_runtime or self._default_install
        self._runtime_installed = runtime_installed or self._default_runtime_installed
        self._probe_npu = probe_npu or self._default_probe
        self._free_bytes = free_bytes or diskspace.free_bytes
        self._on_runtime_installed = on_runtime_installed
        self._task: asyncio.Task[None] | None = None
        self._cancel = asyncio.Event()
        self._gid: str | None = None
        self._phase = "idle"
        self._error: dict[str, str] | None = None
        # Bytes per stage, for the one weighted bar.
        self._rt = {"done": 0, "total": RUNTIME_ARCHIVE_BYTES, "speed": 0.0, "eta": None}
        self._md = {"done": 0, "total": 0, "speed": 0.0, "eta": None}
        self._detail = ""
        self._device_note: dict[str, Any] | None = None
        self._verified: dict[str, Any] | None = None

    # ------------------------------------------------------------------ defaults

    async def _default_install(self, progress: Callable[[dict[str, Any]], None]) -> Any:
        from chatforge.runtime import ovms_install

        cfg = self._get_config()
        return await ovms_install.install(self._paths, cfg.local, progress, self._cancel)

    def _default_runtime_installed(self) -> bool:
        from chatforge.runtime import ovms_install

        cfg = self._get_config()
        return bool(ovms_install.runtime_status(self._paths, cfg.local)["installed"])

    @staticmethod
    async def _default_probe() -> Any:
        from chatforge.runtime import npu_detect

        if os.name != "nt":
            from chatforge import doctor

            nodes = doctor.list_npu_nodes()
            if nodes:
                return npu_detect.NpuProbe(True, "an Intel NPU device node exists")
            return npu_detect.NpuProbe(
                False, "no /dev/accel NPU device node (driver not installed)"
            )
        cached = npu_detect.cached()
        if cached is not None:
            return cached
        return await asyncio.to_thread(npu_detect.probe)

    # ------------------------------------------------------------------ queries

    @property
    def model_id(self) -> str | None:
        entry = self._catalog.recommended() if self._catalog is not None else None
        return entry.id if entry is not None else None

    def _entry(self) -> Any:
        return self._catalog.recommended() if self._catalog is not None else None

    def _model_complete(self) -> bool:
        mid = self.model_id
        if not mid or self._registry is None:
            return False
        rec = self._registry.get(mid)
        return bool(rec is not None and getattr(rec, "complete", False))

    def _local_selected(self) -> bool:
        cfg = self._get_config()
        spec = cfg.providers.get(cfg.chat.provider)
        return spec is None or spec.kind == "ovms"

    def needed(self) -> bool:
        """Should the first-run card be offered? The local provider is the selected one, the
        user has not said "Not now", and the runtime or the starter model is missing."""
        cfg = self._get_config()
        if cfg.startup.local_setup_dismissed or self.model_id is None:
            return False
        if not self._local_selected():
            return False
        return not (self._runtime_installed() and self._any_local_model())

    def _any_local_model(self) -> bool:
        if self._registry is None:
            return False
        return any(getattr(r, "complete", False) for r in self._registry.all())

    def snapshot(self) -> dict[str, Any]:
        """The card's state: phase, one weighted progress figure, sizes, device, error."""
        entry = self._entry()
        model_total = self._md["total"] or int((entry.approx_gb or 0.94) * 1e9 if entry else 0)
        rt_total = max(self._rt["total"], 1)
        done = min(self._rt["done"], rt_total) + min(self._md["done"], model_total or 0)
        total = rt_total + model_total
        if self._runtime_ready_before:
            done, total = min(self._md["done"], model_total), model_total
        percent = 100.0 * done / total if total > 0 else 0.0
        if self._phase == "done":
            percent = 100.0
        stage = self._md if self._phase in ("model", "verifying") else self._rt
        message = self._detail or self._default_message(entry)
        return {
            "phase": self._phase,
            "running": self._phase in RUNNING_PHASES,
            "needed": self.needed() if self._phase not in RUNNING_PHASES else True,
            "percent": round(min(percent, 100.0), 1),
            "downloaded_bytes": int(done),
            "total_bytes": int(total),
            "speed_bps": float(stage["speed"] or 0.0),
            "eta_s": stage["eta"],
            "message": message,
            "model_id": self.model_id,
            "model_label": entry.label if entry else None,
            "model_licence": entry.licence if entry else None,
            "model_bytes": int(model_total),
            "runtime_bytes": int(rt_total),
            "runtime_installed": self._runtime_installed(),
            "model_installed": self._model_complete(),
            "device": self._device_label(),
            "device_note": self._device_note,
            "verified": self._verified,
            "failure": self._error,
        }

    _runtime_ready_before = False

    def _default_message(self, entry: Any) -> str:
        label = entry.label if entry else "the starter model"
        return {
            "idle": f"Download the local runtime and {label} (about 1.1 GB).",
            "checking": "Checking this PC",
            "runtime": "Installing the local runtime",
            "model": f"Downloading {label}",
            "verifying": "Checking the downloaded files",
            "done": "Local AI is ready",
            "error": "Setup stopped",
            "cancelled": "Setup cancelled",
        }[self._phase]

    def _device_label(self) -> str | None:
        note = self._device_note
        if note is None:
            return None
        return str(note.get("label") or "")

    # ------------------------------------------------------------------ control

    async def warm(self) -> None:
        """Find out which device the model will run on, so the card can say so before the
        user presses anything. Pushes the result to the page. Never raises."""
        try:
            if self._device_note is None:
                await self._decide_device()
                self._push()
        except Exception as exc:  # noqa: BLE001 - a label is optional
            log.warning("setup_warm_failed", error=type(exc).__name__)

    async def start(self) -> dict[str, Any]:
        """Begin (or resume) the setup. A run already in progress is left alone."""
        if self._task is not None and not self._task.done():
            return self.snapshot()
        self._cancel = asyncio.Event()
        self._error = None
        self._verified = None
        self._phase = "checking"
        self._detail = ""
        self._runtime_ready_before = False
        self._push()
        self._task = asyncio.create_task(self._run(), name="setup-local-ai")
        return self.snapshot()

    async def cancel(self) -> dict[str, Any]:
        """Stop at the current stage. The model's partial files are deleted."""
        task = self._task
        if task is None or task.done():
            return self.snapshot()
        self._cancel.set()
        if self._gid is not None:
            with contextlib.suppress(Exception):
                await self._downloader.cancel(self._gid, delete_partial=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=15)
        return self.snapshot()

    def dismiss(self) -> None:
        """ "Not now": do not offer the card again (Settings > Models still works)."""
        self._apply_patch({"startup": {"local_setup_dismissed": True}})
        self._push()

    async def aclose(self) -> None:
        task = self._task
        if task is not None and not task.done():
            self._cancel.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ------------------------------------------------------------------ the run

    def _push(self) -> None:
        with contextlib.suppress(Exception):
            self._emit({"type": "setup.progress", **self.snapshot()})

    def _set_phase(self, phase: str, detail: str = "") -> None:
        self._phase = phase
        self._detail = detail
        self._push()

    async def _run(self) -> None:
        try:
            await self._run_steps()
        except asyncio.CancelledError:
            self._set_phase("cancelled")
            raise
        except SetupError as exc:
            if self._cancel.is_set():
                self._set_phase("cancelled")
            else:
                self._error = {"message": exc.message, "hint": exc.hint, "code": exc.code}
                log.warning("setup_failed", code=exc.code, message=exc.message)
                self._set_phase("error")
        except Exception as exc:  # noqa: BLE001 - the card must always end in a state
            if self._cancel.is_set():
                self._set_phase("cancelled")
                return
            message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
            self._error = {
                "message": str(message)[:300],
                "hint": str(getattr(exc, "hint", "") or ""),
                "code": str(getattr(exc, "code", "") or "setup_failed"),
            }
            log.warning("setup_failed", error=type(exc).__name__, message=str(message)[:200])
            self._set_phase("error")

    async def _run_steps(self) -> None:
        entry = self._entry()
        if entry is None:
            raise SetupError(
                "No starter model is listed in this build",
                "Open Settings > Models and download one.",
                "no_catalog_model",
            )
        # 1. This PC: NPU or CPU, and disk space, before the first byte.
        await self._decide_device()
        self._check_disk(entry)
        if self._cancel.is_set():
            raise SetupError("cancelled", code="cancelled")

        # 2. The runtime.
        if self._runtime_installed():
            self._runtime_ready_before = True
        else:
            self._set_phase("runtime")
            await self._install_runtime_step()
        if self._cancel.is_set():
            raise SetupError("cancelled", code="cancelled")

        # 3. The model.
        if not self._model_complete():
            self._set_phase("model")
            await self._download_step(entry)
        if self._cancel.is_set():
            raise SetupError("cancelled", code="cancelled")

        # 4. Check what is on disk.
        self._set_phase("verifying")
        await self._verify_step(entry)

        # 5. Point the chat at it and finish.
        self._select_model(entry.id)
        self._set_phase("done")
        log.info("setup_done", model=entry.id)

    async def _decide_device(self) -> None:
        cfg = self._get_config()
        wanted = str(cfg.local.device).upper()
        if wanted != "NPU":
            self._device_note = {
                "device": wanted,
                "label": f"Runs on the {wanted}",
                "slower": wanted == "CPU",
            }
            return
        probe = await self._probe_npu()
        present = getattr(probe, "present", None)
        if present is False:
            self._device_note = {
                "device": "CPU",
                "label": "No Intel NPU found: runs on the CPU (slower)",
                "slower": True,
                "reason": getattr(probe, "reason", ""),
            }
        else:
            self._device_note = {
                "device": "NPU",
                "label": "Runs on the Intel NPU" if present else "Runs on the NPU if one is found",
                "slower": False,
            }

    def _check_disk(self, entry: Any) -> None:
        need = FREE_SPACE_MARGIN_BYTES
        if not self._runtime_installed():
            need += RUNTIME_ARCHIVE_BYTES + RUNTIME_EXTRACTED_BYTES
        if not self._model_complete():
            need += int((entry.approx_gb or 1.0) * 1e9)
        free = self._free_bytes(Path(self._paths.home))
        if free is not None and free < need:
            raise SetupError(
                f"Not enough disk space: {_human_gb(free)} free, {_human_gb(need)} needed",
                "Free some space on the drive that holds your ChatForge folder, then try again.",
                "insufficient_disk",
            )

    async def _install_runtime_step(self) -> None:
        def progress(p: dict[str, Any]) -> None:
            status = p.get("status", "downloading")
            total = int(p.get("total_bytes") or 0)
            if total:
                self._rt["total"] = total
            self._rt["done"] = int(p.get("downloaded_bytes") or 0)
            self._rt["speed"] = float(p.get("speed_bps") or 0.0)
            self._rt["eta"] = p.get("eta_s")
            if status == "extracting":
                self._detail = "Unpacking the local runtime"
            elif status == "downloading":
                self._detail = "Downloading the local runtime"
            self._push()

        try:
            await self._install_runtime(progress)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._cancel.is_set():
                raise SetupError("cancelled", code="cancelled") from exc
            raise SetupError(
                str(getattr(exc, "message", None) or exc) or "The runtime could not be installed",
                "Check the internet connection and press Try again; the download resumes.",
                str(getattr(exc, "code", "") or "runtime_install_failed"),
            ) from exc
        self._rt["done"] = self._rt["total"]
        if self._on_runtime_installed is not None:
            try:
                self._on_runtime_installed()  # the model chip and status line stop saying "not installed"
            except Exception as exc:  # noqa: BLE001
                log.warning("setup_runtime_hook_failed", error=type(exc).__name__)

    async def _download_step(self, entry: Any) -> None:
        dl = self._downloader
        try:
            gid = await dl.enqueue(entry.id)
        except Exception as exc:
            raise SetupError(
                str(getattr(exc, "message", None) or exc),
                str(getattr(exc, "hint", "") or "Check the internet connection and try again."),
                str(getattr(exc, "code", "") or "download_failed"),
            ) from exc
        self._gid = gid

        def on_progress(payload: dict[str, Any]) -> None:
            if str(payload.get("group_id")) != gid:
                return
            summary = dl.get(gid)
            if summary is None:
                return
            self._md["done"] = int(summary.get("downloaded_bytes") or 0)
            self._md["total"] = int(summary.get("total_bytes") or 0)
            self._md["speed"] = float(summary.get("speed_bps") or 0.0)
            self._md["eta"] = summary.get("eta_s")
            self._push()

        unsubscribe = dl.subscribe(on_progress)
        try:
            summary = dl.get(gid)
            if summary is not None:
                on_progress({"group_id": gid})
            await dl.join(gid)
        finally:
            unsubscribe()
        if self._cancel.is_set():
            raise SetupError("cancelled", code="cancelled")
        summary = dl.get(gid) or {}
        if summary.get("status") != "completed":
            raise SetupError(
                str(summary.get("error") or "The model download did not finish"),
                "Press Try again; the download resumes where it stopped.",
                "download_failed",
            )
        self._md["done"] = self._md["total"] = int(summary.get("total_bytes") or self._md["total"])

    async def _verify_step(self, entry: Any) -> None:
        # The downloader already hashed every file against Hugging Face's SHA-256 as it
        # arrived; here the registry must agree the folder holds the whole model.
        await asyncio.to_thread(self._registry.scan)
        rec = self._registry.get(entry.id)
        if rec is None or not getattr(rec, "complete", False):
            missing = ", ".join(getattr(rec, "missing", [])[:4]) if rec is not None else "all files"
            raise SetupError(
                "The downloaded model is incomplete",
                f"Missing: {missing}. Press Try again to fetch what is missing.",
                "verify_failed",
            )
        summary = (self._downloader.get(self._gid) if self._gid else None) or {}
        self._verified = {
            "files": int(summary.get("files_total") or 0),
            "bytes": int(getattr(rec, "size_bytes", 0) or 0),
            "sha256_checked": bool(self._gid),
        }

    def _select_model(self, model_id: str) -> None:
        """Point the chat at the starter model unless the user already picked another local
        one. Never fails the setup."""
        try:
            cfg = self._get_config()
            current = cfg.providers.get(cfg.chat.provider)
            keep_choice = (
                current is not None
                and current.kind == "ovms"
                and cfg.chat.model
                and cfg.chat.model != model_id
                and self._registry.get(cfg.chat.model) is not None
            )
            if not keep_choice:
                self._apply_patch({"chat": {"provider": "local-npu", "model": model_id}})
        except Exception as exc:  # noqa: BLE001
            log.warning("setup_select_model_failed", error=type(exc).__name__)
