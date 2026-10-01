# Setting up AI Chat on a fresh Windows machine

Everything below runs as a normal user. No step needs administrator rights on a machine
that already has the VC++ runtime and WebView2 (both ship with Windows 11 and most
software); the one exception is called out in step 2.

Tested on: Windows 11 Pro 26H2, Intel Core Ultra 5 226V (Lunar Lake, "Intel AI Boost"
NPU, driver 32.0.100.4841), 16 GB RAM, display at 175 % scaling.

## 1. Prerequisites

| Need | How to get it | How to check |
|---|---|---|
| Python 3.12 | https://www.python.org/downloads/ (tick "py launcher"). The Microsoft Store `python` stub is not enough; always use `py -3.12`. | `py -3.12 --version` |
| uv | `py -3.12 -m pip install --user uv` (or the installer at https://docs.astral.sh/uv/) | `py -3.12 -m uv --version` |
| Git | https://git-scm.com/download/win | `git --version` |
| Node 24 (dev only) | https://nodejs.org/ ; only for `npm run test:js` | `node --version` |
| WebView2 runtime | Part of Windows 11. Otherwise the Evergreen installer at https://developer.microsoft.com/microsoft-edge/webview2/ | `aichat doctor` (step 5) |
| VC++ 2015+ x64 runtime | Usually present. Otherwise `winget install --id Microsoft.VCRedist.2015+.x64 -e` (**needs admin**, ~25 MB) | `aichat doctor` |
| Intel NPU driver | Windows Update or https://www.intel.com/content/www/us/en/download/794734/ ; Device Manager shows "Intel(R) AI Boost" under Neural processors | `aichat doctor` |

Without an NPU the app still works: set `local.device = "CPU"` in Settings > Providers >
Local (slower), or use MiniMax only.

## 2. Get the code and the Python environment

```powershell
cd "$env:USERPROFILE\Desktop\Projects"
git clone https://github.com/LaserLloyd/AI-Chat.git "AI Chat"      # private repo; gh auth login first
cd "AI Chat"
py -3.12 -m uv sync --extra dev
```

`uv sync` creates `.venv\` and installs pywebview, pystray, httpx, pydantic-settings, keyring,
ddgs and the dev tools (about 150 MB). `py -3.12 -m uv run python -c "import aichat"` should
print nothing.

## 3. Install the local runtime (OpenVINO Model Server)

```powershell
py -3.12 -m uv run python -m aichat runtime install
py -3.12 -m uv run python -m aichat runtime status
```

This downloads `ovms_windows_2026.4.0_python_on.zip` (138,798,816 bytes, sha256 pinned in
`aichat.runtime.ovms_install`) into `%LOCALAPPDATA%\AIChat\runtime\downloads\`, verifies it,
and extracts it to `%LOCALAPPDATA%\AIChat\runtime\ovms-2026.4.0\` (357 MiB). A download that
is interrupted resumes on the next run. The same install button exists in Settings > Models.

## 4. Get a model

Start the app once (step 6), open **Settings > Models**, search `Qwen` (the author filter
defaults to `OpenVINO`) and click **Download** on the row badged *recommended*:
`OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` (about 0.94 GB). Progress, speed and ETA are
shown; Cancel removes the partial file; Resume continues after a restart. The download
refuses to start with less than the model size plus 2 GB free.

Alternatively copy a model folder in by hand:

```
%LOCALAPPDATA%\AIChat\models\OpenVINO\Qwen2.5-1.5B-Instruct-int4-ov\
    openvino_model.xml  openvino_model.bin  openvino_tokenizer.xml  openvino_detokenizer.xml  ...
```

The registry adopts it at the next start, verifying file sizes against Hugging Face and
writing a `.aichat-model.json` sidecar.

Do not use `OpenVINO/Qwen3-4B-int4-ov` on the NPU: it compiles but its output is garbage
with OVMS 2026.4 (details in `RUNTIME-NOTES.md`). It is on the catalog's avoid list.

## 5. Check the machine

```powershell
py -3.12 -m uv run python -m aichat doctor
```

Expected on a good machine: every row PASS except *MiniMax key* (WARN until you enter one)
and, before the first run, *Config* and *Autostart* (WARN; the first run writes both). The
command exits 1 on any FAIL and names the fix.

## 6. First run

```powershell
.\.venv\Scripts\pythonw.exe -m aichat --show
```

or double-click `launchers\AI Chat.bat`. On the first run the app:

- writes `%LOCALAPPDATA%\AIChat\config.toml` with defaults;
- enables **start at login**: it writes `%LOCALAPPDATA%\AIChat\AIChat.vbs`, which runs
  `.venv\Scripts\pythonw.exe -m aichat --hidden` silently, and registers a per-user Task
  Scheduler task, "AI Chat", that runs it ten seconds after you sign in (no admin). If Task
  Scheduler refuses the task, the script goes in your Startup folder
  (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AIChat.vbs`) instead. Toggle it
  from the tray menu, Settings > General, or `aichat autostart disable`;
- registers the `Ctrl+Alt+C` hotkey (if it is taken, the tray shows a notice; change it in
  Settings > General; to open AI Chat with the keyboard's Copilot key, see step 8);
- shows the popup at the lower right, above the taskbar.

The first question loads the model. The very first load compiles it for the NPU, about 45 s,
with a countdown in the popup header; every later load takes about 3 s from the compile
cache in `%LOCALAPPDATA%\AIChat\cache\ov\`. After 10 idle minutes the model unloads (the
tray dot turns grey) and reloads on the next question.

## 7. MiniMax (optional)

Get an API key from https://platform.minimax.io/ (International) or https://platform.minimaxi.com/
(China). In the app: Settings > Providers > MiniMax, pick the matching region, paste the
key, **Test**, **Save**. Then pick MiniMax in the popup's model chip. The key is stored in
the Windows Credential Manager (`keyring`, service `AIChat`), never in a file.

To run the live MiniMax test afterwards:

```powershell
py -3.12 -m uv run pytest -m live_minimax -s
```

## 8. Open AI Chat with the Copilot key (optional)

The Copilot key sends `Win+Shift+F23` (left Win and left Shift, then F23). Windows registers
that combination itself, so AI Chat can't take it as its hotkey: `RegisterHotKey` fails with
error 1409 (already registered) whatever the key is set to in Settings. The Settings picker
(Personalization > Text input > *Customize Copilot key on keyboard* > Custom) only lists
MSIX-packaged, signed apps. AI Chat runs from `.venv` with no package identity, so it never
appears there.

What works is PowerToys Keyboard Manager. It catches the key before Windows does and sends
AI Chat's own hotkey instead:

1. Install PowerToys (per-user, about 283 MB, from Microsoft's GitHub release):
   ```powershell
   winget install --id Microsoft.PowerToys -e --scope user
   ```
2. PowerToys > **Keyboard Manager**: turn on *Enable Keyboard Manager*, then **Remap a
   shortcut** > *Add shortcut remapping*.
3. *Select*: click the keyboard button and press the Copilot key. It records
   `Win (Left)` `Shift (Left)` `F23`. *To send*: `Ctrl+Alt+C` (whatever `ui.hotkey` is in
   `config.toml`). Leave *Target app* empty and click **OK**.
4. PowerToys > General: turn on *Run at startup*. Otherwise the key goes back to Windows
   after a restart.
5. Press the Copilot key and the popup opens. If Windows Search or Copilot opens instead,
   PowerToys isn't running.

If you change AI Chat's hotkey later, change the PowerToys target to match. The remap is
saved in `%LOCALAPPDATA%\Microsoft\PowerToys\Keyboard Manager\default.json`.

Not implemented: AI Chat could catch the key itself with a `WH_KEYBOARD_LL` hook in
`desktop/hotkey.py`. The hook would swallow F23 while Win+Shift are down, and it must mask
the Win key-up, or the Start menu opens.

## 9. Verify (optional, developers)

```powershell
py -3.12 -m uv run ruff check . ; py -3.12 -m uv run ruff format --check .
py -3.12 -m uv run pytest -q
npm ci ; npm run -s test:js
$env:AICHAT_LIVE_NPU = "1"; py -3.12 -m uv run pytest -m live_npu -s
```

## Where things are

| Path | Contents |
|---|---|
| `%LOCALAPPDATA%\AIChat\config.toml` | settings (no secrets); `AICHAT_HOME` moves the whole folder |
| `%LOCALAPPDATA%\AIChat\state.json` | last-used times, compile records, launch hashes |
| `%LOCALAPPDATA%\AIChat\conversation.json` | the current conversation |
| `%LOCALAPPDATA%\AIChat\models\` | models and their sidecars |
| `%LOCALAPPDATA%\AIChat\runtime\` | OVMS and the downloaded zip |
| `%LOCALAPPDATA%\AIChat\cache\ov\` | NPU compile blobs (about 306 MiB per model) |
| `%LOCALAPPDATA%\AIChat\logs\` | `aichat.log` and `ovms.log`, rotated |
| `%LOCALAPPDATA%\AIChat\AIChat.vbs` | the script the "AI Chat" start-at-login task runs |
| `%APPDATA%\...\Startup\AIChat.vbs` | start at login when Task Scheduler refused the task (and in earlier versions) |

## Uninstall

Quit from the tray, run `py -3.12 -m uv run python -m aichat autostart disable` (it removes
the "AI Chat" task and any Startup-folder `AIChat.vbs`), delete
`%LOCALAPPDATA%\AIChat` and the repo folder, and remove the `AIChat` entries from the
Windows Credential Manager if you saved a key.

## Design sources

The runtime supervisor, downloader, tray, autostart and logging are adapted from
**StudioForge**; the chat UI, Markdown pipeline and key card from **DisPatch_Chat**; the
streaming client from **CrucibleForge**; the theme bundle from **UnifyingTheme**. See
`docs/PLAN.md` §1.3 for the file-by-file map and `THIRD_PARTY_NOTICES.md` for licences.
