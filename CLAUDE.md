# ChatForge — notes for coding agents

ChatForge is a Windows tray app (Python 3.12, pywebview on WebView2) with a web popup and a
Settings page in `src/chatforge/web/`. It also has a Linux port (see Platforms). README.md describes the current state; docs/PLAN.md and
docs/research-brief.md are the original plan and brief, kept for reference.

## Commands

```
uv sync --locked --extra dev          # once (add --extra office on Windows for legacy .xls/.ppt conversion)
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                      # ~1 min; live NPU/MiniMax/UI tests are deselected by default
npm ci && node --test "tests/js/**/*.test.mjs"
uv run python -m chatforge.desktop.webserver --port 8765   # serve the pages in a browser (dev-mock.js)
node scripts/theme_showcase.mjs        # re-render docs/images/themes/ after a theme update
```

CI (.github/workflows/ci.yml) runs exactly the ruff, pytest and node steps above on Ubuntu and
Windows, so run them before pushing.

## Platforms

Windows 11 is the verified platform. Linux (X11, or Wayland through XWayland) was ported headless
and has not been run on a real desktop; docs/SETUP-LINUX.md is the user guide. The code branches
on `sys.platform` / `os.name`, and the seams are few:

- `desktop/popup.py` (and `settings_window.py`): window placement, sticky/blur and resizing use
  `desktop/win32util.py` on Windows and `desktop/xutil.py` (pywebview's GTK backend, python-xlib;
  `app.py` sets `GDK_BACKEND=x11` so Wayland runs through XWayland) on Linux. The pure placement
  maths (`win32util.place`) is shared. `tray.py` is pystray on both.
- `desktop/hotkey.py`: `RegisterHotKey` on Windows; an X11 grab (python-xlib, `_X11Grabber`) on
  Linux. With no `DISPLAY` (Wayland-only) there is no global hotkey: the user binds a desktop
  shortcut to `chatforge --show`. The hotkey value `Copilot` (`config.parse_hotkey` returns
  `((), "Copilot")`) is Windows only: `_CopilotHook`, a `WH_KEYBOARD_LL` hook that catches
  Win+Shift+F23, swallows F23 and masks the Win key-up so Start stays closed. It is unit-tested
  with simulated events but unverified on real hardware; `chatforge doctor` probes it
  (`doctor._probe_copilot`). The PowerToys remap stays the documented fallback.
- `autostart.py`: the Task Scheduler task on Windows; an XDG `.desktop` file in
  `~/.config/autostart` on Linux (`launchers/chatforge.desktop` is the template).
- `runtime/ovms_install.py` (`host_platform()`, `PLATFORM_ASSETS`: Windows zip, Ubuntu 22 and 24
  tar.gz), `runtime/ovms_supervisor.py` (`ovms_env`, the NPU driver node) and
  `runtime/jobobject.py` (job object on Windows, parent-death signal on Linux).
- Data lives in `~/.local/share/ChatForge` on Linux (`paths.app_home`). Keys go through `keyring`
  (GNOME Keyring or KWallet) on Linux.

Windows only, by design: OCR of pictures (`ocr.py`, Windows.Media.Ocr), legacy `.xls`/`.ppt`
(and the rare `.doc`) conversion through Microsoft Office COM. `pywin32` is the optional `office`
extra, imported lazily in `attachments.py`; never import it at module level. Code that touches
Windows APIs must stay behind a platform check so the Ubuntu CI job still imports it.

## UI styling: ThemeForge

UI styling uses ThemeForge 1.0.0 in `src/chatforge/web/static/ui-theme/` (read
`src/chatforge/web/static/ui-theme/README.md` before writing CSS). Both page heads carry the
theme block: `ui-theme.js` first and blocking, `ui-theme-base.css` + `ui-components.css` before
the app's CSS, `ui-theme.css` after it, `ui-components.js` deferred. Build from its tokens and
`ui-*` classes, never write colour literals, never edit that folder; update it with
`python src/chatforge/web/static/ui-theme/update.py` (`--check` reports only). The app's own
theme settings (default theme, opt-in themes, storage key) live in
`src/chatforge/desktop/theme.py`; the static server writes them onto each page.
`tests/unit/test_theme.py` fails if a bundle file was edited by hand.

## Conventions

- Python: ruff (line length 100, rules E F W I UP B SIM), `from __future__ import annotations`,
  structlog logging, no new dependencies without a reason in the commit message.
- JS: ES modules, no build step, no inline scripts (the CSP forbids them), vendored libraries in
  `web/static/vendor/` are never edited.
- Personal data never goes in the repo (see .gitignore): settings, keys, conversations, logs and
  models live under `%LOCALAPPDATA%\ChatForge`.
- Element ids in the pages are an API: chat.js, settings.js, the Python bridge and the JS tests
  all look them up. Keep them when moving things around.

## Working in parallel

The file sets are disjoint enough for several agents at once: popup (index.html body, app.css,
chat.js, chat.test.mjs), settings (settings.html body, settings.css, settings.js,
settings.test.mjs), rendering (markdown.js, util.js, their tests), Python packages, docs. Keep
the page `<head>` blocks and `desktop/theme.py` with whoever owns the theme bundle.
