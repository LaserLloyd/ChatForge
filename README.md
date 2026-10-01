<p align="center"><img src="docs/images/icon.png" alt="The AI Chat icon: a white speech bubble holding a blue four-pointed sparkle, on a rounded blue square" width="112"></p>

# AI Chat

[![CI](https://github.com/LaserLloyd/AI-Chat/actions/workflows/ci.yml/badge.svg)](https://github.com/LaserLloyd/AI-Chat/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A lightweight Windows desktop assistant that lives in the notification area. Press `Ctrl+Alt+C`
(or the keyboard's Copilot key) and a chat popup opens at the lower right of the screen. Your
question is answered by a small language model running **on the Intel NPU** (no account, nothing
leaves the PC), by **StudioForge** (your own GPU server on the LAN or tailnet), or by **MiniMax**,
**OpenAI**, **DeepSeek** or any OpenAI-compatible service. Answers stream in as rendered Markdown.
Attach a file (text, code, CSV, JSON, Word, Excel, PowerPoint or PDF) and the model reads it; ask
for a report and it saves one, as a Word document if you like, in `Documents\AI Chat`. The model
has nine tools: web and news search, page fetch, weather, Wikipedia, exchange rates, the current
date and time, a calculator, and one that creates documents.

**Status:** v0.1.0. Windows 11 only; built and tested on an Intel Core Ultra 5 226V (Lunar Lake)
with its "Intel AI Boost" NPU. CI runs the Python unit tests on Windows and Ubuntu and the JS
tests on Node 24. Questions and bug reports: [Contact](#contact).

---

## Overview

### In plain English

<p align="center"><img src="docs/images/how-it-works.svg" alt="AI Chat in plain English: you press Ctrl+Alt+C, the Copilot key or click the tray icon and a popup opens at the lower right; AI Chat sends the question to the model you chose with the text of any files you attach, gives it live internet tools, looks live data up first for the small NPU model, replaces a refusal with a real lookup and can retry elsewhere if the NPU model fails; the answer comes from Qwen2.5-1.5B on your NPU, from your StudioForge GPU server, or from MiniMax, OpenAI, DeepSeek or any OpenAI-compatible server; nine tools (web search, news search, page fetch, weather, Wikipedia, exchange rates, date and time, calculator, create a document) let the model look things up and save files in Documents\AI Chat" width="1100"></p>

Three ideas: a hotkey opens a chat from any app and Escape puts it away; the everyday model runs on
the laptop's NPU, so a quick question costs no account, no per-token bill and no data leaving the
machine; and when a question needs more than a 1.5B model can give, a bigger one (your own GPU
server or a cloud provider) is one click away in the same menu. The rest of this section is the
technical version.

### Technical brief

**Why it exists.** Core Ultra laptops ship with an NPU that sits idle, and the quick questions of a
working day (what is the weather, what is 18% of this, what does that page say) do not need a
browser tab or a cloud account. AI Chat puts a chat one keystroke away, runs a small model on the
NPU through [OpenVINO Model Server](https://github.com/openvinotoolkit/model_server), and gives it
the tools a small model needs to be useful: live search, weather, page fetch and a calculator. The
same popup talks to bigger models when you want them, and those have room for long attached files
and can write documents for you.

**What it is not.** It is not an agent. The tools look things up (search, fetch, weather,
calculate); the one that writes, `create_document`, can only add a new file to its documents
folder (`Documents\AI Chat` by default) and never overwrites one, and no tool can run a program or
change a setting.
It keeps one conversation, not a history of chats, and it does not summarise: when a chat outgrows
the model's context window, the oldest turns are left out of the prompt. It needs no admin rights
and sends nothing anywhere except to the provider you chose and the public services its tools
call.

The design lifts working pieces from the owner's other projects: the process supervisor,
downloader, tray, autostart and logging from **StudioForge**, the chat UI, Markdown pipeline, key
card and the OpenAI and DeepSeek presets from **DisPatch_Chat**, the streaming client from
**CrucibleForge** and the `ui-theme` bundle from **UnifyingTheme**. Every adapted module says so
at the top.

### Components

| Component | Runs on | Role |
| --- | --- | --- |
| **Tray app** (`pythonw -m aichat`, one process) | Your PC, as your user | Notification-area icon with a status dot, the global hotkey, single-instance guard, its own taskbar icon, and two windows: the popup and Settings (pywebview on WebView2). Starts at login, hidden; restarts itself from the tray. |
| **Chat engine** (`chat/engine.py`) | Inside the app, on its asyncio core thread | Streams each answer, runs up to 6 rounds of tool calls per message, looks live data up early for the small model, recovers from refusals, falls back to another provider, fits long conversations to the model's context window. |
| **Attachments** (`attachments.py`) | Inside the app | Reads the files you attach as text: plain text and code, CSV, JSON, HTML, Word, Excel and PowerPoint with the standard library, PDF with the optional `pypdf`. See [Files and documents](#files-and-documents). |
| **OpenVINO Model Server** (`ovms.exe` 2026.4) | Your PC, a child process under a kill-on-close job object | Serves the local model on the NPU (or GPU or CPU). Installed by `aichat runtime install`, started on the first question, unloaded after 10 idle minutes. |
| **Local model** | Your PC, `%LOCALAPPDATA%\AIChat\models` | `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` by default, downloaded from Hugging Face in Settings > Models. Compiled once for the NPU and cached. |
| **StudioForge** | Your GPU server on the LAN or tailnet (`http://<host>:1234/v1`) | The owner's own OpenAI-compatible LLM server; any model it serves. Key optional. |
| **Cloud providers** | MiniMax, OpenAI, DeepSeek, or any OpenAI-compatible URL you add | Bigger models, with your API key from the Windows Credential Manager. |
| **Tools** (`tools/`) | Inside the app; they call public services | Nine tools, each shown as a chip in the reply: eight read-only lookups and `create_document`, which saves a file in `Documents\AI Chat`. See [Tools](#tools). |

### How a question flows

1. `Ctrl+Alt+C` (or a tray click) shows the popup. The question goes to the provider and model on
   the header chip, with the text of any files you attached.
2. For the local model, the first question loads it: about 45 s the very first time (the NPU
   compile, with a countdown in the header), about 3 s from the compile cache after that.
3. The conversation is fitted to the model's context window. If it no longer fits, the oldest
   turns are left out (nothing is summarised) and a divider in the popup marks where the model's
   view starts.
4. If the question clearly needs live data (weather, news, prices) and the local model is
   answering, the matching tool runs **before** the model's first round, so the model answers from
   the result instead of guessing.
5. The model streams its answer and may call tools itself. Each call appears as a chip with a
   result summary, and a document the model saved appears as a card with **Open** and **Show in
   folder**. A message can use up to 6 rounds of tool calls (`chat.max_tool_rounds`); a repeated
   call is refused so a small model cannot loop.
6. If any model replies "I can't access real-time data" (or promises to look something up and
   does not), that reply is dropped, the engine runs the lookup and the model answers again.
7. If the local model cannot load, crashes or times out and a fallback is set in Settings >
   General, the message is sent again to that provider, and the reply says which one answered.
8. If you put the popup away while it was answering, it comes back when the reply finishes,
   without taking the focus from what you are doing.
9. After 10 idle minutes the local model unloads (the tray dot turns grey), so an idle AI Chat
   holds no model memory.

### Why it is worth running

- **One keystroke from any app.** `Ctrl+Alt+C` toggles the popup, and so does a click on the tray
  icon. Escape or a click elsewhere hides it; a pin keeps it open. The Copilot key works too, with a
  PowerToys remap ([below](#the-copilot-key)).
- **A model that costs nothing to ask.** Qwen2.5-1.5B on the Intel AI Boost NPU: 42–51 tokens/s,
  about 3 s to load from cache, no account, and nothing leaves the PC.
- **Small model, real answers.** The local model gets a compact toolset sized for its 4096-token
  window, live data is looked up before it answers, and a refusal is replaced by a lookup.
- **Bigger models on the same menu.** StudioForge, MiniMax, OpenAI, DeepSeek and custom
  OpenAI-compatible providers. The model menu lists only the providers you have set up, and each
  provider's model list is re-read from its own API once a day.
- **Files in, documents out.** Pick, drop or paste up to 10 files (Word, Excel, PowerPoint, PDF,
  text, code, CSV, JSON) and ask about them; ask for a report and get a `.docx` (or `.md`, `.csv`,
  …) with an **Open** button.
- **Long chats keep going.** When a conversation outgrows the model's context window, the oldest
  turns are left out instead of the request failing, and a divider shows where the model's view
  starts. Each model has its own window: 1M tokens on MiniMax, whatever StudioForge reports for
  each of its models.
- **A safety net for the local model.** Pick a fallback provider and a failed NPU load, crash or
  timeout still gets an answer.
- **Your assistant, your rules.** A personality (the system prompt) and standing instructions
  (facts about you, how you like answers) go with every message.
- **Keys stay out of files.** API keys live in the Windows Credential Manager (or an environment
  variable you name), never in `config.toml`, and the log redactor knows them.
- **No admin rights.** Everything installs under `%LOCALAPPDATA%\AIChat`, and start at login is a
  per-user Task Scheduler task.

### Where to go next

| You want to… | Go to |
| --- | --- |
| See it | [What it looks like](#what-it-looks-like) |
| Install it | [Install](#install) · [step-by-step guide](docs/SETUP.md) |
| Learn the popup, the tray and the hotkeys | [Usage](#usage) |
| Know what happens when a chat gets long | [Long conversations](#long-conversations) |
| Use StudioForge or a cloud model | [Providers](#providers) · [context windows](#context-windows) |
| Know what the model can look up | [Tools](#tools) |
| Attach files or get a document back | [Files and documents](#files-and-documents) |
| Change a setting | [Settings](#settings) |
| Pick a local model | [Local models](#local-models) |
| Work on the code | [Development](#development) · [Architecture](#architecture) |
| Fix a problem | [Troubleshooting](#troubleshooting) |
| Ask a question or report a bug | [Contact](#contact) |

---

## What it looks like

The popup is 420 × 620 at the lower right of the screen, above the taskbar. Every answer says which
model wrote it and how fast:

| A weather question, answered on the NPU | A spreadsheet in, a Word report out |
| --- | --- |
| ![The chat popup in the dark LaserLloyd theme. The header chip reads "Local (NPU) · Qwen2.5-1.5B-Instruct-int4-ov" with a green ready dot, beside the pin, Clear chat (a circular arrow), Settings and Close buttons, and a status line says "Unloads in 10 min". The user asks "Will it rain in Lisbon this weekend?"; a tool chip shows weather "Lisbon" done, "Weather: Lisbon, Portugal"; the answer is a short paragraph and a Markdown table of Saturday and Sunday with sky, temperature and chance of rain, signed "Qwen2.5-1.5B-Instruct-int4-ov · 47.6 tok/s". The composer has a paperclip button for attaching files](docs/images/chat-weather.png) | ![The popup with MiniMax-M3 selected. The user's message carries a file chip, "Q3 sales.xlsx, 47 KB · 12.8k chars", above the text "Summarise this sheet as a one-page Word report."; the reply has a create_document tool chip reading "Saved Q3 sales summary.docx", a short bulleted summary of revenue, best month and top product, and a document card for "Q3 sales summary.docx" (DOCX · 9 KB) with Open and Show in folder buttons, signed "MiniMax-M3 · 61.8 tok/s"](docs/images/chat-files.png) |

The model menu lists only the providers that are set up:

<p align="center"><img src="docs/images/model-menu.png" alt="The popup with the model menu open over the empty chat. Groups for Local (NPU) with Qwen2.5-1.5B-Instruct-int4-ov ticked, MiniMax with four models, and StudioForge with one model; OpenAI and DeepSeek are absent because no key is saved for them. Below a separator: Unload model and Manage models…" width="420"></p>

Settings has four tabs: **Models**, **Providers**, **General** and **Logs**.

![Settings, Models tab: the installed Qwen2.5 1.5B Instruct INT4 model with Recommended, Active and Loaded badges, 940 MB, "Compiled for NPU", and its catalog note; Set active, Unload, Clear cache and Delete buttons; a disk-usage bar for models, compile cache, runtime and logs; and the Local runtime card showing OpenVINO Model Server 2026.4.0 and the Visual C++ runtime installed](docs/images/settings-models.png)

| Providers — StudioForge on your own server | General — personality, instructions, tools and fallback |
| --- | --- |
| ![Settings, Providers tab, scrolled to the StudioForge card: base URL http://gpu-server:1234/v1, the model picker, a context window left on Auto with the note "Qwen3.8-27B-Q5_K_S reports 66k tokens (from Refresh models)" and a longest reply of 4096 tokens, an optional API key field reading "No key (optional for this server)", and Save key, Test, Refresh models and Remove key buttons; the OpenAI card starts below it](docs/images/settings-providers.png) | ![Settings, General tab: idle unload minutes, max prompt length, the Ctrl+Alt+C hotkey, the LaserLloyd theme and show reasoning; the Personality box holding the default system prompt and the Instructions box holding two example instructions; start at login, hide the popup when it loses focus and show the popup when a reply finishes, all ticked; all nine tools ticked, Create documents among them, with a note that the small local model is offered five of them; home location "Lisbon, Portugal" with metric units; and "When the local model fails: answer with StudioForge"](docs/images/settings-general.png) |

The screenshots are of the real UI in Microsoft Edge (the engine behind WebView2), served from
`src/aichat/web` against its development mock (`static/js/dev-mock.js`) with example data: the
conversation, the spreadsheet and its figures, the location and the server name are made up.

---

## Install

You need Windows 11, Python 3.12 (the `py` launcher), [uv](https://docs.astral.sh/uv/) and Git. The
WebView2 runtime and the VC++ 2015+ x64 runtime ship with Windows 11; the NPU needs an Intel Core
Ultra with the "Intel AI Boost" driver. Node 24 is only for the JS tests. `aichat doctor` checks
all of it. The full walkthrough, with a check after every step, is
[`docs/SETUP.md`](docs/SETUP.md).

1. Clone and create the environment (`uv sync` installs pywebview, pystray, httpx, keyring, ddgs
   and the dev tools into `.venv`):

   ```powershell
   git clone https://github.com/LaserLloyd/AI-Chat.git "AI Chat"
   cd "AI Chat"
   py -3.12 -m uv sync --extra dev
   ```

2. Install the local runtime, OpenVINO Model Server 2026.4.0 `python_on` (a 139 MB download,
   SHA-256 pinned, resumable), and check the machine:

   ```powershell
   py -3.12 -m uv run python -m aichat runtime install
   py -3.12 -m uv run python -m aichat doctor
   ```

3. Start it: double-click **`launchers\AI Chat.bat`**, or

   ```powershell
   .\.venv\Scripts\pythonw.exe -m aichat --show
   ```

4. Get the model: **Settings > Models**, search `Qwen`, then **Download** on the row badged
   *Recommended*, `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` (0.94 GB). A model folder copied in by
   hand under `%LOCALAPPDATA%\AIChat\models\<publisher>\<repo>` is adopted at the next start.

5. Optional: to attach PDFs, add `pypdf` (every other kind of file is read with the standard
   library), then restart AI Chat from the tray menu (**Restart**):

   ```powershell
   py -3.12 -m uv add pypdf
   ```

No NPU? Set the device to GPU or CPU in Settings > Providers > Local (slower), or use StudioForge or
a cloud provider only.

### First run

The first start writes `%LOCALAPPDATA%\AIChat\config.toml` with the defaults, turns on **start at
login** (a per-user Task Scheduler task, "AI Chat", that runs `%LOCALAPPDATA%\AIChat\AIChat.vbs` ten
seconds after you sign in; the script starts `pythonw -m aichat --hidden`), registers the hotkey
and shows the popup. The first question loads the model: about 45 s the very first time, while
the NPU compiles it, and about 3 s from the cache after that.

---

## Usage

```powershell
.\.venv\Scripts\pythonw.exe -m aichat --show     # start and show the popup
.\.venv\Scripts\pythonw.exe -m aichat --hidden   # start in the tray (what autostart runs)
.\.venv\Scripts\pythonw.exe -m aichat --settings # start and open Settings
```

`launchers\AI Chat.bat` does the first one; `launchers\AI Chat Autostart.bat` turns start at login
on or off. Starting a second copy just brings the running popup up.

- **Popup.** `Ctrl+Alt+C` or a tray click. The header chip shows the provider and model with a
  status dot (grey unloaded, amber loading or compiling, green ready, red error) and, while
  loading, an honest countdown ("Compiling for NPU 0:12 / ~0:45" the first time, "Loading… ~3 s"
  after). Enter sends, Shift+Enter adds a line, Stop cancels, and a counter appears near the
  4000-character limit on the typed text (`chat.max_prompt_chars`; attached files have their own
  limit). The circular-arrow button (**Clear chat**) starts over with an empty conversation. With
  the local model selected, the first question after the popup opens loads it automatically.
- **Attaching files.** The paperclip opens the Windows file dialog; you can also drop files
  anywhere on the popup or paste them. Each file becomes a chip above the message. See
  [Files and documents](#files-and-documents).
- **Model menu.** Click the header chip. It lists the local models and every provider that can
  answer now: StudioForge (no key needed) and each provider with a saved or environment key. The
  provider in use stays listed even without a key. At the bottom: Load or Unload model (when the
  local model is selected) and Manage models…, which opens Settings.
- **Replies.** Tables, fenced code with copy buttons and syntax highlighting, footnotes and
  callouts, sanitised with DOMPurify. Tool calls appear as chips with a result summary; a
  reasoning block (for models that produce one) is collapsed under the reply. A document the
  model saved appears as a card under the reply.
- **When a reply finishes.** If the popup was put away while the model was answering, it comes
  back in its corner when the reply finishes or fails, without taking the focus, and stays up
  until you click into it; the hotkey or a tray click then brings it to the front. A reply you
  stopped does not bring it back. Turn this off with Settings > General > *Show the popup when a
  reply finishes*.
- **Tray menu.** Open chat, Settings, **Load model** (while the local model is neither loaded nor
  loading), **Unload models** (frees the local model or cancels its load, whichever provider is
  selected), Open logs folder, Start at login, **Restart** and Quit. Restart shows "Restarting AI
  Chat…", starts a new copy hidden in the tray and quits this one the normal way; if the new copy
  cannot start, a tray message says why and this one keeps running. Quit unloads the model and
  ends `ovms.exe`.

### The Copilot key

The Copilot key sends `Win+Shift+F23`, which Windows registers for itself, so no app can take it
as a hotkey, and the Settings picker for that key only lists packaged, signed apps. PowerToys
Keyboard Manager catches it first: remap the shortcut `Win (Left) + Shift (Left) + F23` to
`Ctrl+Alt+C` and turn on PowerToys *Run at startup*. The steps are in
[`docs/SETUP.md` §8](docs/SETUP.md#8-open-ai-chat-with-the-copilot-key-optional).

### Long conversations

Every model has a context window, the most it can read at once (see
[Context windows](#context-windows)). Before each request AI Chat fits the conversation into it.
Nothing is summarised, so fitting costs no extra model call, only a size estimate:

- **Always sent:** the personality and instructions, your new message, and this turn's tool calls
  and results (long results are shortened to share the room).
- **Left out first:** old tool results are cut to 300 characters, then the oldest whole turns are
  left out. The most recent turn with attached files goes last, so follow-up questions about a
  file still work.
- **What you see:** a divider above the first message the model still sees, reading "Older
  messages are past the context window (too long for context)". The model is told that earlier
  messages were cut, so it asks rather than guesses. The divider survives a restart and goes away
  with Clear chat.
- **A message too long on its own:** its attached files are shortened first, each with a note,
  then the end of the message is cut. The model sees "… (Too long for context: the rest of this
  message was cut.)" and the message's time line in the popup shows *(Too long for context)*.
- **Errors:** a message is refused when the personality, instructions and tool list fill the
  whole window (the hint says to shorten them or switch to a model with a bigger window), or, more
  rarely, when this turn's own tool calls and results do not fit even with each result cut to 120
  characters (the hint says to start a new chat, ask for less or switch to a cloud model). If a
  server still says the prompt is too long, AI Chat leaves out the older half of the remaining
  turns and tries once more.

---

## Providers

| Provider | Base URL | API key | Default model |
| --- | --- | --- | --- |
| **Local (NPU)** | OVMS on `127.0.0.1`, started by the app | none | `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` |
| **StudioForge** | `http://localhost:1234/v1` by default; point it at your GPU server (`http://<host>:1234/v1`) in Settings > Providers | optional (`STUDIOFORGE_API_KEY`) | any model the server lists |
| **MiniMax** | `api.minimax.io/v1` (International), `api.minimaxi.com/v1` (China) or a custom URL | `MINIMAX_API_KEY` | `MiniMax-M3` |
| **OpenAI** | `api.openai.com/v1` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| **DeepSeek** | `api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| **Custom** | any OpenAI-compatible endpoint you add in Settings | an environment variable you name, or a saved key | the models you list, or fetch with **Test** |

Each provider card in **Settings > Providers** has the base URL (MiniMax: the region), a model
picker with a custom-name field, the context window and the longest reply in tokens, the key,
**Test** (which connects, reports the latency and offers the server's model list) and **Refresh
models**. The OpenAI and DeepSeek presets come from DisPatch_Chat and link to where you get a key.

### Model lists

Providers add models over time. **Refresh models** on a card, or **Refresh all model lists** at the
top of the Providers tab, reads each provider's own `GET /models` with its saved key (providers
without a key are skipped). Models still offered keep your order, new ones are added, ones that are
gone are dropped (a default that went becomes the first model), and non-chat models (embeddings,
speech, images) are left out. *Refresh automatically once a day* is on by default
(`chat.auto_refresh_models`): a minute after AI Chat starts, then every 24 hours.

### Context windows

The context window is what a model can read at once, prompt and reply together. AI Chat keeps one
per model:

| Provider | Context window |
| --- | --- |
| **Local (NPU)** | `local.max_prompt_len`, 4096 tokens by default (1024 to 8192; the model recompiles when it changes) |
| **StudioForge** | What the server reports for each model on **Refresh models** (the context it is loaded with), capped by the card's *Context window* when you set one |
| **MiniMax** | 1M tokens, set on the card |
| **OpenAI, DeepSeek, custom** | 100k tokens (*Auto*) unless the provider reports one or you set one |

The note under the card's field says which applies, for example "Qwen3.8-27B-Q5_K_S reports 66k
tokens (from Refresh models)" or "…; capped at 32k". On a remote provider with a known window the
prompt may use the window minus the card's *Longest reply* and a 256-token margin; on *Auto* the
prompt itself may use up to 100k tokens. On the NPU the 4096 tokens are the
prompt limit OVMS enforces, so the prompt may use all but a 128-token margin and the reply is not
counted against it.

### API keys

1. Open Settings > Providers and the provider's card (MiniMax: pick the region that issued the
   key first).
2. Paste the key, click **Test**, then **Save key**. The status line reads "Saved in Windows
   Credential Manager".
3. Choosing a provider in the popup before its key is saved shows an inline "Add your key" card;
   after Save and test the pending message is sent.

Keys are stored with `keyring` under service `AIChat`, one entry per provider. An environment
variable named on the card (`MINIMAX_API_KEY`, `OPENAI_API_KEY`, …) takes precedence over the saved
key, and the card says so. Keys are registered with the log redactor, so they never appear in
logs, config or the JS side. MiniMax error 1004 or 2049 means the key and the region do not match.

### Fallback for the local model

Settings > General > *When the local model fails* > **Answer with**: if the NPU model cannot load,
crashes or times out, the same message is sent to the provider you pick (StudioForge, say), with
its default model or the one you name. The popup says "Local model failed. Asking StudioForge…",
and the reply's footer ends "via StudioForge". The default is *Nothing (show the error)*.

---

## Tools

| Tool | What it does | Data from | Local model |
| --- | --- | --- | --- |
| `web_search` | Search the web for current facts, events and prices | DuckDuckGo (`ddgs`) | yes |
| `news_search` | Search recent news | DuckDuckGo | – |
| `fetch_url` | Fetch a page and return its text; private and local addresses are blocked (SSRF-hardened, rebinding included) | the page | yes |
| `weather` | Current conditions and a 1–7 day forecast; uses your home location when no place is named | [Open-Meteo](https://open-meteo.com) (no key) | yes |
| `wikipedia` | Summary of a Wikipedia article | Wikipedia | – |
| `exchange_rate` | Convert between currencies | ECB reference rates via [Frankfurter](https://frankfurter.dev) | – |
| `current_datetime` | Local date, time and time zone | your PC | yes |
| `calculator` | Evaluate a maths expression (a whitelisted evaluator, no `eval`) | your PC | yes |
| `create_document` | Save a file the model wrote: Markdown, text, CSV, JSON, HTML, code, or a Word `.docx` built from Markdown ([details](#documents-the-model-saves)) | your PC, into `Documents\AI Chat` | – |

Tools are switched on or off in Settings > General. The small local model has a 4096-token prompt
window and gets confused by long tool lists, so it is offered the five marked *yes*; StudioForge and
cloud models get every tool that is on.

**Figuring things out.** A 1.5B model often answers "I don't have real-time data" even with a
search tool on offer. Two rules in `chat/research.py` (plain pattern matching, no extra model call)
cover that:

- **Look it up first.** On the local model, a question with a clear live-data intent (weather,
  news, "latest", "price of", "right now") runs the matching tool before the model's first round,
  so it answers from the result.
- **Refusal recovery.** On any model, a reply with no tool calls that refuses in the first person
  ("I can't access real-time data", "my knowledge cut-off…") or promises a lookup it never makes
  ("let me search…") is dropped; the engine runs the lookup and the model answers again. The popup
  shows "Looking it up…" meanwhile.

**Home location and units.** Settings > General > *Your location* (for example a city and country)
is used for weather and "near me" questions when no place is named; *Units* switches the weather
between metric and imperial. Leave the location empty and the assistant asks.

---

## Files and documents

### Attaching files

Click the paperclip to pick files in the Windows file dialog, drop them anywhere on the popup, or
paste them. Each file is read as text when you attach it and shows as a chip with its size and
length, with a *partial* badge when only part of it is used; a file that cannot be read is listed
above the composer with the reason. A message can be files alone. The files go to the model after
your text, one `<file name="…">` block each.

| Kind | Extensions (examples) | What the model gets |
| --- | --- | --- |
| Text and code | `.txt`, `.md`, `.log`, `.py`, `.js`, `.ps1`, `.sql`, `.cs`, … | The text (UTF-8, UTF-16 or Windows-1252) |
| Data | `.csv`, `.tsv`, `.json`, `.xml`, `.yaml`, `.toml`, `.ini`, … | The text |
| Web pages | `.html`, `.htm` | The visible text |
| Word | `.docx`, `.docm` | Paragraphs in order, headings and list items marked, table rows as `cell \| cell` |
| Excel | `.xlsx`, `.xlsm` | Each sheet, named, with its rows as CSV |
| PowerPoint | `.pptx`, `.pptm` | Each slide's text |
| PDF | `.pdf` | The text of each page, **only with `pypdf` installed** |

The limits are in [`src/aichat/attachments.py`](src/aichat/attachments.py): 20 MB per file and 10
files per message. A file's text is cut at 200,000 characters (`tools.attachment_max_chars` in
`config.toml`, 1,000 to 4,000,000). The model's context window may cut it further: a cut file ends
with a note saying so, and on the local model's 4096-token window the note suggests a cloud or
StudioForge model for long files. The text is kept with the message, so you can ask follow-up
questions without attaching the file again.

Not read: pictures ("Images are not supported yet"); the older Office and OpenDocument formats
(`.doc`, `.xls`, `.ppt`, `.rtf`, `.odt`, `.ods`, `.odp`), which you save as `.docx`, `.xlsx` or
`.pptx` first; and programs, archives, audio, video and databases. PDFs need `pypdf`, which is not
installed by default. Add it and restart AI Chat from the tray menu:

```powershell
py -3.12 -m uv add pypdf
```

### Documents the model saves

With **Create documents** on (Settings > General), a StudioForge or cloud model can save a file for
you: ask for "a one-page Word report", "this table as a CSV" or "that script as a file". The small
local model is not offered this tool.

- **Where.** `Documents\AI Chat`, in the Documents folder Windows uses (a OneDrive-redirected one
  too), created on first use. Set `tools.documents_dir` in `config.toml` to use another folder.
- **Formats.** Text formats are written as UTF-8: `.md`, `.txt`, `.csv`, `.tsv`, `.json`,
  `.html`, `.xml`, `.yaml` and code (`.py`, `.js`, `.ts`, `.css`, `.sql`, `.ps1`, `.sh`). A `.csv`
  starts with a byte-order mark so Excel reads it as UTF-8. A `.docx` is built from Markdown:
  headings, paragraphs, bullet and numbered lists, tables, bold, italic and code.
- **Nothing is overwritten.** A name that is taken becomes `report (2).docx`, `report (3).docx`
  and so on. Characters Windows forbids are replaced, and a document is at most 5 MB.
- **In the chat.** The reply gets a card with the file's name, type and size. **Open** opens it
  in its usual app; scripts and unknown types open in Notepad, so opening never runs anything.
  **Show in folder** selects it in Explorer. Only files in the documents folder can be opened from
  the chat, and one that was moved or deleted says so.

---

## Settings

Settings are stored in `%LOCALAPPDATA%\AIChat\config.toml` (set `AICHAT_HOME` to move the whole
data folder; `AICHAT_<SECTION>__<KEY>` environment variables override single values). Secrets are
never written there.

| Tab | What you can change |
| --- | --- |
| **Models** | Installed models (set active, load, unload, delete, clear compile cache), disk usage, the OVMS runtime card (install, re-check), Hugging Face search with badges (*Recommended*, *Supported*, *Untested*, *Avoid*), downloads with progress, cancel and resume |
| **Providers** | Local: device (NPU, GPU, CPU) and `max_prompt_len` (reload required). Model lists: refresh all, refresh once a day. One card per provider: base URL (MiniMax: region), model, context window (with what the provider reported for the selected model), longest reply, API key with Save key, Test, Refresh models and Remove key. Add a custom OpenAI-compatible provider; custom ones can be removed |
| **General** | Idle unload minutes (0 = never), max prompt characters (200 to 4,000,000, the typed text), hotkey (text such as `Ctrl+Alt+C`; conflicts are reported), theme (LaserLloyd default, with a Light partner), show reasoning, **Personality** and **Instructions**, start at login, hide the popup when it loses focus, show the popup when a reply finishes, the nine tools, home location and units, the local-model fallback |
| **Logs** | The last 500 redacted lines with a level filter, auto-refresh, Copy and Open folder |

**Personality and instructions.** The *Personality* box is the system prompt: who the assistant is
and how it sounds (leave it empty for the default, "You are AI Chat, a concise desktop
assistant…"). The *Instructions* box holds standing directions sent with every message, such as
facts about you and how you like answers; they go last in the system prompt, under "The user's
instructions (follow them in every reply)". Each box takes up to 20,000 characters, and both count
against the model's context window, which matters most on the 4096-token local model.

Two settings are only in `config.toml`: `tools.attachment_max_chars` (the most text one attached
file contributes, 200,000 characters by default) and `tools.documents_dir` (where documents are
saved; empty means `Documents\AI Chat`).

---

## Local models

| Model | NPU | Notes |
| --- | --- | --- |
| `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` | **recommended, default** | Verified on the Lunar Lake NPU (Core Ultra 5 226V): 44.5 s first compile, about 3 s cached load, 42–51 tok/s, clean `hermes3` tool calls, honours the system prompt. Compile cache 306 MiB. |
| `OpenVINO/Qwen3-4B-int4-ov` | avoid | Compiles on the NPU but produces garbage output with OVMS 2026.4 (fine on CPU). Listed as *Avoid*. |

Why the small one: the plan started with Qwen3-4B, but the NPU gate in
[`docs/RUNTIME-NOTES.md`](docs/RUNTIME-NOTES.md) showed its output is corrupt on this NPU, none of
the channel-wise `-cw-` alternatives has a tool parser in OVMS 2026.4, and Qwen2.5-1.5B is
coherent, fast and calls tools cleanly. It runs with a 4096-token static prompt window, so a long
chat loses its oldest turns sooner and a long file is cut sooner (see
[Long conversations](#long-conversations)); StudioForge and the cloud models have far more room.
It is not offered `create_document`. Any `OpenVINO/*int4-ov` Qwen export on Hugging
Face can be tried from the search box; models the catalog
([`models/catalog.toml`](src/aichat/models/catalog.toml)) does not know get an *Untested* badge.

---

## Development

```powershell
py -3.12 -m uv run ruff check .
py -3.12 -m uv run ruff format --check .
py -3.12 -m uv run pytest -q                 # unit tests (live and UI markers deselected)
npm ci; npm run -s test:js                   # Markdown, bridge, settings and chat tests in jsdom

$env:AICHAT_LIVE_NPU = "1"; py -3.12 -m uv run pytest -m live_npu -s   # real OVMS + NPU
py -3.12 -m uv run pytest -m live_minimax -s                           # needs a MiniMax key
py -3.12 -m uv run pytest -m ui -s                                     # launches the app, temp home
```

Use `py -3.12 -m uv run ...` throughout: on a stock Windows install bare `python` is the Microsoft
Store stub. The UI can be developed in a normal browser against `dev-mock.js` with
`py -3.12 -m http.server -d src\aichat\web 8765`; the magic words that drive it ("search",
"document", "overflow", "fallback", attached files and more) are listed at the top of
`dev-mock.js`. CI (GitHub Actions) runs ruff, the unit tests on Ubuntu and Windows, and the JS
tests on Node 24.

## Architecture

One process, several threads:

| Thread or process | What runs there |
| --- | --- |
| Main thread | `webview.start()`: the popup window (`index.html`) and the Settings window (`settings.html`) |
| Core thread | One asyncio loop: `chat.engine` → `llm.providers` → `runtime.manager` → `ovms_supervisor` → `ovms.exe`; the tools, `models.*` and the downloader |
| Events thread | `EventSink` → `window.run_js`, with 50 ms coalescing (Python → JS) |
| Tray, hotkey and single-instance threads | The notification-area icon and menu, the global hotkey, "bring the running copy up" |
| Static server | `127.0.0.1:<random port>` serving `src/aichat/web` with a CSP and `no-cache` |
| `ovms.exe` | A child process under a kill-on-close job object, so it cannot outlive the app |

- `src/aichat/app.py` wires it; `desktop/bridge.py` is the `js_api` contract (every call returns
  `{ok, ...}` or `{ok: false, error}`), `desktop/events.py` the event stream.
- `runtime/ovms_supervisor.py` spawns `ovms.exe`, polls readiness and records the per-model compile
  cache (`runtime/compile_cache.py`). `runtime/manager.py` serialises load, lease and unload;
  `runtime/idle.py` is the idle reaper.
- `llm/client.py` is one OpenAI-compatible SSE client for local and remote providers alike, with
  per-provider quirks for body shaping, error mapping and reasoning fields;
  `llm/model_refresh.py` reads and merges the providers' model lists.
- `chat/engine.py` is the agent loop (events `chat.start` … `chat.done`, with `chat.context` when
  the conversation is fitted), `chat/history.py` fits a conversation into a model's window,
  `chat/conversation.py` keeps and saves it, `chat/research.py` the look-it-up-first and refusal
  rules, `chat/prompts.py` the system prompt (personality, date, tool guidance, home location, your
  instructions).
- `attachments.py` reads attached files as text and holds them until the message is sent.
- `models/` holds the scan-based registry, the resumable Hugging Face downloader, search and the
  badge catalog (`catalog.toml`); `tools/` the nine tools and their registry, with
  `tools/documents.py` saving documents and building `.docx` files.
- `autostart.py` registers the start-at-login task; `desktop/icon.py` draws the app and tray icons.
- `web/` is the UI: `chat.js` (the popup, attachments, document cards and the context divider),
  `keycard.js`, `settings.js`, DisPatch's `markdown.js`, the vendored marked, DOMPurify and
  highlight.js, and the `ui-theme` bundle.

### Data folder

Everything the app writes lives outside the repo, in `%LOCALAPPDATA%\AIChat` (or `AICHAT_HOME`):

| Path | What lives there |
| --- | --- |
| `config.toml` | Settings (no secrets) |
| `state.json` | Last-used times, compile records, launch hashes |
| `conversation.json` | The current conversation, with the text of attached files and where the model's view of it starts |
| `models\` | Models and their `.aichat-model.json` sidecars |
| `runtime\` | OVMS and its downloaded zip |
| `cache\ov\` | NPU compile blobs (about 306 MiB per model) |
| `logs\` | `aichat.log` (the app) and `ovms.log` (the model server), rotated |
| `webview\` | The WebView2 profile |
| `AIChat.vbs` | The script the start-at-login task runs |
| `app-icon-v1.ico` | The window and taskbar icon, drawn on first start |

Documents the model saves go to `Documents\AI Chat`, outside this folder.

Start at login is the "AI Chat" logon task in Task Scheduler (per user, no admin), in
[`autostart.py`](src/aichat/autostart.py). Ten seconds after you sign in, on battery too, it runs
`wscript.exe` on `%LOCALAPPDATA%\AIChat\AIChat.vbs`, which starts the app hidden in the tray. If Task
Scheduler refuses the task, a copy goes in your Startup folder
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`) instead. Earlier versions always
used that folder; `aichat autostart enable` (or turning start at login off and on) replaces it
with the task.

## Troubleshooting

- `py -3.12 -m uv run python -m aichat doctor` prints a pass/warn/fail table: Python, WebView2,
  VC++, the NPU device, OVMS, model completeness, the compile cache, the keyring backend, whether a
  MiniMax key exists (true/false only), the hotkey, autostart, free disk and whether the app is
  running. It exits 1 on any FAIL and names the fix.
- Logs: `%LOCALAPPDATA%\AIChat\logs\aichat.log` and `ovms.log`, or Settings > Logs. Secrets are
  redacted.
- "Ctrl+Alt+C is in use by another app": change the hotkey in Settings > General.
- The Copilot key opens Windows Search or Copilot instead of the popup: PowerToys is not running
  (see [The Copilot key](#the-copilot-key)).
- The model fails to load after a driver update or a sleep/resume: Settings > Models > Clear
  cache, then Load. Setting a fallback provider keeps you answered meanwhile.
- The local model stops using tools, loops or answers badly in a long chat: that is the small 1.5B
  model losing track of a long context. Press **Clear chat** (the circular arrow), or switch to
  StudioForge or a cloud model for harder questions. To read a page, say "Fetch <url> and
  summarise it" rather than just "Summarise <url>".
- A divider says older messages are past the context window: the chat has outgrown the model's
  window, so its oldest turns are no longer sent. Press **Clear chat** to start fresh, or switch to
  a model with a bigger window ([Long conversations](#long-conversations)).
- A PDF will not attach ("Reading PDFs needs the pypdf package"): run
  `py -3.12 -m uv add pypdf`, then **Restart** from the tray menu. A picture will not attach:
  images are not supported yet. An old `.doc`, `.xls` or `.ppt`: save it as `.docx`, `.xlsx` or
  `.pptx` first.
- A model you know a provider offers is missing from the menu: **Refresh models** on its card in
  Settings > Providers (a provider that needs a key is skipped until one is saved).
- The popup comes back by itself when a reply finishes: that is Settings > General > *Show the
  popup when a reply finishes*.
- A stray `ovms.exe` after a crash: end it in Task Manager; the doctor reports it.
- `python` opens the Microsoft Store: use `py -3.12`, or the launchers in `launchers\`.

## Documentation

- [`docs/SETUP.md`](docs/SETUP.md) — a fresh Windows machine, step by step, with a check after each
  step; MiniMax; the Copilot key; where everything lives; uninstall
- [`docs/RUNTIME-NOTES.md`](docs/RUNTIME-NOTES.md) — the OVMS and NPU measurements behind the model
  choice and the defaults
- [`docs/PLAN.md`](docs/PLAN.md) — the design: workstreams, the bridge and event contract, and the
  file-by-file map of what was adapted from where
- [`docs/research-brief.md`](docs/research-brief.md) — the background research
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) — vendored libraries, adapted code, runtime
  downloads and data services

## Contact

AI Chat is built and maintained by **Lloyd** — [LaserLloyd.com](https://laserlloyd.com),
*"Laser and other technology projects. Free for your use."*

- Bugs, questions and ideas: [open an issue](https://github.com/LaserLloyd/AI-Chat/issues)
- Email: [Lloyd@LaserLloyd.com](mailto:Lloyd@LaserLloyd.com) <!-- scrub-ok: the maintainer's published contact address, deliberate -->
- More projects: [github.com/LaserLloyd](https://github.com/LaserLloyd)

## License

**MIT** — see [`LICENSE`](LICENSE). Use it, fork it, ship it; keep the copyright notice.

The Python dependencies are permissive too (MIT, BSD, Apache-2.0, PSF) with one exception worth
naming: `pystray`, which draws the tray icon, is **LGPL-3.0**. It is an unmodified dependency
installed by pip and imported at runtime, which is the arrangement the LGPL is written for, and it
does not reach into this project's own terms. If you redistribute a bundled or frozen build that
embeds it, the LGPL's relinking obligation is yours to satisfy. `pypdf` (BSD-3-Clause), needed
only for PDF attachments, is not installed unless you add it.

Third-party components are listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md): the
vendored marked (MIT), DOMPurify (Apache-2.0 or MPL-2.0) and highlight.js (BSD-3-Clause) in
`src/aichat/web/static/vendor/`; the code adapted from StudioForge, DisPatch_Chat and CrucibleForge
(MIT, LaserLloyd); the private UnifyingTheme `ui-theme` bundle (the owner's private licence, to be
reviewed before any public release); OpenVINO Model Server and the Qwen models, which are
downloaded at run time under Apache-2.0 and not redistributed here; and the data services the tools
call (Open-Meteo data is CC BY 4.0 and credited in every weather result, Wikipedia text CC BY-SA
4.0).
