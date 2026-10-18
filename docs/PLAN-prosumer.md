# Plan: the 0.2.0 "prosumer" release (install, first run, honest docs, extension seams)

Branch `ws/cfg-a` (from `refactor/prosumer-2026`, baseline tag `pre-prosumer-2026-10`). Source of the findings:
the Phase 2 audit of ChatForge 0.1.0 (install story, starter model, StudioForge key wording, autostart consent).
This file records the decisions before the code; `CHANGELOG.md` records what shipped.

## Goals and the numbers they are held to

| Goal | Measure |
|---|---|
| A Windows user who owns no developer tools reaches a first chat | 3 user actions: get one file, double-click it, click **Set up local AI** (plus answering one yes/no about autostart) |
| No terminal | every step is a double-click or a button; the installer prints plain-language progress |
| First test is one download away | catalog starter model `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov`, 0.94 GB, Apache-2.0, plus the 139 MB runtime |
| No NPU is not a dead end | CPU is chosen automatically and labelled "slower" in the card and the status line |
| Nothing persistent without a question | autostart is asked, never silently enabled |

## Decisions

**D1. Installer = PowerShell bootstrap behind two `.bat` files.** `Install ChatForge.bat` (repo root, also in the
zip) calls `deploy/windows/Install-ChatForge.ps1` through `powershell.exe -ExecutionPolicy Bypass` (the bat avoids
the execution-policy and "right-click, Run with PowerShell" problems). The script is Windows PowerShell 5.1 safe
(the one every Windows 11 has). It needs no admin rights and writes nothing outside the install folder, the user's
Start Menu, Desktop, one `HKCU` uninstall entry and (if the user agrees) the logon task. Pinned alternatives
considered: PyInstaller/pynsist (8-12 days, antivirus false positives, LGPL duties for pystray), MSIX/winget
(need a signing certificate). The Inno Setup file is a dormant wrapper over the same script (D13).

**D2. Provisioning order, each step skipped when already satisfied.**
1. `uv`: an existing `uv` on PATH, else a pinned `uv-x86_64-pc-windows-msvc.zip` from the astral-sh release
   with a SHA-256 hard-coded in the script (verified before extraction), else `winget install astral-sh.uv`.
2. Python 3.12: whatever `uv python find 3.12` reports (system or uv-managed), else `uv python install 3.12`
   (a per-user standalone build, no admin, no PATH change), else `winget install Python.Python.3.12 --scope user`,
   else the python.org installer downloaded directly (Authenticode signature checked, per-user switches).
   uv-managed Python goes first because it cannot break the user's other Python installs.
3. App: a source tree next to the script (the release zip or a clone) is copied to the install folder;
   else `git clone` (when git exists); else the GitHub source archive for the ref. The script never needs git.
4. `uv sync --locked --no-dev --python 3.12` in `%LOCALAPPDATA%\Programs\ChatForge\app` (the venv lives there).
5. Shortcuts (Start Menu + Desktop) to the venv `pythonw.exe -P -m chatforge --show` with the app icon.
6. Autostart: asked on the console (default Yes on Enter); on Yes runs `chatforge autostart enable`.
7. Launch the tray app; the first-run card does the rest.

x64 only (OpenVINO Model Server ships x64 Windows builds only); ARM64 gets a clear message.

**D3. Idempotent = upgrade.** Re-running copies the new tree over `app\`, re-syncs, rewrites shortcuts, keeps the
earlier autostart answer, and first stops only a ChatForge whose executable lives inside the install folder.
User data (`%LOCALAPPDATA%\ChatForge`: settings, chat, models, runtime) is never touched by install or upgrade.

**D4. Uninstall keeps data.** `Uninstall ChatForge.bat` removes the logon task, shortcuts, the `HKCU` entry, the
install folder; user data stays unless `--purge` (asked interactively when double-clicked). `--purge` also removes the
data folder and the `*@ChatForge` Credential Manager entries (the API keys).

**D5. Autostart is opt-in.** `startup.autostart` defaults to `false`; new `startup.autostart_asked` records that the
user was asked. First run no longer calls `autostart.enable`. The installer asks once on the console; a source install
gets the same question as a card in the popup. Existing config files (schema < 5) are migrated with
`autostart_asked = true` so nobody is asked again and nobody's current setting changes. `chatforge autostart enable|disable`
now also records the answer in `config.toml`.

**D6. "Set up local AI" card (first run).** A small state machine in `chatforge/setup_local.py`, driven by the
bridge (`setup_local_start/cancel/status/dismiss`) and reporting `setup.progress` events: runtime (139 MB, SHA-256
pinned) -> model (0.94 GB through the existing resumable downloader, per-file SHA-256 against Hugging Face's LFS oid)
-> verify (registry rescans; every listed file present with the listed size) -> ready. One progress bar weighted by
bytes, Cancel at every stage (partial files deleted), disk-space check before the first byte, and two escape hatches
on the same card: **Use StudioForge** and **Use an API key**. The card shows only when the selected provider is local
and the runtime or the model is missing, and it can be dismissed. The model id is read from the catalog, never hard-coded.

**D7. No NPU -> CPU, labelled.** On Windows the OVMS launch used to fail on a PC without an NPU. A one-time background
probe (`runtime/npu_detect.py`, wraps `doctor.list_npu_devices`) caches "present / absent / unknown"; only a confirmed
"absent" switches the device, so an unknown result keeps today's behaviour. The config stays `local.device = "NPU"`
(a later NPU upgrade just works). The CPU run is reported through the existing `device_fallback` field, so the status
line reads "Runs on CPU instead of NPU: no Intel NPU found (slower)", and the setup card says it before the download.
`precompile` already skips CPU.

**D8. StudioForge key.** The seed provider changes from `key_required=False` to automatic: required unless the base
URL is loopback (the rule custom providers already follow). A saved `key_required=false` on the `studioforge` row is
reset by the v5 migration. The 401/403 error for a provider that was sent no key now names the provider and, for
StudioForge, says "StudioForge 2.0 needs an API key when the server is not on this PC: create one with
`studioforge keys create`, then paste it in Settings > Providers > StudioForge". Verified against a fake server that
answers 401 unless the bearer matches. README wording corrected in all three places the audit found.

**D9. Docker.** The README says plainly that a tray + WebView2 + NPU app is not a container app, and offers the
neighbour (DisPatch in Docker pointed at StudioForge) instead. No compose file, no Dockerfile.

**D10. Config format.** The brief asked for `config.example.yaml`. ChatForge's file is `config.toml` and the loader
reads nothing else, so a YAML example would be a file that does nothing. Shipped as `config.example.toml`
(every key valid for `config.py`, checked by a test that loads it).

**D11. Python pin.** `.python-version` = 3.12 and `requires-python = ">=3.12,<3.14"`, so a fresh machine cannot resolve
to an interpreter pythonnet/pywebview have not been tried on. `uv.lock` regenerated.

**D12. Default theme for new installs: `glacier`** (the family default). Existing config files keep their choice.

**D13. Inno Setup file is dormant.** `deploy/windows/chatforge-installer.iss` wraps the zip contents and runs the same
bootstrap/uninstall scripts; it is not built in CI and not advertised as an install method until the owner has a
code-signing certificate (documented in `deploy/windows/README.md`: where the `SignTool` line goes, what SmartScreen
does to an unsigned build).

**D14. Extension seams, no voice.** `chatforge.extensions`: an entry-point group `chatforge.extensions`; each entry
point is a callable `setup(api: ExtensionAPI)`. `ExtensionAPI` offers `register_settings_section()`, `on_turn()`,
`on_event()`, `on_startup()/on_shutdown()`, `chat.send()/cancel()` and `audio` (placeholders: capture/playback backend
registries with no backend shipped). Settings live under a new `[extensions.<name>]` config table (so the closed
`AppConfig` stops dropping them); a generic card in Settings > General renders registered sections. A broken extension
is logged and skipped, never fatal. Documented in `docs/extensions.md`.

**D15. SemVer.** 0.1.0 -> 0.2.0 (new features, one behaviour change: autostart default). One source of truth
test (`pyproject.toml` == `chatforge.__version__` == `package.json`), `chatforge --version`, `CHANGELOG.md` with an
"Upgrading" block.

## Out of scope (honest list)

Voice (another agent builds the core; D14 is only the socket). Signed binaries, MSIX, winget. Linux and macOS GUI
verification (unchanged: Linux port is untested on a real desktop, macOS unsupported). Translations. A headless API.
New models beyond the one catalog starter. A bundled NPU driver (Windows Update owns it).

## Test plan

Unit tests with fakes only (no NPU, no live StudioForge: any default points at `127.0.0.1:9`). Fake Hugging Face over
a real loopback HTTP server for the setup flow. A fake StudioForge that answers 401 without the right bearer. A
PowerShell parse test for every `.ps1` (Windows only). The bootstrap itself is exercised in a scratch folder with
`-InstallDir`, `-ShortcutDir`, `CHATFORGE_HOME` overrides, then uninstalled, and the shortcuts/logon task/registry key
checked absent afterwards.
