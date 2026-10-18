# chatforge-voice

An **optional** voice add-on for [ChatForge](../../README.md): hold a key, speak, let go, and ChatForge
answers out loud. Speech recognition (NVIDIA Parakeet) and the spoken reply (Chatterbox-Turbo) both run
on this computer's **CPU**. No audio and no text leave the machine, and nothing is installed unless
you ask for it.

This is release 0.1 (milestone M1): **push-to-talk only**. Hands-free listening and a remote speech
server are not in this release (see Limits).

## Install

Without this package ChatForge is unchanged. From a ChatForge checkout (use the same Python
environment that runs ChatForge):

```text
uv pip install ./packages/chatforge-voice
```

This also installs the engine package `forge-voice-core` (from
`git+https://github.com/LaserLloyd/DisPatch_Voice@main#subdirectory=packages/forge-voice-core`),
`sounddevice` (bundles PortAudio on Windows) and `keyring`. Python 3.12 or 3.13. Roughly 100 MB of
wheels (numpy, onnxruntime, onnx-asr, tokenizers and friends); no GPU, no torch.

Restart ChatForge. If the engines are missing or broken, **Settings shows one sentence saying what to
install** instead of the Voice card, and ChatForge itself starts normally.

## Use it

1. **Settings > General > Voice (CPU)**: switch it on.
2. **Download the voice models** (about 1.4 GB, once). The download, its progress and Cancel live in the
   *Voice panel*: with ChatForge running, open a terminal and run

   ```text
   chatforge-voice panel
   ```

   which opens the panel in your browser (it listens on `127.0.0.1` only and the address carries a
   one-time token). Press **Download voice models**, read the size and licences, confirm. A cancelled or
   failed download keeps finished files and resumes at the next one. Without ChatForge running you can
   also use `chatforge-voice models download`.
3. **Press to talk.** Hold **Ctrl+Alt+Space** (Settings > Voice (CPU) > Push-to-talk key), speak, release.
   Your words are transcribed, sent to ChatForge as an ordinary message (it appears in the chat), and the
   reply is read aloud sentence by sentence as it streams in. The mic button on the Voice panel does the same
   (hold it). **Stop** on the panel silences the voice at once and stops the answer being written.
   Pressing the key while ChatForge is talking also stops it.

The first question after starting ChatForge is slow (the models load: several seconds). They are freed
again after 10 idle minutes (setting *Unload models after*), or at once when you switch Voice off.

## What it needs

| | |
|---|---|
| Disk | about 1.4 GB for the models (Parakeet 0.67 GB, Chatterbox 0.72 GB, small VAD files) |
| Memory | budget **3 GB** free while voice is loaded (Parakeet ~1 GB, Chatterbox ~1.5-2 GB; an estimate from the engine package, not measured on ChatForge's target laptop) |
| CPU | an 8-core laptop is the target. Chatterbox runs at roughly real time (RTF 0.7-0.9 on a 10-core laptop; **no number exists for an 8-core Lunar Lake**), so long sentences may stutter while the chat model is also busy. Thread counts are lowered from the engine defaults (`threads`, `tts_threads`). |
| Latency | from the engine's own measurements: first audio about 1.5-2 s after the first full sentence, transcription about 0.3 s after you let go. End to end "stop talking to first spoken word" is planned at 3-4 s with the NPU chat model (**an estimate to be measured**). |
| OS | Windows 10/11 for the global hold-key; the panel button works anywhere PortAudio does. Linux/macOS push-to-talk by key is not in this release. |
| Programs | none for push-to-talk. `ffmpeg` and `libopus` appear in the panel for completeness; they matter only for voice cloning and a slow-link wire, neither in this release. |

## Settings

Everything on the Settings card is stored by ChatForge under `[extensions.voice]` in `config.toml`.
Each setting can also be set in the environment (`CHATFORGE_VOICE_*`, which wins over the file).

| Setting | Environment | Default | |
|---|---|---|---|
| `enabled` | `CHATFORGE_VOICE_ENABLED` | off | the "Voice (CPU)" toggle |
| `hotkey` | `CHATFORGE_VOICE_HOTKEY` | `Ctrl+Alt+Space` | hold to talk; modifiers plus one key. The key still reaches the focused app (it is only watched), so pick one that does nothing there. |
| `speak_replies` | `CHATFORGE_VOICE_SPEAK_REPLIES` | `voice_only` | `voice_only` reads out answers to what you said, `always` every answer, `off` never |
| `speed` | `CHATFORGE_VOICE_SPEED` | 1.05 | speaking speed 0.8-1.3 |
| `max_record_s` | `CHATFORGE_VOICE_MAX_RECORD_S` | 30 | a recording ends by itself after this long |
| `input_device` / `output_device` | `CHATFORGE_VOICE_INPUT_DEVICE` / `..._OUTPUT_DEVICE` | system default | part of a device name; `chatforge-voice devices` lists them |
| `idle_unload_minutes` | `CHATFORGE_VOICE_IDLE_UNLOAD_MINUTES` | 10 | 0 keeps the models loaded |
| `threads`, `tts_threads` | `CHATFORGE_VOICE_THREADS`, `..._TTS_THREADS` | half the cores (max 6 / 8) | onnxruntime threads; need a restart |
| `mode`, `backend` | `CHATFORGE_VOICE_MODE`, `..._BACKEND` | `push_to_talk`, `cpu` | the other values are understood but **not in this release** |

Only the first seven are on the Settings card; the rest are for `config.toml` or the environment.

## Privacy and licences

* Audio is recorded only while you hold the key (or the panel button), kept in memory, transcribed
  locally and dropped. Nothing is written to disk or sent over the network, except the one-off model
  download from Hugging Face. The transcript becomes a normal ChatForge message, handled like typed text.
* The optional GPU-rig token (not used in this release) would live in Windows Credential Manager under
  the service name `ChatForge Voice`, never in a file.
* Models: Silero VAD (MIT), Smart Turn v3.2 (BSD-2-Clause), Chatterbox-Turbo (MIT),
  **NVIDIA Parakeet TDT 0.6B v2, licensed under CC-BY-4.0** (ONNX int8 export by istupakov, run through
  onnx-asr, MIT). The Voice panel shows the attribution before you download.

## Limits (this release)

* Push-to-talk only. **Hands-free listening is not in this release**: setting `mode = hands_free`
  shows a notice and push-to-talk is used.
* **No remote speech server** (SpeechForge or an OpenAI-compatible server): `backend`, `stt_provider`,
  `hq_url` and the rig settings are ignored with a notice; the engines are always local.
* No echo cancellation: use headphones if you keep the microphone key held while it is talking (pressing
  the key stops the voice anyway).
* The popup has no mic button yet; the Voice panel (a page in your browser) has one. ChatForge's add-on
  seams are settings, hooks and a chat port today; a native mic button and download card need seams
  that do not exist yet (see `INTEGRATION.md`).
* The reply is spoken with ChatForge's normal system prompt; replies are cleaned for speaking (markdown,
  code, links, emoji dropped; numbers read as words) but not shortened.
* Text already spoken cannot be unsaid: if ChatForge resets a reply mid-stream (`chat.reset`), the
  spoken part stays spoken.

## Commands

```text
chatforge-voice status                 # are the models installed?
chatforge-voice devices                # audio devices
chatforge-voice models [status|download [--yes]]
chatforge-voice panel [--print-url]    # open the Voice panel of the running ChatForge
chatforge-voice --version
```

## Developing

`forge-voice-core` comes from a git branch by default. To work against a sibling checkout of
DisPatch_Voice (the folder that holds both clones), uncomment the `[tool.uv.sources]` block in
`pyproject.toml` (it points at `../../../DisPatch_Voice/packages/forge-voice-core`, relative to this
folder) and:

```text
cd packages/chatforge-voice
uv venv --python 3.12
uv sync --extra dev          # or: uv pip install -e . pytest pytest-asyncio pytest-timeout ruff
uv run pytest -q
```

The block is commented out because `uv pip install ./packages/chatforge-voice` also reads it, and would
fail for anyone without that sibling checkout. Without `uv sync`, install the core by path first:
`uv pip install -e <path-to>/DisPatch_Voice/packages/forge-voice-core`, then `uv pip install -e . --no-deps`
and the dependencies listed in `pyproject.toml`.

The suite (about 100 tests, ~10 s) uses fake audio devices, fake speech engines and a fake chat. It
downloads no model, opens no audio device and uses no network. `tests/test_real_extension_host.py` runs
against ChatForge's real `chatforge.extensions` and is skipped where ChatForge is not installed.

Layout: `controller.py` (the loop), `audio_io.py` (sounddevice capture/playback), `hotkey.py` (hold-key),
`host.py` + `secrets_store.py` (the `forge-voice-core` host link), `settings.py` (schema), `models_card.py`
(Models card service), `panel.py` + `web/` (Voice panel), `runtime.py` (ChatForge registration), `cli.py`.
