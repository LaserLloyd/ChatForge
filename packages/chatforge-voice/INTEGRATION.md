# Integration notes for the merge (ws/cfg-voice into refactor/prosumer-2026)

Written against wave A's extension seams and re-checked against the merged `main` at 7260cc8:
`src/chatforge/extensions.py` there is byte-identical to the copy this add-on's tests ran against
(`API_VERSION = 1`), and `docs/extensions.md` agrees on every name used here (entry point
`chatforge.extensions` -> `chatforge_voice:setup`, `api.data_dir`, `register_settings_section`, `on_turn`,
`on_event`, `on_startup`/`on_shutdown`, `chat.send`/`cancel`, `audio`). No add-on change was needed. The add-on
touches **no** ChatForge file. (Re-check done by diff and reading only; no run after the merge.)

## Verified

* `tests/test_real_extension_host.py` passes (4 tests) against a copy of wave A's `chatforge/extensions.py`
  (`API_VERSION = 1`, identical to merged main 7260cc8): entry point `chatforge_voice:setup`, `SettingField` / `register_settings_section`,
  `on_turn`, `on_event`, `on_startup` / `on_shutdown`, `chat` port, `audio` registries, `validate_values`,
  and a failed `setup` showing as `LoadedExtension(ok=False, error=...)`. It is SKIPPED where `chatforge.extensions`
  does not import (the base `refactor/prosumer-2026` today), so rerun it after the merge:
  `cd packages/chatforge-voice && uv run pytest tests/test_real_extension_host.py -q -rs` in an environment that has
  both ChatForge (with the seams) and this package.

## Assumptions about wave A's seams (re-check against the merged code)

1. Entry point value is the callable `chatforge_voice:setup`, and `API_VERSION` is read off that object (set as an
   attribute on `setup`). `docs/extensions.md` (not yet written when read) should say the same; the audit text used
   `chatforge_voice:EXTENSION`, which is NOT what wave A built.
2. `api.settings` is re-read often: the add-on polls it about once a second (hold-key spec, enabled flag), so a
   Settings save takes effect without a restart. There is no "settings changed" hook; if one is added, replace the
   1 s poll in `runtime.VoiceAddon._reap` / `engine_settings` TTL.
3. `api.data_dir` is `<home>/extensions/voice`. Models go to `<data_dir>/models` (1.4 GB). `bridge.disk_usage`
   and the "Models" disk reporting count only `Paths.models_dir`, so voice models are invisible there. Alternative: the
   host exposes `Paths.models_dir` and the add-on uses `<models_dir>/voice` (audit C.4).
4. Config key `[extensions.voice]` accepts unregistered keys (`settings_for` keeps them): used for `threads`,
   `tts_threads`, `idle_unload_minutes`, `mode`, `backend`, which are not on the card.
5. `ChatPort.send(text)` returns the request id or `None`; events carry `request_id`. Events can arrive before
   `send()` returns (the add-on claims the first `chat.start` while a voice send is pending).
6. `on_event` runs on the core loop thread; the add-on only queues text there.

## Seams the add-on needed and does not have (worked around; each is a candidate for a generic seam)

| Need | Workaround in this package | Better seam |
|---|---|---|
| Buttons and live status in Settings (Models: download, progress bar, Cancel, attribution) | `panel.py`: loopback page on `127.0.0.1:<random>` with a per-launch token in the URL fragment, opened with `chatforge-voice panel` (reads `<data_dir>/panel.json`) | `register_settings_section(..., actions=[...], status=callable)` or a `register_panel(html_dir)` served by ChatForge's static server (same origin, no extra socket) |
| Mic button in the popup composer | the panel's hold-to-talk button; the hold-key works without any UI | `docs/extensions.md` promises a mic button and status strip as a **version-2 slot**: when it lands, move the hold-to-talk button and the state line from `web/panel.js` into it (the controller API `talk_start` / `talk_stop` / `stop_all` / `status` is what they call) |
| A spoken-reply system-prompt suffix (short sentences, no tables) | none; `speak` cleans markdown/code/numbers, replies are not shortened | `ExtensionAPI.system_prompt_suffix(request_id) -> str` (audit C.4: about 60 tokens for the 4096-token local model) |
| `doctor` rows for voice (models, audio device, PortAudio) | `chatforge-voice status` on the command line | `ExtensionAPI.register_doctor_check()` |
| Global hold-key | `hotkey.py` polls `GetAsyncKeyState` at 50 Hz while Voice is on (Windows only; nothing is swallowed) | key-up support in `desktop/hotkey.py` (a low-level hook like `_CopilotHook`) and an API to register a hold-key |
| Quit/lifecycle | `on_shutdown` stops the threads, closes the panel, drops the engines | fine as is |
| CPU contention with OVMS compiling a model | none (unmeasured) | a "busy" hint from `runtime/manager` |

## Other things to adjust at merge

* **Root `pyproject.toml` / CI (wave A's files):** nothing is required; the root suite's `testpaths = ["tests"]` does not
  collect the package, and the root `ruff check .` / `ruff format --check .` pass over `packages/` (verified: 201 files).
  To test the add-on in CI add a job that does `cd packages/chatforge-voice && uv sync --extra dev && uv run pytest -q`
  (the git source for `forge-voice-core` must be reachable; it points at the pushed branch `feat/forge-voice-core`,
  PR #20 of DisPatch_Voice: **repoint to a tag or `main` once that PR merges**).
* **`requires-python`:** wave A sets `>=3.12,<3.14` on ChatForge; the add-on uses the same range.
* **README / SETUP:** the main README section text is in the report of this branch (not applied here, README is wave A's).
* **`THIRD_PARTY_NOTICES.md`:** add the Parakeet CC-BY-4.0 attribution line (text in `messages.ATTRIBUTION`) and
  sounddevice (MIT), forge-voice-core (MIT) when the add-on is documented as shipped.
* **Uninstall:** `deploy/windows/Uninstall-ChatForge.ps1` removes the home folder only if asked; the voice models live in
  `<home>/extensions/voice/models` and go with it. The Credential Manager service `ChatForge Voice` is only used by the
  unused rig token and is empty in this release.
* **Installer:** the zip installer from wave A does not install the add-on; it is `uv pip install ./packages/chatforge-voice`
  into the app's environment (document it next to the installer or add an optional "voice" switch to it).
