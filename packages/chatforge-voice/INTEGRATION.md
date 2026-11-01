# Integration notes

`chatforge-voice` is the optional CPU voice add-on for ChatForge: push-to-talk, local Parakeet speech
recognition and Chatterbox-Turbo speech. It touches no ChatForge file. It plugs in through the
`chatforge.extensions` entry point (`voice = "chatforge_voice:setup"`) at extension API version 1; the API is
described in `docs/extensions.md`. The user-facing description (install, models, settings, limits) is in this
package's README and in the main README under "Voice (optional add-on)".

* `forge-voice-core` (the engines) comes from the `main` branch of the DisPatch_Voice repository through the
  git URL in `pyproject.toml`. To work against a sibling checkout, uncomment the `[tool.uv.sources]` block there;
  it is commented out because `uv pip install ./packages/chatforge-voice` also reads it.
* `chatforge.extensions` is read for settings (polled about once a second, there is no change hook), the chat
  port (`send`/`cancel`, events carry `request_id`) and `on_event`, which runs on the core loop thread, so the
  add-on only queues text there.
* Models live in `<data_dir>/models` (`<home>/extensions/voice/models`, about 1.4 GB) and go when the home
  folder is removed. Keys under `[extensions.voice]` that are not on the Settings card (`threads`, `tts_threads`,
  `idle_unload_minutes`, `mode`, `backend`) are kept as written.
* `tests/test_real_extension_host.py` runs against the real `chatforge.extensions` and is skipped where ChatForge
  is not installed.
* The engines' licences and the Parakeet attribution are in `THIRD_PARTY_NOTICES.md`.
