"""Per-model OpenVINO compile cache: location, "compiled" marker, ETA, clear.

Layout (PLAN §1.2)::

    <cache_root>/ov/<model-slug>/<DEVICE>-<max_prompt_len>/   <- OVMS --cache_dir
        <hash>.blob                                            <- written by OVMS
        .aichat-compiled.json                                  <- our marker

``state.json`` also carries ``"compiled": {"<id>|NPU|4096|2026.4.0": {"at", "load_s"}}``
so a first-compile time survives a cache clear (the ETA then stays honest).

Measured on the target laptop (docs/RUNTIME-NOTES.md): Qwen2.5-1.5B first NPU
compile 45 s, cached load 3 s; Qwen3-4B first compile 64-72 s, cached 5 s.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Marker file written after a successful load. Its presence (plus at least one
#: blob) is what "compiled for NPU" means in the UI.
MARKER_NAME = ".aichat-compiled.json"
#: Expected load time when the cache is warm and nothing better was measured.
CACHED_LOAD_S = 20.0
#: Expected first-compile time when nothing was ever measured for this key.
DEFAULT_FIRST_COMPILE_S = 360.0
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


def is_compiled(cache_dir: Path, *, ovms_version: str | None = None) -> bool:
    """Whether a load from ``cache_dir`` will be a warm (cached) load.

    Needs our marker **and** at least one OVMS blob: a marker whose blobs were
    deleted by hand is not a warm cache. A marker from another OVMS version is
    treated as cold, since the blob format is not guaranteed across releases.
    """
    marker = read_marker(cache_dir)
    if marker is None or not _blobs(cache_dir):
        return False
    return not (ovms_version is not None and marker.get("ovms_version") not in (None, ovms_version))


def mark_compiled(
    cache_dir: Path,
    *,
    model_id: str,
    device: str,
    max_prompt_len: int,
    ovms_version: str,
    load_s: float,
    launch_hash: str | None = None,
    state_file: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Record a successful load. The first one is the compile time.

    ``first_compile_s`` is only set when the cache was cold before this load;
    ``last_load_s`` is always updated (a warm load's time becomes the ETA).
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    at = time.time() if now is None else now
    previous = read_marker(cache_dir) or {}
    was_compiled = bool(previous)
    marker: dict[str, Any] = {
        "schema": 1,
        "model_id": model_id,
        "device": device.upper(),
        "max_prompt_len": int(max_prompt_len),
        "ovms_version": ovms_version,
        "launch_hash": launch_hash,
        "compiled_at": previous.get("compiled_at", at),
        "first_compile_s": previous.get("first_compile_s", round(load_s, 2)),
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
) -> float:
    """Honest ETA for the next load from ``cache_dir``.

    * warm cache: the measured cached load time, else :data:`CACHED_LOAD_S`;
    * cold cache: the recorded first-compile time for ``key`` in ``state.json``
      (it survives a cache clear), else :data:`DEFAULT_FIRST_COMPILE_S`.
    """
    if is_compiled(cache_dir, ovms_version=ovms_version):
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
    return DEFAULT_FIRST_COMPILE_S


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
