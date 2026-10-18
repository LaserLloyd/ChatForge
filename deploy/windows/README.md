# Installing ChatForge on Windows without a terminal

Two files at the root of the project do everything: **`Install ChatForge.bat`** and
**`Uninstall ChatForge.bat`**. They start the PowerShell scripts in this folder, which need no
administrator rights and write nothing outside the install folder, your Start menu and desktop, one
"Installed apps" entry and, only if you say yes, the sign-in entry.

| File | What it is |
| --- | --- |
| `Install-ChatForge.ps1` | Installs or upgrades. Run by `Install ChatForge.bat`. |
| `Uninstall-ChatForge.ps1` | Removes the app, keeps your data unless `-Purge`. Run by `Uninstall ChatForge.bat`. |
| `build-release-zip.ps1` | Builds `ChatForge-<version>-windows.zip`, the download for people without git. |
| `zip-readme.txt` | The few lines of instructions inside that zip (`README.txt`). |
| `chatforge-installer.iss` | An Inno Setup wrapper for a future signed `Setup.exe`. **Dormant**, see the end. |

## What the installer does, in order

1. Checks the PC: Windows, 64-bit Intel/AMD (the local AI runtime has no ARM build), 1.5 GB free. Closes a
   running ChatForge from the install folder so it can be replaced.
2. **uv**: uses the one it finds; otherwise downloads `uv 0.12.23` (about 18 MB) from the `astral-sh/uv`
   GitHub release and refuses to use it unless its SHA-256 matches the value written in the script
   (`winget install astral-sh.uv` is the fallback).
3. **Python 3.12**: uses one that is there; otherwise `uv python install 3.12` (a private copy, about 30 MB;
   nothing else on the PC changes); then winget, then the signature-checked python.org installer as fallbacks.
4. **The app**: copies it from the folder next to the `.bat` (the release zip, or a git clone); if there is none,
   `git clone`, or the GitHub source archive when git is missing.
5. `uv sync --locked --no-dev --inexact`: the app's own libraries (about 150 MB), the versions in `uv.lock`;
   `--inexact` leaves anything you added to that environment (an add-on) in place.
6. Start menu and desktop shortcuts, and the entry in Settings > Apps > Installed apps (current user only).
7. **Asks once**: "Start ChatForge automatically when you sign in to Windows? [Y/n]". Enter means yes. If it cannot
   ask (no console) it registers nothing. Then it opens ChatForge, whose first-run card downloads the model.

Where things go:

| What | Where |
| --- | --- |
| The app and its private Python libraries | `%LOCALAPPDATA%\Programs\ChatForge\app` (and `tools\uv.exe` if uv was downloaded) |
| Install log | `%LOCALAPPDATA%\Programs\ChatForge\logs\install-<time>.log` |
| Settings, chat, models, runtime, logs | `%LOCALAPPDATA%\ChatForge` (unchanged by install, upgrade and a plain uninstall) |
| Shortcuts | Start menu and desktop, `ChatForge.lnk` |
| Sign-in entry (only if you said yes) | Task Scheduler task `ChatForge`, run for your user |

## Options

Run from a command prompt in the project folder:

```bat
"Install ChatForge.bat" --yes                  :: no questions: start at sign-in (add --no-autostart to decline instead)
"Install ChatForge.bat" --no-autostart --no-launch --no-shortcuts
"Install ChatForge.bat" --install-dir "D:\Apps\ChatForge"
"Install ChatForge.bat" --ref v0.2.0           :: which version to download when no app is next to the file
"Uninstall ChatForge.bat" --purge --yes        :: also delete your settings, chats, models and saved API keys
```

The PowerShell script takes the same switches under their PowerShell names (`-Yes`, `-NoAutostart`, `-NoLaunch`,
`-NoShortcuts`, `-NoDesktopIcon`, `-InstallDir`, `-SourceDir`, `-Ref`, `-Repo`). Five more exist for testing and
diagnosis: `-ShortcutDir`, `-DataHome` and `-UninstallKeyName` (point the shortcuts, the data folder and the
Installed-apps entry somewhere harmless), and `-ForceDownloadUv` / `-ForceManagedPython` (ignore the uv and Python
already on the PC, to exercise the download paths).

## Upgrade, uninstall, purge

- **Upgrade** is the same installer run again: the app files are replaced, the libraries re-synced, shortcuts
  rewritten, your earlier answer about signing in is kept, your data is not touched. A running ChatForge is closed
  first (only the one in the install folder).
- **Uninstall** (`Uninstall ChatForge.bat`, or Settings > Apps > ChatForge): removes the sign-in entry, the
  shortcuts, the Installed-apps entry and the install folder. **Your data stays** in `%LOCALAPPDATA%\ChatForge`.
- **Purge** (`--purge`, or answer Y when the uninstaller asks, then type YES): also deletes `%LOCALAPPDATA%\ChatForge`
  and the API keys ChatForge stored in Credential Manager. `Documents\ChatForge` (files the model made for you) is
  never touched. uv and Python are shared tools and stay; `uv python uninstall 3.12` removes a uv-managed Python.

## Windows warnings you may see

The scripts are not signed, so Windows may say "Windows protected your PC" (SmartScreen) the first time you run
the `.bat`: choose **More info**, then **Run anyway**. They are plain text; open them in Notepad first if you like.
A downloaded zip is also marked "from the internet": right-click it, Properties, **Unblock**, before extracting,
and the warning goes away for everything inside.

## Building the zip

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\build-release-zip.ps1     # -> dist\ChatForge-<version>-windows.zip (+ .sha256)
```

It packs the tracked files of the working tree, so build from a clean checkout of the tag you are releasing. CI does
this on every push and keeps the zip as a build artifact (`windows-installer` job).

## A signed Setup.exe, later

`chatforge-installer.iss` is an Inno Setup 6 script that shows a normal wizard and then runs the same
`Install-ChatForge.ps1` on the same payload, so a `Setup.exe` and the zip cannot disagree. It is **dormant**: it has
not been compiled, CI does not build it, and the README does not offer it, because an *unsigned* `Setup.exe` is
no better than the zip (the same SmartScreen warning, plus more antivirus false positives) and a signed one needs
a code-signing certificate that the project does not have yet.

When there is a signing certificate (an OV or EV certificate from a certificate authority, or Azure Trusted Signing):

1. Install Inno Setup 6 and register the signing tool (Tools > Configure Sign Tools, name it `chatforgesign`, for
   example `signtool.exe sign /fd SHA256 /tr <timestamp-url> /td SHA256 /a $f`).
2. Uncomment the `SignTool=chatforgesign $f` line in the `.iss` file.
3. Build as described at the top of the `.iss` file, and sign the two `.ps1` files as well if you want them to pass a
   strict PowerShell execution policy.
4. Only then publish the `Setup.exe` and add it to the README as the simplest method. A `winget` manifest also
   needs a signed, versioned, hash-pinned installer URL, so it waits for the same step.

## Why not PyInstaller, a Windows service or a container?

ChatForge is a per-user desktop app (a tray icon and a WebView2 window), so a Windows service (WinSW) cannot show
its window. A frozen bundle (PyInstaller) is 120-200 MB, trips antivirus without a signature, and has to ship the
LGPL-3.0 `pystray` as a replaceable package. A container cannot host the tray, the window, the global hotkey or the
NPU. A private Python environment installed by `uv` is small, transparent, and upgrades by running the installer
again, which is why it is the method here.
