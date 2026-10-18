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


# --------------------------------------------------------------------------- #
# Behaviour of the scripts' helpers and of the uninstaller, in a scratch folder
# --------------------------------------------------------------------------- #

windows_ps = pytest.mark.skipif(
    sys.platform != "win32" or _powershell() is None, reason="Windows PowerShell only"
)


def _run_ps_functions(script: Path, names: list[str], body: str) -> str:
    """Define the named functions from ``script`` (parsed, not run) and run ``body``."""
    wanted = ",".join(f"'{n}'" for n in names)
    command = (
        "$ErrorActionPreference='Stop';$t=$null;$e=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$t,[ref]$e);"
        f"foreach($n in @({wanted})){{"
        "$f=$ast.Find({param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst]"
        " -and $x.Name -eq $n},$true);"
        "if(-not $f){throw ('no function '+$n)};"
        ". ([scriptblock]::Create($f.Extent.Text))};"
        "$OwnItems=@('app','tools','logs','uninstall','ChatForge.ico','install.json');" + body
    )
    out = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout.strip()


_FAKE_PROCESSES = r"""
$t0=[datetime]'2026-01-01'
function P($id,$parent,$exe,$min){[pscustomobject]@{ProcessId=$id;ParentProcessId=$parent;ExecutablePath=$exe;CreationDate=$t0.AddMinutes($min)}}
$all=@(
  (P 10 1 'C:\X\ChatForge\app\.venv\Scripts\pythonw.exe' 10),
  (P 11 10 'C:\Py\python.exe' 11),
  (P 12 11 'C:\Data\ChatForge\runtime\ovms.exe' 12),
  (P 20 10 'C:\Windows\explorer.exe' 0),
  (P 30 1 'C:\X\ChatForge2\app.exe' 10),
  (P 31 1 'C:\X\ChatForge Beta\pythonw.exe' 10),
  (P 40 1 $null 0)
)
"""


@windows_ps
@pytest.mark.parametrize("script", ["Install-ChatForge.ps1", "Uninstall-ChatForge.ps1"])
def test_only_the_install_folders_processes_are_stopped(script: str) -> None:
    """Pid 20 claims parent 10 but is older than it (a recycled id); 30 and 31 live in sibling
    folders whose names merely start with the install folder's."""
    body = _FAKE_PROCESSES + (
        "(Select-InstallProcesses 'C:\\X\\ChatForge' $all 999 | ForEach-Object { $_.ProcessId })"
        " -join ','"
    )
    assert _run_ps_functions(WIN / script, ["Select-InstallProcesses"], body) == "10,11,12"


@windows_ps
def test_purge_finds_every_credential_chatforge_and_the_voice_addon_saved() -> None:
    listing = (
        "Currently stored credentials:\n\n"
        "    Target: LegacyGeneric:target=ChatForge\n    Type: Generic\n    User: studioforge\n\n"
        "    Target: LegacyGeneric:target=minimax@ChatForge\n"
        "    Target: LegacyGeneric:target=ChatForge Voice\n"
        "    Target: LegacyGeneric:target=rig_token@ChatForge Voice\n"
        "    Ziel: LegacyGeneric:target=openai@ChatForge\n"
        "    Target: LegacyGeneric:target=MyChatForge\n"
        "    Target: LegacyGeneric:target=x@ChatForgeOther\n"
        "    Target: Domain:target=ChatForge-share\n"
    )
    body = "$l=@'\n" + listing + "\n'@\n(Select-ChatForgeCredentials $l) -join '|'"
    got = _run_ps_functions(WIN / "Uninstall-ChatForge.ps1", ["Select-ChatForgeCredentials"], body)
    assert got.split("|") == [
        "LegacyGeneric:target=ChatForge",  # the most recently saved key (keyring's bare target)
        "LegacyGeneric:target=minimax@ChatForge",
        "LegacyGeneric:target=ChatForge Voice",
        "LegacyGeneric:target=rig_token@ChatForge Voice",
        "LegacyGeneric:target=openai@ChatForge",  # "Target:" is translated on other Windows
    ]


def _uninstall(tmp_path: Path, inst: Path) -> subprocess.CompletedProcess:
    """The real uninstaller, pointed at scratch folders only (no -Purge, so it never runs
    cmdkey; with no app Python it only reads the logon task, and removes it only when it runs
    the scratch data folder's script, which no task does)."""
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WIN / "Uninstall-ChatForge.ps1"),
            "-InstallDir",
            str(inst),
            "-ShortcutDir",
            str(tmp_path / "shortcuts"),
            "-DataHome",
            str(tmp_path / "data"),
            "-UninstallKeyName",
            f"ChatForgeTest-{tmp_path.name}",
            "-Yes",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _fake_install(inst: Path) -> None:
    (inst / "app" / "src").mkdir(parents=True)
    (inst / "app" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (inst / "tools").mkdir()
    (inst / "tools" / "uv.exe").write_bytes(b"MZ")
    (inst / "install.json").write_text("{}", encoding="utf-8")


@windows_ps
def test_uninstall_never_deletes_files_it_did_not_install(tmp_path: Path) -> None:
    """Installed with --install-dir pointing at a folder that already held things (D:\\Apps):
    the uninstaller used to rmdir /s the whole folder."""
    inst = tmp_path / "Apps"
    _fake_install(inst)
    (inst / "my-other-app").mkdir()
    (inst / "my-other-app" / "data.db").write_bytes(b"precious")
    (inst / "notes.txt").write_text("keep me", encoding="utf-8")
    out = _uninstall(tmp_path, inst)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (inst / "my-other-app" / "data.db").read_bytes() == b"precious"
    assert (inst / "notes.txt").is_file()
    assert not (inst / "app").exists() and not (inst / "tools").exists()
    assert not (inst / "install.json").exists()
    assert "Kept the folder" in out.stdout


@windows_ps
def test_uninstall_removes_a_folder_that_only_holds_chatforge(tmp_path: Path) -> None:
    inst = tmp_path / "ChatForge"
    _fake_install(inst)
    out = _uninstall(tmp_path, inst)
    assert out.returncode == 0, out.stdout + out.stderr
    assert not inst.exists()


@windows_ps
def test_the_installer_refuses_a_folder_that_holds_other_files(tmp_path: Path) -> None:
    """Refused before anything is created or downloaded (the uninstaller deletes the install
    folder, so it must be ChatForge's own). ``-SourceDir`` is an empty folder, so even without
    the guard the run would stop at step 4 instead of installing."""
    inst = tmp_path / "Apps"
    (inst / "my-other-app").mkdir(parents=True)
    empty = tmp_path / "empty-source"
    empty.mkdir()
    out = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WIN / "Install-ChatForge.ps1"),
            "-InstallDir",
            str(inst),
            "-SourceDir",
            str(empty),
            "-DataHome",
            str(tmp_path / "data"),
            "-ShortcutDir",
            str(tmp_path / "shortcuts"),
            "-UninstallKeyName",
            f"ChatForgeTest-{tmp_path.name}",
            "-NoLaunch",
            "-NoShortcuts",
            "-NoAutostart",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert out.returncode == 1, out.stdout + out.stderr
    assert "not ChatForge's" in out.stdout
    assert sorted(p.name for p in inst.iterdir()) == ["my-other-app"]  # no logs folder either


def test_paths_never_go_into_code_strings() -> None:
    """A quote in a profile path (C:\\Users\\O'Brien) broke the PowerShell download command and
    the Python icon one-liner; both now take the path as data."""
    bat = (ROOT / "Install ChatForge.bat").read_text(encoding="ascii")
    assert "'%TEMP%" not in bat and "$env:TEMP" in bat
    ps1 = (WIN / "Install-ChatForge.ps1").read_text(encoding="ascii")
    assert "r'$icon'" not in ps1 and "write_app_ico(sys.argv[1])" in ps1


def test_the_release_zip_takes_tracked_files_only() -> None:
    text = (WIN / "build-release-zip.ps1").read_text(encoding="ascii")
    assert "ls-files --cached" in text and "--others" not in text
