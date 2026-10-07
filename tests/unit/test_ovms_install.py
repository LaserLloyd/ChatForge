"""OVMS runtime install: download/verify/resume, zip-slip guard, status."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from chatforge.runtime import ovms_install as oi


@pytest.fixture(autouse=True)
def _windows_host(monkeypatch):
    """The zip/ovms.exe flow below is the Windows one; Linux tests override this."""
    monkeypatch.setattr(oi, "host_platform", lambda: "windows")


@pytest.fixture
def linux_host(monkeypatch):
    monkeypatch.setattr(oi, "host_platform", lambda: "ubuntu24")


def _zip_bytes(members: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "target")
    return buf.getvalue()


def _asset(data: bytes, name: str = "ovms_test.zip") -> oi.OvmsAsset:
    return oi.OvmsAsset("python_on", name, len(data), hashlib.sha256(data).hexdigest())


def _server(data: bytes, *, honour_range: bool = True, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(dict(request.headers))
        rng = request.headers.get("Range")
        if rng and honour_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                return httpx.Response(416)
            body = data[start:]
            return httpx.Response(
                206,
                content=body,
                headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"},
            )
        return httpx.Response(200, content=data)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_pinned_constants():
    assert oi.OVMS_VERSION == "2026.4.0"
    assert oi.ASSET == "ovms_windows_2026.4.0_python_on.zip"
    assert oi.ASSET_BYTES == 138_798_816
    assert oi.ASSET_SHA256 == "5a022e44e794e6a9cb0f1c6c40822167dac53a36daf9af123c9411974cef1914"
    assert oi.ASSETS["python_off"].size == 117_195_695
    assert oi.ASSETS["python_on"].url().endswith("/v2026.4.0/ovms_windows_2026.4.0_python_on.zip")
    with pytest.raises(oi.InstallError) as err:
        oi.asset_for("python_maybe")
    assert err.value.code == "unknown_variant"


async def test_download_verifies_and_publishes(tmp_path):
    data = os.urandom(300_000)
    asset = _asset(data)
    events: list[dict] = []
    async with _server(data) as client:
        out = await oi.download_asset(
            asset,
            tmp_path / asset.filename,
            client=client,
            progress=events.append,
            url="https://x/test.zip",
        )
    assert out.read_bytes() == data
    assert not (tmp_path / (asset.filename + ".part")).exists()
    assert any(e["status"] == "verifying" for e in events)


async def test_checksum_mismatch_deletes_part(tmp_path):
    data = os.urandom(50_000)
    asset = oi.OvmsAsset("python_on", "bad.zip", len(data), "0" * 64)
    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.download_asset(asset, tmp_path / "bad.zip", client=client, url="https://x/z")
    assert err.value.code == "checksum"
    assert not (tmp_path / "bad.zip").exists()
    assert not (tmp_path / "bad.zip.part").exists()


async def test_size_mismatch_refused(tmp_path):
    data = os.urandom(10_000)
    asset = oi.OvmsAsset("python_on", "s.zip", len(data) + 5, hashlib.sha256(data).hexdigest())
    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.download_asset(asset, tmp_path / "s.zip", client=client, url="https://x/z")
    assert err.value.code == "size"


async def test_resume_with_range(tmp_path):
    data = os.urandom(200_000)
    asset = _asset(data)
    (tmp_path / (asset.filename + ".part")).write_bytes(data[:70_000])
    calls: list = []
    async with _server(data, calls=calls) as client:
        out = await oi.download_asset(
            asset, tmp_path / asset.filename, client=client, url="https://x/z"
        )
    assert out.read_bytes() == data
    assert calls[0].get("range") == "bytes=70000-"


async def test_server_ignoring_range_restarts_cleanly(tmp_path):
    data = os.urandom(120_000)
    asset = _asset(data)
    (tmp_path / (asset.filename + ".part")).write_bytes(b"\0" * 40_000)  # garbage prefix
    async with _server(data, honour_range=False) as client:
        out = await oi.download_asset(
            asset, tmp_path / asset.filename, client=client, url="https://x/z"
        )
    assert out.read_bytes() == data


async def test_complete_but_wrong_part_restarts(tmp_path):
    data = os.urandom(20_000)
    asset = _asset(data)
    # A stale .part of the right length but wrong bytes is re-fetched from zero.
    (tmp_path / (asset.filename + ".part")).write_bytes(os.urandom(len(data)))
    calls: list = []
    async with _server(data, calls=calls) as client:
        out = await oi.download_asset(
            asset, tmp_path / asset.filename, client=client, url="https://x/z"
        )
    assert out.read_bytes() == data
    assert "range" not in calls[0]


async def test_complete_part_published_without_request(tmp_path):
    data = os.urandom(20_000)
    asset = _asset(data)
    (tmp_path / (asset.filename + ".part")).write_bytes(data)

    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await oi.download_asset(asset, tmp_path / asset.filename, client=client)
    assert out.read_bytes() == data


async def test_416_recovers_by_restarting(tmp_path):
    data = os.urandom(30_000)
    asset = _asset(data)
    (tmp_path / (asset.filename + ".part")).write_bytes(data[:1000])

    def handler(request):
        if request.headers.get("Range"):
            return httpx.Response(416)
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await oi.download_asset(
            asset, tmp_path / asset.filename, client=client, url="https://x/z"
        )
    assert out.read_bytes() == data


async def test_existing_verified_zip_is_reused_without_request(tmp_path):
    data = os.urandom(5_000)
    asset = _asset(data)
    (tmp_path / asset.filename).write_bytes(data)

    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await oi.download_asset(asset, tmp_path / asset.filename, client=client)
    assert out.read_bytes() == data


async def test_cancel_keeps_part_for_resume(tmp_path):
    data = os.urandom(3 * (1 << 20))
    asset = _asset(data)
    cancel = asyncio.Event()

    def progress(event):
        if event.get("downloaded_bytes", 0) > 0:
            cancel.set()

    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.download_asset(
                asset,
                tmp_path / asset.filename,
                client=client,
                url="https://x/z",
                progress=progress,
                cancel=cancel,
            )
    assert err.value.code == "cancelled"
    assert (tmp_path / (asset.filename + ".part")).exists()


async def test_http_error_is_not_retried(tmp_path):
    data = b"abc"
    asset = _asset(data)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.download_asset(asset, tmp_path / "x.zip", client=client, url="https://x/z")
    assert err.value.code == "http"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "name",
    ["../evil.txt", "ovms/../../evil.txt", "/abs/evil.txt", "C:/evil.txt", "..\\evil.txt"],
)
def test_zip_slip_is_refused_before_writing(tmp_path, name):
    zpath = tmp_path / "evil.zip"
    zpath.write_bytes(_zip_bytes({"ovms/ok.txt": b"ok", name: b"bad"}))
    dest = tmp_path / "out"
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract(zpath, dest)
    assert err.value.code == "unsafe_zip"
    assert not (dest / "ovms" / "ok.txt").exists()  # nothing written at all
    assert not (tmp_path / "evil.txt").exists()


def test_symlink_member_refused(tmp_path):
    zpath = tmp_path / "link.zip"
    zpath.write_bytes(_zip_bytes({"ovms/ok.txt": b"ok"}, symlink="ovms/link"))
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract(zpath, tmp_path / "out")
    assert err.value.code == "unsafe_zip"


def test_corrupt_zip(tmp_path):
    zpath = tmp_path / "junk.zip"
    zpath.write_bytes(b"not a zip")
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract(zpath, tmp_path / "out")
    assert err.value.code == "bad_zip"


def _runtime_zip() -> bytes:
    return _zip_bytes(
        {
            "ovms/ovms.exe": b"MZ fake",
            "ovms/setupvars.ps1": b"# vars",
            "ovms/python/python.exe": b"MZ py",
            "ovms/python/Lib/site-packages/jinja2/__init__.py": b"",
        }
    )


async def test_install_end_to_end_and_idempotent(tmp_path, monkeypatch):
    data = _runtime_zip()
    monkeypatch.setitem(oi.ASSETS, "python_on", _asset(data, oi.ASSET))
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    events: list[dict] = []
    async with _server(data) as client:
        exe = await oi.install(paths, cfg, events.append, asyncio.Event(), client=client)
    assert exe == tmp_path / "runtime" / "ovms-2026.4.0" / "ovms" / "ovms.exe"
    assert exe.read_bytes() == b"MZ fake"
    assert not (tmp_path / "runtime" / "ovms-2026.4.0.tmp").exists()
    assert events[-1]["status"] == "done"
    assert [e["status"] for e in events].count("extracting") == 1
    status = oi.runtime_status(paths, cfg)
    assert status["installed"] and status["variant"] == "python_on"
    assert status["version"] == "2026.4.0" and status["variant_matches"]
    assert status["sha256"] == hashlib.sha256(data).hexdigest()
    assert status["vcredist"] == oi.vcredist_present()
    assert status["platform"] == "windows"

    def handler(request):  # pragma: no cover - second install must not download
        raise AssertionError("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        again = await oi.install(paths, cfg, lambda e: None, asyncio.Event(), client=client)
    assert again == exe


async def test_install_rejects_zip_without_exe(tmp_path, monkeypatch):
    data = _zip_bytes({"ovms/readme.txt": b"hi"})
    monkeypatch.setitem(oi.ASSETS, "python_on", _asset(data, oi.ASSET))
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    events: list[dict] = []
    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.install(paths, cfg, events.append, asyncio.Event(), client=client)
    assert err.value.code == "bad_zip"
    assert events[-1]["status"] == "error"
    assert not (tmp_path / "runtime" / "ovms-2026.4.0").exists()


async def _installed_python_on(tmp_path, monkeypatch) -> SimpleNamespace:
    data = _runtime_zip()
    monkeypatch.setitem(oi.ASSETS, "python_on", _asset(data, oi.ASSET))
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    async with _server(data) as client:
        await oi.install(paths, cfg, lambda e: None, asyncio.Event(), client=client)
    return paths


async def test_variant_change_refused_while_the_runtime_runs(tmp_path, monkeypatch):
    paths = await _installed_python_on(tmp_path, monkeypatch)
    final = oi.install_dir(paths.runtime_dir)
    seen: list[Path] = []
    monkeypatch.setattr(oi, "running_from", lambda folder: seen.append(folder) or [4242])
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_off")
    events: list[dict] = []

    def handler(request):  # pragma: no cover - must refuse before downloading
        raise AssertionError("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.install(paths, cfg, events.append, asyncio.Event(), client=client)
    assert err.value.code == "in_use" and "Unload the model" in err.value.message
    assert events[-1] == {"status": "error", "error": oi.IN_USE_MESSAGE, "code": "in_use"}
    assert seen == [final]
    assert oi.detect_variant(final / "ovms") == "python_on"  # untouched


async def test_rename_access_denied_is_reported_as_in_use(tmp_path, monkeypatch):
    paths = await _installed_python_on(tmp_path, monkeypatch)
    final = oi.install_dir(paths.runtime_dir)
    off = _zip_bytes({"ovms/ovms.exe": b"MZ off"})
    monkeypatch.setitem(oi.ASSETS, "python_off", _asset(off, "ovms_off.zip"))
    monkeypatch.setattr(oi, "running_from", lambda folder: [])  # started after the check
    real_replace = os.replace

    def replace(src, dst):
        if Path(src) == final:
            raise PermissionError(13, "Access is denied", str(src))
        real_replace(src, dst)

    monkeypatch.setattr(oi.os, "replace", replace)
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_off")
    events: list[dict] = []
    async with _server(off) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.install(paths, cfg, events.append, asyncio.Event(), client=client)
    assert err.value.code == "in_use"  # not "disk"
    assert events[-1]["code"] == "in_use"
    assert not final.with_name(final.name + ".tmp").exists()
    assert oi.detect_variant(final / "ovms") == "python_on"


def test_running_from_matches_only_ovms_under_the_folder(tmp_path, monkeypatch):
    import psutil

    folder = tmp_path / "runtime" / "ovms-2026.4.0"

    class Proc:
        def __init__(self, pid, name, exe):
            self.pid, self.info, self._exe = pid, {"name": name}, exe

        def exe(self):
            if self._exe is None:
                raise psutil.AccessDenied(self.pid)
            return self._exe

    procs = [
        Proc(1, "ovms.exe", str(folder / "ovms" / "ovms.exe")),
        Proc(2, "OVMS.EXE", str(folder).upper() + "\\ovms\\ovms.exe"),
        Proc(3, "ovms.exe", str(tmp_path / "elsewhere" / "ovms.exe")),
        Proc(4, "ovms.exe", str(folder) + "-old\\ovms.exe"),  # a sibling, not inside
        Proc(5, "python.exe", str(folder / "ovms" / "python" / "python.exe")),
        Proc(6, "ovms.exe", None),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    # Process 2 differs only in case and separators: the same folder on Windows only.
    assert oi.running_from(folder) == ([1, 2] if os.name == "nt" else [1])


def test_status_not_installed(tmp_path):
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    status = oi.runtime_status(paths, cfg)
    assert status["installed"] is False
    assert status["exe"] is None and status["version"] is None


def test_detect_variant_and_adopt(tmp_path):
    ovms = tmp_path / "ovms-2026.4.0" / "ovms"
    ovms.mkdir(parents=True)
    assert oi.detect_variant(ovms) is None
    (ovms / "ovms.exe").write_bytes(b"MZ")
    assert oi.detect_variant(ovms) == "python_off"
    (ovms / "python").mkdir()
    (ovms / "python" / "python.exe").write_bytes(b"MZ")
    assert oi.detect_variant(ovms) == "python_on"
    marker = oi.adopt_existing(tmp_path)
    assert marker["variant"] == "python_on" and marker["adopted"]
    assert oi.read_runtime_marker(tmp_path)["sha256"] == oi.ASSET_SHA256


@pytest.mark.skipif(os.name != "nt", reason="VC++ runtime is Windows-only")
def test_vcredist_present_on_this_box():
    # The target laptop has VC++ x64 v14.50 (PLAN §4); CI windows runners have it too.
    assert oi.vcredist_present() is True
    assert (oi.vcredist_version() or "").startswith("v14")


@pytest.mark.skipif(os.name == "nt", reason="non-Windows behaviour")
def test_vcredist_not_applicable_off_windows():
    assert oi.vcredist_present() is None
    assert oi.vcredist_version() is None


def test_member_target_accepts_normal_names(tmp_path):
    target = oi._member_target(tmp_path, "ovms/python/Lib/x.py")
    assert target == (tmp_path / "ovms" / "python" / "Lib" / "x.py").resolve()
    assert Path(target).is_relative_to(tmp_path.resolve())


# ---------------------------------------------------------------------------
# Linux: assets, platform choice, tar extraction, executable layout
# ---------------------------------------------------------------------------

#: name, size, sha256 as published on the v2026.4.0 release (companion .sha256 files).
_LINUX_PINS = {
    ("ubuntu22", "python_on"): (
        "ovms_ubuntu22_2026.4.0_python_on.tar.gz",
        192_806_746,
        "bb14ef8987bf4e3796905b89cba01ac92acd9599dc42e5d8a46fdbfafd660e12",
    ),
    ("ubuntu22", "python_off"): (
        "ovms_ubuntu22_2026.4.0_python_off.tar.gz",
        176_488_078,
        "a5a9e4a4a48dbd30a5d18f9088d2a030758f788d13e9ff14d891e008083756ab",
    ),
    ("ubuntu24", "python_on"): (
        "ovms_ubuntu24_2026.4.0_python_on.tar.gz",
        196_991_877,
        "4a142a7a7409d91299f115562c587b342c74f3b6749b588ac8e40c8f977dcbf8",
    ),
    ("ubuntu24", "python_off"): (
        "ovms_ubuntu24_2026.4.0_python_off.tar.gz",
        180_278_899,
        "cc5caad0e859b249fac586847fa922c9f57e1c910abd2233a57b3cca0172fb91",
    ),
}


@pytest.mark.parametrize(("platform", "variant"), sorted(_LINUX_PINS))
def test_linux_assets_are_pinned(platform, variant):
    name, size, digest = _LINUX_PINS[(platform, variant)]
    asset = oi.asset_for(variant, platform)
    assert (asset.filename, asset.size, asset.sha256) == (name, size, digest)
    assert asset.is_tar and not oi.ASSETS[variant].is_tar
    assert asset.url() == f"{oi.RELEASE_BASE.format(version='2026.4.0')}{name}"
    assert len(asset.sha256) == 64


def _os_release(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "os-release"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="22.04"\n', "ubuntu22"),
        ('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\nID_LIKE=debian\n', "ubuntu24"),
        ('ID=ubuntu\nVERSION_ID="20.04"\n', "ubuntu24"),
        ('ID=ubuntu\nVERSION_ID="26.04"\n', "ubuntu24"),
        ('ID=fedora\nVERSION_ID="40"\n', "ubuntu24"),
        ('ID=debian\nVERSION_ID="22.04"\n', "ubuntu24"),  # only Ubuntu's version counts
        ("garbage without equals\n", "ubuntu24"),
    ],
)
def test_linux_platform_key(tmp_path, text, expected):
    assert oi.linux_platform_key(_os_release(tmp_path, text)) == expected


def test_linux_platform_key_defaults_with_a_log_line_when_unreadable(tmp_path, monkeypatch):
    from structlog.testing import capture_logs

    monkeypatch.setattr(oi, "_logged_platform", set())
    with capture_logs() as logs:
        assert oi.linux_platform_key(tmp_path / "missing") == "ubuntu24"
        assert oi.linux_platform_key(tmp_path / "missing") == "ubuntu24"
    lines = [e for e in logs if e["event"] == "ovms_linux_default_archive"]
    assert len(lines) == 1  # said once, not on every status poll
    assert "ubuntu24" in lines[0]["reason"]


def test_host_platform_follows_sys_platform(monkeypatch):
    monkeypatch.undo()  # the autouse fixture pins "windows"
    monkeypatch.setattr(sys, "platform", "win32")
    assert oi.host_platform() == "windows"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(oi, "linux_platform_key", lambda *a: "ubuntu22")
    assert oi.host_platform() == "ubuntu22"
    assert oi.asset_for("python_on").filename == "ovms_ubuntu22_2026.4.0_python_on.tar.gz"


def test_unknown_variant_on_linux(linux_host):
    with pytest.raises(oi.InstallError) as err:
        oi.asset_for("python_maybe")
    assert err.value.code == "unknown_variant"


def test_ovms_exe_resolves_per_platform(tmp_path, linux_host, monkeypatch):
    runtime = tmp_path / "runtime"
    assert oi.ovms_exe(runtime) == runtime / "ovms-2026.4.0" / "ovms" / "bin" / "ovms"
    monkeypatch.setattr(oi, "host_platform", lambda: "windows")
    assert oi.ovms_exe(runtime) == runtime / "ovms-2026.4.0" / "ovms" / "ovms.exe"


def test_detect_variant_and_adopt_linux(tmp_path, linux_host):
    ovms = tmp_path / "ovms-2026.4.0" / "ovms"
    (ovms / "bin").mkdir(parents=True)
    assert oi.detect_variant(ovms) is None
    (ovms / "bin" / "ovms").write_bytes(b"\x7fELF")
    assert oi.detect_variant(ovms) == "python_off"
    (ovms / "lib" / "python").mkdir(parents=True)
    (ovms / "lib" / "python" / "pyovms.so").write_bytes(b"\x7fELF")
    assert oi.detect_variant(ovms) == "python_on"
    marker = oi.adopt_existing(tmp_path)
    assert marker["variant"] == "python_on"
    assert marker["asset"] == "ovms_ubuntu24_2026.4.0_python_on.tar.gz"
    assert oi.read_runtime_marker(tmp_path)["sha256"] == _LINUX_PINS[("ubuntu24", "python_on")][2]


def test_status_on_linux_has_no_vcredist(tmp_path, linux_host):
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    if os.name == "nt":  # vcredist_present() is the real registry probe there
        pytest.skip("the None contract is for non-Windows hosts")
    status = oi.runtime_status(paths, cfg)
    assert "vcredist" in status and status["vcredist"] is None
    assert status["platform"] == "ubuntu24"


def _tar_bytes(members: list[tuple[str, dict]]) -> bytes:
    """``(name, spec)`` pairs; spec keys: data, mode, type ('file'|'dir'|'sym'|'hard'|'fifo'), link."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, spec in members:
            kind = spec.get("type", "file")
            info = tarfile.TarInfo(name)
            info.mode = spec.get("mode", 0o755 if kind == "dir" else 0o644)
            data = spec.get("data", b"")
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = spec["link"]
                tf.addfile(info)
            elif kind == "hard":
                info.type = tarfile.LNKTYPE
                info.linkname = spec["link"]
                tf.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _linux_runtime_tar(*, python: bool = True) -> bytes:
    members: list[tuple[str, dict]] = [
        ("ovms/", {"type": "dir"}),
        ("ovms/bin/", {"type": "dir"}),
        ("ovms/bin/ovms", {"data": b"\x7fELF", "mode": 0o555}),
        ("ovms/lib/", {"type": "dir"}),
        ("ovms/lib/libovms_shared.so", {"data": b"so", "mode": 0o555}),
        ("ovms/lib/libtbb.so.12.13", {"data": b"tbb"}),
        ("ovms/lib/libtbb.so.12", {"type": "sym", "link": "libtbb.so.12.13"}),
        ("ovms/lib/libopenvino_tokenizers.so", {"data": b"tok"}),
    ]
    if python:
        members += [
            ("ovms/lib/python/", {"type": "dir"}),
            ("ovms/lib/python/pyovms.so", {"data": b"py", "mode": 0o555}),
            ("ovms/lib/python/openvino_tokenizers/", {"type": "dir"}),
            ("ovms/lib/python/openvino_tokenizers/lib/", {"type": "dir"}),
            (
                # The one absolute link in the real archive: the build image's path.
                "ovms/lib/python/openvino_tokenizers/lib/libopenvino_tokenizers.so",
                {"type": "sym", "link": "/ovms/lib/libopenvino_tokenizers.so"},
            ),
        ]
    return _tar_bytes(members)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits and symlinks")
def test_safe_extract_tar_keeps_exec_bits_and_relative_links(tmp_path):
    archive = tmp_path / "ovms.tar.gz"
    archive.write_bytes(_linux_runtime_tar())
    dest = tmp_path / "out"
    written = oi.safe_extract_tar(archive, dest)
    assert written >= 7
    exe = dest / "ovms" / "bin" / "ovms"
    assert exe.read_bytes() == b"\x7fELF"
    assert os.access(exe, os.X_OK) and stat.S_IMODE(exe.stat().st_mode) & 0o111 == 0o111
    assert stat.S_IMODE(exe.stat().st_mode) & 0o200  # owner can replace it on re-install
    assert not os.access(dest / "ovms" / "lib" / "libtbb.so.12.13", os.X_OK)
    assert (dest / "ovms" / "lib" / "libtbb.so.12").is_symlink()
    assert os.readlink(dest / "ovms" / "lib" / "libtbb.so.12") == "libtbb.so.12.13"
    assert (dest / "ovms" / "lib" / "libtbb.so.12").read_bytes() == b"tbb"
    # /ovms/lib/... was remapped to a relative link that works inside the extraction root.
    abs_link = dest / "ovms" / "lib" / "python" / "openvino_tokenizers" / "lib"
    link = abs_link / "libopenvino_tokenizers.so"
    assert not os.readlink(link).startswith("/")
    assert link.read_bytes() == b"tok"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize(
    "name",
    ["../evil.txt", "ovms/../../evil.txt", "/abs/evil.txt", "C:/evil.txt", "..\\evil.txt"],
)
def test_tar_slip_is_refused_before_writing(tmp_path, name):
    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(_tar_bytes([("ovms/ok.txt", {"data": b"ok"}), (name, {"data": b"bad"})]))
    dest = tmp_path / "out"
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract_tar(archive, dest)
    assert err.value.code == "unsafe_zip"
    assert not (dest / "ovms" / "ok.txt").exists()  # nothing written at all
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize(
    "member",
    [
        ("ovms/x", {"type": "sym", "link": "/etc/passwd"}),
        ("ovms/x", {"type": "sym", "link": "/ovmsx/../etc/passwd"}),
        ("ovms/x", {"type": "sym", "link": "/ovms/../etc/passwd"}),
        ("ovms/x", {"type": "sym", "link": "../../outside"}),
        ("ovms/lib/x", {"type": "sym", "link": "../../../outside"}),
        ("ovms/x", {"type": "sym", "link": "a/../../../outside"}),
        ("ovms/x", {"type": "sym", "link": "..\\outside"}),
        ("ovms/x", {"type": "sym", "link": ""}),
        ("ovms/x", {"type": "hard", "link": "ovms/ok.txt"}),
        ("ovms/x", {"type": "fifo"}),
    ],
)
def test_tar_unsafe_members_refused_before_writing(tmp_path, member):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(_tar_bytes([("ovms/ok.txt", {"data": b"ok"}), member]))
    dest = tmp_path / "out"
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract_tar(archive, dest)
    assert err.value.code == "unsafe_zip"
    assert not (dest / "ovms" / "ok.txt").exists()
    assert not (tmp_path / "outside").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_tar_refuses_writing_through_a_symlink(tmp_path):
    archive = tmp_path / "through.tar.gz"
    archive.write_bytes(
        _tar_bytes(
            [
                ("ovms/link", {"type": "sym", "link": "."}),
                ("ovms/link/sub/file.txt", {"data": b"x"}),
            ]
        )
    )
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract_tar(archive, tmp_path / "out")
    assert err.value.code == "unsafe_zip"
    # ... nor may a later member replace the link with a directory/file.
    archive.write_bytes(
        _tar_bytes([("ovms/link", {"type": "sym", "link": "."}), ("ovms/link", {"type": "dir"})])
    )
    with pytest.raises(oi.InstallError):
        oi.safe_extract_tar(archive, tmp_path / "out2")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_tar_symlink_that_escapes_through_another_link_is_removed(tmp_path):
    # Lexically fine (x/y/up -> .., then up/../..), physically it leaves the tree.
    archive = tmp_path / "chain.tar.gz"
    archive.write_bytes(
        _tar_bytes(
            [
                ("ovms/x/y/up", {"type": "sym", "link": ".."}),
                ("ovms/x/y/z", {"type": "sym", "link": "up/../../../.."}),
            ]
        )
    )
    dest = tmp_path / "out" / "deep"
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract_tar(archive, dest)
    assert err.value.code == "unsafe_zip"


def test_tar_corrupt_and_cancel(tmp_path):
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a tarball")
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract_tar(junk, tmp_path / "out")
    assert err.value.code == "bad_zip"

    archive = tmp_path / "ok.tar.gz"
    archive.write_bytes(_linux_runtime_tar(python=False))
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract_tar(archive, tmp_path / "out2", cancel=cancel)
    assert err.value.code == "cancelled"


def test_extract_archive_dispatches_on_name(tmp_path):
    (tmp_path / "a.tar.gz").write_bytes(_linux_runtime_tar(python=False))
    (tmp_path / "b.zip").write_bytes(_zip_bytes({"ovms/ovms.exe": b"MZ"}))
    oi.extract_archive(tmp_path / "a.tar.gz", tmp_path / "ta")
    oi.extract_archive(tmp_path / "b.zip", tmp_path / "zb")
    assert (tmp_path / "ta" / "ovms" / "bin" / "ovms").is_file()
    assert (tmp_path / "zb" / "ovms" / "ovms.exe").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_install_end_to_end_linux(tmp_path, monkeypatch, linux_host):
    data = _linux_runtime_tar()
    asset = oi.OvmsAsset(
        "python_on", "ovms_ubuntu24_test.tar.gz", len(data), hashlib.sha256(data).hexdigest()
    )
    monkeypatch.setitem(oi.UBUNTU24_ASSETS, "python_on", asset)
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    events: list[dict] = []
    async with _server(data) as client:
        exe = await oi.install(paths, cfg, events.append, asyncio.Event(), client=client)
    assert exe == tmp_path / "runtime" / "ovms-2026.4.0" / "ovms" / "bin" / "ovms"
    assert os.access(exe, os.X_OK)
    assert events[-1]["status"] == "done"
    status = oi.runtime_status(paths, cfg)
    assert status["installed"] and status["variant"] == "python_on" and status["variant_matches"]
    assert status["exe"] == str(exe)
    assert oi.read_runtime_marker(paths.runtime_dir)["asset"] == "ovms_ubuntu24_test.tar.gz"

    def handler(request):  # pragma: no cover - second install must not download
        raise AssertionError("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        again = await oi.install(paths, cfg, lambda e: None, asyncio.Event(), client=client)
    assert again == exe


async def test_install_linux_archive_without_binary_is_refused(tmp_path, monkeypatch, linux_host):
    data = _tar_bytes([("ovms/readme.txt", {"data": b"hi"})])
    asset = oi.OvmsAsset("python_on", "x.tar.gz", len(data), hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(oi.UBUNTU24_ASSETS, "python_on", asset)
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.install(paths, cfg, lambda e: None, asyncio.Event(), client=client)
    assert err.value.code == "bad_zip"
    assert "ovms/bin/ovms" in err.value.message
    assert not (tmp_path / "runtime" / "ovms-2026.4.0").exists()


def test_running_from_accepts_ovms_without_exe(tmp_path, monkeypatch, linux_host):
    import psutil

    folder = tmp_path / "runtime" / "ovms-2026.4.0"

    class Proc:
        def __init__(self, pid, name, exe):
            self.pid, self.info, self._exe = pid, {"name": name}, exe

        def exe(self):
            return self._exe

    procs = [
        Proc(1, "ovms", str(folder / "ovms" / "bin" / "ovms")),
        Proc(2, "ovms.exe", str(folder / "ovms" / "ovms.exe")),
        Proc(3, "OVMS", str(folder / "ovms" / "bin" / "ovms")),
        Proc(4, "ovms", str(tmp_path / "elsewhere" / "bin" / "ovms")),
        Proc(5, "ovmsx", str(folder / "ovms" / "bin" / "ovms")),
        Proc(6, "python3", str(folder / "ovms" / "bin" / "ovms")),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    assert oi.running_from(folder) == [1, 2, 3]
