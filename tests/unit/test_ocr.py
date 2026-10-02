"""chatforge.ocr: Windows' OCR engine through PowerShell (faked, plus one real run on
Windows)."""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from chatforge import ocr


class FakeRun:
    """Stands in for ``subprocess.run``: records the call, answers with ``result``."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, raises=None) -> None:
        self.result = SimpleNamespace(stdout=stdout, stderr=b"", returncode=returncode)
        self.raises = raises
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kw):
        self.calls.append((list(args), kw))
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def picture(tmp_path: Path) -> Path:
    path = tmp_path / "shot.png"
    Image.new("RGB", (10, 10), "white").save(path)
    return path


def test_the_command_runs_the_script_hidden_with_the_path_in_the_environment(picture) -> None:
    run = FakeRun(b"\xef\xbb\xbfHello  world\r\n\r\n  second line \r\n")
    text = ocr.recognize(picture, runner=run, exe="powershell.exe", timeout=7)
    assert text == "Hello world\nsecond line"
    (args, kw) = run.calls[0]
    assert args[:6] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
    ]
    assert base64.b64decode(args[6]).decode("utf-16-le") == ocr.SCRIPT
    assert str(picture) not in " ".join(args)  # never on the command line
    assert kw["env"][ocr.PATH_ENV] == str(picture.resolve())
    assert kw["timeout"] == 7 and kw["capture_output"] is True and kw["check"] is False
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize(
    "run",
    [
        FakeRun(raises=subprocess.TimeoutExpired(["powershell"], 1)),
        FakeRun(raises=OSError("no such program")),
        FakeRun(b"text", returncode=ocr.EXIT_NO_LANGUAGE),
        FakeRun(b"Exception calling ...", returncode=1),
    ],
)
def test_any_failure_is_no_text(picture, run) -> None:
    assert ocr.recognize(picture, runner=run, exe="powershell.exe") == ""


def test_no_powershell_or_no_file_is_no_text(tmp_path, monkeypatch) -> None:
    run = FakeRun(b"x")
    assert ocr.recognize(tmp_path / "missing.png", runner=run, exe="powershell.exe") == ""
    monkeypatch.setattr(ocr, "powershell", lambda: None)
    assert ocr.recognize(tmp_path / "missing.png", runner=run) == ""
    assert ocr.available() is False
    assert run.calls == []


def test_long_text_is_capped(picture) -> None:
    run = FakeRun(("word " * 5000).encode())
    assert len(ocr.recognize(picture, runner=run, exe="powershell.exe", max_chars=100)) == 100


@pytest.mark.skipif(os.name != "nt", reason="Windows OCR is a Windows feature")
def test_windows_reads_drawn_text(tmp_path) -> None:
    path = tmp_path / "words.png"
    im = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(im).text(
        (20, 60), "Hello OCR world 2026", fill="black", font=ImageFont.load_default(size=48)
    )
    im.save(path)
    text = ocr.recognize(path)
    if not text:
        pytest.skip("Windows OCR is not available here (no OCR language installed)")
    assert "Hello" in text and "2026" in text
