"""Scan-based registry of local OpenVINO models under ``models\\<publisher>\\<repo>``.

Adapted from StudioForge src/studioforge/core/registry.py (MIT, LaserLloyd):
the ``scan``/``_walk`` shape, ``_is_under``, ``_assert_inside_model_dirs`` and
``delete_model``. GGUF metadata, mmproj pairing, adapters, virtual models,
aliases and SQLite are cut; the filesystem is the only source of truth.

A model is complete when its directory holds the four files OVMS needs
(:data:`REQUIRED_FILES`). Dot-dirs (``.cache\\huggingface`` from the HF CLI) are
ignored everywhere. ``graph.pbtxt`` is written by OVMS into the model directory
(PLAN §7 item 2): it is expected, never a completeness file, ignored in size
checks, and never deleted on adopt.

The sidecar ``.aichat-model.json`` records where a model came from. The
downloader writes it on finish; :meth:`Registry.adopt` writes it for a model
that was already on disk, after checking every file's size against the repo
listing.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from aichat.models.catalog import Catalog, CatalogEntry
from aichat.models.hf_search import ModelError, RepoFile, safe_relpath, validate_repo_id
from aichat.models.npu_compat import npu_verdict
from aichat.runtime import compile_cache
from aichat.runtime.compile_cache import model_cache_root

log = logging.getLogger(__name__)

__all__ = [
    "GRAPH_FILE",
    "REQUIRED_FILES",
    "SIDECAR_NAME",
    "ModelRecord",
    "Registry",
    "model_slug",
    "read_sidecar",
    "write_sidecar",
]

REQUIRED_FILES: Final = (
    "openvino_model.xml",
    "openvino_model.bin",
    "openvino_tokenizer.xml",
    "openvino_detokenizer.xml",
)
SIDECAR_NAME: Final = ".aichat-model.json"
GRAPH_FILE: Final = "graph.pbtxt"
SIDECAR_SCHEMA: Final = 1

#: Files the registry never counts, never compares and never deletes on adopt.
_IGNORED_FILES: Final = frozenset({SIDECAR_NAME.casefold(), GRAPH_FILE.casefold()})
#: Dot/dunder dirs are tool caches (.cache/huggingface, .git, __pycache__).
_SKIP_DIR_PREFIXES: Final = (".", "__")
_PART_SUFFIX: Final = ".part"


def model_slug(repo_id: str) -> str:
    """``OpenVINO/Qwen3-4B-int4-ov`` -> ``OpenVINO--Qwen3-4B-int4-ov`` (download group id).

    Not the compile-cache directory name: that is
    :func:`aichat.runtime.compile_cache.model_slug`, which also cleans and truncates.
    """
    return repo_id.replace("/", "--")


# Adapted from StudioForge src/studioforge/core/registry.py (MIT, LaserLloyd).
def _is_under(path: Path, root: Path) -> bool:
    """Whether ``path`` lies under ``root`` (after resolving; the root may not exist)."""
    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def read_sidecar(model_dir: Path) -> dict[str, Any] | None:
    """The parsed sidecar, or ``None`` when it is missing or unreadable."""
    try:
        data = json.loads((model_dir / SIDECAR_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_sidecar(
    model_dir: Path,
    *,
    repo_id: str,
    files: Sequence[RepoFile],
    source: str,
    revision: str | None = None,
    license: str | None = None,
    models_dir: Path | None = None,
) -> Path:
    """Write ``.aichat-model.json`` atomically (PLAN §1.8 format).

    ``models_dir``, when given, is asserted to contain the target first.
    """
    target = model_dir / SIDECAR_NAME
    if models_dir is not None and not _is_under(target, models_dir):
        raise ModelError(
            f"refusing to write {target}: outside the models directory", code="path_escape"
        )
    counted = [f for f in files if Path(f.path).name.casefold() not in _IGNORED_FILES]
    payload = {
        "schema": SIDECAR_SCHEMA,
        "repo_id": repo_id,
        "revision": revision,
        "source": source,
        "files": [f.to_dict() for f in counted],
        "total_bytes": sum(f.size for f in counted),
        "downloaded_at": _utc_now_iso(),
        "license": license,
    }
    _atomic_write_json(target, payload)
    return target


@dataclass
class ModelRecord:
    """One ``<publisher>/<repo>`` directory as the registry sees it (PLAN §1.4)."""

    id: str
    path: Path
    size_bytes: int
    complete: bool
    missing: list[str]
    catalog: CatalogEntry | None = None
    last_used_at: float | None = None
    #: Warm compile caches on disk: ``{"NPU": True, "NPU|4096|2026.4.0": True}``. The
    #: plain device key is what Settings shows as "Compiled for NPU".
    compiled: dict[str, bool] = field(default_factory=dict)
    source: str | None = None
    revision: str | None = None
    #: :class:`aichat.models.npu_compat.NpuVerdict` ``ok``: True, False (it will not
    #: run correctly on the NPU; the runtime uses the fallback device) or None (unknown).
    npu_ok: bool | None = None
    npu_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "complete": self.complete,
            "missing": list(self.missing),
            "catalog": self.catalog.to_dict() if self.catalog else None,
            "last_used_at": self.last_used_at,
            "compiled": dict(self.compiled),
            "source": self.source,
            "revision": self.revision,
            "npu_ok": self.npu_ok,
            "npu_note": self.npu_note,
        }


def _local_files(model_dir: Path) -> dict[str, int]:
    """``{posix relpath: size}`` for every counted file, skipping dot-dirs,
    ``graph.pbtxt``, the sidecar and in-progress ``.part`` files."""
    found: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(model_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(_SKIP_DIR_PREFIXES))
        for name in filenames:
            if name.casefold() in _IGNORED_FILES or name.endswith(_PART_SUFFIX):
                continue
            full = Path(dirpath) / name
            try:
                st = full.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            found[full.relative_to(model_dir).as_posix()] = st.st_size
    return found


def _stat_or_none(path: Path, attr: str) -> int | None:
    try:
        return int(getattr(path.stat(), attr))
    except OSError:
        return None


def _probe_writable(directory: Path) -> bool:
    """Create and delete a scratch file: the only honest writability test on Windows."""
    probe = directory / f".aichat-write-test-{uuid.uuid4().hex[:8]}"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:
        return False
    os.close(fd)
    with contextlib.suppress(OSError):
        probe.unlink()
    return True


def _rmtree_onexc(func: Any, path: str, _exc: BaseException) -> None:
    """Clear the read-only bit (HF CLI caches set it) and retry once."""
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IWRITE)
    func(path)


class Registry:
    """The local model library.

    ``models_dir`` and ``cache_dir`` are passed in (``Paths.models_dir`` and
    ``Paths.cache_dir``); ``state_file`` (``Paths.state_file``) is read, never
    written, for ``last_used`` and compile markers.
    """

    def __init__(
        self,
        models_dir: Path,
        cache_dir: Path,
        *,
        catalog: Catalog | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.models_dir = Path(models_dir)
        self.cache_dir = Path(cache_dir)
        self._catalog = catalog
        self._state_file = state_file
        self._lock = threading.RLock()
        self._models: dict[str, ModelRecord] = {}
        self._scanned = False

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def model_dir(self, repo_id: str) -> Path:
        """``models_dir/<publisher>/<repo>`` for a validated id, confined to ``models_dir``."""
        repo = validate_repo_id(repo_id)
        publisher, _, name = repo.partition("/")
        target = self.models_dir / publisher / name
        self._assert_inside(target, self.models_dir)
        return target

    def cache_dir_for(self, repo_id: str) -> Path:
        """``cache_dir/ov/<slug>``: this model's OVMS compile cache root.

        The same path the runtime compiles into (:func:`compile_cache.model_cache_root`),
        so :meth:`delete` finds it for every id, including ones the slug cleans or cuts.
        """
        repo = validate_repo_id(repo_id)
        target = model_cache_root(self.cache_dir, repo)
        self._assert_inside(target, self.cache_dir)
        return target

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    # Adapted from StudioForge src/studioforge/core/registry.py (MIT, LaserLloyd).
    def scan(self) -> list[ModelRecord]:
        """Walk ``models_dir`` and rebuild the index. One bad dir never aborts it."""
        state = self._read_state()
        records: dict[str, ModelRecord] = {}
        for model_id, model_dir in self._walk():
            try:
                records[model_id] = self._build_record(model_id, model_dir, state)
            except OSError as exc:
                log.warning("registry.scan_failed id=%s error=%s", model_id, exc)
        with self._lock:
            self._models = records
            self._scanned = True
        log.info("registry.scan models=%d", len(records))
        return self.all()

    def fingerprint(self) -> tuple[tuple[str, int | None, int | None], ...]:
        """A cheap snapshot of the model folders, for "would :meth:`scan` change anything?".

        Each ``<publisher>/<repo>`` with its sidecar's mtime (written when a download
        completes or a model is adopted) and its weights' size. No hashing, no full
        walk of every file, no log line; the background precompiler polls it.
        """
        out: list[tuple[str, int | None, int | None]] = []
        for model_id, model_dir in self._walk():
            out.append(
                (
                    model_id,
                    _stat_or_none(model_dir / SIDECAR_NAME, "st_mtime_ns"),
                    _stat_or_none(model_dir / "openvino_model.bin", "st_size"),
                )
            )
        return tuple(out)

    # Adapted from StudioForge src/studioforge/core/registry.py (MIT, LaserLloyd).
    def _walk(self) -> list[tuple[str, Path]]:
        """Every ``<publisher>/<repo>`` directory with at least one real file.

        Exactly two levels. Dot/dunder directories are skipped at both levels.
        """
        root = self.models_dir
        if not root.is_dir():
            return []
        found: list[tuple[str, Path]] = []
        for publisher in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
            if not publisher.is_dir() or publisher.name.startswith(_SKIP_DIR_PREFIXES):
                continue
            for repo in sorted(publisher.iterdir(), key=lambda p: p.name.casefold()):
                if not repo.is_dir() or repo.name.startswith(_SKIP_DIR_PREFIXES):
                    continue
                if not self._has_content(repo):
                    continue
                found.append((f"{publisher.name}/{repo.name}", repo))
        return found

    @staticmethod
    def _has_content(repo_dir: Path) -> bool:
        try:
            for entry in repo_dir.iterdir():
                if entry.name.startswith(_SKIP_DIR_PREFIXES) and entry.name != SIDECAR_NAME:
                    continue
                if entry.is_file():
                    return True
        except OSError:
            return False
        return False

    def _build_record(self, model_id: str, model_dir: Path, state: dict[str, Any]) -> ModelRecord:
        local = _local_files(model_dir)
        missing = [name for name in REQUIRED_FILES if name not in local]
        sidecar = read_sidecar(model_dir)
        if sidecar is not None:
            for entry in sidecar.get("files") or []:
                if not isinstance(entry, dict):
                    continue
                rel = entry.get("path")
                if not isinstance(rel, str) or rel in missing:
                    continue
                if rel not in local:
                    missing.append(rel)
                elif isinstance(entry.get("size"), int) and local[rel] != entry["size"]:
                    missing.append(f"{rel} (size mismatch)")
        last_used = (state.get("last_used") or {}).get(model_id)
        # From the cache folders, not state.json: state keeps first-compile times after a
        # cache is cleared (they are the next ETA), so it cannot say what is warm now.
        compiled: dict[str, bool] = {}
        for variant in compile_cache.warm_variants(self.cache_dir, model_id):
            compiled[variant] = True
            compiled[variant.partition("|")[0]] = True
        verdict = npu_verdict(model_id, model_dir, self._catalog)
        return ModelRecord(
            id=model_id,
            path=model_dir,
            size_bytes=sum(local.values()),
            complete=not missing,
            missing=missing,
            catalog=self._catalog.get(model_id) if self._catalog else None,
            last_used_at=float(last_used) if isinstance(last_used, int | float) else None,
            compiled=compiled,
            source=sidecar.get("source") if sidecar else None,
            revision=sidecar.get("revision") if sidecar else None,
            npu_ok=verdict.ok,
            npu_note=verdict.reason,
        )

    def _read_state(self) -> dict[str, Any]:
        if self._state_file is None:
            return {}
        try:
            data = json.loads(Path(self._state_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def all(self) -> list[ModelRecord]:
        with self._lock:
            return sorted(self._models.values(), key=lambda r: r.id.casefold())

    def get(self, mid: str) -> ModelRecord | None:
        """Look up by id, scanning first if nothing has been scanned yet."""
        if not self._scanned:
            self.scan()
        with self._lock:
            return self._models.get(mid)

    def unadopted(self) -> list[ModelRecord]:
        """Complete models with no sidecar: candidates for :meth:`adopt`."""
        if not self._scanned:
            self.scan()
        return [r for r in self.all() if r.complete and r.source is None]

    # ------------------------------------------------------------------
    # Adoption
    # ------------------------------------------------------------------

    def adopt(
        self,
        repo_id: str,
        repo_files: Sequence[RepoFile] | None = None,
        *,
        revision: str | None = None,
        license: str | None = None,
    ) -> ModelRecord:
        """Record a model that is already on disk (``source: "adopted"``).

        With ``repo_files`` (the online case) every repo file must exist locally
        with exactly the listed size; ``graph.pbtxt`` and dot-dirs are ignored
        on both sides. Without it, the sidecar lists the local files with no
        checksums. Nothing is ever deleted or moved. The directory must be
        writable, because OVMS writes ``graph.pbtxt`` there (PLAN §7 item 2).
        """
        model_dir = self.model_dir(repo_id)
        repo = validate_repo_id(repo_id)
        if not model_dir.is_dir():
            raise ModelError(f"{repo} is not on disk", code="not_found")
        local = _local_files(model_dir)
        missing_required = [name for name in REQUIRED_FILES if name not in local]
        if missing_required:
            raise ModelError(
                f"{repo} is incomplete: missing {', '.join(missing_required)}",
                code="incomplete_model",
                details={"missing": missing_required},
            )
        if not _probe_writable(model_dir):
            raise ModelError(
                f"{model_dir} is not writable; OVMS needs to write graph.pbtxt there",
                code="not_writable",
                hint="Check the folder's permissions, or move the model to a writable folder.",
            )

        if repo_files is not None:
            problems: list[str] = []
            expected: list[RepoFile] = []
            for rf in repo_files:
                rel = safe_relpath(rf.path)
                if Path(rel).name.casefold() in _IGNORED_FILES or rel.startswith("."):
                    continue
                expected.append(rf)
                have = local.get(rel)
                if have is None:
                    problems.append(f"{rel}: missing")
                elif rf.size > 0 and have != rf.size:
                    problems.append(f"{rel}: {have} bytes on disk, {rf.size} in the repository")
            if problems:
                raise ModelError(
                    f"{repo} does not match the repository: {'; '.join(problems[:5])}",
                    code="adopt_mismatch",
                    hint="Delete the model and download it again.",
                    details={"problems": problems},
                )
            files = expected
        else:
            files = [RepoFile(path=rel, size=size) for rel, size in sorted(local.items())]

        write_sidecar(
            model_dir,
            repo_id=repo,
            files=files,
            source="adopted",
            revision=revision,
            license=license,
            models_dir=self.models_dir,
        )
        log.info("registry.adopted id=%s verified=%s", repo, repo_files is not None)
        self.scan()
        record = self.get(repo)
        assert record is not None  # noqa: S101 - we just wrote into it
        return record

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    # Adapted from StudioForge src/studioforge/core/registry.py (MIT, LaserLloyd).
    @staticmethod
    def _assert_inside(path: Path, root: Path) -> Path:
        """Resolve ``path`` and require it to be strictly inside ``root``."""
        try:
            resolved = Path(path).resolve(strict=False)
            root_resolved = Path(root).resolve(strict=False)
        except OSError as exc:  # pragma: no cover
            raise ModelError(f"cannot resolve {path}: {exc}", code="path_escape") from exc
        if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
            raise ModelError(
                f"refusing to touch {resolved}: outside {root_resolved}", code="path_escape"
            )
        return resolved

    # Adapted from StudioForge src/studioforge/core/registry.py (MIT, LaserLloyd).
    def _assert_inside_model_dirs(self, paths: Iterable[Path]) -> list[Path]:
        """Resolve each path and require it to be inside ``models_dir``."""
        return [self._assert_inside(p, self.models_dir) for p in paths]

    # Adapted from StudioForge src/studioforge/core/registry.py (MIT, LaserLloyd).
    def delete(self, mid: str) -> list[Path]:
        """Delete a model's directory and its ``cache_dir/ov/<slug>``.

        Returns the paths removed. Callers must first refuse a model that is
        loaded or downloading (PLAN §1.4). The model directory must resolve to
        exactly ``models_dir/<publisher>/<repo>``, so a junction pointing
        elsewhere, or an id that walks upward, is refused before anything is
        touched.
        """
        repo = validate_repo_id(mid)
        model_dir = self.model_dir(repo)
        if model_dir.is_symlink() or os.path.isjunction(model_dir):
            raise ModelError(
                f"refusing to delete {model_dir}: it is a link to another folder",
                code="path_escape",
            )
        (resolved,) = self._assert_inside_model_dirs([model_dir])
        root = self.models_dir.resolve(strict=False)
        if resolved.parent.parent != root:
            raise ModelError(
                f"refusing to delete {resolved}: not a <publisher>/<repo> folder of {root}",
                code="path_escape",
            )
        cache = self.cache_dir_for(repo)
        removed: list[Path] = []
        if resolved.is_dir():
            shutil.rmtree(resolved, onexc=_rmtree_onexc)
            removed.append(resolved)
            with contextlib.suppress(OSError):
                resolved.parent.rmdir()  # only succeeds when the publisher dir is empty
        if cache.is_dir() and not cache.is_symlink() and not os.path.isjunction(cache):
            shutil.rmtree(cache, onexc=_rmtree_onexc)
            removed.append(cache.resolve(strict=False))
        if not removed:
            raise ModelError(f"{repo} is not on disk", code="not_found")
        with self._lock:
            self._models.pop(repo, None)
        log.info("registry.model_deleted id=%s paths=%d", repo, len(removed))
        return removed
