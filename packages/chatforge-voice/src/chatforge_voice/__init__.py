"""chatforge-voice: an optional CPU voice add-on for ChatForge (push-to-talk, M1).

ChatForge finds it through the ``chatforge.extensions`` entry point and calls :func:`setup`.
This module imports nothing heavy and nothing from ``forge_voice_core``: when the engine
package (or the audio library) is missing, ``setup`` raises one clear sentence, which
ChatForge shows in Settings in place of the add-on instead of crashing. Everything else lives
in submodules that are only imported after that check.
"""

from __future__ import annotations

import importlib.util
from typing import Any

__version__ = "0.1.0"

#: The ``ExtensionAPI`` generation this add-on was written against (``chatforge.extensions.API_VERSION``).
API_VERSION = 1

#: What Settings shows when the engines are not installed.
CORE_MISSING = (
    "Voice needs the forge-voice-core engine package, which is not installed. Reinstall the add-on "
    "with `uv pip install ./packages/chatforge-voice` (it pulls forge-voice-core in), then restart "
    "ChatForge."
)


def core_problem() -> str:
    """"" when the engine package can be imported, else one sentence saying what to do."""
    if importlib.util.find_spec("forge_voice_core") is None:
        return CORE_MISSING
    return ""


def setup(api: Any) -> None:
    """The ``chatforge.extensions`` entry point. ``api`` is a ``chatforge.extensions.ExtensionAPI``."""
    problem = core_problem()
    if problem:
        raise RuntimeError(problem)
    from .runtime import VoiceAddon

    VoiceAddon(api).register()


# ChatForge refuses an add-on that declares another API generation (it reads this off the
# entry-point object, which is ``setup``).
setup.API_VERSION = API_VERSION  # type: ignore[attr-defined]
