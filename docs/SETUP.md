# Setting up ChatForge on a fresh Windows machine

On a Linux desktop, follow [`SETUP-LINUX.md`](SETUP-LINUX.md) instead.

Everything below runs as a normal user. No step needs administrator rights on a machine that
already has the VC++ runtime and WebView2 (both ship with Windows 11 and most software); the one
exception is called out in step 1.

Tested on: Windows 11 Home 26H2, Intel Core Ultra 5 226V (Lunar Lake, "Intel AI Boost" NPU,
driver 32.0.100.4841), 16 GB RAM, display at 175 % scaling.

**Coming from AI Chat?** ChatForge was called AI Chat before. Its first start migrates your AI
Chat settings, saved keys and models automatically.

## 1. Prerequisites

| Need | How to get it | How to check |
|---|---|---|
| Python 3.12 | <https://www.python.org/downloads/> (tick "py launcher"). The Microsoft Store `python` stub is not enough; always use `py -3.12`. | `py -3.12 --version` |
| uv | `py -3.12 -m pip install --user uv`. (With uv's standalone installer from <https://docs.astral.sh/uv/> instead, drop the `py -3.12 -m` prefix from every `uv` command below.) | `py -3.12 -m uv --version` |
| Git | <https://git-scm.com/download/win> | `git --version` |
| Node 24 (dev only) | <https://nodejs.org/>; only for `npm run test:js` | `node --version` |
| WebView2 runtime | Part of Windows 11. Otherwise the Evergreen installer at <https://developer.microsoft.com/microsoft-edge/webview2/> | the doctor (step 5) |
| VC++ 2015+ x64 runtime | Usually present. Otherwise `winget install --id Microsoft.VCRedist.2015+.x64 -e` (**needs admin**, ~25 MB) | the doctor |
| Intel NPU driver | Windows Update or <https://www.intel.com/content/www/us/en/download/794734/>; Device Manager shows "Intel(R) AI Boost" under Neural processors | the doctor |

Without an NPU the app still works: set the device to CPU (or GPU) in Settings > Providers >
Local (`local.device`; slower), or use StudioForge or a cloud provider only.

## 2. Get the code and the Python environment

In the folder where you keep your projects:

```powershell
git clone https://github.com/LaserLloyd/ChatForge.git
cd ChatForge
py -3.12 -m uv sync --extra dev
```

`uv sync` creates `.venv\` and installs pywebview, pystray, httpx, pydantic-settings, keyring,
ddgs and the dev tools (about 150 MB). `pywin32` is not part of the base install any more; add
`--extra office` (`py -3.12 -m uv sync --extra dev --extra office`) only if you want old `.xls`
and `.ppt` attachments converted through Microsoft Office. `py -3.12 -m uv run python -c "import chatforge"` should
print nothing.

## 3. Install the local runtime (OpenVINO Model Server)

```powershell
py -3.12 -m uv run chatforge runtime install
py -3.12 -m uv run chatforge runtime status
```

This downloads `ovms_windows_2026.4.0_python_on.zip` (138,798,816 bytes, SHA-256 pinned in
`chatforge.runtime.ovms_install`) into `%LOCALAPPDATA%\ChatForge\runtime\downloads\`, verifies
it, and extracts it to `%LOCALAPPDATA%\ChatForge\runtime\ovms-2026.4.0\` (357 MiB). An
interrupted download resumes on the next run. The same install button is in Settings > Models.

## 4. Get a model

Start the app once (step 6), open **Settings > Models**, search `Qwen` (the author filter
defaults to `OpenVINO`) and click **Download** on the row badged *Recommended*:
`OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` (about 0.94 GB). Progress, speed and ETA are shown;
Cancel removes the partial files; **Resume** continues after a restart. The download refuses to
start with less than the model size plus 2 GB free.

Alternatively copy a model folder in by hand:

```
%LOCALAPPDATA%\ChatForge\models\OpenVINO\Qwen2.5-1.5B-Instruct-int4-ov\
    openvino_model.xml  openvino_model.bin  openvino_tokenizer.xml  openvino_detokenizer.xml  ...
```

The registry adopts it at the next start, verifying file sizes against Hugging Face and writing
a small JSON sidecar next to it.

Do not use `OpenVINO/Qwen3-4B-int4-ov` on the NPU: it compiles but its output is garbage with
OVMS 2026.4 (details in [`RUNTIME-NOTES.md`](RUNTIME-NOTES.md)). It is on the catalog's avoid
list.

## 5. Check the machine

```powershell
py -3.12 -m uv run chatforge doctor
```

Expected on a good machine: every row PASS except *MiniMax key* (WARN until you enter one) and,
before the first run, *Config* and *Autostart* (WARN; the first run writes both). The command
exits 1 on any FAIL and names the fix.

## 6. First run

```powershell
.\.venv\Scripts\pythonw.exe -m chatforge --show
```

or double-click `launchers\ChatForge.bat`. On the first run the app:

- writes `%LOCALAPPDATA%\ChatForge\config.toml` with defaults, or migrates your AI Chat settings,
  keys and models if you used AI Chat before;
- enables **start at login**: it writes `%LOCALAPPDATA%\ChatForge\ChatForge.vbs`, which runs
  `.venv\Scripts\pythonw.exe -m chatforge --hidden` silently, and registers a per-user Task
  Scheduler task, "ChatForge", that runs it ten seconds after you sign in, on battery too (no
  admin). If Task Scheduler refuses the task, the script goes in your Startup folder
  (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`) instead. Toggle it from the tray
  menu, Settings > General, or `py -3.12 -m uv run chatforge autostart disable`;
- registers the `Ctrl+Alt+C` hotkey (if it is taken, the tray shows a notice; change it in
  Settings > General; to open ChatForge with the keyboard's Copilot key, see step 8);
- shows the popup at the lower right, above the taskbar.

Opening the popup with the local model selected starts loading it. The very first load compiles
it for the NPU, about 45 s, with a countdown in the status line under the popup's header; every later load takes about
3 s from the compile cache in `%LOCALAPPDATA%\ChatForge\cache\ov\`. After 10 idle minutes the
model unloads (the tray dot turns grey) and loads again when you next open the popup or ask a
question.

## 7. MiniMax (optional)

Get an API key from <https://platform.minimax.io/> (International) or
<https://platform.minimaxi.com/> (China). In the app: Settings > Providers > MiniMax, pick the
matching region, paste the key, **Test**, **Save key**. Then pick MiniMax in the popup's model
chip. The key is stored in the Windows Credential Manager (`keyring`), never in a file.

To run the live MiniMax test afterwards:

```powershell
py -3.12 -m uv run pytest -m live_minimax -s
```

## 8. Open ChatForge with the Copilot key (optional)

The Copilot key sends `Win+Shift+F23` (left Win and left Shift, then F23). Windows registers that
combination itself, so ChatForge can't take it with `RegisterHotKey`: that fails with error
1409 (already registered). The Settings picker (Personalization > Text input > *Customize
Copilot key on keyboard* > Custom) only lists MSIX-packaged, signed apps. ChatForge runs from
`.venv` with no package identity, so it never appears there.

### Without PowerToys: the `Copilot` hotkey

ChatForge can catch the key itself. In Settings > General, click **Copilot key** next to the
hotkey field (it fills in `Copilot`) and **Save**; or set it in `config.toml`:

```toml
[ui]
hotkey = "Copilot"
```

ChatForge then installs a low-level keyboard hook (`WH_KEYBOARD_LL`, `_CopilotHook` in
`desktop/hotkey.py`). It watches for F23 while left Win and left Shift are down, opens the popup,
swallows the F23 key (and only that key, never a modifier) and masks the Win key-up so the Start
menu stays closed. The log shows `Copilot key hooked` when it is installed, or why it could not
be. `chatforge doctor` reports it too: its Hotkey line says whether the hook can be installed
(it installs and removes a throwaway hook). It is Windows only.

**Not verified on real hardware in this release.** The logic is tested against simulated key
events, but nobody has yet pressed a real Copilot key with it. If the key still opens Windows
Copilot or Search, or the Start menu flashes up, set the hotkey back to `Ctrl+Alt+C` and use
PowerToys below.

### With PowerToys (the fallback)

PowerToys Keyboard Manager catches the key before Windows does and sends ChatForge's own hotkey
instead (keep `ui.hotkey` at `Ctrl+Alt+C` for this):

1. Install PowerToys (per-user, about 283 MB, from Microsoft's GitHub release):

   ```powershell
   winget install --id Microsoft.PowerToys -e --scope user
   ```

2. PowerToys > **Keyboard Manager**: turn on *Enable Keyboard Manager*, then **Remap a
   shortcut** > *Add shortcut remapping*.
3. *Select*: click the keyboard button and press the Copilot key. It records
   `Win (Left)` `Shift (Left)` `F23`. *To send*: `Ctrl+Alt+C` (whatever `ui.hotkey` is in
   `config.toml`). Leave *Target app* empty and click **OK**.
4. PowerToys > General: turn on *Run at startup*. Otherwise the key goes back to Windows after a
   restart.
5. Press the Copilot key and the popup opens. If Windows Search or Copilot opens instead,
   PowerToys isn't running.

If you change ChatForge's hotkey later, change the PowerToys target to match. The remap is saved
in `%LOCALAPPDATA%\Microsoft\PowerToys\Keyboard Manager\default.json`. Do not use both at once: with the
`Copilot` hotkey set, turn the PowerToys remap off.

## 9. Verify (optional, developers)

```powershell
py -3.12 -m uv run ruff check . ; py -3.12 -m uv run ruff format --check .
py -3.12 -m uv run pytest -q
npm ci ; npm run -s test:js
$env:CHATFORGE_LIVE_NPU = "1"; py -3.12 -m uv run pytest -m live_npu -s
```

## Where things are

| Path | Contents |
|---|---|
| `%LOCALAPPDATA%\ChatForge\config.toml` | settings (no secrets); `CHATFORGE_HOME` moves the whole folder |
| `%LOCALAPPDATA%\ChatForge\state.json` | last-used times, compile records, launch hashes |
| `%LOCALAPPDATA%\ChatForge\conversation.json` | the current conversation |
| `%LOCALAPPDATA%\ChatForge\downloads.json` | model downloads in progress |
| `%LOCALAPPDATA%\ChatForge\models\` | models and their sidecars |
| `%LOCALAPPDATA%\ChatForge\runtime\` | OVMS and the downloaded zip |
| `%LOCALAPPDATA%\ChatForge\cache\ov\` | NPU compile blobs (about 306 MiB per model) |
| `%LOCALAPPDATA%\ChatForge\logs\` | the app's log and `ovms.log`, rotated |
| `%LOCALAPPDATA%\ChatForge\ChatForge.vbs` | the script the "ChatForge" start-at-login task runs |
| `%APPDATA%\...\Startup\ChatForge.vbs` | start at login when Task Scheduler refused the task |
| `Documents\ChatForge\` | documents the model saved for you |

None of these are in the repo, and `.gitignore` blocks stray copies of `config.toml`,
`conversation.json`, `state.json`, logs and models, so personal settings are never committed.

## Uninstall

Quit from the tray, run `py -3.12 -m uv run chatforge autostart disable` (it removes the
"ChatForge" task and any Startup-folder copy of the script), delete `%LOCALAPPDATA%\ChatForge`
and the repo folder, and remove the ChatForge entries from the Windows Credential Manager
(Windows Credentials) if you saved a key. `Documents\ChatForge` holds documents the model saved;
keep it or delete it.

## Design sources

The runtime supervisor, downloader, tray, autostart and logging are adapted from
**StudioForge**; the chat UI, Markdown pipeline and key card from **DisPatch_Chat**; the
streaming client from **CrucibleForge**; the theme bundle from **ThemeForge** (<https://github.com/LaserLloyd/ThemeForge>, public, MIT; update it
with `python src/chatforge/web/static/ui-theme/update.py`, see the README's Themes section). See
[`PLAN.md`](PLAN.md) §1.3 (the original plan) for the file-by-file map and
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) for licences.
