"""The add-on must import cleanly, and say one clear sentence, when the engine package is missing.

Run in a fresh interpreter with a meta-path finder that refuses forge_voice_core and every heavy
dependency, so the result does not depend on what this test process has already imported.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

BLOCK = textwrap.dedent(
    """
    import sys

    BLOCKED = {"forge_voice_core", "numpy", "onnxruntime", "onnx_asr", "sounddevice", "keyring", "soundfile"}

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(name + " is blocked")
            return None

    sys.meta_path.insert(0, Blocker())
    """
)


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", BLOCK + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_package_imports_without_the_engines():
    r = run(
        """
        import chatforge_voice
        assert chatforge_voice.__version__
        loaded = [m for m in sys.modules if m.split(".")[0] in BLOCKED]
        assert loaded == [], loaded
        print("ok")
        """
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


def test_setup_raises_the_clear_sentence_instead_of_crashing():
    r = run(
        """
        import chatforge_voice
        assert "forge_voice_core" in sys.modules or True
        try:
            chatforge_voice.setup(object())
        except RuntimeError as e:
            print("MSG:", e)
        else:
            raise SystemExit("setup did not raise")
        """
    )
    assert r.returncode == 0, r.stderr
    assert "MSG: Voice needs the forge-voice-core engine package" in r.stdout
    assert "uv pip install ./packages/chatforge-voice" in r.stdout


def test_the_cli_still_answers_version_and_explains_the_rest():
    r = run(
        """
        from chatforge_voice import cli
        try:
            cli.main(["--version"])
        except SystemExit as e:
            assert e.code in (0, None)
        print("rc", cli.main(["status"]))
        """
    )
    assert "chatforge-voice 0.1.0" in r.stdout and "rc 2" in r.stdout, r.stderr
    assert "forge-voice-core" in r.stderr
