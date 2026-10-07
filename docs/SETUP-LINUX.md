# ChatForge on Linux

Status: **ported headless, not yet run on a real desktop.** The desktop shell was adapted on a
machine without a display; the Linux branches are covered by unit tests against fakes
(`tests/unit/test_linux_shell.py`, `test_linux_services.py`), and nothing here has been seen
working on GNOME, KDE or any other session. Treat this page as the plan and the checklist at the
bottom as what still has to be verified.

ChatForge runs on X11, or on Wayland through XWayland (`GDK_BACKEND=x11` is set for you when
`WAYLAND_DISPLAY` and `DISPLAY` are both present). A pure-Wayland session without XWayland cannot
place the popup or grab a hotkey.

## 1. System packages (Debian / Ubuntu)

pywebview draws the pages with GTK 3 and WebKit2GTK. PyGObject is not installable from PyPI
without a compiler and the GTK development headers, so use the distribution's packages and let
the virtual environment see them.

```
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
                 gir1.2-ayatana-appindicator3-0.1   # the last one is optional (tray, see 5)
```

`gir1.2-webkit2-4.1` is Ubuntu 22.04+ / Debian 12+. Older systems have `gir1.2-webkit2-4.0`,
which pywebview also accepts. A Qt build (`pywebview[qt]`) is the alternative if GTK is not an
option; it is untested here.

## 2. Install

```
uv venv --system-site-packages            # sees python3-gi from apt
uv sync --locked --extra dev              # in a checkout, or: uv pip install .
```

or, as a tool: `pipx install --system-site-packages /path/to/ChatForge`.

Check the result:

```
chatforge doctor
```

On Linux it adds rows for the display, the web view toolkit (GTK + WebKit2 or Qt), the tray host
and the NPU device node, and drops the WebView2 and VC++ rows.

Start it with `chatforge --show` (popup) or `chatforge --hidden` (tray only). Copy
`launchers/chatforge.desktop` to `~/.local/share/applications/` for an app-menu entry (the icon
name `chatforge` needs an icon of that name in your icon theme; the app also writes
`app-icon-v2.png` into its data folder, `~/.local/share/ChatForge` or what `CHATFORGE_HOME`
says, which you can point `Icon=` at).

## 3. Local model

```
chatforge runtime install
```

downloads the OpenVINO Model Server archive for Ubuntu 22.04 or 24.04 (other distros get the 24.04
build) into the data folder. Download a model in Settings > Models.

* The default variant on Linux is `python_off` (`local.ovms_variant`): the `python_on` build does
  not bundle an interpreter and needs a system `python3` with `numpy` and `Jinja2`. Choose
  `python_on` in `config.toml` only if you installed those.
* Without `/dev/accel/accel0` (the Intel NPU driver, kernel `intel_vpu`) a configured NPU device
  runs on the **CPU** for that launch, with one warning in the log; `config.toml` is not changed,
  so the NPU is used again as soon as the driver is there.
* The first CPU load is slower than a cached NPU load but needs no compile step per model. A
  model that is too large for your RAM will be killed by the kernel; pick a smaller one.
* Cloud providers need none of this.

## 4. Hotkey

* **X11:** the hotkey (default `Ctrl+Alt+Space`) is an `XGrabKey` on the root window. If another
  program holds the combination, Settings reports it and you pick another.
* **Wayland:** the grab only sees keys while an XWayland window has the focus, so bind a desktop
  shortcut to `chatforge --show` in your system settings (a second `chatforge --show` shows the
  popup of the running instance). `chatforge doctor` says which case you are in.
* The Copilot key option is Windows only.

## 5. Tray icon

pystray's `xorg` backend is the default when `DISPLAY` is set, because the AppIndicator and GTK
backends need a GTK main loop of their own and pywebview already owns it. Set
`PYSTRAY_BACKEND=appindicator` (with `gir1.2-ayatana-appindicator3-0.1`) or `gtk` to try those.
GNOME shows no tray icons at all unless the "AppIndicator and KStatusNotifierItem Support"
extension is installed; without a tray, open the popup with the hotkey or `chatforge --show`.
Notifications are not available from the `xorg` backend; messages go to the log instead.

## 6. Start at login

Settings > General > Start at login, the tray menu, or `chatforge autostart enable` writes
`~/.config/autostart/chatforge.desktop` (`$XDG_CONFIG_HOME` is respected):
`Exec=<python> -P -m chatforge --hidden`, `X-GNOME-Autostart-Delay=10`. `chatforge autostart
disable` removes it. `config.toml` refuses a documents folder inside `~/.config/autostart`.

## 7. API keys

Keys are stored with `keyring`: GNOME Keyring (libsecret) or KWallet, whichever your session runs.
On a headless box, a minimal window manager or any session without a secret service, `keyring`
has no backend; use the environment variable named in Settings > Providers instead (for example
`MINIMAX_API_KEY`, `OPENAI_API_KEY`) in the environment that starts ChatForge, such as
`~/.config/environment.d/chatforge.conf` for a systemd user session. `chatforge doctor` shows the
keyring row.

## 8. Not available on Linux

* **OCR of pictures** (it uses the Windows OCR engine): a model that cannot see images gets no
  text from an attached picture.
* **Old Office files** (`.doc`, `.xls`, `.ppt`): they are converted by Microsoft Office on
  Windows; here only the modern and open formats are read.
* **The NPU** unless the Intel driver is installed and `/dev/accel/accel0` exists (CPU is used).
* **The Copilot key hotkey**, the Windows taskbar identity and rounded window corners.
* Native notifications with the default `xorg` tray backend.

## To verify on a real desktop

1. Window opens with the app icon, page shows real data (not the dev mock: look for the model
   status, not "mock").
2. Popup appears at the lower right of the primary monitor, above other windows, not in the
   dock/taskbar; Escape and the close button hide it; Alt+F4 hides instead of quitting.
3. Drag-resize from the top-left grip, top and left edges; the size is kept next time.
4. Show without focus when a reply finishes (`ui.show_on_reply`) does not steal typing focus,
   and a click into it focuses it.
5. Hotkey on X11, and `chatforge --show` as a shortcut on Wayland.
6. Tray: icon visible (and on GNOME with the extension), left click opens, menu works, Quit and
   Restart work, SIGTERM and Ctrl+C quit cleanly.
7. Settings window opens, raises when opened a second time; file dialogs (attach, save) work.
8. Autostart entry runs at login; the window class matches `StartupWMClass=chatforge` in the
   launcher entry (otherwise change it to the class `xprop WM_CLASS` shows).
9. `chatforge runtime install`, a CPU model load and a chat on the local model.
10. HiDPI (`GDK_SCALE=2`): popup placement and resize.
