"""Text in a picture, read by Windows' own OCR engine (``Windows.Media.Ocr``).

For a model that cannot see pictures, the text in an attached picture (a screenshot, a
scanned page, a sign) is the next best thing. Windows 10 and 11 ship an OCR engine for the
languages installed with the user's profile; Windows PowerShell 5.1 can call it through
WinRT, so no extra Python package is needed.

:func:`recognize` runs one hidden PowerShell process for one picture, waits at most
``timeout`` seconds (then the process is killed), and returns the recognised lines, or
``""`` when anything goes wrong: not Windows, no OCR language installed, a picture the
engine cannot decode, a timeout. It blocks, so the chat engine calls it in a worker thread.
The picture's path reaches the script through an environment variable, never the command
line, and the script itself is passed with ``-EncodedCommand``.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chatforge.logging_setup import get_logger

log = get_logger(__name__)

#: How long one recognition may take (PowerShell start-up included), in seconds.
TIMEOUT_S = 20.0
#: The most recognised text kept for one picture.
MAX_CHARS = 8000
PATH_ENV = "CHATFORGE_OCR_IMAGE"
#: The exit code the script uses when no OCR language is installed.
EXIT_NO_LANGUAGE = 3

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $task.Wait(-1)
    $task.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { exit 3 }
$path = $env:CHATFORGE_OCR_IMAGE
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    foreach ($line in $result.Lines) { [Console]::Out.WriteLine($line.Text) }
} finally { $stream.Dispose() }
"""

Runner = Callable[..., Any]


def powershell() -> str | None:
    """Windows PowerShell 5.1 (``powershell.exe``), or ``None`` off Windows."""
    if os.name != "nt":
        return None
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exe) if exe.is_file() else shutil.which("powershell")


def available() -> bool:
    """True when :func:`recognize` can run here (Windows with PowerShell)."""
    return powershell() is not None


def command(exe: str) -> list[str]:
    """The PowerShell command line that runs :data:`SCRIPT`."""
    encoded = base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii")
    return [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def tidy(text: str, max_chars: int = MAX_CHARS) -> str:
    """Recognised lines without blank ones or stray whitespace, at most ``max_chars``."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)[: max(0, int(max_chars))]


def recognize(
    path: str | Path,
    *,
    timeout: float = TIMEOUT_S,
    runner: Runner = subprocess.run,
    exe: str | None = None,
    max_chars: int = MAX_CHARS,
) -> str:
    """The text Windows recognises in the picture at ``path`` ("" when there is none or
    it cannot be read). Blocks for up to ``timeout`` seconds; never raises."""
    exe = exe or powershell()
    target = Path(path)
    if exe is None or not target.is_file():
        return ""
    env = {**os.environ, PATH_ENV: str(target.resolve())}
    try:
        result = runner(
            command(exe),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        log.warning("ocr_timeout", timeout_s=timeout)
        return ""
    except (OSError, ValueError) as exc:
        log.warning("ocr_failed", error=type(exc).__name__)
        return ""
    code = getattr(result, "returncode", 1)
    if code != 0:
        reason = "no_language" if code == EXIT_NO_LANGUAGE else f"exit_{code}"
        log.info("ocr_unavailable", reason=reason)
        return ""
    raw = getattr(result, "stdout", b"") or b""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    found = tidy(text.lstrip("\ufeff"), max_chars)
    log.info("ocr_done", chars=len(found))
    return found


__all__ = ["MAX_CHARS", "SCRIPT", "TIMEOUT_S", "available", "command", "recognize", "tidy"]
