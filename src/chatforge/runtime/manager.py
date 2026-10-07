"""The one local model: load, lease, switch, unload, status (PLAN §1.4, §2 WS6, §3, §7).

Adapted from StudioForge src/studioforge/core/manager.py (MIT, LaserLloyd): the
in-flight counter with the ``mark_request_start/end`` try/finally discipline of
``openai_routes._forward``, and the "check under the lock" shape the idle sweeper
relies on. Cut: leases/pins, reconciler, rebalance, eviction, priority tiers, planner.

Concurrency model
-----------------
* ``lock`` (one ``asyncio.Lock``) serialises every load / unload / switch decision.
  ``IdleReaper`` takes it too, so a request that arrives while the reaper unloads
  waits, then reloads: it never sees a dead port.
* The actual OVMS start runs in a background *load task*. A caller waiting for it
  can be cancelled (the user pressed Stop) without killing a 6-minute compile; the
  next caller joins the same task. ``unload()`` cancels it.
* ``lease()`` increments ``in_flight`` under the lock (reloading if needed), then
  releases the lock and holds ``Semaphore(1)``: the NPU runs one request at a time.
  ``last_activity`` is touched at the start and the end; the decrement is in
  ``finally``.
* A child that exits on its own sets state ``error``; the next request reloads once.
* ``unload(reason)`` first fires every in-flight lease's ``on_cancel`` callback (the
  engine reports ``cancelled``), waits briefly for them to finish, then stops OVMS.
  Switching to another model does the same for the old model's leases first, so a
  reply that is still streaming ends as ``cancelled`` rather than a dropped connection.

Device
------
``local.device`` is where models run, except a model that is known not to work on the
NPU (:func:`chatforge.models.npu_compat.npu_verdict`: catalog avoid list, or an export the
NPU cannot run). With ``device = "NPU"`` such a model runs on
``local.npu_fallback_device`` (GPU by default) and, if that load fails, on the CPU.
``status()["device"]`` is the device in use and ``status()["device_fallback"]`` says
why it is not the configured one. ``npu_fallback_device = "none"`` refuses instead.

Background precompile
---------------------
:meth:`precompile` loads a model only to fill its compile cache, and only while nothing
else is loaded, loading or in flight (``runtime.precompile.Precompiler`` drives it,
started by :meth:`start`). A caller that asks for the same model meanwhile joins that
load and the model then stays loaded; a caller for another model cancels it. While it
runs, ``status()["background"]`` is true.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from chatforge.llm.errors import LLMError
from chatforge.logging_setup import get_logger
from chatforge.models import npu_compat
from chatforge.runtime import compile_cache
from chatforge.runtime.ovms_supervisor import (
    NPU_DEVICE_NODE,
    OVMS_VERSION,
    LaunchSpec,
    OvmsError,
    npu_unavailable_on_host,
    spec_compile_hash,
)

log = get_logger(__name__)

RuntimeState = Literal[
    "not_installed", "unloaded", "starting", "compiling", "ready", "unloading", "error"
]
LOADING_STATES = frozenset({"starting", "compiling"})
#: Where an NPU-incompatible model goes when the configured fallback fails too.
LAST_RESORT_DEVICE = "CPU"
#: Load failures after which a fallback device is given up for the next one.
FALLBACK_RETRY_CODES = frozenset({"exited", "model_failed"})
#: ``unload()`` reason after a background precompile.
PRECOMPILED = "precompiled"

StatusCallback = Callable[[dict], None]


class SupervisorLike(Protocol):
    """What the manager needs from ``OvmsSupervisor`` (tests pass a stub)."""

    exit_event: asyncio.Event

    async def start(
        self, spec: LaunchSpec, on_tick: Callable[[str, float], None] | None = None
    ) -> str: ...

    async def stop(self, timeout_s: float = 10) -> None: ...

    def is_alive(self) -> bool: ...


@dataclass
class _Lease:
    model_id: str
    on_cancel: Callable[[], None] | None
    cancelled: bool = False


def _consume_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


def _log_task_failure(task: asyncio.Task) -> None:
    """Done-callback for a background task nobody awaits: its failure is logged, not lost."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background_task_failed", task=task.get_name(), error=repr(exc)[:300])


def _cfg(get_config: Callable[[], Any], dotted: str, default: Any) -> Any:
    node: Any = get_config()
    for part in dotted.split("."):
        if node is None:
            return default
        node = node.get(part) if isinstance(node, dict) else getattr(node, part, None)
    return default if node is None else node


class LocalModelManager:
    """Owns the single OVMS child through ``supervisor``.

    ``get_config`` returns the live ``AppConfig`` (read on every load, so a changed
    ``local.device`` / ``max_prompt_len`` / ``extra_args`` reloads on next use).
    ``registry`` (``models.registry.Registry``) resolves model folders; ``catalog``
    (``models.catalog.Catalog``) supplies tool/reasoning parsers. ``clock`` is the
    monotonic clock shared with ``IdleReaper``; ``wall_clock`` feeds ``unload_at``.
    """

    def __init__(
        self,
        supervisor: SupervisorLike,
        *,
        get_config: Callable[[], Any],
        paths: Any,
        registry: Any = None,
        catalog: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        is_installed: Callable[[], bool] | None = None,
        health_check: bool = True,
        unload_grace_s: float = 5.0,
        stop_timeout_s: float = 10.0,
        precompile: bool = True,
    ) -> None:
        self.supervisor = supervisor
        self._get_config = get_config
        self.paths = paths
        self.registry = registry
        self._catalog = catalog
        self._clock = clock
        self._wall = wall_clock
        self._is_installed = is_installed
        self.health_check = health_check
        self.unload_grace_s = unload_grace_s
        self.stop_timeout_s = stop_timeout_s

        self.lock = asyncio.Lock()
        self._npu = asyncio.Semaphore(1)
        self.state: RuntimeState = "unloaded"
        self.model_id: str | None = None
        self.device: str | None = None
        self.base_url: str | None = None
        self.error: str | None = None
        self.in_flight = 0
        self.last_activity = self._clock()
        self.first_compile = False
        self.expected_s: float | None = None
        self.last_unload: dict | None = None
        self._load_started: float | None = None
        self._spec: LaunchSpec | None = None
        self._spec_key: tuple | None = None
        self._load_task: asyncio.Task[str] | None = None
        self._loading_key: tuple | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._leases: dict[int, _Lease] = {}
        self._lease_seq = 0
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._subscribers: list[StatusCallback] = []
        #: ``{"from", "to", "reason"}`` while the loaded/loading model is not on the
        #: configured device (see "Device" above).
        self.device_fallback: dict[str, str] | None = None
        #: (model id, device) pairs whose fallback load failed in this run.
        self._failed_devices: set[tuple[str, str]] = set()
        #: The ``_key`` of a background precompile no caller has asked for yet.
        self._precompile_key: tuple | None = None
        self._precompile_enabled = precompile
        self._precompile_task: asyncio.Task[None] | None = None
        self.precompiler: Any = None

    # ------------------------------------------------------------------ #
    # Config helpers
    # ------------------------------------------------------------------ #

    def idle_ttl_s(self) -> float:
        """``local.idle_unload_minutes`` in seconds (0 = never). Pass to ``IdleReaper``."""
        return float(_cfg(self._get_config, "local.idle_unload_minutes", 10)) * 60.0

    @property
    def catalog(self) -> Any:
        if self._catalog is None:
            from chatforge.models.catalog import Catalog

            self._catalog = Catalog.load()
        return self._catalog

    def model_supports_tools(self, model_id: str) -> bool:
        """Unknown non-Qwen models run with tools disabled (catalog badge)."""
        try:
            return bool(self.catalog.badge(model_id).tools_enabled)
        except Exception:  # noqa: BLE001 - a broken catalog must not break chat
            return True

    def installed(self) -> bool:
        if self._is_installed is not None:
            return bool(self._is_installed())
        if getattr(self.supervisor, "command_prefix", None):
            return True
        exe = getattr(self.supervisor, "exe", None)
        return exe is None or Path(exe).is_file()

    def _model_path(self, model_id: str) -> Path:
        if self.registry is not None:
            rec = self.registry.get(model_id)
            if rec is None and hasattr(self.registry, "scan"):
                self.registry.scan()
                rec = self.registry.get(model_id)
            if rec is None:
                raise LLMError(
                    f"The model {model_id} is not installed",
                    code="not_found",
                    hint="Download it in Settings → Models, or pick another model.",
                    action="open_settings",
                )
            if not getattr(rec, "complete", True):
                missing = ", ".join(getattr(rec, "missing", [])[:4])
                raise LLMError(
                    f"The model {model_id} is incomplete",
                    code="not_found",
                    hint=f"Missing files: {missing}. Download it again in Settings → Models.",
                    action="open_settings",
                )
            return Path(rec.path)
        publisher, _, name = model_id.partition("/")
        return Path(self.paths.models_dir) / publisher / name

    def setting(self, dotted: str, default: Any) -> Any:
        """A value of the live config (``"local.precompile"``), or ``default``."""
        return _cfg(self._get_config, dotted, default)

    def build_spec(self, model_id: str) -> LaunchSpec:
        """The ``LaunchSpec`` for ``model_id`` under the current config and catalog."""
        return self.plan(model_id)[0]

    _warned_no_npu = False

    def _resolve_device(self, model_id: str, model_path: Path) -> tuple[str, dict[str, str] | None]:
        """The device to run ``model_id`` on, and why it is not the configured one."""
        device = str(_cfg(self._get_config, "local.device", "NPU")).upper()
        if "NPU" not in device:
            return device, None
        if npu_unavailable_on_host(device):
            # Linux without the NPU driver node: CPU for this launch (the config is kept),
            # chosen here so the compile-cache key is the CPU's.
            reason = f"no {NPU_DEVICE_NODE} on this host (Intel NPU driver not installed)"
            if not self._warned_no_npu:
                self._warned_no_npu = True
                log.warning("npu_unavailable_using_cpu", reason=reason)
            return LAST_RESORT_DEVICE, {"from": device, "to": LAST_RESORT_DEVICE, "reason": reason}
        verdict = npu_compat.npu_verdict(model_id, model_path, self._catalog_safe())
        if verdict.ok is not False:
            return device, None
        short = model_id.rpartition("/")[2]
        wanted = str(_cfg(self._get_config, "local.npu_fallback_device", "GPU"))
        if wanted.lower() == "none":
            raise LLMError(
                f"{short} does not run correctly on the NPU",
                code="model_loading_failed",
                hint=f"{verdict.reason} {self._npu_alternative_hint()}".strip(),
                action="open_settings",
                details={"npu": verdict.to_dict()},
            )
        order = [wanted.upper()]
        if LAST_RESORT_DEVICE not in order:
            order.append(LAST_RESORT_DEVICE)
        for candidate in order:
            if (model_id, candidate) not in self._failed_devices:
                return candidate, {"from": device, "to": candidate, "reason": verdict.reason}
        raise LLMError(
            f"{short} does not run correctly on the NPU and failed on {' and '.join(order)}",
            code="model_loading_failed",
            hint=f"{verdict.reason} {self._npu_alternative_hint()}".strip(),
            action="open_settings",
            details={"npu": verdict.to_dict(), "failed_devices": order},
        )

    def _catalog_safe(self) -> Any:
        try:
            return self.catalog
        except Exception:  # noqa: BLE001 - a broken catalog must not block a load
            return None

    def _npu_alternative_hint(self) -> str:
        try:
            entry = self.catalog.recommended()
        except Exception:  # noqa: BLE001
            entry = None
        if entry is None:
            return "Pick a model marked for the NPU in Settings → Models."
        return f"{entry.id} runs on the NPU; pick it in Settings → Models."

    def plan(self, model_id: str) -> tuple[LaunchSpec, dict[str, str] | None]:
        """``build_spec`` plus the device fallback that applies (``None`` if none)."""
        max_len = int(_cfg(self._get_config, "local.max_prompt_len", 4096))
        extra = list(_cfg(self._get_config, "local.extra_args", []) or [])
        model_path = self._model_path(model_id)
        device, fallback = self._resolve_device(model_id, model_path)
        badge = self.catalog.badge(model_id)
        cache_dir = compile_cache.cache_dir_for(
            Path(self.paths.cache_dir), model_id, device, max_len
        )
        log_path = getattr(self.paths, "ovms_log", None)
        if log_path is None and getattr(self.paths, "logs_dir", None) is not None:
            log_path = Path(self.paths.logs_dir) / "ovms.log"
        spec = LaunchSpec(
            model_id=model_id,
            model_path=model_path,
            device=device,
            max_prompt_len=max_len,
            cache_dir=cache_dir,
            tool_parser=badge.tool_parser,
            reasoning_parser=badge.reasoning_parser,
            port=0,
            extra_args=extra,
            log_path=log_path,
        )
        return spec, fallback

    def compile_hash(self, spec: LaunchSpec) -> str:
        return spec_compile_hash(spec, ovms_version=self._ovms_version())

    def cache_key(self, spec: LaunchSpec) -> str:
        """``state.json`` key of one compile: ``<id>|NPU|4096|2026.4.0|<compile hash>``."""
        base = compile_cache.compile_key(
            spec.model_id, spec.device, spec.max_prompt_len, self._ovms_version()
        )
        return f"{base}|{self.compile_hash(spec)}"

    def is_warm(self, spec: LaunchSpec) -> bool:
        """Whether loading ``spec`` reuses a compiled blob (no first-time compile)."""
        return compile_cache.is_compiled(
            spec.cache_dir, ovms_version=self._ovms_version(), compile_hash=self.compile_hash(spec)
        )

    def _key(self, spec: LaunchSpec) -> tuple:
        variant = _cfg(self._get_config, "local.ovms_variant", "python_on")
        return (
            spec.model_id,
            str(spec.model_path),
            spec.device,
            spec.max_prompt_len,
            spec.tool_parser,
            spec.reasoning_parser,
            tuple(spec.extra_args),
            variant,
        )

    def _ovms_version(self) -> str:
        return str(
            getattr(self.supervisor, "ovms_version", None)
            or _cfg(self._get_config, "local.ovms_version", OVMS_VERSION)
        )

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    def touch(self) -> None:
        self.last_activity = self._clock()

    def status(self) -> dict:
        state = self.state
        if state in ("unloaded", "not_installed"):
            state = "unloaded" if self.installed() else "not_installed"
            self.state = state
        ttl = self.idle_ttl_s()
        now = self._clock()
        elapsed = None
        if state in LOADING_STATES and self._load_started is not None:
            elapsed = round(max(0.0, now - self._load_started), 1)
        unload_at = None
        if state == "ready" and ttl > 0 and self.in_flight == 0:
            unload_at = round(self._wall() + max(0.0, ttl - (now - self.last_activity)), 1)
        background = (
            self._precompile_key is not None
            and self._precompile_key == self._loading_key
            and state in (*LOADING_STATES, "ready")
        )
        return {
            "state": state,
            "model_id": self.model_id,
            "device": self.device,
            "elapsed_s": elapsed,
            "expected_s": self.expected_s,
            "first_compile": self.first_compile,
            "idle_timeout_s": ttl,
            "unload_at": unload_at,
            "in_flight": self.in_flight,
            "error": self.error,
            "last_unload": self.last_unload,
            "background": background,
            "device_fallback": dict(self.device_fallback) if self.device_fallback else None,
        }

    def subscribe(self, cb: StatusCallback) -> Callable[[], None]:
        self._subscribers.append(cb)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(cb)

        return unsubscribe

    def _emit(self) -> None:
        snap = self.status()
        for cb in list(self._subscribers):
            try:
                cb(dict(snap))
            except Exception:  # noqa: BLE001 - one bad subscriber must not break loading
                log.exception("runtime_status_subscriber_failed")

    def _set_state(self, state: RuntimeState, *, error: str | None = None) -> None:
        self.state = state
        self.error = error
        self._emit()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Initial state (``not_installed`` or ``unloaded``). Nothing is loaded.

        With a registry, also starts the background precompiler (it waits a while,
        then compiles cold models whenever the runtime is idle).
        """
        self.state = "unloaded" if self.installed() else "not_installed"
        self._emit()
        if self._precompile_enabled and self.registry is not None and self._precompile_task is None:
            from chatforge.runtime.precompile import Precompiler

            self.precompiler = Precompiler(self)
            self._precompile_task = asyncio.create_task(
                self.precompiler.run(), name="ovms-precompiler"
            )
            self._precompile_task.add_done_callback(_log_task_failure)

    async def aclose(self) -> None:
        """Stop the precompiler, unload (reason ``shutdown``) and close the supervisor."""
        task = self._precompile_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            self._precompile_task = None
        with contextlib.suppress(Exception):
            await self.unload("shutdown")
        if self._watch_task is not None:
            self._watch_task.cancel()
        aclose = getattr(self.supervisor, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    # ------------------------------------------------------------------ #
    # Load
    # ------------------------------------------------------------------ #

    async def ensure_loaded(self, model_id: str) -> str:
        """Load (or switch to) ``model_id`` and return its base URL (``…/v3``)."""
        async with self.lock:
            return await self._ensure_locked(model_id)

    async def _child_ok(self) -> bool:
        try:
            if not self.supervisor.is_alive():
                return False
        except Exception:  # noqa: BLE001
            return False
        health = getattr(self.supervisor, "health", None) if self.health_check else None
        if health is None:
            return True
        try:
            return bool(await health())
        except Exception:  # noqa: BLE001
            return False

    async def _ensure_locked(self, model_id: str) -> str:
        if not self.installed():
            self._set_state("not_installed")
            raise LLMError(
                "The local model runtime is not installed",
                code="model_loading_failed",
                hint="Install it in Settings → Models → Runtime.",
                action="open_settings",
            )
        spec, fallback = self.plan(model_id)
        key = self._key(spec)
        if self._precompile_key is not None and self._precompile_key == key:
            # Someone wants the model a background precompile is loading: keep it loaded.
            self._precompile_key = None

        task = self._load_task
        if task is not None and not task.done():
            if self._loading_key == key:
                return await self._await_load(task)
            task.cancel()
            await asyncio.wait({task})

        if self.state == "ready" and self._spec_key == key and self.base_url:
            if await self._child_ok():
                self.touch()
                return self.base_url
            log.warning("ovms_unhealthy_reloading", model_id=model_id)

        if self.state in ("ready", "error", "unloading") or self._spec is not None:
            # switch model, apply a reload-required setting, or clean up after a crash
            await self._drain_other_leases(model_id)
            self._generation += 1
            with contextlib.suppress(Exception):
                await self.supervisor.stop(self.stop_timeout_s)
            self._spec = self._spec_key = None
            self.base_url = None

        self._loading_key = key
        task = asyncio.create_task(self._load(spec, key, fallback=fallback), name="ovms-load")
        task.add_done_callback(_consume_result)  # a failure nobody awaits is still "retrieved"
        self._load_task = task
        return await self._await_load(task)

    async def _drain_other_leases(self, model_id: str) -> None:
        """Cancel the leases of other models (a switch) and give them a moment to end.

        Their ``on_cancel`` makes the engine report ``cancelled``; without this the
        stop below would cut a streaming reply off mid-way. Leases on ``model_id``
        itself are left alone (the crash/reload path runs inside one of them).
        """
        others = [lease for lease in self._leases.values() if lease.model_id != model_id]
        if not others:
            return
        log.info("model_switch_cancels_leases", count=len(others), to_model=model_id)
        for lease in others:
            self._cancel_lease(lease)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.unload_grace_s
        while loop.time() < deadline and any(
            lease.model_id != model_id for lease in self._leases.values()
        ):
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------------ #
    # Background precompile
    # ------------------------------------------------------------------ #

    def idle_for_background(self) -> bool:
        """Nothing loaded, loading or in flight: a background compile disturbs no one."""
        task = self._load_task
        return (
            self.state == "unloaded"
            and self.in_flight == 0
            and self._spec is None
            and (task is None or task.done())
        )

    async def precompile(self, model_id: str) -> str:
        """Fill ``model_id``'s compile cache without anyone waiting on it.

        Returns ``"warm"`` (nothing to do), ``"busy"`` (the runtime is in use),
        ``"cancelled"`` (a caller took the runtime over meanwhile) or ``"compiled"``.
        The model is unloaded afterwards unless a caller asked for it meanwhile.
        Raises :class:`LLMError` when the load fails.
        """
        async with self.lock:
            if not self.installed() or not self.idle_for_background():
                return "busy"
            spec, fallback = self.plan(model_id)
            if self.is_warm(spec):
                return "warm"
            key = self._key(spec)
            self._precompile_key = key
            self._loading_key = key
            task = asyncio.create_task(
                self._load(spec, key, fallback=fallback), name="ovms-precompile"
            )
            task.add_done_callback(_consume_result)
            self._load_task = task
        try:
            # Not under the lock: a chat request must be able to join or cancel it.
            await asyncio.wait({task})
            if task.cancelled():
                return "cancelled"
            exc = task.exception()
            if exc is not None:
                raise exc
            async with self.lock:
                if (
                    self._precompile_key == key
                    and self._spec_key == key
                    and self.state == "ready"
                    and self.in_flight == 0
                ):
                    await self.unload_locked(PRECOMPILED)
            return "compiled"
        finally:
            if self._precompile_key == key:
                self._precompile_key = None

    async def _await_load(self, task: asyncio.Task[str]) -> str:
        try:
            base = await asyncio.shield(task)
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if task.cancelled() and (me is None or me.cancelling() == 0):
                raise LLMError(
                    "Loading the model was cancelled",
                    code="cancelled",
                    hint="The model was unloaded while it was loading.",
                ) from None
            raise
        self.touch()
        return base

    def _on_tick(self, phase: str, _elapsed: float) -> None:
        if self.state not in LOADING_STATES:
            return
        compiling = phase == "compiling" or self.first_compile
        state: RuntimeState = "compiling" if compiling else "starting"
        self.state = state
        self._emit()  # the supervisor ticks about once a second while loading

    async def _load(
        self, spec: LaunchSpec, key: tuple, *, fallback: dict[str, str] | None = None
    ) -> str:
        version = self._ovms_version()
        chash = self.compile_hash(spec)
        self._generation += 1
        gen = self._generation
        self.model_id = spec.model_id
        self.device = spec.device
        self.device_fallback = dict(fallback) if fallback else None
        self.base_url = None
        self._load_started = self._clock()
        self.first_compile = not compile_cache.is_compiled(
            spec.cache_dir, ovms_version=version, compile_hash=chash
        )
        self.expected_s = round(
            compile_cache.expected_load_s(
                spec.cache_dir,
                state_file=getattr(self.paths, "state_file", None),
                key=compile_cache.compile_key(
                    spec.model_id, spec.device, spec.max_prompt_len, version
                ),
                ovms_version=version,
                compile_hash=chash,
                model_path=spec.model_path,
            ),
            1,
        )
        self._set_state("compiling" if self.first_compile else "starting")
        if fallback:
            log.warning(
                "npu_incompatible_model_fallback",
                model_id=spec.model_id,
                configured=fallback.get("from"),
                device=spec.device,
                reason=fallback.get("reason"),
            )
        log.info(
            "model_load_start",
            model_id=spec.model_id,
            device=spec.device,
            first_compile=self.first_compile,
            expected_s=self.expected_s,
            background=self._precompile_key == key,
        )
        try:
            base = await self.supervisor.start(spec, on_tick=self._on_tick)
        except asyncio.CancelledError:
            self._load_started = None
            self._set_state("unloaded")
            raise
        except OvmsError as exc:
            self._load_started = None
            background = self._precompile_key == key
            if fallback and exc.code in FALLBACK_RETRY_CODES:
                # The fallback device cannot run it either: the next plan skips it.
                self._failed_devices.add((spec.model_id, spec.device))
                log.warning(
                    "fallback_device_failed",
                    model_id=spec.model_id,
                    device=spec.device,
                    ovms_code=exc.code,
                )
                if not background:
                    retry = self._retry_plan(spec)
                    if retry is not None:
                        next_spec, next_fallback = retry
                        next_key = self._key(next_spec)
                        self._loading_key = next_key
                        return await self._load(next_spec, next_key, fallback=next_fallback)
            if background:
                # Nobody asked for this load: no error banner, the precompiler logs it.
                self._set_state("unloaded")
            else:
                state: RuntimeState = "not_installed" if exc.code == "not_installed" else "error"
                self._set_state(state, error=exc.message)
            raise LLMError(
                "The local model failed to load",
                code="model_loading_failed",
                hint=(exc.hint or "See Settings → Logs.") + f" ({exc.code})",
                action="open_settings" if exc.code == "not_installed" else "retry",
                details={"ovms_code": exc.code, "device": spec.device},
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self._load_started = None
            if self._precompile_key == key:
                self._set_state("unloaded")
            else:
                self._set_state("error", error=f"{type(exc).__name__}: {str(exc)[:300]}")
            raise LLMError(
                "The local model failed to load",
                code="model_loading_failed",
                hint=f"{type(exc).__name__}: {str(exc)[:200]}. See Settings → Logs.",
            ) from exc
        self._load_started = None
        self._spec, self._spec_key = spec, key
        self.base_url = base
        self.touch()
        if self._precompile_key != key:
            self._record_last_used(spec.model_id)
        self._set_state("ready")
        log.info("model_ready", model_id=spec.model_id, device=spec.device, base_url=base)
        self._start_watch(gen)
        return base

    def _retry_plan(self, failed: LaunchSpec) -> tuple[LaunchSpec, dict[str, str] | None] | None:
        """The plan after ``failed``'s device was given up, if it picks another device."""
        try:
            spec, fallback = self.plan(failed.model_id)
        except LLMError:
            return None
        if spec.device == failed.device:
            return None
        return spec, fallback

    def _start_watch(self, gen: int) -> None:
        if self._watch_task is not None and not self._watch_task.done():
            self._watch_task.cancel()
        event = getattr(self.supervisor, "exit_event", None)
        if event is None:
            return
        self._watch_task = asyncio.create_task(self._watch_exit(event, gen), name="ovms-watch")
        self._watch_task.add_done_callback(_log_task_failure)

    async def _watch_exit(self, event: asyncio.Event, gen: int) -> None:
        """``exit_event`` is set on every exit, deliberate or not. Our own stops bump
        ``_generation`` first; ``supervisor.crashed`` tells a crash from a stop that
        someone else asked for."""
        await event.wait()
        if gen != self._generation or self.state != "ready":
            return  # we stopped it ourselves, or it was replaced
        self._generation += 1
        self.base_url = None
        if not getattr(self.supervisor, "crashed", True):
            log.info("ovms_stopped_externally", model_id=self.model_id)
            self.last_unload = {"reason": "stopped", "at": round(self._wall(), 1)}
            self._set_state("unloaded")
            return
        message = getattr(self.supervisor, "last_error", None) or "The local model server exited."
        log.warning("ovms_child_exited", model_id=self.model_id)
        self._set_state("error", error=str(message)[:2000])

    # ------------------------------------------------------------------ #
    # Lease
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def lease(
        self, model_id: str, *, on_cancel: Callable[[], None] | None = None
    ) -> AsyncIterator[str]:
        """Use the loaded model for one request: yields its base URL.

        ``on_cancel`` is called if the model is unloaded (by the user) while the lease
        is held; the holder should stop its request.
        """
        async with self.lock:
            base_url = await self._ensure_locked(model_id)
            self.in_flight += 1
            self._idle_event.clear()
            self._lease_seq += 1
            token = self._lease_seq
            lease = _Lease(model_id, on_cancel)
            self._leases[token] = lease
            self.touch()
        try:
            async with self._npu:
                if lease.cancelled:
                    raise LLMError("The model was unloaded", code="cancelled")
                if self.state != "ready" or self.base_url != base_url:
                    # it crashed (or was reloaded) while we queued for the NPU: reload once
                    async with self.lock:
                        base_url = await self._ensure_locked(model_id)
                self.touch()
                yield base_url
        finally:
            self._leases.pop(token, None)
            self.in_flight -= 1
            self.touch()
            if self.in_flight == 0:
                self._idle_event.set()
            self._emit()

    # ------------------------------------------------------------------ #
    # Unload
    # ------------------------------------------------------------------ #

    async def unload(self, reason: str = "user") -> None:
        """Stop the model. In-flight requests are cancelled first; a load in progress
        is abandoned (its waiters get ``LLMError(cancelled)``)."""
        self._cancel_leases()
        task = self._load_task
        if task is not None and not task.done():
            task.cancel()
        async with self.lock:
            await self.unload_locked(reason)

    def _cancel_leases(self) -> None:
        for lease in list(self._leases.values()):
            self._cancel_lease(lease)

    @staticmethod
    def _cancel_lease(lease: _Lease) -> None:
        if lease.cancelled:
            return
        lease.cancelled = True
        if lease.on_cancel is not None:
            try:
                lease.on_cancel()
            except Exception:  # noqa: BLE001
                log.exception("lease_cancel_callback_failed")

    async def unload_locked(self, reason: str) -> None:
        """Unload while the caller holds ``lock`` (``IdleReaper`` uses this)."""
        task = self._load_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        if reason != "idle":
            self._cancel_leases()
        if self.in_flight > 0:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.unload_grace_s):
                    await self._idle_event.wait()
        had_model = self._spec is not None or self.state not in ("unloaded", "not_installed")
        self._generation += 1
        if had_model:
            self._set_state("unloading", error=None)
            try:
                await self.supervisor.stop(self.stop_timeout_s)
            except Exception as exc:  # noqa: BLE001
                log.warning("ovms_stop_failed", error=str(exc)[:200])
        if self._spec is not None and reason != PRECOMPILED:
            self._record_last_used(self._spec.model_id)
        self._spec = self._spec_key = None
        self.base_url = None
        self._load_started = None
        self.last_unload = {"reason": reason, "at": round(self._wall(), 1)}
        self._record_unload(reason)
        log.info("model_unloaded", model_id=self.model_id, reason=reason)
        self._set_state("unloaded" if self.installed() else "not_installed")

    # ------------------------------------------------------------------ #
    # state.json
    # ------------------------------------------------------------------ #

    def _state_file(self) -> Path | None:
        sf = getattr(self.paths, "state_file", None)
        return Path(sf) if sf is not None else None

    def _record_last_used(self, model_id: str) -> None:
        now = self._wall()

        def mutate(state: dict) -> None:
            state.setdefault("last_used", {})[model_id] = now

        with contextlib.suppress(Exception):
            if self._state_file() is not None:
                compile_cache.update_state(self._state_file(), mutate)

    def _record_unload(self, reason: str) -> None:
        entry = {"reason": reason, "at": self._wall(), "model_id": self.model_id}

        def mutate(state: dict) -> None:
            state["last_unload"] = entry

        with contextlib.suppress(Exception):
            if self._state_file() is not None:
                compile_cache.update_state(self._state_file(), mutate)


__all__ = [
    "LOADING_STATES",
    "PRECOMPILED",
    "LocalModelManager",
    "RuntimeState",
    "SupervisorLike",
]
