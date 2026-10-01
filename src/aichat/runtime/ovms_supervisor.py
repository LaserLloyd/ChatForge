"""Supervision of the single ``ovms.exe`` child: argv, env, spawn, readiness, stop.

Adapted from StudioForge src/studioforge/core/supervisor.py (MIT, LaserLloyd):
``_spawn`` (suspended -> assign to job -> resume), ``_resume``, ``_pump``, the
``_await_ready`` loop, ``_Instance.stderr_ring``/``stderr_tail``, ``_drain_pumps``,
``_log_child_exit`` and ``redact_argv``. Cut: llama.cpp flags, feature probing,
speculative decoding, VRAM, placements, identity confirmation via ``/props``,
policy checks and the multi-instance map -- AI Chat runs one model at a time.

Facts this module relies on were measured in WS1 Phase A (docs/RUNTIME-NOTES.md):

* ``/v2/health/ready`` answers 503 while the model loads and 200 once it is
  servable; ``/v1/config`` is ``{}`` while loading (no ``LOADING`` state is ever
  shown for an LLM graph) and then reports ``"state": "AVAILABLE"``. A failed
  load logs ``LOADING_PRECONDITION_FAILED`` and the process exits with code 1.
* ``--max_prompt_len`` is an NPU-only plugin property: CPU and GPU refuse it
  ("Unsupported property MAX_PROMPT_LEN by CPU plugin") and OVMS exits.
* With ``--task`` on the command line OVMS builds the graph **in memory** and
  ignores any ``graph.pbtxt`` in the model folder (a stale 2048 graph lost to a
  4096 CLI flag). ``start()`` still removes a stale ``graph.pbtxt`` when the
  launch hash changes (PLAN §7 required change 2), which keeps the folder honest
  for anyone who runs ``ovms --config_path`` by hand.
* A busy REST port logs ``Bind address failed`` / ``Failed to start REST server``
  and exits with code 1 about 25 s later; ``start()`` retries once on a new port.
* OVMS enables API-key auth when an ``API_KEY`` env var is present, so
  :func:`ovms_env` strips it along with the venv's Python variables.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO, Any

import httpx
import structlog

from aichat.runtime import compile_cache
from aichat.runtime.jobobject import (
    WindowsChildJob,
    create_child_job,
    describe_exit_code,
    kill_process_tree,
    process_create_time,
    process_is_alive,
    resume_process,
    spawn_creationflags,
    track_pid,
    untrack_pid,
)
from aichat.runtime.ports import CHILD_HOST, DEFAULT_PORT_START, pick_free_port

log = structlog.get_logger(__name__)

OVMS_VERSION = "2026.4.0"
DEFAULT_VARIANT = "python_on"

#: ``/v1/config`` state of a servable model.
MODEL_STATE_AVAILABLE = "AVAILABLE"
#: ``/v1/config`` / log states that mean the load will not succeed.
MODEL_STATES_FAILED = frozenset({"LOADING_PRECONDITION_FAILED", "UNLOADING", "END", "RETIRED"})

#: Lines of child output kept in memory for error reporting.
OUTPUT_RING_SIZE = 200
#: Substrings of the OVMS log that mean "the REST port was taken".
BIND_FAILURE_MARKERS = ("Bind address failed", "Failed to start REST server")
#: Env vars never passed to the child. The venv's Python variables would point
#: the bundled interpreter at the wrong stdlib; ``API_KEY`` would silently turn
#: on OVMS's API-key auth and every request would 401.
STRIPPED_ENV = frozenset(
    {
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
        "API_KEY",
    }
)
#: Flags whose value must never be logged.
_SECRET_VALUE_FLAGS = frozenset({"--api_key_file"})

TickCallback = Callable[[str, float], None]


class OvmsError(RuntimeError):
    """The local model server could not be started or failed.

    ``code``: ``not_installed`` | ``spawn`` | ``port`` | ``exited`` |
    ``model_failed`` | ``timeout``. ``hint`` is a user-facing next step.
    WS9 may re-parent this onto ``aichat.errors.AppError``.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.details = details or {}


class _BindFailed(Exception):
    pass


# ---------------------------------------------------------------------------
# Launch spec and argv (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchSpec:
    """Everything that determines one OVMS launch. ``port=0`` means "pick one"."""

    model_id: str
    model_path: Path
    device: str
    max_prompt_len: int
    cache_dir: Path
    tool_parser: str | None = None
    reasoning_parser: str | None = None
    port: int = 0
    #: ``None`` leaves OVMS's default (true; on NPU it becomes
    #: ``NPUW_LLM_ENABLE_PREFIX_CACHING``). True/False are passed explicitly.
    enable_prefix_caching: bool | None = None
    extra_args: list[str] = field(default_factory=list)
    log_path: Path | None = None
    log_level: str = "INFO"

    @property
    def is_npu(self) -> bool:
        return "NPU" in self.device.upper()


def launch_hash(
    spec: LaunchSpec, *, variant: str = DEFAULT_VARIANT, ovms_version: str = OVMS_VERSION
) -> str:
    """Hash of every field that changes what OVMS compiles or how it serves.

    The port and log path are deliberately excluded: they change on every run.
    """
    payload = {
        "model_path": str(spec.model_path),
        "device": spec.device.upper(),
        "max_prompt_len": int(spec.max_prompt_len),
        "tool_parser": spec.tool_parser,
        "reasoning_parser": spec.reasoning_parser,
        "cache_dir": str(spec.cache_dir),
        "enable_prefix_caching": spec.enable_prefix_caching,
        "extra_args": list(spec.extra_args),
        "variant": variant,
        "ovms_version": ovms_version,
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def spec_compile_hash(spec: LaunchSpec, *, ovms_version: str = OVMS_VERSION) -> str:
    """:func:`compile_cache.compile_hash` of the fields of ``spec`` that change the blob."""
    return compile_cache.compile_hash(
        model_path=spec.model_path,
        device=spec.device,
        max_prompt_len=spec.max_prompt_len,
        ovms_version=ovms_version,
        extra_args=spec.extra_args,
        enable_prefix_caching=spec.enable_prefix_caching,
    )


def build_argv(exe: Path, spec: LaunchSpec) -> list[str]:
    """The exact OVMS command line for ``spec``. Pure; ``spec.port`` must be set."""
    if spec.port <= 0:
        raise ValueError("build_argv needs a concrete port; resolve port=0 first")
    argv = [
        str(exe),
        "--rest_port",
        str(spec.port),
        "--rest_bind_address",
        CHILD_HOST,
        "--model_name",
        spec.model_id,
        "--model_path",
        str(spec.model_path),
        "--task",
        "text_generation",
        "--target_device",
        spec.device.upper(),
    ]
    if spec.is_npu:
        # NPU-only plugin property; CPU/GPU refuse it and OVMS exits.
        argv += ["--max_prompt_len", str(int(spec.max_prompt_len))]
    argv += ["--cache_dir", str(spec.cache_dir)]
    if spec.tool_parser:
        argv += ["--tool_parser", spec.tool_parser]
    if spec.reasoning_parser:
        argv += ["--reasoning_parser", spec.reasoning_parser]
    if spec.enable_prefix_caching is not None:
        argv += ["--enable_prefix_caching", "true" if spec.enable_prefix_caching else "false"]
    argv += ["--log_level", spec.log_level]
    argv += [str(a) for a in spec.extra_args]
    return argv


def _is_absolute_path(token: str) -> bool:
    return PureWindowsPath(token).is_absolute() or PurePosixPath(token).is_absolute()


# Adapted from StudioForge src/studioforge/core/supervisor.py redact_argv (MIT, LaserLloyd)
def redact_argv(argv: Sequence[str]) -> list[str]:
    """The argv as it may appear in the app log: absolute paths reduced to their
    basename (no username or disk layout) and secret-carrying values replaced."""
    out: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            out.append("<redacted>")
            redact_next = False
            continue
        if token in _SECRET_VALUE_FLAGS:
            out.append(token)
            redact_next = True
            continue
        if _is_absolute_path(token):
            out.append(PureWindowsPath(token).name or PurePosixPath(token).name or token)
        else:
            out.append(token)
    return out


def _split_path(value: str) -> list[str]:
    return [p for p in value.split(os.pathsep) if p]


def ovms_env(ovms_dir: Path, base: Mapping[str, str]) -> dict[str, str]:
    """The child environment: ``base`` minus the venv, plus what ``setupvars.ps1`` sets.

    ``setupvars.ps1`` (2026.4.0) sets, when ``ovms\\python`` exists (python_on)::

        OVMS_DIR   = <ovms dir>
        PYTHONHOME = <ovms dir>\\python
        SCRIPTS    = <ovms dir>\\python\\Scripts
        PATH       = <ovms dir>;<PYTHONHOME>;<SCRIPTS>;<old PATH>
        ESPEAK_DATA_PATH = <ovms dir>\\espeak-ng-data   (if present)

    and for python_off only ``PATH = <old PATH>;<ovms dir>``. It never sets
    ``PYTHONPATH``; the bundled interpreter is isolated by ``python312._pth``.
    Keys are matched case-insensitively (Windows env semantics).
    """
    ovms_dir = Path(ovms_dir)
    env: dict[str, str] = {}
    old_path = ""
    venv = None
    for key, value in base.items():
        name = key.upper()
        if name == "VIRTUAL_ENV":
            venv = value
        if name in STRIPPED_ENV:
            continue
        if name == "PATH":
            old_path = value
            continue
        env[key] = value
    path_parts = _split_path(old_path)
    if venv:
        venv_bins = {
            os.path.normcase(os.path.normpath(os.path.join(venv, d))) for d in ("Scripts", "bin")
        }
        path_parts = [
            p for p in path_parts if os.path.normcase(os.path.normpath(p)) not in venv_bins
        ]
    env["OVMS_DIR"] = str(ovms_dir)
    python = ovms_dir / "python"
    if python.is_dir():
        env["PYTHONHOME"] = str(python)
        env["SCRIPTS"] = str(python / "Scripts")
        env["PATH"] = os.pathsep.join(
            [str(ovms_dir), str(python), str(python / "Scripts"), *path_parts]
        )
    else:
        env["PATH"] = os.pathsep.join([*path_parts, str(ovms_dir)])
    espeak = ovms_dir / "espeak-ng-data"
    if espeak.is_dir():
        env["ESPEAK_DATA_PATH"] = str(espeak)
    return env


def parse_model_state(config: Any, model_id: str) -> str | None:
    """The newest version's ``state`` for ``model_id`` in a ``/v1/config`` body."""
    if not isinstance(config, dict):
        return None
    entry = config.get(model_id)
    if not isinstance(entry, dict):
        return None
    versions = entry.get("model_version_status")
    if not isinstance(versions, list) or not versions:
        return None
    best = None
    for item in versions:
        if not isinstance(item, dict):
            continue
        try:
            ver = int(item.get("version", 0))
        except (TypeError, ValueError):
            ver = 0
        if best is None or ver >= best[0]:
            best = (ver, item.get("state"))
    return str(best[1]) if best and best[1] is not None else None


def rotate_log(path: Path, *, max_bytes: int = 5_000_000, backups: int = 3) -> None:
    """``ovms.log`` -> ``ovms.log.1`` ... when larger than ``max_bytes``. Never raises."""
    with contextlib.suppress(OSError):
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for i in range(backups, 0, -1):
            src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
            dst = path.with_name(f"{path.name}.{i}")
            if src.exists():
                with contextlib.suppress(OSError):
                    dst.unlink(missing_ok=True)
                    os.replace(src, dst)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class OvmsSupervisor:
    """Owns at most one ``ovms.exe`` child.

    ``start()`` returns the OpenAI base URL (``http://127.0.0.1:<port>/v3``) once
    the model is ``AVAILABLE``. ``exit_event`` is set whenever the child ends
    (cleared by the next ``start()``); ``crashed`` says whether that exit was
    unrequested. ``on_tick(phase, elapsed_s)`` is called about once a second with
    ``phase`` in ``starting`` (process up, REST not listening yet), ``compiling``
    (REST up, cold compile cache) or ``loading`` (REST up, warm cache), and once
    more with ``ready``.
    """

    def __init__(
        self,
        exe: Path,
        *,
        variant: str = DEFAULT_VARIANT,
        ovms_version: str = OVMS_VERSION,
        state_file: Path | None = None,
        base_env: Mapping[str, str] | None = None,
        load_timeout_s: float = 900.0,
        poll_interval_s: float = 0.5,
        tick_interval_s: float = 1.0,
        http_timeout_s: float = 2.0,
        command_prefix: Sequence[str] = (),
        port_start: int = DEFAULT_PORT_START,
        use_job: bool = True,
        log_max_bytes: int = 5_000_000,
        log_backups: int = 3,
    ) -> None:
        self.exe = Path(exe)
        self.variant = variant
        self.ovms_version = ovms_version
        self.state_file = Path(state_file) if state_file is not None else None
        self._base_env = base_env
        self.load_timeout_s = load_timeout_s
        self.poll_interval_s = poll_interval_s
        self.tick_interval_s = tick_interval_s
        self.http_timeout_s = http_timeout_s
        self.command_prefix = list(command_prefix)
        self.port_start = port_start
        self.use_job = use_job
        self.log_max_bytes = log_max_bytes
        self.log_backups = log_backups

        self.exit_event = asyncio.Event()
        self.state = "stopped"  # stopped|starting|loading|ready|stopping|exited|failed
        self.spec: LaunchSpec | None = None
        self.port: int | None = None
        self.pid: int | None = None
        self.base_url: str | None = None
        self.exit_code: int | None = None
        self.crashed = False
        self.last_error: str | None = None
        self.load_s: float | None = None
        self.first_compile = False
        self.expected_s: float | None = None
        self.model_state: str | None = None
        self.graph_removed = False
        self.argv: list[str] = []

        self._proc: asyncio.subprocess.Process | None = None
        self._create_time: float | None = None
        self._pump: asyncio.Task[None] | None = None
        self._watch: asyncio.Task[None] | None = None
        self._ring: deque[str] = deque(maxlen=OUTPUT_RING_SIZE)
        self._log_fh: IO[str] | None = None
        self._stopping = False
        self._exit_logged_pid: int | None = None
        self._job: WindowsChildJob | None = None
        self._job_tried = False
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # --- public surface ------------------------------------------------

    def is_alive(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        return process_is_alive(proc.pid, create_time=self._create_time)

    def stderr_tail(self, n: int = 40) -> list[str]:
        """The last ``n`` lines the child printed (stdout and stderr are merged)."""
        return list(self._ring)[-n:] if n > 0 else []

    def status(self) -> dict[str, Any]:
        spec = self.spec
        return {
            "state": self.state,
            "pid": self.pid,
            "port": self.port,
            "base_url": self.base_url,
            "model_id": spec.model_id if spec else None,
            "device": spec.device if spec else None,
            "max_prompt_len": spec.max_prompt_len if spec else None,
            "first_compile": self.first_compile,
            "expected_s": self.expected_s,
            "load_s": self.load_s,
            "model_state": self.model_state,
            "exit_code": self.exit_code,
            "crashed": self.crashed,
            "error": self.last_error,
        }

    async def health(self) -> bool:
        """``/v2/health/ready`` is 200 and the process is alive (pre-lease check)."""
        if not self.is_alive() or self.port is None:
            return False
        try:
            resp = await self._http().get(f"http://{CHILD_HOST}:{self.port}/v2/health/ready")
        except (httpx.HTTPError, OSError):
            return False
        return resp.status_code == 200

    async def start(self, spec: LaunchSpec, on_tick: TickCallback | None = None) -> str:
        """Launch OVMS for ``spec`` and wait until the model is servable.

        A running child is stopped first. Cancelling the call kills the child.
        Raises :class:`OvmsError` on every failure, with the output tail.
        """
        async with self._lock:
            if self._proc is not None:
                await self._stop_locked(10.0)
            return await self._start_locked(spec, on_tick)

    async def stop(self, timeout_s: float = 10) -> None:
        """Kill the child tree and wait for it. Idempotent."""
        async with self._lock:
            await self._stop_locked(timeout_s)

    async def aclose(self) -> None:
        """Stop the child and close the job object (which kills any survivor)."""
        await self.stop()
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- start ------------------------------------------------------------

    async def _start_locked(self, spec: LaunchSpec, on_tick: TickCallback | None) -> str:
        if not self.command_prefix and not self.exe.is_file():
            raise OvmsError(
                f"The local model server is not installed ({self.exe}).",
                code="not_installed",
                hint="Open Settings -> Models and install the runtime.",
            )
        self.exit_event.clear()
        self._ring.clear()
        self._stopping = False
        self.crashed = False
        self.exit_code = None
        self.last_error = None
        self.load_s = None
        self.model_state = None
        self.base_url = None
        self.state = "starting"

        cache_dir = Path(spec.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = compile_cache.compile_key(
            spec.model_id, spec.device, spec.max_prompt_len, self.ovms_version
        )
        chash = spec_compile_hash(spec, ovms_version=self.ovms_version)
        self.first_compile = not compile_cache.is_compiled(
            cache_dir, ovms_version=self.ovms_version, compile_hash=chash
        )
        self.expected_s = compile_cache.expected_load_s(
            cache_dir,
            state_file=self.state_file,
            key=key,
            ovms_version=self.ovms_version,
            compile_hash=chash,
            model_path=spec.model_path,
        )
        digest = launch_hash(spec, variant=self.variant, ovms_version=self.ovms_version)
        self._prepare_graph(spec, digest)

        t0 = time.monotonic()
        explicit_port = spec.port > 0
        port = spec.port if explicit_port else pick_free_port(self.port_start)
        tried: set[int] = set()
        try:
            for attempt in (1, 2):
                current = replace(spec, port=port)
                self.spec = current
                if attempt > 1:
                    # The failed first child's exit must not read as this one's.
                    self.exit_event.clear()
                    self._ring.clear()
                    self._stopping = False
                    self.crashed = False
                    self.exit_code = None
                    self.state = "starting"
                await self._spawn(current)
                try:
                    await self._await_ready(current, on_tick, t0)
                    break
                except _BindFailed:
                    tried.add(port)
                    await self._teardown(timeout_s=5.0)
                    if explicit_port or attempt == 2:
                        raise OvmsError(
                            f"The local model server could not bind 127.0.0.1:{port}.",
                            code="port",
                            hint="Another program is using that port. Retry, or restart the app.",
                            details={"port": port, "output": self.stderr_tail()},
                        ) from None
                    port = pick_free_port(self.port_start, exclude=tried)
                    log.warning("ovms_port_retry", failed_port=current.port, new_port=port)
        except asyncio.CancelledError:
            self._stopping = True
            await self._teardown(timeout_s=5.0)
            self.state = "stopped"
            raise
        except OvmsError as exc:
            self.last_error = exc.message
            await self._teardown(timeout_s=5.0)
            self.state = "failed"
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            await self._teardown(timeout_s=5.0)
            self.state = "failed"
            raise

        elapsed = time.monotonic() - t0
        self.load_s = round(elapsed, 2)
        self.state = "ready"
        self.base_url = f"http://{CHILD_HOST}:{self.port}/v3"
        with contextlib.suppress(OSError):
            compile_cache.mark_compiled(
                cache_dir,
                model_id=spec.model_id,
                device=spec.device,
                max_prompt_len=spec.max_prompt_len,
                ovms_version=self.ovms_version,
                load_s=elapsed,
                launch_hash=digest,
                compile_hash=chash,
                cold=self.first_compile,
                state_file=self.state_file,
            )
        self._record_launch(spec.model_id, digest)
        log.info(
            "ovms_ready",
            model_id=spec.model_id,
            port=self.port,
            load_s=self.load_s,
            first_compile=self.first_compile,
        )
        if on_tick is not None:
            with contextlib.suppress(Exception):
                on_tick("ready", elapsed)
        return self.base_url

    def _prepare_graph(self, spec: LaunchSpec, digest: str) -> None:
        """Remove a ``graph.pbtxt`` written for different launch parameters.

        OVMS 2026.4 ignores the file when ``--task`` is on the command line (it
        builds the graph in memory), so this is hygiene rather than a fix; it is
        still done because a stale graph is what ``ovms --config_path`` would load.
        """
        self.graph_removed = False
        graph = Path(spec.model_path) / "graph.pbtxt"
        if not graph.exists():
            return
        previous = (
            (compile_cache.load_state(self.state_file).get("launch") or {}).get(spec.model_id) or {}
        ).get("hash")
        if previous == digest:
            return
        try:
            graph.unlink()
            self.graph_removed = True
            log.info("ovms_stale_graph_removed", model_id=spec.model_id, path=str(graph))
        except OSError as exc:
            log.warning("ovms_stale_graph_kept", model_id=spec.model_id, error=str(exc))

    def _record_launch(self, model_id: str, digest: str) -> None:
        def _mutate(state: dict[str, Any]) -> None:
            state.setdefault("launch", {})[model_id] = {"hash": digest, "at": time.time()}

        with contextlib.suppress(OSError):
            compile_cache.update_state(self.state_file, _mutate)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # trust_env=False: an HTTP(S)_PROXY in the user's env must never
            # capture loopback traffic to the child.
            self._client = httpx.AsyncClient(timeout=self.http_timeout_s, trust_env=False)
        return self._client

    def _full_argv(self, spec: LaunchSpec) -> list[str]:
        argv = build_argv(self.exe, spec)
        if self.command_prefix:
            return [*self.command_prefix, *argv[1:]]
        return argv

    def _open_log(self, spec: LaunchSpec) -> None:
        self._close_log()
        if spec.log_path is None:
            return
        path = Path(spec.log_path)
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            rotate_log(path, max_bytes=self.log_max_bytes, backups=self.log_backups)
            self._log_fh = path.open("a", encoding="utf-8", errors="replace")

    def _write_log(self, line: str) -> None:
        if self._log_fh is None:
            return
        with contextlib.suppress(ValueError, OSError):
            self._log_fh.write(line + "\n")
            self._log_fh.flush()

    def _close_log(self) -> None:
        if self._log_fh is not None:
            with contextlib.suppress(OSError):
                self._log_fh.close()
            self._log_fh = None

    # Adapted from StudioForge src/studioforge/core/supervisor.py Supervisor._spawn (MIT, LaserLloyd)
    async def _spawn(self, spec: LaunchSpec) -> None:
        """Create the child suspended, put it in the job, resume it, start pumping."""
        argv = self._full_argv(spec)
        self.argv = argv
        env = ovms_env(
            self.exe.parent, self._base_env if self._base_env is not None else os.environ
        )
        cwd = str(self.exe.parent) if self.exe.parent.is_dir() else None
        if self.use_job and not self._job_tried:
            self._job_tried = True
            self._job = create_child_job()
        suspended = os.name == "nt" and self._job is not None and self._job.available
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = spawn_creationflags(suspended=suspended)
        else:
            kwargs["start_new_session"] = True

        self._open_log(spec)
        self._write_log(
            f"=== aichat launch {time.strftime('%Y-%m-%d %H:%M:%S')}: "
            + " ".join(redact_argv(argv))
        )
        log.info("ovms_spawn", model_id=spec.model_id, port=spec.port, argv=redact_argv(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=env,
                limit=1 << 20,
                **kwargs,
            )
        except OSError as exc:
            raise OvmsError(
                f"Could not launch the local model server: {exc}",
                code="spawn",
                details={"argv": redact_argv(argv)},
            ) from exc

        if self._job is not None:
            self._job.assign(proc.pid)
        if suspended:
            try:
                resume_process(proc.pid)
            except Exception as exc:  # noqa: BLE001 - reported as a load failure
                with contextlib.suppress(Exception):
                    kill_process_tree(proc.pid, timeout=2.0, force=True)
                raise OvmsError(
                    f"The local model server was created suspended and could not be resumed: {exc}",
                    code="spawn",
                ) from exc

        self._proc = proc
        self.pid = proc.pid
        self.port = spec.port
        self._create_time = process_create_time(proc.pid)
        self._exit_logged_pid = None
        track_pid(proc.pid)
        self._pump = asyncio.create_task(self._pump_output(proc), name="ovms-pump")
        self._watch = asyncio.create_task(self._watch_exit(proc), name="ovms-watch")

    # Adapted from StudioForge src/studioforge/core/supervisor.py Supervisor._pump (MIT, LaserLloyd)
    async def _pump_output(self, proc: asyncio.subprocess.Process) -> None:
        stream = proc.stdout
        if stream is None:
            return
        while True:
            try:
                raw = await stream.readline()
            except asyncio.LimitOverrunError as exc:
                raw = await stream.read(exc.consumed)
            except ValueError:
                continue
            except Exception as exc:  # noqa: BLE001 - transport teardown races
                log.warning("ovms_output_pump_ended", error=f"{type(exc).__name__}: {exc}")
                return
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.strip():
                continue
            self._ring.append(line)
            self._write_log(line)

    async def _drain_pump(self, timeout_s: float = 2.0) -> None:
        if self._pump is None:
            return
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(self._pump), timeout=timeout_s)

    async def _watch_exit(self, proc: asyncio.subprocess.Process) -> None:
        """Wait for the child to end; record, log and signal it exactly once."""
        code: int | None
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - fall back to the pid
            while process_is_alive(proc.pid, create_time=self._create_time):
                await asyncio.sleep(2.0)
            code = proc.returncode
        await self._drain_pump()
        self.exit_code = code
        untrack_pid(proc.pid)
        if self._stopping:
            log.info("ovms_stopped", pid=proc.pid, **describe_exit_code(code))
        else:
            self.crashed = True
            phase = "running" if self.state == "ready" else "startup"
            if self.state == "ready":
                self.state = "exited"
                self.last_error = self._failure_message(f"exited with code {code}")
            self._log_exit(proc.pid, phase=phase, code=code)
        self.exit_event.set()

    # Adapted from StudioForge src/studioforge/core/supervisor.py _log_child_exit (MIT, LaserLloyd)
    def _log_exit(self, pid: int, *, phase: str, code: int | None) -> None:
        if self._exit_logged_pid == pid:
            return
        self._exit_logged_pid = pid
        log.warning(
            "ovms_exited",
            pid=pid,
            phase=phase,
            tail=self.stderr_tail(5),
            **describe_exit_code(code),
        )

    def _failure_message(self, what: str) -> str:
        tail = self.stderr_tail(15)
        text = f"The local model server {what}."
        if tail:
            text += " Last output:\n" + "\n".join(tail)
        return text

    def _bind_failed(self) -> bool:
        return any(marker in line for line in self._ring for marker in BIND_FAILURE_MARKERS)

    # Adapted from StudioForge src/studioforge/core/supervisor.py _await_ready (MIT, LaserLloyd)
    async def _await_ready(self, spec: LaunchSpec, on_tick: TickCallback | None, t0: float) -> None:
        """Poll ``/v2/health/ready`` + ``/v1/config`` until AVAILABLE, exit or timeout."""
        base = f"http://{CHILD_HOST}:{spec.port}"
        deadline = t0 + self.load_timeout_s
        last_tick = 0.0
        client = self._http()
        rest_up = False
        while True:
            proc = self._proc
            if proc is None or proc.returncode is not None or self.exit_event.is_set():
                await self._drain_pump()
                code = proc.returncode if proc is not None else None
                if self._bind_failed():
                    raise _BindFailed
                failed_state = next(
                    (s for s in MODEL_STATES_FAILED for line in self._ring if s in line), None
                )
                raise OvmsError(
                    self._failure_message(f"exited with code {code} while loading"),
                    code="model_failed" if failed_state else "exited",
                    hint="See logs\\ovms.log. A model that is not NPU-compatible, or a "
                    "driver problem, is the usual cause.",
                    details={
                        "exit_code": code,
                        "output": self.stderr_tail(),
                        "state": failed_state,
                    },
                )
            ready = False
            try:
                resp = await client.get(f"{base}/v2/health/ready")
                rest_up = True
                if resp.status_code == 200:
                    cfg = await client.get(f"{base}/v1/config")
                    if cfg.status_code == 200:
                        with contextlib.suppress(ValueError):
                            self.model_state = parse_model_state(cfg.json(), spec.model_id)
                    if self.model_state == MODEL_STATE_AVAILABLE:
                        ready = True
                    elif self.model_state in MODEL_STATES_FAILED:
                        raise OvmsError(
                            f"The model '{spec.model_id}' failed to load "
                            f"(state {self.model_state}).",
                            code="model_failed",
                            details={"output": self.stderr_tail()},
                        )
            except (httpx.HTTPError, OSError):
                pass
            if ready:
                return
            phase = "starting"
            if rest_up:
                phase = "compiling" if self.first_compile else "loading"
            if phase != "starting" and self.state == "starting":
                self.state = "loading"
            now = time.monotonic()
            if on_tick is not None and now - last_tick >= self.tick_interval_s:
                last_tick = now
                with contextlib.suppress(Exception):
                    on_tick(phase, now - t0)
            if now >= deadline:
                raise OvmsError(
                    self._failure_message(f"did not become ready within {self.load_timeout_s:g}s"),
                    code="timeout",
                    hint="Retry; if it keeps happening, clear the compile cache for this model.",
                    details={"output": self.stderr_tail()},
                )
            await asyncio.sleep(self.poll_interval_s)

    # --- stop ---------------------------------------------------------------

    async def _teardown(self, *, timeout_s: float) -> None:
        """Kill the process tree (if any) and wait for the watcher. Keeps the job."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            await asyncio.to_thread(kill_process_tree, proc.pid, timeout=timeout_s, force=False)
        if self._watch is not None:
            watch = self._watch
            try:
                await asyncio.wait_for(asyncio.shield(watch), timeout=max(timeout_s, 1.0) + 5.0)
            except (TimeoutError, asyncio.CancelledError):
                watch.cancel()
            except Exception:  # noqa: BLE001
                pass
        await self._drain_pump()
        if proc is not None:
            untrack_pid(proc.pid)
        self._close_log()
        self._proc = None
        self._watch = None
        self._pump = None

    async def _stop_locked(self, timeout_s: float) -> None:
        if self._proc is None:
            if self.state not in ("failed",):
                self.state = "stopped"
            return
        self._stopping = True
        self.state = "stopping"
        await self._teardown(timeout_s=timeout_s)
        self.state = "stopped"
        self.base_url = None
        self.exit_event.set()
