# Changelog

All notable changes to ChatForge. Versions follow [Semantic Versioning](https://semver.org/):
0.x while the settings file and the add-on API may still change, with the change listed here.

## [0.2.0] - 2026-10-18

### Added
- **Windows installer without a terminal.** Double-click `Install ChatForge.bat`: it fetches `uv` and Python 3.12 if
  they are missing, installs the app for the current user (no administrator rights), creates Start Menu and Desktop
  shortcuts, asks once whether to start at sign-in, and opens ChatForge. Run it again to upgrade.
  `Uninstall ChatForge.bat` removes the app and keeps your data unless you add `--purge`.
  See [`deploy/windows/README.md`](deploy/windows/README.md). A dormant Inno Setup file
  (`deploy/windows/chatforge-installer.iss`) wraps the same scripts for a future signed installer.
- **"Set up local AI" card** on first run: one button installs the local runtime (139 MB) and downloads the starter
  model `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` (0.94 GB, Apache-2.0), with one progress bar, Cancel, a check of the
  files afterwards, and two other ways in ("Use StudioForge", "Use an API key").
- **CPU fallback on Windows PCs without an NPU.** ChatForge now detects the missing NPU and runs the model on the CPU,
  labelled "slower" on the card and in the status line. The `local.device` setting is not rewritten.
- `config.example.toml` and `docs/demo-prompts.md`.
- `chatforge --version`.
- **Add-on seams** (no add-on ships yet): the `chatforge.extensions` entry-point group, `ExtensionAPI`
  (`register_settings_section()`, `on_turn()`, `on_event()`, `on_startup()`/`on_shutdown()`, `chat`, `audio`
  placeholders), an `[extensions.<name>]` table in `config.toml`, and a generic settings card. See
  [`docs/extensions.md`](docs/extensions.md).
- `docs/PLAN-prosumer.md`: the decisions behind this release.

### Changed
- **Autostart is opt-in.** It used to be switched on silently at first run. Now the installer asks, or a one-time
  card in the popup does. `chatforge autostart enable|disable` records the answer in `config.toml`.
- **StudioForge key wording.** A key is required whenever the StudioForge server is not on this PC (StudioForge 2.0).
  A refused request now says so and names the command (`studioforge keys create`). The seed provider no longer
  claims the key is optional.
- Default theme for new installs is Glacier (existing installs keep their choice).
- Python is pinned to 3.12 or 3.13 (`.python-version`, `requires-python = ">=3.12,<3.14"`).
- The README install section is organised by platform, simplest method first.

### Upgrading from 0.1.0
- Your `config.toml` is migrated to schema 5 on the first start: `startup.autostart` keeps the value it had, the
  autostart question is marked answered (you will not be asked), and a saved `key_required = false` on the
  StudioForge row is removed so the new rule (key required off this PC) applies.
- Installed with git and `uv sync`? `git pull && uv sync` (the lock file changed: Python is now capped below 3.14).
- Nothing in your data folder (`%LOCALAPPDATA%\ChatForge`) needs to change. To go back, check out the tag
  `pre-prosumer-2026-10`; a 0.2.0 `config.toml` is read by 0.1.0 with a warning about the new keys, which it ignores.

## [0.1.0]

First tagged baseline: tray + popup assistant, OpenVINO Model Server on the Intel NPU, OpenAI-compatible providers
(StudioForge, MiniMax, OpenAI, DeepSeek, custom), nine tools, eight themes.
