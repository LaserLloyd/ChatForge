# AI Chat

A lightweight Windows desktop assistant that lives in the notification area. Press
`Ctrl+Alt+C` and a chat popup appears at the lower right of the screen. Ask a question, and
it is answered by a small language model running **on the Intel NPU** (no network, no
account), or by **MiniMax** in the cloud when you want a stronger model. Answers stream as
rendered Markdown, and the model can use four tools: web search, web page fetch, the
current date and time, and a calculator.

The design lifts working pieces from the owner's other projects: the process supervisor,
downloader, tray, autostart and logging from **StudioForge**, and the chat UI, Markdown
pipeline and key card from **DisPatch_Chat** (plus the streaming client from CrucibleForge
and the `ui-theme` bundle from UnifyingTheme). Every adapted module says so at the top.

## Features

- **Tray app, one hotkey.** Starts hidden at login. `Ctrl+Alt+C` toggles the popup; so does a
  click on the tray icon. Escape or a click elsewhere hides it; a pin keeps it open.
- **Local inference on the NPU.** [OpenVINO Model Server](https://github.com/openvinotoolkit/model_server)
  2026.4 runs as a supervised child process and serves `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov`
  on the Intel AI Boost NPU: about 45 s for the one-off first compile, about 3 s to load
  afterwards, 42–57 tokens/s. The model unloads after 10 idle minutes and reloads on the next
  question, so an idle AI Chat costs no memory.
- **Cloud provider.** MiniMax (`MiniMax-M3` by default), or any OpenAI-compatible endpoint
  added in Settings. Keys live in the Windows Credential Manager, never in a file.
- **Tools.** `web_search` (DuckDuckGo), `fetch_url` (with SSRF protection), `current_datetime`
  and `calculator` (a whitelisted expression evaluator). Each call shows as a chip in the reply.
- **Markdown replies.** Tables, fenced code with copy buttons and syntax highlighting,
  footnotes, callouts; sanitised with DOMPurify.
- **Settings window.** Models (search Hugging Face, download with resume, delete, compile
  cache), Providers (MiniMax region and key, custom providers), General (hotkey, theme, idle
  timeout, tools) and Logs.
- **No admin rights.** Everything installs under `%LOCALAPPDATA%\AIChat` and the per-user
  Startup folder.

## Install

Requirements: Windows 11, Python 3.12 (`py -3.12`), [uv](https://docs.astral.sh/uv/), the
WebView2 runtime (ships with Windows 11), the VC++ 2015+ x64 runtime, and an Intel Core Ultra
with the AI Boost NPU driver. `aichat doctor` checks all of this. Node 24 is only needed for
the JS tests. See [docs/SETUP.md](docs/SETUP.md) for the step-by-step version.

```powershell
cd "C:\Users\jlloy\Desktop\Projects\AI Chat"
py -3.12 -m uv sync --extra dev                     # creates .venv and installs everything
py -3.12 -m uv run python -m aichat runtime install # OVMS 2026.4.0 python_on (139 MB download)
py -3.12 -m uv run python -m aichat doctor          # environment health check
```

The default model is downloaded from Settings > Models (search `Qwen`, then Download on
`OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov`, 0.94 GB). A model folder copied in by hand under
`%LOCALAPPDATA%\AIChat\models\<publisher>\<repo>` is adopted automatically at the next start.

## Usage

```powershell
.\.venv\Scripts\pythonw.exe -m aichat --show     # start and show the popup
.\.venv\Scripts\pythonw.exe -m aichat --hidden   # start in the tray (what autostart runs)
.\.venv\Scripts\pythonw.exe -m aichat --settings # start and open Settings
```

`launchers\AI Chat.bat` does the first one; `launchers\AI Chat Autostart.bat` toggles start
at login. Starting a second copy just brings the running popup up.

- **Popup.** `Ctrl+Alt+C` or a tray click. The header shows the current model with a status
  dot (grey unloaded, amber loading or compiling, green ready, red error) and, while loading,
  an honest countdown ("Compiling for NPU 0:12 / ~0:45" the first time, "Loading ~3 s" after).
  Enter sends, Shift+Enter adds a line, Stop cancels, the counter shows the 4000-character limit.
  "New chat" clears the single conversation. The first question after the popup opens loads
  the model automatically.
- **Tray menu.** Open chat, Settings, Load or Unload model, Open logs folder, Start at login,
  Quit. Quit unloads the model and ends `ovms.exe`.
- **Reasoning and tools.** A reasoning block (for models that produce one) is collapsed under
  the reply; tool calls appear as chips with a result summary.

## Settings

Settings are stored in `%LOCALAPPDATA%\AIChat\config.toml` (set `AICHAT_HOME` to move the
whole data folder; `AICHAT_<SECTION>__<KEY>` environment variables override single values).
Secrets are never written there.

| Tab | What you can change |
|---|---|
| Models | Installed models (select, load, unload, delete, clear compile cache, disk usage), the OVMS runtime card, Hugging Face search with badges (recommended, supported, untested, avoid), downloads with progress, cancel and resume |
| Providers | Local: device (NPU, GPU, CPU) and `max_prompt_len` (reload required). MiniMax: region (International `api.minimax.io`, China `api.minimaxi.com`, custom URL), model, API key with Save, Test and Remove. Custom OpenAI-compatible providers |
| General | Idle unload minutes (0 = never), max prompt characters, hotkey (text such as `Ctrl+Alt+C`; conflicts are reported), theme (LaserLloyd default, with a Light partner), start at login, hide on blur, enabled tools, show reasoning |
| Logs | The last 500 redacted lines with a level filter, Open folder, Copy |

## MiniMax API key

1. Open Settings > Providers > MiniMax.
2. Pick the region that issued your key (International or China), paste the key, click
   **Test**, then **Save**. The status line reads "Saved in Windows Credential Manager".
3. Selecting MiniMax in the popup before a key is saved shows an inline "Add your MiniMax API
   key" card; after Save and test the pending message is sent.

The key is stored with `keyring` under service `AIChat`, user `minimax`. If `MINIMAX_API_KEY`
is set in the environment it takes precedence over the saved key, and the UI says so. Keys
are registered with the log redactor, so they never appear in logs, config or the JS side.
A 1004/2049 error means the key and region do not match.

## Models

| Model | NPU | Notes |
|---|---|---|
| `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` | **recommended, default** | Verified on the Lunar Lake NPU (Core Ultra 5 226V): 44.5 s first compile, 3 s cached load, 42–57 tok/s, clean hermes3 tool calls, honours the system prompt. Compile cache 306 MiB. |
| `OpenVINO/Qwen3-4B-int4-ov` | avoid | Compiles on the NPU but produces garbage output with OVMS 2026.4 (fine on CPU). Kept on disk, listed as "avoid". |

Why the small one: the plan started with Qwen3-4B, but the NPU gate in
[docs/RUNTIME-NOTES.md](docs/RUNTIME-NOTES.md) showed its output is corrupt on this NPU, none of
the channel-wise `-cw-` alternatives has a tool parser in OVMS 2026.4, and Qwen2.5-1.5B is
coherent, fast and calls tools cleanly. It runs with a 4096-token static prompt window, so the
history is trimmed to fit (whole turns are dropped, tool results are capped); a message that
cannot fit gives a clear error suggesting a cloud model. Any `OpenVINO/*int4-ov` Qwen export
from Hugging Face can be tried from the search box; unknown models get an "untested" badge.

## Development

```powershell
py -3.12 -m uv run ruff check .
py -3.12 -m uv run ruff format --check .
py -3.12 -m uv run pytest -q                 # unit tests (live and UI markers deselected)
npm ci; npm run -s test:js                   # Markdown, bridge and settings tests in jsdom

$env:AICHAT_LIVE_NPU = "1"; py -3.12 -m uv run pytest -m live_npu -s   # real OVMS + NPU
py -3.12 -m uv run pytest -m live_minimax -s                           # needs a MiniMax key
py -3.12 -m uv run pytest -m ui -s                                     # launches the app, temp home
```

Use `py -3.12 -m uv run ...` throughout: bare `python` is the Store stub on this machine. The
UI can be developed in a normal browser against `dev-mock.js` with
`py -3.12 -m http.server -d src\aichat\web 8765`. CI (GitHub Actions) runs ruff, the unit
tests on Ubuntu and Windows, and the JS tests on Node 24.

## Architecture

```
pythonw -m aichat  (one process)
  main thread     webview.start()  -> popup window (index.html) + settings window (settings.html)
  core thread     asyncio loop: chat.engine -> llm.providers -> runtime.manager -> ovms_supervisor -> ovms.exe
                                tools (web_search, fetch_url, clock, calculator), models.*, downloader
  events thread   EventSink -> window.run_js (50 ms coalescing)   Python -> JS
  tray / hotkey / single-instance threads
  static server   127.0.0.1:<random> serving src/aichat/web with CSP and no-cache
```

- `src/aichat/app.py` wires it; `desktop/bridge.py` is the `js_api` contract (every call returns
  `{ok, ...}` or `{ok: false, error}`), `desktop/events.py` the event stream.
- `runtime/ovms_supervisor.py` spawns `ovms.exe` under a kill-on-close job object, polls
  readiness and records the per-model compile cache (`runtime/compile_cache.py`).
  `runtime/manager.py` serialises load, lease and unload; `runtime/idle.py` is the idle reaper.
- `llm/client.py` is one OpenAI-compatible SSE client for both local and cloud, with provider
  quirks (`OvmsQuirks`, `MiniMaxQuirks`) for body shaping, error mapping and reasoning fields.
- `models/` holds the scan-based registry, the resumable Hugging Face downloader, search and
  the badge catalog (`catalog.toml`).
- `web/` is the UI: `chat.js`, `keycard.js`, `settings.js`, DisPatch's `markdown.js` and the
  `ui-theme` bundle.

Data lives outside the repo in `%LOCALAPPDATA%\AIChat`: `config.toml`, `state.json`,
`conversation.json`, `models\`, `runtime\`, `cache\ov\` (compile blobs), `logs\` and `webview\`.

## Troubleshooting

- `py -3.12 -m uv run python -m aichat doctor` prints a pass/warn/fail table: Python, WebView2,
  VC++, the NPU device, OVMS, model completeness, the compile cache, the keyring backend,
  whether a MiniMax key exists (true/false only), the hotkey, autostart, free disk and whether
  the app is running. It exits 1 on any FAIL.
- Logs: `%LOCALAPPDATA%\AIChat\logs\aichat.log` (the app) and `ovms.log` (the model server),
  or Settings > Logs. Secrets are redacted.
- "Ctrl+Alt+C is in use by another app": change the hotkey in Settings > General.
- The model fails to load after a driver update or a sleep/resume: Settings > Models >
  Clear compile cache, then Load.
- The local model stops using tools, loops or answers badly in a long chat: that is the
  small 1.5B model losing track of a long context. Press "New chat", or switch to MiniMax
  for harder questions. To read a page, say "Fetch <url> and summarise it" rather than
  just "Summarise <url>".
- A stray `ovms.exe` after a crash: end it in Task Manager; the doctor reports it.
- `python` opens the Microsoft Store: use `py -3.12`, or the launchers in `launchers\`.

## Licence and third-party notices

AI Chat is MIT licensed (see `LICENSE`). Third-party components and their licences are listed
in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md): the vendored marked, DOMPurify and
highlight.js (`src/aichat/web/static/vendor/README.md`), the code adapted from StudioForge,
DisPatch_Chat and CrucibleForge (MIT, LaserLloyd), the private UnifyingTheme `ui-theme`
bundle (review before any public release), and the OpenVINO Model Server and Qwen models,
which are downloaded at runtime under Apache-2.0 and not redistributed here.
