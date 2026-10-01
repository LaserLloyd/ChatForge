"""Per-model OpenVINO compile cache: location, "compiled" marker, ETA, clear.

Layout (PLAN §1.2)::

    <cache_root>/ov/<model-slug>/<DEVICE>-<max_prompt_len>/   <- OVMS --cache_dir
        <hash>.blob                                            <- written by OVMS
        .aichat-compiled.json                                  <- our marker

``state.json`` also carries ``"compiled": {"<id>|NPU|4096|2026.4.0": {"at", "load_s"}}``
so a first-compile time survives a cache clear (the ETA then stays honest).

The directory name only covers the device and the prompt window. Everything else
that changes the NPU blob (the model files' location, ``extra_args`` such as a
``--plugin_config``, prefix caching, the OVMS version) is folded into
:func:`compile_hash`, which the marker records: a marker written for other
compile settings is a cold cache, even though OVMS will write the new blob into
the same directory.

Measured on the target laptop (docs/RUNTIME-NOTES.md): Qwen2.5-1.5B first NPU
compile 45 s, cached load 3 s; Qwen3-4B first compile 64-72 s, cached 5 s. In the
app (other work running) the same first compiles took 66 s and 123 s, and cached
loads 6-11 s.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

#: Marker file written after a successful load. Its presence (plus at least one
#: blob) is what "compiled for NPU" means in the UI.
MARKER_NAME = ".aichat-compiled.json"
#: Expected load time when the cache is warm and nothing better was measured.
CACHED_LOAD_S = 20.0
#: Expected first-compile time when nothing was ever measured for this key and
#: the model's size is unknown.
DEFAULT_FIRST_COMPILE_S = 360.0
#: First-compile estimate from the weights' size (``openvino_model.bin``), fitted
#: to the in-app NPU compiles: 0.87 GB -> 66 s, 2.26 GB -> 123 s.
FIRST_COMPILE_BASE_S = 30.0
FIRST_COMPILE_S_PER_GB = 41.0
#: The weights file whose size drives the estimate.
WEIGHTS_FILE = "openvino_model.bin"
#: Sub-directory of the app cache dir that holds OpenVINO caches.
OV_SUBDIR = "ov"

_STATE_LOCK = threading.Lock()


def model_slug(model_id: str) -> str:
    """``OpenVINO/Qwen3-4B-int4-ov`` -> ``OpenVINO--Qwen3-4B-int4-ov`` (filesystem-safe)."""
    cleaned = "".join(
        c if (c.isalnum() or c in "-_.") else "-" for c in model_id.replace("/", "--")
    )
    cleaned = cleaned.strip(".-") or "model"
    if cleaned in {".", ".."}:  # pragma: no cover - strip above already prevents it
        cleaned = "model"
    return cleaned[:150]


def model_cache_root(cache_root: Path, model_id: str) -> Path:
    """``<cache_root>/ov/<slug>`` -- every device/length variant of one model."""
    return Path(cache_root) / OV_SUBDIR / model_slug(model_id)


def cache_dir_for(cache_root: Path, model_id: str, device: str, max_prompt_len: int) -> Path:
    """The ``--cache_dir`` for one (model, device, prompt window) combination."""
    return model_cache_root(cache_root, model_id) / f"{device.upper()}-{int(max_prompt_len)}"


def compile_key(model_id: str, device: str, max_prompt_len: int, ovms_version: str) -> str:
    """The ``state.json`` ``compiled`` key: ``"<id>|NPU|4096|2026.4.0"``."""
    return f"{model_id}|{device.upper()}|{int(max_prompt_len)}|{ovms_version}"


def compile_hash(
    *,
    model_path: Path | str,
    device: str,
    max_prompt_len: int,
    ovms_version: str,
    extra_args: Sequence[str] = (),
    enable_prefix_caching: bool | None = None,
) -> str:
    """Hash of every launch setting that changes the compiled blob.

    Unlike ``launch_hash`` it leaves out what only changes serving (port, log
    path, tool and reasoning parsers, the python_on/off package), so a parser
    change in the catalog does not make a warm cache look cold.
    """
    payload = {
        "model_path": str(model_path),
        "device": device.upper(),
        "max_prompt_len": int(max_prompt_len),
        "extra_args": [str(a) for a in extra_args],
        "enable_prefix_caching": enable_prefix_caching,
        "ovms_version": ovms_version,
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def estimate_first_compile_s(model_path: Path | str | None) -> float | None:
    """First-compile estimate from the size of the model's weights; ``None`` if unknown."""
    if model_path is None:
        return None
    try:
        size = (Path(model_path) / WEIGHTS_FILE).stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None
    return round(FIRST_COMPILE_BASE_S + FIRST_COMPILE_S_PER_GB * size / 1e9, 1)


def dir_size(path: Path) -> int:
    """Total bytes of regular files under ``path`` (0 when missing). Never raises."""
    total = 0
    root = Path(path)
    if not root.exists():
        return 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / name).stat().st_size
    return total


def _blobs(cache_dir: Path) -> list[Path]:
    try:
        return [p for p in Path(cache_dir).iterdir() if p.is_file() and p.suffix == ".blob"]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# state.json helpers (shared file: every write preserves unknown keys)
# ---------------------------------------------------------------------------


def load_state(state_file: Path | None) -> dict[str, Any]:
    """Read ``state.json``; a missing or corrupt file reads as ``{}``."""
    if state_file is None:
        return {}
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_state(state_file: Path | None, mutate: Callable[[dict[str, Any]], None]) -> dict:
    """Read-modify-write ``state.json`` atomically (tmp + ``os.replace``).

    Other modules own other keys of the same file (``last_used``,
    ``last_unload``); the mutation sees the whole document and only touches
    its own keys, so nothing written by someone else is lost.
    """
    if state_file is None:
        return {}
    path = Path(state_file)
    with _STATE_LOCK:
        data = load_state(path)
        mutate(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return data


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------


def read_marker(cache_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(cache_dir) / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _marker_matches(
    marker: dict[str, Any], ovms_version: str | None, compile_hash: str | None
) -> bool:
    """A marker missing a field (older app versions) matches any value of it."""
    if ovms_version is not None and marker.get("ovms_version") not in (None, ovms_version):
        return False
    return not (compile_hash is not None and marker.get("compile_hash") not in (None, compile_hash))


def is_compiled(
    cache_dir: Path, *, ovms_version: str | None = None, compile_hash: str | None = None
) -> bool:
    """Whether a load from ``cache_dir`` will be a warm (cached) load.

    Needs our marker **and** at least one OVMS blob: a marker whose blobs were
    deleted by hand is not a warm cache. A marker from another OVMS version is
    treated as cold, since the blob format is not guaranteed across releases, and
    so is one written for other compile settings (``compile_hash``).
    """
    marker = read_marker(cache_dir)
    if marker is None or not _blobs(cache_dir):
        return False
    return _marker_matches(marker, ovms_version, compile_hash)


def mark_compiled(
    cache_dir: Path,
    *,
    model_id: str,
    device: str,
    max_prompt_len: int,
    ovms_version: str,
    load_s: float,
    launch_hash: str | None = None,
    compile_hash: str | None = None,
    cold: bool | None = None,
    state_file: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Record a successful load. The first one is the compile time.

    ``cold`` says whether this load compiled (the caller checked
    :func:`is_compiled` before it started); when ``None`` it is inferred from
    the previous marker. A cold load resets ``first_compile_s`` and
    ``compiled_at``; ``last_load_s`` is always updated, and a warm load's time
    becomes the ETA (``cached_load_s``).
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    at = time.time() if now is None else now
    previous = read_marker(cache_dir) or {}
    if cold is None:
        cold = not previous or not _marker_matches(previous, ovms_version, compile_hash)
    was_compiled = not cold
    base = previous if was_compiled else {}
    marker: dict[str, Any] = {
        "schema": 1,
        "model_id": model_id,
        "device": device.upper(),
        "max_prompt_len": int(max_prompt_len),
        "ovms_version": ovms_version,
        "launch_hash": launch_hash,
        "compile_hash": compile_hash,
        "compiled_at": base.get("compiled_at", at),
        "first_compile_s": base.get("first_compile_s", round(load_s, 2)),
        "last_load_s": round(load_s, 2),
        "last_load_at": at,
    }
    if was_compiled:
        marker["cached_load_s"] = round(load_s, 2)
    tmp = cache_dir / f"{MARKER_NAME}.tmp"
    tmp.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    os.replace(tmp, cache_dir / MARKER_NAME)

    key = compile_key(model_id, device, max_prompt_len, ovms_version)

    def _mutate(state: dict[str, Any]) -> None:
        compiled = state.setdefault("compiled", {})
        entry = compiled.get(key) or {}
        if not was_compiled or "load_s" not in entry:
            entry["load_s"] = round(load_s, 2)
        entry["at"] = at
        compiled[key] = entry

    update_state(state_file, _mutate)
    return marker


def expected_load_s(
    cache_dir: Path,
    *,
    state_file: Path | None = None,
    key: str | None = None,
    ovms_version: str | None = None,
    compile_hash: str | None = None,
    model_path: Path | str | None = None,
) -> float:
    """Honest ETA for the next load from ``cache_dir``.

    * warm cache: the measured cached load time, else :data:`CACHED_LOAD_S`;
    * cold cache: the recorded first-compile time for ``key`` in ``state.json``
      (it survives a cache clear), else the marker's, else an estimate from the
      size of the weights in ``model_path``, else :data:`DEFAULT_FIRST_COMPILE_S`.
    """
    if is_compiled(cache_dir, ovms_version=ovms_version, compile_hash=compile_hash):
        marker = read_marker(cache_dir) or {}
        cached = marker.get("cached_load_s")
        if isinstance(cached, int | float) and cached > 0:
            return float(cached)
        return CACHED_LOAD_S
    if key is not None:
        entry = (load_state(state_file).get("compiled") or {}).get(key) or {}
        recorded = entry.get("load_s")
        if isinstance(recorded, int | float) and recorded > 0:
            return float(recorded)
    marker = read_marker(cache_dir) or {}
    recorded = marker.get("first_compile_s")
    if isinstance(recorded, int | float) and recorded > 0:
        return float(recorded)
    estimate = estimate_first_compile_s(model_path)
    if estimate is not None:
        return estimate
    return DEFAULT_FIRST_COMPILE_S


def warm_variants(cache_root: Path, model_id: str) -> dict[str, dict[str, Any]]:
    """Every warm ``<DEVICE>-<len>`` cache of a model, keyed ``"NPU|4096|2026.4.0"``.

    Read from disk (marker plus blob), so a cleared cache drops out at once.
    """
    root = model_cache_root(cache_root, model_id)
    found: dict[str, dict[str, Any]] = {}
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for child in children:
        device, sep, length = child.name.rpartition("-")
        if not sep or not device or not length.isdigit() or not is_compiled(child):
            continue
        marker = read_marker(child) or {}
        version = marker.get("ovms_version") or "?"
        found[f"{device.upper()}|{int(length)}|{version}"] = marker
    return found


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def clear(
    cache_root: Path,
    model_id: str,
    *,
    device: str | None = None,
    max_prompt_len: int | None = None,
) -> int:
    """Delete a model's compile cache; return the bytes freed.

    With ``device`` and ``max_prompt_len`` only that variant is removed,
    otherwise every variant of the model. Refuses any path that would resolve
    outside ``<cache_root>/ov``. The recorded first-compile times in
    ``state.json`` are kept on purpose (they are the next ETA).
    """
    ov_root = Path(cache_root) / OV_SUBDIR
    if device is not None and max_prompt_len is not None:
        target = cache_dir_for(cache_root, model_id, device, max_prompt_len)
    else:
        target = model_cache_root(cache_root, model_id)
    if not _is_under(target, ov_root) or target.resolve() == ov_root.resolve():
        raise ValueError(f"refusing to clear {target}: not inside {ov_root}")
    if not target.exists():
        return 0
    freed = dir_size(target)

    def _onerror(func: Callable[..., Any], path: str, _exc: BaseException) -> None:
        # OVMS writes blobs read-only; clear the flag and retry once.
        with contextlib.suppress(OSError):
            os.chmod(path, 0o666)
            func(path)

    shutil.rmtree(target, onexc=_onerror)
    return freed
