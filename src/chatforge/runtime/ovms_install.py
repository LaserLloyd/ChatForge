"""Download, verify and extract the OpenVINO Model Server runtime; VC++ check.

The transfer core is adapted from StudioForge src/studioforge/core/downloader.py
(MIT, LaserLloyd): the ``.part`` + ``Range`` resume with the 206/``Content-Range``
check (``_range_honoured``, ``_total_size``), restart on a 200, the 416
recovery, the streamed sha256 and the jittered backoff (``_is_transient``,
``_backoff_delay``). One file, no DB, no queue.

The archive sha256 values are pinned below (Windows: recorded by WS1 Phase A on
2026-09-30; Linux: read from the release's published ``.sha256`` files, the Ubuntu 24
python_on archive also hashed after download), so integrity does not depend on the
sibling ``.sha256`` file served next to the archive at install time.

Windows ships a ``.zip`` (``ovms/ovms.exe``); Linux ships a ``.tar.gz``
(``ovms/bin/ovms``, shared libraries in ``ovms/lib``). :func:`host_platform` picks
the asset set, and the tests patch it to exercise either flow on any OS.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import re
import shutil
import stat
import sys
import tarfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)

OVMS_VERSION = "2026.4.0"
RELEASE_BASE = "https://github.com/openvinotoolkit/model_server/releases/download/v{version}/"


@dataclass(frozen=True, slots=True)
class OvmsAsset:
    variant: str
    filename: str
    size: int
    sha256: str

    def url(self, version: str = OVMS_VERSION) -> str:
        return RELEASE_BASE.format(version=version) + self.filename

    @property
    def is_tar(self) -> bool:
        return self.filename.endswith((".tar.gz", ".tgz"))


#: python_on is the default (PLAN §7 required change 1): python_off cannot use
#: tools and drops the system message. On Windows it bundles its own Python 3.12
#: (``ovms/python``, isolated by ``python312._pth``), so no system Python is needed.
#: This dict is the **Windows** set (kept under its original name); see
#: :data:`PLATFORM_ASSETS` and :func:`asset_for` for the per-host choice.
ASSETS: dict[str, OvmsAsset] = {
    "python_on": OvmsAsset(
        "python_on",
        "ovms_windows_2026.4.0_python_on.zip",
        138_798_816,
        # Verified with Get-FileHash against the downloaded zip, 2026-09-30.
        "5a022e44e794e6a9cb0f1c6c40822167dac53a36daf9af123c9411974cef1914",
    ),
    "python_off": OvmsAsset(
        "python_off",
        "ovms_windows_2026.4.0_python_off.zip",
        117_195_695,
        # From the release's published .sha256 file (the zip itself was not tested).
        "46d03114c97abfe05f2c5a8fde772c655aeef541ee254c23f402f81a616474e3",
    ),
}

#: Linux assets. Sizes are the release assets' Content-Length; the digests are the
#: contents of the release's companion ``<archive>.sha256`` files (both Ubuntu 24
#: archives were also downloaded and hashed: same values). NOTE: unlike Windows,
#: the Linux python_on build does not bundle an interpreter; it needs the system
#: ``python3`` plus ``Jinja2`` and ``MarkupSafe`` (and ``numpy`` for Python nodes).
UBUNTU22_ASSETS: dict[str, OvmsAsset] = {
    "python_on": OvmsAsset(
        "python_on",
        "ovms_ubuntu22_2026.4.0_python_on.tar.gz",
        192_806_746,
        "bb14ef8987bf4e3796905b89cba01ac92acd9599dc42e5d8a46fdbfafd660e12",
    ),
    "python_off": OvmsAsset(
        "python_off",
        "ovms_ubuntu22_2026.4.0_python_off.tar.gz",
        176_488_078,
        "a5a9e4a4a48dbd30a5d18f9088d2a030758f788d13e9ff14d891e008083756ab",
    ),
}
UBUNTU24_ASSETS: dict[str, OvmsAsset] = {
    "python_on": OvmsAsset(
        "python_on",
        "ovms_ubuntu24_2026.4.0_python_on.tar.gz",
        196_991_877,
        "4a142a7a7409d91299f115562c587b342c74f3b6749b588ac8e40c8f977dcbf8",
    ),
    "python_off": OvmsAsset(
        "python_off",
        "ovms_ubuntu24_2026.4.0_python_off.tar.gz",
        180_278_899,
        "cc5caad0e859b249fac586847fa922c9f57e1c910abd2233a57b3cca0172fb91",
    ),
}
#: ``host_platform()`` value -> asset set.
PLATFORM_ASSETS: dict[str, dict[str, OvmsAsset]] = {
    "windows": ASSETS,
    "ubuntu22": UBUNTU22_ASSETS,
    "ubuntu24": UBUNTU24_ASSETS,
}
DEFAULT_VARIANT = "python_on"
ASSET = ASSETS[DEFAULT_VARIANT].filename
ASSET_BYTES = ASSETS[DEFAULT_VARIANT].size
ASSET_SHA256 = ASSETS[DEFAULT_VARIANT].sha256

#: Written into ``runtime/ovms-<ver>/`` after a verified extraction.
RUNTIME_MARKER = ".chatforge-runtime.json"

_CHUNK = 1 << 20
_EMIT_INTERVAL_S = 0.25
_MAX_ATTEMPTS = 5
_RETRY_BASE_S = 2.0
_RETRY_CAP_S = 30.0
_CONTENT_RANGE_RE = re.compile(r"bytes (?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+|\*)")


class PathsLike(Protocol):
    runtime_dir: Path


class LocalCfgLike(Protocol):
    ovms_version: str
    ovms_variant: str


class InstallError(RuntimeError):
    """Runtime install failed. ``code`` is one of: ``cancelled``, ``checksum``,
    ``size``, ``http``, ``network``, ``unsafe_zip``, ``bad_zip``, ``disk``,
    ``unknown_variant``, ``in_use``."""

    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


# ---------------------------------------------------------------------------
# Locations and status
# ---------------------------------------------------------------------------


_logged_platform: set[str] = set()


def linux_platform_key(os_release: Path | str = "/etc/os-release") -> str:
    """``ubuntu22`` / ``ubuntu24`` from ``/etc/os-release`` (``ID=ubuntu`` + ``VERSION_ID``).

    Any other release (other Ubuntu versions, other distros, an unreadable file) gets
    the ``ubuntu24`` archive, with a log line saying so.
    """
    fields: dict[str, str] = {}
    try:
        for line in Path(os_release).read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    distro = fields.get("ID", "").lower()
    version = fields.get("VERSION_ID", "")
    if distro == "ubuntu" and version == "22.04":
        return "ubuntu22"
    if distro == "ubuntu" and version == "24.04":
        return "ubuntu24"
    if "ubuntu24_default" not in _logged_platform:
        _logged_platform.add("ubuntu24_default")
        log.info(
            "ovms_linux_default_archive",
            reason="not Ubuntu 22.04 or 24.04; using the ubuntu24 archive",
            id=distro or None,
            version_id=version or None,
        )
    return "ubuntu24"


def host_platform() -> str:
    """``windows``, ``ubuntu22`` or ``ubuntu24``: which OVMS asset set this host uses."""
    if sys.platform == "win32":
        return "windows"
    return linux_platform_key()


def asset_for(variant: str, platform: str | None = None) -> OvmsAsset:
    assets = PLATFORM_ASSETS[platform or host_platform()]
    try:
        return assets[variant]
    except KeyError:
        raise InstallError(
            f"Unknown OVMS package variant '{variant}' (expected one of {sorted(assets)})",
            code="unknown_variant",
        ) from None


def install_dir(runtime_dir: Path, version: str = OVMS_VERSION) -> Path:
    """``runtime/ovms-<version>`` -- the folder the archive is extracted into."""
    return Path(runtime_dir) / f"ovms-{version}"


def ovms_dir(runtime_dir: Path, version: str = OVMS_VERSION) -> Path:
    """``runtime/ovms-<version>/ovms`` -- holds the executable (and ``setupvars.ps1`` on
    Windows, ``lib/`` on Linux)."""
    return install_dir(runtime_dir, version) / "ovms"


def exe_in(ovms_folder: Path | str, platform: str | None = None) -> Path:
    """The OVMS executable inside an ``ovms`` folder: ``ovms.exe`` or ``bin/ovms``."""
    if (platform or host_platform()) == "windows":
        return Path(ovms_folder) / "ovms.exe"
    return Path(ovms_folder) / "bin" / "ovms"


def ovms_exe(runtime_dir: Path, version: str = OVMS_VERSION) -> Path:
    return exe_in(ovms_dir(runtime_dir, version))


def downloads_dir(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / "downloads"


def detect_variant(ovms_folder: Path) -> str | None:
    """``python_on`` when Python support is present, else ``python_off``.

    Windows: the bundled ``python/python.exe``. Linux: ``lib/python/pyovms.so`` (the
    archive's Python-node module; the interpreter itself is the system one).
    """
    folder = Path(ovms_folder)
    if not exe_in(folder).is_file():
        return None
    if host_platform() == "windows":
        return "python_on" if (folder / "python" / "python.exe").is_file() else "python_off"
    return "python_on" if (folder / "lib" / "python" / "pyovms.so").is_file() else "python_off"


def read_runtime_marker(runtime_dir: Path, version: str = OVMS_VERSION) -> dict[str, Any] | None:
    try:
        data = json.loads(
            (install_dir(runtime_dir, version) / RUNTIME_MARKER).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_VC_KEYS = (
    r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
    r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
)
_VC_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")


def vcredist_present() -> bool | None:
    """VC++ 2015+ x64 runtime: HKLM ``...\\VC\\Runtimes\\x64 Installed=1`` plus the DLLs.

    ``None`` off Windows (not applicable; callers treat it as fine). Never raises.
    (Only a missing runtime needs admin to fix; the app shows the ``winget``
    instruction and never runs it.)
    """
    if os.name != "nt":
        return None
    registry_ok = False
    try:
        import winreg

        for key in _VC_KEYS:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "Installed")
                    if int(value) == 1:
                        registry_ok = True
                        break
            except OSError:
                continue
    except ImportError:  # pragma: no cover - non-Windows
        return False
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    dlls_ok = all((system32 / name).is_file() for name in _VC_DLLS)
    return registry_ok and dlls_ok


def vcredist_version() -> str | None:
    """The installed VC++ x64 runtime version string (e.g. ``v14.50.35719.00``)."""
    if os.name != "nt":
        return None
    try:
        import winreg

        for key in _VC_KEYS:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "Version")
                    return str(value)
            except OSError:
                continue
    except ImportError:  # pragma: no cover
        return None
    return None


def runtime_status(paths: PathsLike, cfg: LocalCfgLike) -> dict[str, Any]:
    """``{"installed", "version", "variant", "exe", "vcredist", ...}`` for the Settings card.

    ``vcredist`` is ``None`` off Windows (not applicable); ``platform`` is
    :func:`host_platform`.
    """
    version = getattr(cfg, "ovms_version", OVMS_VERSION) or OVMS_VERSION
    wanted = getattr(cfg, "ovms_variant", DEFAULT_VARIANT) or DEFAULT_VARIANT
    exe = ovms_exe(paths.runtime_dir, version)
    marker = read_runtime_marker(paths.runtime_dir, version) or {}
    variant = marker.get("variant") or detect_variant(ovms_dir(paths.runtime_dir, version))
    installed = exe.is_file()
    return {
        "installed": installed,
        "version": version if installed else None,
        "variant": variant,
        "wanted_variant": wanted,
        "variant_matches": installed and variant == wanted,
        "exe": str(exe) if installed else None,
        "vcredist": vcredist_present(),
        "platform": host_platform(),
        "sha256": marker.get("sha256"),
    }


# ---------------------------------------------------------------------------
# Transfer (adapted from StudioForge core/downloader.py)
# ---------------------------------------------------------------------------


class _RangeUnsatisfiable(Exception):
    pass


class _Transient(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Adapted from StudioForge src/studioforge/core/downloader.py _range_honoured (MIT, LaserLloyd)
def _range_honoured(response: httpx.Response, have: int) -> bool:
    """Whether the server really resumed at ``have`` (206 *and* matching start)."""
    if response.status_code != 206:
        return False
    header = response.headers.get("Content-Range")
    if not header:
        return False
    match = _CONTENT_RANGE_RE.match(header.strip())
    return match is not None and int(match.group("start")) == have


# Adapted from StudioForge src/studioforge/core/downloader.py _total_size (MIT, LaserLloyd)
def _total_size(response: httpx.Response, have: int, fallback: int) -> int:
    header = response.headers.get("Content-Range")
    if header:
        match = _CONTENT_RANGE_RE.match(header.strip())
        if match is not None and match.group("total") != "*":
            return int(match.group("total"))
    length = response.headers.get("Content-Length")
    if length is not None:
        with contextlib.suppress(ValueError):
            return have + int(length)
    return fallback


# Adapted from StudioForge src/studioforge/core/downloader.py _backoff_delay (MIT, LaserLloyd)
def _backoff_delay(attempt: int, *, retry_after: float | None = None) -> float:
    delay = min(_RETRY_CAP_S, _RETRY_BASE_S * (2 ** max(0, attempt - 1)))
    if retry_after is not None:
        delay = max(delay, min(_RETRY_CAP_S, retry_after))
    return delay * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def file_sha256(path: Path) -> str:
    """sha256 of a file on disk. Blocking; call it from a worker thread."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(_CHUNK):
            hasher.update(block)
    return hasher.hexdigest()


class _Progress:
    def __init__(self, total: int, emit: Callable[[dict[str, Any]], None]) -> None:
        self.total = total
        self.done = 0
        self.status = "downloading"
        self._emit = emit
        self._samples: list[tuple[float, int]] = []
        self._last = 0.0

    def speed_bps(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def emit(self, *, force: bool = False, **extra: Any) -> None:
        now = time.monotonic()
        self._samples.append((now, self.done))
        self._samples = [s for s in self._samples if now - s[0] <= 5.0]
        if not force and now - self._last < _EMIT_INTERVAL_S:
            return
        self._last = now
        speed = self.speed_bps()
        remaining = max(0, self.total - self.done)
        payload = {
            "status": self.status,
            "downloaded_bytes": self.done,
            "total_bytes": self.total,
            "speed_bps": round(speed, 1),
            "eta_s": round(remaining / speed, 1) if speed > 0 else None,
            "error": None,
        }
        payload.update(extra)
        with contextlib.suppress(Exception):
            self._emit(payload)


async def _transfer(
    client: httpx.AsyncClient,
    url: str,
    part: Path,
    asset: OvmsAsset,
    progress: _Progress,
    cancel: asyncio.Event | None,
    *,
    allow_resume: bool,
) -> str:
    """One attempt. Returns the sha256 hex of the complete ``.part``."""
    hasher = hashlib.sha256()
    have = part.stat().st_size if (allow_resume and part.exists()) else 0
    if have > asset.size:
        have = 0
    if have:
        await asyncio.to_thread(_rehash, part, hasher)
    if have == asset.size:
        # Every byte is here (a crash between the last write and the rename):
        # prove it from the hash just computed instead of asking for nothing.
        if hasher.hexdigest() == asset.sha256:
            return asset.sha256
        have = 0  # same length, different bytes: start clean
        hasher = hashlib.sha256()
    headers = {"Range": f"bytes={have}-"} if have else {}
    progress.done = have
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 416:
            raise _RangeUnsatisfiable
        if response.status_code == 429 or response.status_code >= 500:
            await response.aread()
            raise _Transient(
                f"HTTP {response.status_code} from {url}", _parse_retry_after(response)
            )
        if response.status_code >= 400:
            await response.aread()
            raise InstallError(
                f"Download failed: HTTP {response.status_code} from {url}",
                code="http",
                details={"status": response.status_code},
            )
        resumed = have > 0 and _range_honoured(response, have)
        if have and not resumed:
            # 200 to a Range request: the whole object is coming. Appending
            # would splice a duplicate prefix into a corrupt file.
            have = 0
            hasher = hashlib.sha256()
            progress.done = 0
        total = _total_size(response, have, asset.size)
        if total != asset.size:
            raise InstallError(
                f"{asset.filename}: expected {asset.size:,} bytes but the server is "
                f"sending {total:,}; refusing to install a file that does not match",
                code="size",
                details={"expected": asset.size, "server": total},
            )
        with part.open("r+b" if resumed else "wb") as fh:
            if resumed:
                fh.seek(have)
            async for chunk in response.aiter_bytes(_CHUNK):
                if cancel is not None and cancel.is_set():
                    raise InstallError("Runtime download cancelled", code="cancelled")
                if not chunk:
                    continue
                fh.write(chunk)
                hasher.update(chunk)
                progress.done += len(chunk)
                progress.emit()
            fh.flush()
            os.fsync(fh.fileno())
    return hasher.hexdigest()


def _rehash(path: Path, hasher: Any) -> None:
    with path.open("rb") as handle:
        while block := handle.read(_CHUNK):
            hasher.update(block)


async def download_asset(
    asset: OvmsAsset,
    dest: Path,
    *,
    version: str = OVMS_VERSION,
    client: httpx.AsyncClient | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel: asyncio.Event | None = None,
    url: str | None = None,
) -> Path:
    """Download ``asset`` to ``dest`` via ``dest.part``; verify size and sha256.

    An existing ``dest`` whose sha256 matches is reused without a request. A
    ``.part`` is resumed with ``Range``. A checksum mismatch deletes the
    ``.part`` (the bytes are garbage) and raises ``InstallError(code="checksum")``.
    A cancel keeps the ``.part`` so the next attempt resumes.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tracker = _Progress(asset.size, progress or (lambda _p: None))
    if dest.is_file() and dest.stat().st_size == asset.size:
        tracker.status = "verifying"
        tracker.done = asset.size
        tracker.emit(force=True)
        if await asyncio.to_thread(file_sha256, dest) == asset.sha256:
            return dest
        log.warning("ovms_zip_checksum_mismatch_existing", path=str(dest))
        dest.unlink(missing_ok=True)
    part = dest.with_name(dest.name + ".part")
    source = url or asset.url(version)
    owns_client = client is None
    http = client or httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0)
    )
    try:
        attempt = 0
        while True:
            attempt += 1
            try:
                digest = ""
                for allow_resume in (True, False):
                    try:
                        digest = await _transfer(
                            http, source, part, asset, tracker, cancel, allow_resume=allow_resume
                        )
                        break
                    except _RangeUnsatisfiable:
                        part.unlink(missing_ok=True)
                else:  # pragma: no cover - both attempts 416
                    raise InstallError("The server kept answering HTTP 416", code="http")
                break
            except (httpx.TransportError, _Transient) as exc:
                if attempt >= _MAX_ATTEMPTS:
                    raise InstallError(
                        f"Download failed after {attempt} attempts: {exc}", code="network"
                    ) from exc
                delay = _backoff_delay(attempt, retry_after=getattr(exc, "retry_after", None))
                log.warning("ovms_download_retry", attempt=attempt, delay_s=round(delay, 1))
                await asyncio.sleep(delay)
    finally:
        if owns_client:
            await http.aclose()

    tracker.status = "verifying"
    tracker.emit(force=True)
    size = part.stat().st_size
    if size != asset.size:
        part.unlink(missing_ok=True)
        raise InstallError(
            f"{asset.filename}: downloaded {size:,} bytes, expected {asset.size:,}",
            code="size",
        )
    if digest != asset.sha256:
        part.unlink(missing_ok=True)
        raise InstallError(
            f"{asset.filename}: sha256 {digest} does not match the pinned {asset.sha256}",
            code="checksum",
            details={"expected": asset.sha256, "actual": digest},
        )
    os.replace(part, dest)
    return dest


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _member_target(root: Path, name: str) -> Path:
    """Where zip member ``name`` would land under ``root``; raises on zip-slip."""
    unsafe = InstallError(f"Unsafe path in runtime zip: {name!r}", code="unsafe_zip")
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or pure.is_absolute()
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part == ".." for part in pure.parts)
        or "\x00" in normalized
    ):
        raise unsafe
    target = (root / Path(*pure.parts)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise unsafe from None
    return target


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def safe_extract(zip_path: Path, dest: Path, *, cancel: asyncio.Event | None = None) -> int:
    """Extract ``zip_path`` into ``dest`` with a zip-slip guard. Returns files written.

    Every member is validated **before** anything is written: absolute paths,
    drive letters, ``..`` segments and symlinks refuse the whole archive.
    Blocking; call it from a worker thread.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise InstallError(f"Corrupt runtime zip: {exc}", code="bad_zip") from exc
    with archive:
        members = archive.infolist()
        plan: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in members:
            if _is_symlink(info):
                raise InstallError(
                    f"Unsafe symlink in runtime zip: {info.filename!r}", code="unsafe_zip"
                )
            plan.append((info, _member_target(dest, info.filename)))
        written = 0
        for info, target in plan:
            if cancel is not None and cancel.is_set():
                raise InstallError("Runtime install cancelled", code="cancelled")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
            written += 1
    return written


#: Build-image root the Linux archives' absolute symlinks point into
#: (``/ovms/lib/libopenvino_tokenizers.so``); remapped to the extraction root.
_ARCHIVE_ROOT_PREFIX = "/ovms/"


def _tar_link_target(root: Path, member_name: str, linkname: str) -> str:
    """The (possibly rewritten) target for symlink member ``member_name``; raises if unsafe.

    Relative targets must stay inside the archive tree. The one absolute form the
    official archives contain, ``/ovms/...`` (the build image's install path), is
    rewritten to the equivalent relative link; any other absolute target is refused.
    """
    unsafe = InstallError(
        f"Unsafe symlink in runtime archive: {member_name!r} -> {linkname!r}", code="unsafe_zip"
    )
    if not linkname or "\x00" in linkname or "\\" in linkname:
        raise unsafe
    link_dir = PurePosixPath(member_name).parent
    if linkname.startswith("/"):
        if not linkname.startswith(_ARCHIVE_ROOT_PREFIX) or ".." in linkname.split("/"):
            raise unsafe
        wanted = PurePosixPath(linkname[1:])
        rel = os.path.relpath(wanted.as_posix(), link_dir.as_posix())
        linkname = rel.replace(os.sep, "/")
    resolved = os.path.normpath((link_dir / linkname).as_posix())
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise unsafe
    return linkname


def safe_extract_tar(tar_path: Path, dest: Path, *, cancel: asyncio.Event | None = None) -> int:
    """Extract the ``.tar.gz`` ``tar_path`` into ``dest``; the twin of :func:`safe_extract`.

    Every member is validated **before** anything is written: absolute paths, drive
    letters, ``..`` segments, hard links, device/fifo members, symlinks pointing outside
    the tree (see :func:`_tar_link_target`) and members written *through* a symlink refuse
    the whole archive. Regular files keep their permission bits (executable included;
    setuid/setgid/sticky are dropped, owner write is added so a re-install can replace
    them). Returns files written (symlinks included). Blocking; call it from a worker thread.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        archive = tarfile.open(tar_path, "r:gz")  # noqa: SIM115 - closed in the with below
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise InstallError(f"Corrupt runtime archive: {exc}", code="bad_zip") from exc
    with archive:
        plan: list[tuple[tarfile.TarInfo, Path, str | None]] = []
        links: set[str] = set()
        try:
            for info in archive:
                target = _member_target(dest, info.name)
                name = PurePosixPath(info.name.replace("\\", "/")).as_posix().rstrip("/")
                if any(parent in links for parent in map(str, PurePosixPath(name).parents)):
                    raise InstallError(
                        f"Unsafe path in runtime archive (through a symlink): {info.name!r}",
                        code="unsafe_zip",
                    )
                if name in links and not info.issym():
                    raise InstallError(
                        f"Unsafe path in runtime archive (replaces a symlink): {info.name!r}",
                        code="unsafe_zip",
                    )
                if info.issym():
                    link = _tar_link_target(dest, name, info.linkname)
                    links.add(name)
                    plan.append((info, target, link))
                elif info.isreg() or info.isdir():
                    plan.append((info, target, None))
                else:
                    raise InstallError(
                        f"Unsafe member type in runtime archive: {info.name!r}", code="unsafe_zip"
                    )
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise InstallError(f"Corrupt runtime archive: {exc}", code="bad_zip") from exc
        written = 0
        made_links: list[Path] = []
        try:
            for info, target, link in plan:
                if cancel is not None and cancel.is_set():
                    raise InstallError("Runtime install cancelled", code="cancelled")
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o700 | (info.mode & 0o755))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if link is not None:
                    if target.is_symlink() or target.exists():
                        target.unlink()
                    os.symlink(link, target)
                    made_links.append(target)
                    written += 1
                    continue
                source = archive.extractfile(info)
                if source is None:  # pragma: no cover - isreg() members always have data
                    continue
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, _CHUNK)
                target.chmod(0o200 | (info.mode & 0o755))
                written += 1
            root = dest.resolve()
            for link_path in made_links:
                real = Path(os.path.realpath(link_path))
                if real != root and root not in real.parents:
                    for made in made_links:
                        made.unlink(missing_ok=True)
                    raise InstallError(
                        f"Unsafe symlink in runtime archive: {link_path.name!r} leaves the tree",
                        code="unsafe_zip",
                    )
        except InstallError:
            raise
        except (tarfile.TarError, EOFError) as exc:
            raise InstallError(f"Corrupt runtime archive: {exc}", code="bad_zip") from exc
    return written


def extract_archive(archive: Path, dest: Path, *, cancel: asyncio.Event | None = None) -> int:
    """:func:`safe_extract` or :func:`safe_extract_tar`, by file name."""
    if Path(archive).name.endswith((".tar.gz", ".tgz")):
        return safe_extract_tar(archive, dest, cancel=cancel)
    return safe_extract(archive, dest, cancel=cancel)


#: Process names of the server (Windows ``ovms.exe``, Linux ``ovms``), lower case.
OVMS_PROCESS_NAMES = ("ovms.exe", "ovms")

IN_USE_MESSAGE = (
    "The installed runtime is in use, so it cannot be replaced. "
    "Unload the model first, then install again."
)


def running_from(folder: Path) -> list[int]:
    """Pids of running ``ovms.exe`` / ``ovms`` processes whose executable lies under ``folder``.

    Never raises: a process that exits or denies access while being looked at is skipped.
    """
    import psutil

    root = os.path.normcase(os.path.abspath(folder))
    pids: list[int] = []
    for proc in psutil.process_iter(["name"]):
        with contextlib.suppress(psutil.Error, OSError):
            if (proc.info.get("name") or "").lower() not in OVMS_PROCESS_NAMES:
                continue
            exe = os.path.normcase(os.path.abspath(proc.exe()))
            if exe.startswith(root + os.sep):
                pids.append(proc.pid)
    return pids


def _rmtree(path: Path) -> None:
    def _onexc(func: Callable[..., Any], p: str, _exc: BaseException) -> None:
        with contextlib.suppress(OSError):
            os.chmod(p, 0o666)
            func(p)

    if path.exists():
        shutil.rmtree(path, onexc=_onexc)


async def install(
    paths: PathsLike,
    cfg: LocalCfgLike,
    progress: Callable[[dict[str, Any]], None],
    cancel: asyncio.Event,
    *,
    client: httpx.AsyncClient | None = None,
    url: str | None = None,
    force: bool = False,
) -> Path:
    """Install the OVMS runtime (download, verify, extract) and return the executable.

    Idempotent: an existing install of the wanted variant is returned as is.
    Extraction goes to ``ovms-<ver>.tmp`` and is renamed into place only after
    the executable is found inside, so a crash never leaves a half runtime that
    looks installed.
    """
    version = getattr(cfg, "ovms_version", OVMS_VERSION) or OVMS_VERSION
    variant = getattr(cfg, "ovms_variant", DEFAULT_VARIANT) or DEFAULT_VARIANT
    platform = host_platform()
    asset = asset_for(variant, platform)
    runtime_dir = Path(paths.runtime_dir)
    final = install_dir(runtime_dir, version)
    exe = ovms_exe(runtime_dir, version)
    if not force and exe.is_file() and detect_variant(ovms_dir(runtime_dir, version)) == variant:
        progress(
            {
                "status": "done",
                "downloaded_bytes": asset.size,
                "total_bytes": asset.size,
                "speed_bps": 0,
                "eta_s": 0,
                "error": None,
            }
        )
        return exe
    if final.exists() and await asyncio.to_thread(running_from, final):
        # Replacing the folder renames it, which Windows refuses while ovms runs from
        # it ("Access is denied"). Say so before a 100+ MB download, not after.
        progress({"status": "error", "error": IN_USE_MESSAGE, "code": "in_use"})
        raise InstallError(IN_USE_MESSAGE, code="in_use")

    try:
        zip_path = await download_asset(
            asset,
            downloads_dir(runtime_dir) / asset.filename,
            version=version,
            client=client,
            progress=progress,
            cancel=cancel,
            url=url,
        )
        progress(
            {
                "status": "extracting",
                "downloaded_bytes": asset.size,
                "total_bytes": asset.size,
                "speed_bps": 0,
                "eta_s": None,
                "error": None,
            }
        )
        tmp = final.with_name(final.name + ".tmp")
        await asyncio.to_thread(_rmtree, tmp)
        await asyncio.to_thread(extract_archive, zip_path, tmp, cancel=cancel)
        wanted_exe = exe_in(tmp / "ovms", platform)
        if not wanted_exe.is_file():
            await asyncio.to_thread(_rmtree, tmp)
            shown = wanted_exe.relative_to(tmp).as_posix()
            raise InstallError(f"The runtime archive does not contain {shown}", code="bad_zip")
        marker = {
            "schema": 1,
            "version": version,
            "variant": variant,
            "asset": asset.filename,
            "sha256": asset.sha256,
            "installed_at": time.time(),
        }
        (tmp / RUNTIME_MARKER).write_text(json.dumps(marker, indent=2), encoding="utf-8")
        if final.exists():
            old = final.with_name(final.name + ".old")
            await asyncio.to_thread(_rmtree, old)
            try:
                os.replace(final, old)
            except PermissionError as exc:  # ovms (or another program) holds it
                await asyncio.to_thread(_rmtree, tmp)
                raise InstallError(IN_USE_MESSAGE, code="in_use") from exc
            await asyncio.to_thread(_rmtree, old)
        os.replace(tmp, final)
    except InstallError as exc:
        progress(
            {
                "status": "cancelled" if exc.code == "cancelled" else "error",
                "error": exc.message,
                "code": exc.code,
            }
        )
        raise
    except OSError as exc:
        progress({"status": "error", "error": f"{type(exc).__name__}: {exc}", "code": "disk"})
        raise InstallError(f"Could not install the runtime: {exc}", code="disk") from exc
    progress(
        {
            "status": "done",
            "downloaded_bytes": asset.size,
            "total_bytes": asset.size,
            "speed_bps": 0,
            "eta_s": 0,
            "error": None,
        }
    )
    log.info("ovms_installed", version=version, variant=variant, exe=str(exe))
    return exe


def adopt_existing(runtime_dir: Path, version: str = OVMS_VERSION) -> dict[str, Any] | None:
    """Write the runtime marker for an install extracted by hand (WS1 Phase A).

    Returns the marker, or ``None`` when there is no executable to adopt.
    """
    variant = detect_variant(ovms_dir(runtime_dir, version))
    if variant is None:
        return None
    asset = asset_for(variant)
    marker = {
        "schema": 1,
        "version": version,
        "variant": variant,
        "asset": asset.filename,
        "sha256": asset.sha256,
        "installed_at": time.time(),
        "adopted": True,
    }
    (install_dir(runtime_dir, version) / RUNTIME_MARKER).write_text(
        json.dumps(marker, indent=2), encoding="utf-8"
    )
    return marker
