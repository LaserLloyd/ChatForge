"""The Windows install scripts: they parse, stay plain ASCII (Windows PowerShell 5.1 reads a
BOM-less file as ANSI), pin what they download, and the release zip has the layout the batch
file expects. The scripts' real behaviour (install, upgrade, uninstall) is exercised by hand and
by the CI "windows-installer" job, which needs a network."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "deploy" / "windows"
PS1 = sorted(WIN.glob("*.ps1"))
BATS = [ROOT / "Install ChatForge.bat", ROOT / "Uninstall ChatForge.bat"]


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def test_the_expected_files_exist() -> None:
    names = {p.name for p in WIN.iterdir()}
    assert {
        "Install-ChatForge.ps1",
        "Uninstall-ChatForge.ps1",
        "build-release-zip.ps1",
        "chatforge-installer.iss",
        "zip-readme.txt",
        "README.md",
    } <= names
    for bat in BATS:
        assert bat.is_file()


@pytest.mark.parametrize("path", [*PS1, *BATS, WIN / "zip-readme.txt"], ids=lambda p: p.name)
def test_scripts_are_plain_ascii(path: Path) -> None:
    data = path.read_bytes()
    bad = [i for i, b in enumerate(data) if b > 127]
    assert not bad, f"{path.name}: non-ASCII byte at {bad[:3]}"


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_batch_files_have_windows_line_endings(path: Path) -> None:
    data = path.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), path.name


@pytest.mark.parametrize("path", PS1, ids=lambda p: p.name)
def test_powershell_scripts_parse(path: Path) -> None:
    exe = _powershell()
    if exe is None:
        pytest.skip("no PowerShell on this machine")
    script = (
        "$e=$null;$t=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$t,[ref]$e);"
        "if($e.Count){$e|ForEach-Object{$_.Message+' line '+$_.Extent.StartLineNumber};exit 1}"
    )
    out = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_downloads_are_pinned_and_checked() -> None:
    text = (WIN / "Install-ChatForge.ps1").read_text(encoding="utf-8")
    version = re.search(r"\$UvVersion = '([\d.]+)'", text).group(1)
    sha = re.search(r"\$UvSha256\s*=\s*'([0-9a-f]{64})'", text).group(1)
    assert "/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" in text
    assert re.fullmatch(r"\d+\.\d+\.\d+", version) and len(sha) == 64
    assert "Test-Sha256 $zip $UvSha256" in text  # checked before it is extracted
    assert "Get-AuthenticodeSignature" in text  # the python.org fallback is signature-checked


def test_the_installer_asks_instead_of_enabling_silently() -> None:
    text = (WIN / "Install-ChatForge.ps1").read_text(encoding="utf-8")
    assert "Read-Host" in text and "[Y/n]" in text
    # No console to ask in and no -Yes: nothing is registered.
    assert "nothing is registered" in text
    assert "-NoAutostart" in text


def test_the_uninstaller_keeps_data_unless_asked() -> None:
    text = (WIN / "Uninstall-ChatForge.ps1").read_text(encoding="utf-8")
    assert "[switch]$Purge" in text
    assert text.count("Remove-Item $dataDir") == 1 and "if ($Purge)" in text
    assert "Assert-SafeToDelete" in text
    assert "autostart disable --no-record" in text  # must not create a data folder


def test_the_inno_file_is_documented_as_dormant() -> None:
    iss = (WIN / "chatforge-installer.iss").read_text(encoding="utf-8")
    assert "DORMANT" in iss and "NOT been compiled" in iss
    assert "PrivilegesRequired=lowest" in iss
    assert re.search(r"^;SignTool=", iss, re.M), "the SignTool line must stay commented out"
    readme = (WIN / "README.md").read_text(encoding="utf-8")
    assert "signing certificate" in readme


@pytest.mark.skipif(sys.platform != "win32" or _powershell() is None, reason="Windows only")
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_release_zip_has_the_layout_the_batch_file_expects(tmp_path: Path) -> None:
    out = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WIN / "build-release-zip.ps1"),
            "-OutDir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    zips = list(tmp_path.glob("ChatForge-*-windows.zip"))
    assert len(zips) == 1
    assert (tmp_path / (zips[0].name + ".sha256")).is_file()
    with zipfile.ZipFile(zips[0]) as z:
        names = z.namelist()
        top = names[0].split("/")[0]
        for need in (
            "Install ChatForge.bat",
            "Uninstall ChatForge.bat",
            "README.txt",
            "deploy/windows/Install-ChatForge.ps1",
            "deploy/windows/Uninstall-ChatForge.ps1",
            "app/pyproject.toml",
            "app/uv.lock",
            "app/src/chatforge/__init__.py",
            "app/Install ChatForge.bat",
        ):
            assert f"{top}/{need}" in names, need
        assert not any(
            "/.venv/" in n or "/.scratch-data/" in n or "/node_modules/" in n for n in names
        )
        bat = z.read(f"{top}/Install ChatForge.bat")
        assert b"\r\n" in bat
