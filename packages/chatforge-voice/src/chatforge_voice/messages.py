"""Words shown to people. Stdlib only; no engine imports."""

from __future__ import annotations

NOT_IN_THIS_RELEASE_HANDS_FREE = (
    "Hands-free listening is not in this release (it is planned for the next one). "
    "Push-to-talk is used instead: hold the key while you speak."
)
NOT_IN_THIS_RELEASE_REMOTE = (
    "Using a remote speech server (SpeechForge or an OpenAI-compatible server) is not in this "
    "release. Speech recognition and the spoken reply run on this computer's CPU."
)

#: The Parakeet attribution CC-BY-4.0 asks for. Shown in the Voice panel and the README.
ATTRIBUTION = (
    "Speech recognition: NVIDIA Parakeet TDT 0.6B v2 (ONNX int8 export by istupakov), "
    "licensed under CC-BY-4.0, run through onnx-asr (MIT)."
)

#: Every model the download fetches, with its licence.
LICENCES = (
    {"name": "Silero VAD", "licence": "MIT", "by": "snakers4/silero-vad"},
    {"name": "Smart Turn v3.2", "licence": "BSD-2-Clause", "by": "pipecat-ai/smart-turn-v3"},
    {
        "name": "Parakeet TDT 0.6B v2 (int8)",
        "licence": "CC-BY-4.0",
        "by": "NVIDIA; ONNX export by istupakov",
        "note": "Attribution required (see below).",
    },
    {"name": "Chatterbox-Turbo (ONNX q4)", "licence": "MIT", "by": "Resemble AI"},
)

DOWNLOAD_SIZE_MB = 1400
RAM_NEEDED_GB = 3
