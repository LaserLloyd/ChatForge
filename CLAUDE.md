# ChatForge — notes for coding agents

ChatForge is a Windows tray app (Python 3.12, pywebview on WebView2) with a web popup and a
Settings page in `src/chatforge/web/`. README.md describes the current state; docs/PLAN.md and
docs/research-brief.md are the original plan and brief, kept for reference.

## Commands

```
uv sync --locked --extra dev          # once
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                      # ~1 min; live NPU/MiniMax/UI tests are deselected by default
npm ci && node --test "tests/js/**/*.test.mjs"
uv run python -m chatforge.desktop.webserver --port 8765   # serve the pages in a browser (dev-mock.js)
node scripts/theme_showcase.mjs        # re-render docs/images/themes/ after a theme update
```

CI (.github/workflows/ci.yml) runs exactly the ruff, pytest and node steps above on Ubuntu and
Windows, so run them before pushing.

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
