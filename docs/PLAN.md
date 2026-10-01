# AI Chat implementation plan

## 0. Decisions

| Topic | Decision |
|---|---|
| Local inference | **(A) Supervise an OVMS 2026.4 child process**, using the `python_off` build. Reasons below. |
| UI shell | pywebview 6.2 (WebView2) popup and settings windows, pystray tray, ctypes `RegisterHotKey`. |
| Static assets | Served by our own tiny stdlib HTTP server on `127.0.0.1:<random>`. Headers: `no-cache`, a forced `.js → text/javascript` MIME map, and CSP. Not `file://`, because ES modules are blocked there. Not pywebview's bottle server, because Windows' registry can map `.js` to `text/plain`, which breaks modules, and ui-theme needs `no-cache`. |
| One client for all providers | `OpenAICompatClient` (raw httpx SSE, adapted from CrucibleForge). Local is simply `http://127.0.0.1:<port>/v3`. |
| Default model | `OpenVINO/Qwen3-4B-int4-ov` on NPU, `enable_thinking=false`. Fallback `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov`. |
| Default cloud | MiniMax, International region, `MiniMax-M3`. |
| Theme | UnifyingTheme `ui-theme/` copied verbatim. Default `laserlloyd`, with `data-families="true"` for the Light partner. DisPatch `theme.css` and `clawchat-compat.css` are **not** used (measured: 9 of 55 tokens covered). The chat CSS is ported with its tokens rewritten to contract names. |
| Keys | keyring, service `AIChat`, username = provider id. The env var (`api_key_env`) wins. Keys are never written to config, logs or JS. |
| Config | `%LOCALAPPDATA%\AIChat\config.toml` (pydantic-settings TOML source, written with tomli-w atomically). The home folder can be overridden with `AICHAT_HOME`. |
| Autostart | Startup-folder `AIChat.vbs` (UTF-16 LE BOM) runs `pythonw -m aichat --hidden`. On by default. No admin. |
| Web text extraction | stdlib `html.parser`, not trafilatura (too heavy). |
| Conversation | A single current conversation, persisted to `conversation.json`, with a "New chat" button. No thread list. |

### Why A (OVMS child) over B (in-process openvino-genai)

| Criterion | A: OVMS child | B: openvino-genai in process |
|---|---|---|
| Install size | 112 MiB zip on first run. Python venv stays small. | About 100–150 MB of wheels in the venv. Slightly lighter. |
| Tool calling | Built-in `--tool_parser hermes3` (plus `--reasoning_parser qwen3`) returns OpenAI `tool_calls`. This is the proven NPU demo path. | We would have to write and maintain our own `<tool_call>` parser and chat-template handling. |
| Client | The same OpenAI SSE client as MiniMax. One code path, one test harness. | A second, non-HTTP code path. |
| Unload | Killing the process tree releases NPU and shared memory deterministically. | `del pipeline`, then hope the GC and driver free it. Known to be leak-prone. |
| Crash isolation | An NPU driver fault kills only the child. The tray survives and reloads. | Takes the whole app, tray included, down. |
| Compile cache | `--cache_dir`, per model and device, so it can be cleared per model. | Same capability (`CACHE_DIR`). |
| Model switching | Stop, then relaunch with another `--model_path`. | Rebuild the pipeline. |
| Fit with owner direction | Mirrors the StudioForge supervisor, which can be copied: job object, kill tree, pumps, readiness poll. | No StudioForge code to reuse. |

"Lightweight" is about the app's footprint, and a stopped OVMS costs 0 bytes of RAM. The extra ~112 MiB of disk is acceptable. **A.**

---

## 1. Architecture

```
                         ┌──────────────── pythonw -m aichat (one process) ────────────────┐
 Tray icon (pystray thread) ──┐                                                            │
 Hotkey thread (RegisterHotKey)┤──► desktop.popup  (show/hide/place, win32util)            │
 2nd-instance "SHOW" socket ───┘         │                                                  │
                                         ▼                                                  │
  MAIN THREAD: webview.start()  ── Popup window (frameless, on top) ── index.html ─┐        │
                                 └─ Settings window (on demand)    ── settings.html┤        │
                                          ▲ js_api (desktop.bridge.Api)            │        │
                                          │ events: EventSink → evaluate_js        │        │
                                          │    (dispatch thread, 50 ms batching)   │        │
  CORE THREAD: asyncio loop (desktop.core_loop) ◄──────────────────────────────────┘        │
     chat.engine ── agent loop ── tools.ToolRegistry (web_search/fetch_url/clock/calc)      │
        │                                                                                   │
        ├─► llm.providers.ProviderRegistry                                                  │
        │      ├─ LocalOvmsProvider ─► runtime.manager (lease, NPU semaphore, IdleReaper)   │
        │      │                          └─► runtime.ovms_supervisor ─► ovms.exe child ────┼─► 127.0.0.1:<port>/v3
        │      └─ RemoteProvider(minimax, custom…) ─► secrets (env > keyring)               │
        │             └─► llm.client.OpenAICompatClient (+ quirks: ovms | minimax) ─────────┼─► https://api.minimax.io/v1
        ├─► models.registry / models.downloader / models.hf_search ─────────────────────────┼─► huggingface.co
        └─► config / paths / logging_setup (redaction) / autostart                           │
  Static server thread: desktop.webserver (127.0.0.1:<rand>, serves src/aichat/web)          │
                                                                                            │
 %LOCALAPPDATA%\AIChat\  config.toml  models\  runtime\  cache\  logs\  state.json  …       │
```

**Threads.** The main thread runs pywebview (it must). The core thread runs one asyncio loop that owns every service. The pystray icon runs `icon.run()` in a daemon thread (the win32 backend allows this). The hotkey thread runs a message loop. The dispatch thread drains the event queue into `window.evaluate_js`, which blocks, so it must never be called on the loop. `js_api` calls arrive on pywebview worker threads and forward to the loop with `asyncio.run_coroutine_threadsafe(...).result(timeout)` for short calls. Long work returns an id immediately and reports through events.

### 1.1 Repo tree (`%USERPROFILE%\Desktop\Projects\AI Chat`)

```
pyproject.toml  uv.lock  package.json  package-lock.json  README.md  LICENSE (MIT)
THIRD_PARTY_NOTICES.md  .gitignore  .github/workflows/ci.yml
docs/  research-brief.md  SETUP.md  RUNTIME-NOTES.md
launchers/  AI Chat.bat  AI Chat Autostart.bat          (adapted from StudioForge launchers/)
scripts/  ovms_bringup.ps1
src/aichat/
  __init__.py  __main__.py  app.py  doctor.py
  paths.py  config.py  secrets.py  errors.py  logging_setup.py  logfiles.py
  autostart.py  single_instance.py
  llm/      __init__.py client.py events.py thinking.py errors.py quirks.py minimax.py providers.py probe.py
  runtime/  __init__.py ovms_install.py ovms_supervisor.py jobobject.py ports.py manager.py idle.py compile_cache.py
  models/   __init__.py registry.py downloader.py hf_search.py catalog.py catalog.toml diskspace.py
  tools/    __init__.py calculator.py clock.py web_search.py fetch_url.py htmltext.py
  chat/     __init__.py engine.py history.py conversation.py prompts.py
  desktop/  __init__.py core_loop.py bridge.py events.py popup.py settings_window.py tray.py icon.py
            hotkey.py win32util.py webserver.py
  web/      index.html  settings.html
    static/ ui-theme/ (verbatim UnifyingTheme/ui-theme, VERSION 6c17ce081f8b)
            vendor/   marked.min.js purify.min.js highlight.min.js github-dark.min.css README.md (verbatim)
            js/       markdown.js util.js (verbatim DisPatch)  i18n.js (shim)  bridge.js dev-mock.js
                      chat.js keycard.js settings.js
            css/      app.css settings.css
            img/      icon.svg
tests/
  conftest.py
  fakes/  openai_server.py ovms_child.py hf_api.py keyring_backend.py clock.py
  unit/   test_*.py
  live/   test_live_npu.py test_live_minimax.py
  ui/     test_ui_smoke.py
  js/     markdown.test.mjs fixtures/*.md
```

### 1.2 Data locations (outside the repo)

```
%LOCALAPPDATA%\AIChat\                      (AICHAT_HOME overrides)
  config.toml                               settings (no secrets)
  state.json                                last_used per model, compile markers, last unload reason
  conversation.json                         current conversation (if chat.persist_conversation)
  downloads.json                            download queue/resume state
  models\<publisher>\<repo>\                model files (+ .aichat-model.json sidecar); dot-dirs ignored
  runtime\ovms-2026.4.0\ovms\ovms.exe       extracted OVMS; runtime\downloads\*.zip(.part)
  cache\ov\<model-slug>\<DEVICE>-<max_prompt_len>\   per-model OVMS --cache_dir (+ .aichat-compiled.json)
  logs\aichat.log (+.1..3)  logs\ovms.log (+.1..3)
  webview\                                  WebView2 profile (storage_path)
```

`OpenVINO\Qwen3-4B-int4-ov` is already present, with a `.cache\huggingface` folder from the HF CLI. The registry adopts it by size, and ignores dot-dirs.

### 1.3 Module responsibilities and sources

Every copied or adapted file carries `# Adapted from StudioForge src/studioforge/<path> (MIT, LaserLloyd)` (or the DisPatch/CrucibleForge equivalent) at the top, and at each lifted function.

| Module | Responsibility | Copied or adapted from | Cut |
|---|---|---|---|
| `paths.py` | `app_home()`, `Paths` dataclass, `ensure_dirs()` | StudioForge `config.py` `default_data_dir()` (env override, `%LOCALAPPDATA%` fallback) | `<repo>/data` checkout mode, XDG, LM Studio probing |
| `config.py` | pydantic-settings models, TOML load, atomic save, `update_config(patch)` returning restart-required keys | StudioForge `config.py` load/validate shape (`load_config`, "data_dir never in file" rule) | YAML, the 60 KB of GPU/planner/gateway/MCP sections |
| `logging_setup.py` | structlog plus redaction, `RING_BUFFER`, file handler | StudioForge `logging.py` (whole file: `_SECRET_KEYS`, `register_secret`, `_scrub_text`, `_redact*`, `RingBufferHandler`, `_SafeStreamHandler`, `configure_logging`, `first_time`) | the `owner=False` guest mode, uvicorn loggers. Add `x-api-key` and `api-key` to `_SECRET_KEYS`. |
| `logfiles.py` | rotation | StudioForge `logfiles.py` (`SafeRotatingFileHandler`, `rotate_if_large`, `prune_backups`) | `AppendFileHandler` |
| `errors.py` | `AppError(message, code, hint, action, details)` | StudioForge `errors.py` shape (`to_payload`) | HTTP status plumbing |
| `autostart.py` | enable, disable, status of `AIChat.vbs` | StudioForge `core/autostart.py` (`startup_dir`, `_tray_interpreter`, `_quote_for_vbs`, `_enable_windows`, `_disable_windows`, the Windows half of `status`, the UTF-16 LE BOM writer) | Linux systemd, `serve`/`open_gui` modes, `Config` coupling (argv is passed in) |
| `single_instance.py` | mutex socket `127.0.0.1:47831`. A second instance sends `SHOW\n` and exits. | StudioForge `tray/tray_app.py` `acquire_single_instance` | the tkinter "already running" box (replaced by the SHOW signal) |
| `secrets.py` | keyring get/set/delete, env precedence, `key_status` | new (DisPatch `resolve_key` idea) | none |
| `runtime/ports.py` | free-port pick | StudioForge `core/ports.py` (`port_is_bindable`, `port_has_listener`, `find_port_holder`) | watchdog, supervisor env, adoption, LM Studio hints |
| `runtime/jobobject.py` | kill-on-close job object | StudioForge `core/supervisor.py` (`WindowsChildJob`, `create_child_job`, `_load_win32`, `CREATE_SUSPENDED`, `_TRACKED_PIDS`/atexit net, `kill_process_tree`, `process_is_alive`, `process_create_time`, `describe_exit_code`, `WINDOWS_EXIT_STATUS`) | the POSIX pdeathsig shim |
| `runtime/ovms_supervisor.py` | build argv, spawn, pump logs, poll readiness, watch exit, stop | StudioForge `core/supervisor.py` (`_spawn` suspended-then-assign-then-resume, `_resume`, `_pump`, `_await_ready` loop, `_Instance.stderr_ring`/`stderr_tail`, `_drain_pumps`, `_log_child_exit`, `redact_argv`) | llama flags, features, spec/draft, VRAM, placements, identity confirm, policy checks, multi-instance map |
| `runtime/ovms_install.py` | download, verify, extract OVMS, VC++ check | the file-transfer core from `models/downloader.py` (below) | none |
| `runtime/manager.py`, `runtime/idle.py` | one local model: ensure_loaded, lease, status, unload; idle reaper | StudioForge `core/manager.py` (`_ttl_loop`, `_sweep_step`, `_sweep_ttl`: in-flight and ready checks, "sweeper must never die"); `supervisor.mark_request_start/end` and `openai_routes._forward` try/finally discipline | leases, pins, reconciler, rebalance, throughput, eviction ring, priority tiers, planner |
| `runtime/compile_cache.py` | per-model cache dir, "compiled" marker, expected load time, clear | new | none |
| `models/registry.py` | scan `models\<pub>\<repo>`, completeness check, sidecar, delete | StudioForge `core/registry.py` (`scan`/`_walk` shape, `_is_under`, `_assert_inside_model_dirs`, `delete_model(delete_files=True)`, `all`/`get`/`resolve`) | GGUF meta, mmproj pairing, adapters, virtual models, aliases, sqlite, stale/unreachable carry-over |
| `models/downloader.py` | resumable multi-file repo download with progress and cancel | StudioForge `core/downloader.py` (`DownloadProgress`, `_FileState.observe/speed_bps/eta_s/snapshot`, `_PartFile` (all), `_lock_part_fd`, `_is_transient`, `_backoff_delay`, `_retry_after_s`, `_describe`, `_file_sha256`, `_transfer_with_retries`, `_transfer_once`, `_transfer`, `_finish`, `_verify`, `_range_honoured`, `_total_size`, `_parse_retry_after`, cancel through task cancellation) | DB (use `downloads.json`, written atomically at 1 Hz), planner/`fit_verdict`/KV allowance, mmproj, GGUF, quarantine, sibling-writer wait, `TransfersDisabledError`, the hashing threshold above 2 GB on adopt (adopt by size, as SF does) |
| `models/hf_search.py` | search, and repo file tree with sizes and sha256 | StudioForge `core/hf_search.py` (`HfSearch.__init__`/`_headers`/`_get_page` (429 backoff)/`_raise_for_status`, `_retry_after_seconds`, `file_url`, `safe_filename`, `parse_hf_timestamp`) | GGUF quant parsing, logical downloads, shards, mmproj, MTP, the date-window walk |
| `models/diskspace.py` | free space, folder sizes | StudioForge `core/diskspace.py` (`_existing_ancestor`, `_usage`, `disk_report`) | queue accounting beyond one number |
| `desktop/tray.py`, `desktop/icon.py` | icon, menu, notifications | StudioForge `tray/tray_app.py` (`_load_font`, `make_icon_image` recoloured with an "AI" glyph and a 4-state dot, the `_build_menu` shape, `_spawn_thread`, `_open_path`, `_notify`, `_on_toggle_autostart`, `_on_quit`) | server supervision, adoption, watchdog, ports, MCP, API client |
| `llm/client.py`, `llm/thinking.py` | SSE streaming, tool-delta merge, think split | CrucibleForge `crucibleforge/api.py` (`split_thinking`, `strip_thinking_tags`, `_THINK_TAGS`, `_merge_tool_call_deltas`, the `stream_chat` SSE loop, stall/wall timeouts, "no SSE data in 200" check, usage and tok/s) | WrongModelError (use `verify_model=False` semantics), priority holds, VRAM classes, sync httpx (becomes an async generator) |
| `llm/errors.py`, `llm/probe.py` | error mapping, Test button | DisPatch `backend/app/llm_api.py` (`normalize_base_url`, `_provider_message`, `_status_error`, `_transport_error`, `_probe_openai` with `/models` then a one-token chat fallback) | Anthropic SDK path, bot/connect plumbing |
| `llm/minimax.py` | MiniMax quirks | new, plus DisPatch `backend/app/openclaw_text.py` `_MINIMAX_QUICK_RE`, `_MINIMAX_TOOL_XML_RE`, `_strip_minimax_tool_call_xml`, `_strip_outside_code` | none |
| `tools/*` | allowlisted tools | allowlist and `assert_tool_allowed()` pattern (MailForge `agent/tools.py`, per the brief); StudioForge `openai_routes._validate_tools` for schema validation | none |
| `web/static/js/markdown.js`, `util.js`, `vendor/*` | markdown pipeline | DisPatch, **verbatim** | nothing (see 1.9) |
| `web/static/js/chat.js`, `css/app.css` | popup UI | DisPatch `main.js` and `app.css` sections (see 1.9) | see 1.9 |
| `web/static/js/keycard.js`, `settings.js` | key card, settings | DisPatch `llm.js` (`firstRunCard`, `runTest`/`save` flow, Enter-to-test wiring) | bot/connect, i18n |

### 1.4 Key interfaces (frozen before parallel work)

```python
# paths.py
def app_home() -> Path                      # AICHAT_HOME or %LOCALAPPDATA%\AIChat
@dataclass(frozen=True)
class Paths:
    home: Path; config_file: Path; state_file: Path; conversation_file: Path
    downloads_file: Path; models_dir: Path; runtime_dir: Path; cache_dir: Path
    logs_dir: Path; webview_dir: Path
    @classmethod
    def default(cls) -> "Paths"
    def ensure_dirs(self) -> None

# config.py
class AppConfig(BaseSettings): chat: ChatCfg; local: LocalCfg; providers: dict[str, ProviderSpec]
                               tools: ToolsCfg; ui: UiCfg; startup: StartupCfg; logging: LogCfg; hf: HfCfg
def load_config(paths: Paths) -> AppConfig                 # creates the file with defaults if missing
def save_config(cfg: AppConfig, paths: Paths) -> None      # tmp + os.replace
def update_config(cfg: AppConfig, patch: dict, paths: Paths) -> tuple[AppConfig, list[str]]  # (new, restart_keys)

# secrets.py
SERVICE = "AIChat"
KeySource = Literal["env", "keyring", "none"]
def get_api_key(provider_id: str, env_name: str | None) -> tuple[str | None, KeySource]
def set_api_key(provider_id: str, key: str) -> None        # strip; reject \n or >512 chars; register_secret
def delete_api_key(provider_id: str) -> bool
def key_status(provider_id: str, env_name: str | None) -> dict   # {"source", "env_name", "env_overrides_saved": bool}

# llm/events.py
@dataclass class ToolCall: id: str; name: str; arguments: str
@dataclass class AssistantMessage:
    content: str                  # visible (think stripped, minimax XML stripped)
    reasoning: str                # merged reasoning text (display only)
    tool_calls: list[ToolCall]
    extras: dict                  # provider-verbatim fields to echo: raw_content, reasoning_details, reasoning_content
@dataclass class ContentDelta: text: str
@dataclass class ReasoningDelta: text: str
@dataclass class Completed: message: AssistantMessage; finish_reason: str | None; usage: dict; served_model: str | None; elapsed_s: float; tok_per_s: float | None
StreamEvent = ContentDelta | ReasoningDelta | Completed

# llm/client.py
@dataclass class ChatRequest: model: str; messages: list[dict]; tools: list[dict] | None
                             temperature: float | None; max_tokens: int; extra_body: dict
@dataclass class Timeouts: connect_s: float = 10; stall_s: float = 90; wall_s: float = 600
class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str | None, *, quirks: "Quirks",
                 timeouts: Timeouts = Timeouts(), transport: httpx.AsyncBaseTransport | None = None)
    def stream_chat(self, req: ChatRequest, cancel: asyncio.Event | None = None) -> AsyncIterator[StreamEvent]
    async def list_models(self) -> list[str]                # 404/405 -> []
    async def aclose(self) -> None

# llm/quirks.py
class Quirks(Protocol):
    def prepare_body(self, body: dict) -> dict
    def check_payload(self, obj: dict) -> None             # raises LLMError (base_resp etc.)
    def reasoning_from_delta(self, delta: dict) -> str | None
    def finalize(self, raw_content: str, reasoning_parts: list, tool_calls: list[ToolCall]) -> AssistantMessage
    def history_message(self, msg: AssistantMessage) -> dict   # what to echo back next turn
class GenericQuirks; class OvmsQuirks(enable_thinking: bool); class MiniMaxQuirks  # (llm/minimax.py)

# llm/errors.py
class LLMError(AppError): code: Literal["no_key","auth","region_or_key","rate_limit","quota","balance",
    "context_overflow","bad_request","not_found","unreachable","timeout","stalled","server",
    "model_loading_failed","cancelled"]; retryable: bool; action: Literal["add_key","open_settings","retry",None]

# llm/providers.py
class ProviderSpec(BaseModel): id; kind: Literal["ovms","openai"]; display_name; base_url: str | None
    region: Literal["international","china","custom"] | None; api_key_env: str | None
    models: list[str]; default_model: str | None; quirks: list[str]; supports_tools: bool = True
    max_output_tokens: int = 2048; temperature: float | None = None; extra_body: dict = {}
    timeouts: Timeouts = Timeouts(); builtin: bool = False
class Provider(Protocol):
    spec: ProviderSpec
    async def client_for(self, model: str, on_status: Callable[[dict], None]) -> OpenAICompatClient
    def key_status(self) -> dict
    def budget(self, model: str) -> "PromptBudget"        # local: token-limited; cloud: large
class ProviderRegistry:
    def list(self) -> list[ProviderSpec]; def get(self, pid: str) -> Provider
    def upsert(self, spec: ProviderSpec) -> None; def remove(self, pid: str) -> None  # refuses builtin
SEED_PROVIDERS: dict[str, ProviderSpec]
MINIMAX_BASE = {"international": "https://api.minimax.io/v1", "china": "https://api.minimaxi.com/v1"}

# llm/probe.py
async def test_provider(spec: ProviderSpec, api_key: str | None, model: str | None) -> dict
    # {"ok", "models": [...], "latency_s", "error", "hint", "code"}

# runtime/ovms_supervisor.py
@dataclass class LaunchSpec: model_id: str; model_path: Path; device: str; max_prompt_len: int
    cache_dir: Path; tool_parser: str | None; reasoning_parser: str | None; port: int
    enable_prefix_caching: bool; extra_args: list[str]; log_path: Path
def build_argv(exe: Path, spec: LaunchSpec) -> list[str]   # pure
def ovms_env(ovms_dir: Path, base: Mapping[str, str]) -> dict[str, str]   # mirrors setupvars.ps1
class OvmsSupervisor:
    async def start(self, spec: LaunchSpec, on_tick: Callable[[str, float], None]) -> str  # returns base_url …/v3
    async def stop(self, timeout_s: float = 10) -> None
    def is_alive(self) -> bool; def stderr_tail(self, n: int = 40) -> list[str]
    exit_event: asyncio.Event

# runtime/ovms_install.py
OVMS_VERSION = "2026.4.0"; ASSET = "ovms_windows_2026.4.0_python_off.zip"; ASSET_BYTES = 117_195_695
def runtime_status(paths: Paths, cfg: LocalCfg) -> dict      # {"installed", "version", "variant", "exe", "vcredist"}
def vcredist_present() -> bool
async def install(paths: Paths, cfg: LocalCfg, progress: Callable[[dict], None], cancel: asyncio.Event) -> Path

# runtime/manager.py
RuntimeState = Literal["not_installed","unloaded","starting","compiling","ready","unloading","error"]
class LocalModelManager:
    async def start(self) -> None; async def aclose(self) -> None   # aclose unloads
    async def ensure_loaded(self, model_id: str) -> str              # base_url; serialised; switches model
    @asynccontextmanager
    async def lease(self, model_id: str) -> AsyncIterator[str]       # in_flight++, NPU Semaphore(1), touch
    async def unload(self, reason: str = "user") -> None
    def status(self) -> dict
    def subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]
# runtime/idle.py
class IdleReaper:
    def __init__(self, manager, ttl_s: Callable[[], float], *, clock=time.monotonic,
                 sleep=asyncio.sleep, interval_s: float = 15)
    async def run(self) -> None; async def sweep_once(self) -> bool

# models
@dataclass class ModelRecord: id: str; path: Path; size_bytes: int; complete: bool; missing: list[str]
    catalog: CatalogEntry | None; last_used_at: float | None; compiled: dict[str, bool]
class Registry: def scan(self) -> list[ModelRecord]; def get(self, mid: str) -> ModelRecord | None
                def delete(self, mid: str) -> list[Path]   # refuses loaded/downloading (callers check)
class HfSearch: async def search(self, q: str, *, author: str | None, limit: int = 30) -> list[dict]
                async def repo_files(self, repo_id: str) -> list[RepoFile]   # path, size, sha256|None
class Downloader: async def enqueue(self, repo_id: str) -> str; async def cancel(self, gid: str, *, delete_partial=True)
                  async def resume(self, gid: str); def all(self) -> list[dict]
                  def subscribe(self, cb) -> Callable[[], None]

# tools/__init__.py
@dataclass class ToolResult: ok: bool; content: str; summary: str
class ToolNotAllowed(AppError): ...
def assert_tool_allowed(name: str, enabled: Iterable[str]) -> None
class ToolRegistry:
    def schemas(self, enabled: Iterable[str]) -> list[dict]
    async def call(self, name: str, arguments_json: str, *, enabled: Iterable[str], max_chars: int) -> ToolResult

# chat/engine.py
class ChatEngine:
    async def send(self, text: str, request_id: str, emit: Callable[[dict], None]) -> None
    def cancel(self, request_id: str) -> None
    def new_chat(self) -> None
    def snapshot(self) -> dict               # for get_state
# chat/history.py
@dataclass class PromptBudget: max_prompt_tokens: int | None; chars_per_token: float = 3.0; tool_result_chars: int
def fit_messages(system: dict, history: list[dict], tools: list[dict], budget: PromptBudget) -> list[dict]  # raises LLMError(context_overflow)
```

### 1.5 JS ↔ Python bridge contract (`desktop/bridge.py` `Api`, exposed as `js_api`)

`Api` must have **no public attributes other than these methods**, because pywebview walks public attributes. Services are held in `_`-prefixed fields. Every method returns JSON: `{"ok": true, ...}` or `{"ok": false, "error": {code, message, hint, action}}`.

| Method | Window | Returns / effect |
|---|---|---|
| `get_state()` | both | `{config (UI subset), providers:[{id, display_name, kind, models, default_model, region, key: key_status}], selected:{provider, model}, runtime: status, conversation: [...], limits:{max_prompt_chars}, theme}` |
| `send_message(text)` | popup | `{request_id}`. Validates length. Progress arrives as events. |
| `stop_generation(request_id)` | popup | cancels |
| `new_chat()` | popup | clears the conversation |
| `select_model(provider_id, model_id)` | both | persists `chat.provider/model`. Warms up local if `local.autoload_on_open`. |
| `load_model()` / `unload_model()` | both | `{ok}`. Status through `runtime.status`. |
| `hide_popup()` / `set_pinned(bool)` / `open_settings()` / `open_external(url)` | popup | window ops. `open_external` accepts http(s) only. |
| `save_api_key(provider_id, key)` | both | stores in keyring and returns `{ok, key: key_status}`. **Never echoes the key.** |
| `remove_api_key(provider_id)` | settings | `{ok, key}` |
| `test_provider(provider_id, key_or_null, model_or_null)` | both | `{ok, models, latency_s, error?, hint?}`. A typed key is used transiently and not stored. |
| `get_settings()` / `update_settings(patch)` | settings | `{ok, config, restart_required:[...], errors:{field: msg}}` |
| `list_providers()` / `upsert_provider(spec)` / `remove_provider(id)` | settings | provider CRUD. `local-npu` cannot be removed. |
| `list_models()` / `delete_model(id)` / `clear_compile_cache(id)` | settings | registry ops. Delete refuses a loaded or downloading model. |
| `search_models(query, author_or_null)` | settings | `[{id, downloads, likes, last_modified, badge: recommended|supported|untested|avoid, note}]` |
| `repo_details(repo_id)` | settings | `{files, total_bytes, fits_disk, free_bytes}` |
| `start_download(repo_id)` / `cancel_download(id)` / `resume_download(id)` / `list_downloads()` | settings | `{download_id}` and progress events |
| `runtime_install()` / `runtime_status()` | settings | OVMS install with progress events |
| `get_logs(n, level)` / `open_logs_folder()` | settings | ring-buffer tail (already redacted) |
| `get_autostart()` / `set_autostart(bool)` | settings | `AutostartStatus.describe()` |
| `set_hotkey(spec)` | settings | `{ok, error?}` (for example "Ctrl+Alt+Space is in use by another app") |
| `disk_usage()` | settings | `{models, cache, runtime, logs, free}` in bytes |

**Events (Python → JS).** `EventSink` queues `{type, ...}`. The dispatch thread runs `window.evaluate_js("window.__aichat && window.__aichat.emit(<json>)")` for each open window. `chat.delta` is coalesced per request at 50 ms, so reasoning and content text are concatenated.

| type | fields |
|---|---|
| `chat.start` | `request_id, provider, model` |
| `chat.phase` | `request_id, phase: loading_model|thinking|calling_tool|generating` |
| `chat.delta` | `request_id, content?, reasoning?` |
| `chat.tool_call` | `request_id, call_id, name, arguments` (arguments truncated to 300 chars) |
| `chat.tool_result` | `request_id, call_id, name, ok, summary` |
| `chat.done` | `request_id, finish_reason, usage, elapsed_s, tok_per_s` |
| `chat.error` | `request_id, code, message, hint, action: add_key|open_settings|retry|null` |
| `runtime.status` | `state, model_id, device, elapsed_s, expected_s, first_compile, idle_timeout_s, unload_at, error` (1 Hz while starting or compiling) |
| `download.progress` | `DownloadProgress.to_dict()` plus `group_id, repo_id` |
| `runtime.install` | `status, downloaded_bytes, total_bytes, speed_bps, eta_s, error` |
| `key.status` | `provider_id, source, env_name` |
| `settings.changed` | `config` (so the other window re-applies theme and limits) |
| `popup.shown` | (JS focuses the composer) |

`bridge.js` waits for `pywebviewready`, then exposes `api.call(name, ...args)` and `on(type, fn)`. When `window.pywebview` is absent it loads `dev-mock.js`, so the UI can be built in a normal browser with `py -3.12 -m http.server -d src\aichat\web 8765`.

### 1.6 Streaming contract (engine loop)

1. `engine.send` checks `len(text) <= chat.max_prompt_chars`, emits `chat.start`, and resolves the provider.
   - If a cloud provider has no key: `chat.error{code:"no_key", action:"add_key"}` and return. The popup shows the key card and keeps the composer text.
   - If local: emit `chat.phase: loading_model`, then `await manager.ensure_loaded(model)`. The status events carry compile ETA.
2. Build messages. The system prompt is `prompts.system(now)` (short, includes the date). Add the history (serialised through `quirks.history_message`) and the new user turn. Then `fit_messages()` against the provider's budget.
3. For local requests, `async with manager.lease(model) as base_url:`. Then `async for ev in client.stream_chat(req, cancel)`:
   - `ReasoningDelta` → `chat.delta{reasoning}` (the first one also emits `chat.phase: thinking`)
   - `ContentDelta` → `chat.delta{content}`
   - `Completed` → append `quirks.history_message(msg)` to the conversation
4. If `msg.tool_calls` is non-empty and the round is below `chat.max_tool_rounds` (default 4):
   - For each call: `assert_tool_allowed`, then `chat.tool_call`, then `ToolRegistry.call` (max_chars depends on local or cloud), then `chat.tool_result`. Append `{"role":"tool","tool_call_id","content"}`.
   - Go back to step 2 without a new user turn.
   - Stop early if an identical `(name, arguments)` repeats from the previous round.
5. `chat.done`, then persist `conversation.json`. Every exception becomes `chat.error` (an `LLMError`'s fields, or `server` with "see logs"). A cancel gives `code:"cancelled"`, and the partial text is kept.

### 1.7 `config.toml` schema and defaults

```toml
schema_version = 1

[chat]
provider = "local-npu"
model = "OpenVINO/Qwen3-4B-int4-ov"
system_prompt = "You are AI Chat, a concise desktop assistant. Answer briefly. Use tools only when they help (current facts, web pages, dates, arithmetic)."
max_prompt_chars = 4000        # composer limit ("max prompt length"); UI counter + server check
max_tool_rounds = 4
temperature = 0.7              # local; cloud uses provider value
max_output_tokens = 1024       # local
show_reasoning = "collapsed"   # collapsed | hidden
persist_conversation = true

[local]
device = "NPU"                 # NPU | GPU | CPU   (change => reload + new compile)
idle_unload_minutes = 10       # 0 = never
max_prompt_len = 4096          # OVMS --max_prompt_len (NPU static prompt window; ceiling ~8192)
enable_thinking = false        # chat_template_kwargs for Qwen3
autoload_on_open = true        # warm the local model when the popup opens (local provider only)
load_timeout_s = 900
ovms_version = "2026.4.0"
ovms_variant = "python_off"    # python_on if WS1 finds python_off can't render templates/tools
port = 0                       # 0 = auto (first bindable from 18611)
enable_prefix_caching = true
extra_args = []                # e.g. ["--plugin_config", "{\"NPUW_LLM_GENERATE_HINT\":\"BEST_PERF\"}"] if WS1 validates it

[providers.local-npu]
kind = "ovms"
display_name = "Local (NPU)"
builtin = true
quirks = ["ovms"]

[providers.minimax]
kind = "openai"
display_name = "MiniMax"
region = "international"       # international -> api.minimax.io ; china -> api.minimaxi.com ; custom -> base_url
base_url = "https://api.minimax.io/v1"
api_key_env = "MINIMAX_API_KEY"
models = ["MiniMax-M3", "MiniMax-M2.7-highspeed", "MiniMax-M2.7", "MiniMax-M3.1-Flash-Preview"]
default_model = "MiniMax-M3"
quirks = ["minimax"]
supports_tools = true
max_output_tokens = 4096
temperature = 1.0
[providers.minimax.extra_body]
reasoning_split = true
[providers.minimax.timeouts]
connect_s = 10
stall_s = 90
wall_s = 600

[tools]
enabled = ["web_search", "fetch_url", "current_datetime", "calculator"]
web_search_max_results = 5
web_search_min_interval_s = 2.0
fetch_max_bytes = 1000000
fetch_timeout_s = 10
tool_result_max_chars_local = 1500
tool_result_max_chars_cloud = 6000
block_private_addresses = true

[ui]
theme = "laserlloyd"
themes = ["laserlloyd", "laserlloyd-light", "midnight-gold", "glacier", "forest", "paper", "daylight", "purple"]
hotkey = "Ctrl+Alt+Space"
hide_on_blur = true
width = 420                    # logical px
height = 620
margin = 12

[startup]
autostart = true               # applied on first run; the toggle writes/removes AIChat.vbs

[logging]
level = "INFO"
max_bytes = 5000000
backup_count = 3

[hf]
endpoint = "https://huggingface.co"
default_author = "OpenVINO"
```

Env override: `AICHAT_<SECTION>__<KEY>`. Changes to `local.device`, `local.max_prompt_len`, `local.extra_args` and `local.ovms_variant` are reported as "reload required", and the running model is reloaded on the next use.

### 1.8 Registry formats

**Model registry.** Scan-based: a model is `models\<publisher>\<repo>\` that contains `openvino_model.xml`, `openvino_model.bin`, `openvino_tokenizer.xml` and `openvino_detokenizer.xml`. The sidecar `.aichat-model.json` is written by the downloader, or by adoption:

```json
{"schema":1,"repo_id":"OpenVINO/Qwen3-4B-int4-ov","revision":"<commit sha>","source":"aichat-downloader|adopted",
 "files":[{"path":"openvino_model.bin","size":2263625445,"sha256":"<lfs oid or null>"}],
 "total_bytes":2290000000,"downloaded_at":"2026-09-30T12:00:00Z","license":"apache-2.0"}
```

`state.json` holds `{"last_used": {id: ts}, "compiled": {"<id>|NPU|4096|2026.4.0": {"at": ts, "load_s": 312.4}}, "last_unload": {...}}`.

Built-in catalog, `models/catalog.toml`:

```toml
[[model]]
id = "OpenVINO/Qwen3-4B-int4-ov"
label = "Qwen3 4B INT4"
approx_gb = 2.29
npu = "recommended"
tool_parser = "hermes3"
reasoning_parser = "qwen3"
thinking_toggle = true
licence = "apache-2.0"
note = "Default. First NPU compile ~5-6 min."

[[model]]
id = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"
label = "Qwen2.5 1.5B Instruct INT4"
approx_gb = 0.93
npu = "supported"
tool_parser = "hermes3"
licence = "apache-2.0"
note = "Fallback; faster, weaker."

[[avoid]]
pattern = "Qwen3-8B"
reason = "Too large for 16 GB shared memory"
# Also avoid: "Qwen3-1.7B-int4-ov", "Qwen3-0.6B-int4-ov" and "Phi-4-mini" (asymmetric
# exports, NPU-incompatible), and "Qwen3.5", "Qwen3.6" and "LFM2" (unsupported).
```

Search results that are not in the catalog get the badge "untested" and, if the name has no `int4`, a note about the symmetric-INT4 requirement. Unknown `qwen*` models default to the hermes3 tool parser. Any other unknown model runs with tools disabled and a note.

**Provider registry.** The `[providers.<id>]` tables in `config.toml` (schema above). Seeds are `local-npu` and `minimax`. Settings can add `kind="openai"` custom providers with id, name, base URL, `api_key_env`, a models list (from Test, or typed) and quirks `[]`. Keys go only to keyring (`AIChat`/`<id>`).

### 1.9 Frontend: what to lift and what to strip

**Copied verbatim** (listed in `THIRD_PARTY_NOTICES.md`; licences per `vendor/README.md`: marked MIT 18.0.5, highlight.js BSD-3 11.11.1, DOMPurify Apache-2.0 OR MPL-2.0 3.4.9):

- `vendor/{marked.min.js, purify.min.js, highlight.min.js, github-dark.min.css, README.md}`
- `js/markdown.js` (93,794 bytes)
- `js/util.js`

**How `markdown.js` loads.** It is an ES module with `import { loadScript, loadStyle, escapeHtml } from './util.js?v=13'` and `import { t } from './i18n.js?v=3'`. It expects the globals `marked` and `DOMPurify` at evaluation time (it calls `marked.setOptions` at module eval) and lazy-loads `/static/vendor/highlight.min.js` and `github-dark.min.css` from root-absolute paths. Therefore:

- `index.html` order is: `ui-theme.js` (blocking, in head), `ui-theme-base.css`, `css/app.css`, `ui-theme.css`, then at the end of body `<script defer src="/static/vendor/marked.min.js">`, `<script defer src="/static/vendor/purify.min.js">`, `<script type="module" src="/static/js/chat.js">`. Deferred classic scripts and module scripts run in document order, which matches DisPatch lines 979–981.
- Our static server roots at `src/aichat/web/`, so `/static/...` resolves unchanged. Query strings are ignored by the server.
- `js/i18n.js` is a **new 20-line shim** exporting `t(key, vars)`, `applyDom()` (no-op) and `hasDictionary()`. It contains the English strings markdown.js uses, taken from DisPatch `locales/en.json`: `msg.view_raw`, `msg.link_retargeted`, `msg.code_lang_detected`, `msg.code_wrap`, `msg.copy`, `msg.copied`, `msg.callout_{note,tip,important,warning,caution}`.
- Always call `renderMarkdown(text, {noMedia: true, noLocal: true})`. That disables the `[[media:]]`, `[[doc:]]` and `/api/media` paths. After `chat.done`, run `enhanceContent(el, {noLocal: true})`. Call `installMarkdownHandlers(toast)` once.
- Put rendered markdown inside `.bubble .ui-markdown`.

**`chat.js`** is adapted from DisPatch `main.js`, lifted by function (line numbers from ca186b1):

- `messageEl` (1281–1482): user and assistant bubble, `.msg-col`, `.bubble`, `.msg-time`
- `renderMessages` (1502–1573), `appendMessageToView` (1574), `typingEl` (1605), `appendErrorBubble` (1758)
- `isNearBottom`/`scrollToBottom`/`showScrollButton`/`updateScrollBadge` (1785–1940)
- `updateSendEnabled`/`autosize` (1941–1956): the 4000 constant becomes `limits.max_prompt_chars`. The counter is always visible past 80% of the limit.
- `sendMessage` (1957): body replaced by `api.send_message`
- Streaming: `streamBuffers`, `markStreamSettled`, `scheduleStreamRender` (`STREAM_PAINT_MS=100`, `STREAM_PAINT_CHARS=160`), `beginStream`, `renderStreamMarkdown` (4760–4870)
- `handleWs`'s stream cases are replaced by `on('chat.*')`
- New: a collapsed `<details class="think">` block for reasoning (text via `escapeHtml`, not markdown), tool-call chips (`🔧 web_search "…" ✓`), the header model chip with status dot and compile timer, and the pin/new/settings/close buttons.

**Stripped** from DisPatch: auth/PIN/Safe Mode/decoy (`state.decoy`, `mediaHidden`, `handleLocked`), websockets (`ws.js`, `handleWs`), jobs/harness/StudioForge panels, reactions, avatars (`avatarNode`, avatar pool), bots rail/sidebar/thread list/tabs, pins, companions, search, transcripts, file server/drops/attachments, i18n (`i18n.js` and `locales/`), PWA (`sw.js`, manifest), dashboard, `theme.js`/`theme.css` (replaced by ui-theme), the index.html CSP hash scripts, and the no-FOUC palette script.

**`keycard.js`** is adapted from `llm.js` `firstRunCard`, `runTest`, `save`, and the Enter-to-test wiring (lines 275–395). It builds with `el()` and uses no i18n.

**`css/app.css`** ports DisPatch `app.css` rules for `.msg`, `.bubble`, `.stream-cursor`, `.typing`, the code-block chrome (`.code-block*`), tables, callouts, footnotes, the scroll badge and the composer. Tokens are rewritten to contract names:

| DisPatch | Contract |
|---|---|
| `--bg-primary`, `--bg-secondary`, `--bg-tertiary`, `--bg-elevated` | `--surface-0`, `--surface-1`, `--surface-2`, `--surface-3` |
| `--bg-hover` | `--surface-3` |
| `--bg-input` | `--surface-sunken` |
| `--text-muted` | `--text-tertiary` |
| `--user-bubble` | `--accent-subtle` |
| `--bot-bubble` | `--surface-2` |
| `--error`, `--error-text` | `--danger`, `--danger-text` |
| `--ok` | `--success` |
| `--focus` | `--focus-ring` |
| `--easing` | `--ease-standard` |
| `--shadow-sm`, `--shadow-md`, `--shadow-lg` | `--shadow-1`, `--shadow-2`, `--shadow-3` |
| `--border-light` | `--border-subtle` |
| `--code-inline` | `--code-bg` |
| `--md-quote` | `--quote-bar` |

Must pass `python tools/lint_colors.py src/aichat/web/static/css` from UnifyingTheme. Only contract tokens, no raw colours. Text is quieted by tier, never by opacity.

**ui-theme tag:**

```html
<script src="/static/ui-theme/ui-theme.js" data-themes="laserlloyd,laserlloyd-light,midnight-gold,glacier,forest,paper,daylight,purple" data-default="laserlloyd" data-storage-key="aichat.theme" data-families="true"></script>
```

`config.ui.theme` is the source of truth. On load, JS calls `UITheme.set(cfg.theme)`. `UITheme.onChange` calls `update_settings`, and the other window re-applies on `settings.changed`.

**Accepted gap.** `markdown.js` loads `github-dark.min.css` after `ui-theme-base.css`, so code blocks stay GitHub-dark on LaserLloyd Light (the same as DisPatch today). markdown.js is left untouched.

**CSP** is set as a response header by `webserver.py`:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

pywebview's bridge is injected through WebView2 script injection, which page CSP does not block. WS7 verifies this on day one.

### 1.10 OVMS launch (validated and recorded by WS1)

```
ovms.exe --rest_port <P> --rest_bind_address 127.0.0.1 --model_name <repo_id> --model_path <models\pub\repo>
         --task text_generation --target_device NPU --max_prompt_len 4096 --cache_dir <cache\ov\slug\NPU-4096>
         --tool_parser hermes3 --reasoning_parser qwen3 --enable_prefix_caching true --log_level INFO
```

- `--rest_bind_address 127.0.0.1` avoids the Windows Firewall prompt, which needs admin to answer. No gRPC `--port`.
- Spawn with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED`, assign to the job, then resume. Without `CREATE_NO_WINDOW`, a console window flashes under pythonw.
- The working directory is the OVMS dir, and the environment comes from `ovms_env()` (replicates `setupvars.ps1`).
- Readiness: poll `GET /v2/health/ready`, then `GET /v1/config` until the model state is `AVAILABLE`. Show `LOADING` as "compiling".
- Per request, OVMS gets `chat_template_kwargs: {"enable_thinking": false}`, `stream: true` and `stream_options: {include_usage: true}`, and never `n`.

---

## 2. Workstreams (non-overlapping files)

Branches are `ws/<n>-<slug>`. The lead merges them in the integration order. Dependency list: **WS0 owns `pyproject.toml`**, with every runtime and dev dependency added up front:

- Runtime: `pywebview>=6.2,<7`, `pystray>=0.19.5`, `pillow>=12`, `httpx>=0.28,<1`, `pydantic>=2.9`, `pydantic-settings>=2.15`, `platformdirs>=4.12`, `tomli-w>=1.2`, `structlog>=26.1`, `psutil>=7.2`, `keyring>=25.7`, `ddgs>=9.16,<10`, `pywin32>=312; sys_platform=='win32'`
- Dev: `pytest>=8.3,<10`, `pytest-asyncio>=1.4,<2`, `pytest-timeout>=2.3`, `ruff==0.16.9`

Nobody else edits it. Requests for new dependencies go to the lead.

### WS0: Scaffold and CI (general-purpose, **haiku**). Start first (short).

- **Files:** `pyproject.toml`, `uv.lock`, `package.json`, `package-lock.json` (devDependency `jsdom`), `.gitignore` (`.venv/`, `node_modules/`, `*.part`, `.env`), `LICENSE`, `README.md` (skeleton), `THIRD_PARTY_NOTICES.md`, `.github/workflows/ci.yml`, `src/aichat/__init__.py`, all package `__init__.py` files, `tests/conftest.py`, `tests/fakes/{keyring_backend.py, clock.py}`.
- **Steps:**
  1. `git init -b main`, then the pyproject: hatchling, `src/aichat`, include `web/**` and `models/catalog.toml`, ruff (line length 100, py312, the StudioForge rule set), pytest markers `live_npu`, `live_minimax`, `ui`, and `addopts = "-m 'not live_npu and not live_minimax and not ui'"`, `asyncio_mode = "auto"`, `timeout = 120`.
  2. `py -3.12 -m uv lock`.
  3. Write the fixtures `aichat_home` (tmp dir plus `AICHAT_HOME`), `fake_keyring` (an in-memory `KeyringBackend` set with `keyring.set_keyring`), and `fake_clock`.
  4. CI jobs:
     - `python`, a matrix of `ubuntu-latest` and `windows-latest`: `astral-sh/setup-uv`, `uv sync --locked --extra dev`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`.
     - `js` on ubuntu with Node 24: `npm ci`, then `node --test tests/js`.
- **Accept:** CI is green on an empty test suite. `py -3.12 -m uv run python -c "import aichat"` works on both OSes.

### WS1: NPU runtime bring-up and OVMS supervisor (general-purpose, **opus**). **Starts immediately.** Phase A needs no repo.

- **Files:** `scripts/ovms_bringup.ps1`, `docs/RUNTIME-NOTES.md`, `src/aichat/runtime/{ovms_install.py, ovms_supervisor.py, jobobject.py, ports.py, compile_cache.py}`, `tests/fakes/ovms_child.py`, `tests/unit/{test_ovms_install.py, test_ovms_supervisor.py, test_ports.py, test_compile_cache.py}`, `tests/live/test_live_npu.py`.
- **Phase A (manual, about 30–45 min, including the ~5–6 min compile):**
  1. Download `ovms_windows_2026.4.0_python_off.zip` (117,195,695 bytes) and its `.sha256` (102 bytes) from `https://github.com/openvinotoolkit/model_server/releases/download/v2026.4.0/` into `%LOCALAPPDATA%\AIChat\runtime\downloads\`. Verify with `Get-FileHash`. Extract to `runtime\ovms-2026.4.0\`.
  2. Read `setupvars.ps1` and record exactly which environment variables it sets.
  3. Confirm VC++ (already v14.50) and the NPU device (`Get-PnpDevice -FriendlyName '*AI Boost*'`).
  4. Verify the existing model folder against `GET https://huggingface.co/api/models/OpenVINO/Qwen3-4B-int4-ov/tree/main` sizes.
  5. Run the §1.10 command with the local `--model_path`. Time the first compile. Restart and time the cached load. Record the cache dir size.
  6. With Python httpx, test streaming, `enable_thinking:false`, a `tools` call (for example `current_datetime`) producing `tool_calls` with `finish_reason:"tool_calls"`, a tool-result round trip, tok/s, and whether `/v3/tokenize` exists (use exact counts if it does).
  7. Test whether `--plugin_config '{"NPUW_LLM_GENERATE_HINT":"BEST_PERF"}'` is accepted and helps.
  8. If python_off fails on the chat template or tools, repeat with `_python_on` (138,798,816 bytes) and set the `ovms_variant` default.
  9. Also compile Qwen2.5-1.5B only if Qwen3-4B fails.
  10. Write `docs/RUNTIME-NOTES.md` with the exact working flags, readiness endpoints, env, timings, sizes, and any quirks such as tool calls leaking into content.
- **Phase B (code):**
  - Install with the downloader-style `.part` + Range + sha256, then a zip-slip-guarded extract to `ovms-<ver>.tmp`, then rename.
  - `vcredist_present()` checks HKLM `...\VC\Runtimes\x64 Installed=1` plus the System32 DLLs.
  - `build_argv`, `ovms_env`, the supervisor (adapted SF code listed in 1.3), port pick with one retry on a bind failure, and a `watch` task that sets `exit_event` and logs `describe_exit_code`.
  - `compile_cache`: slug dirs, marker, `expected_load_s()` (cached: 20 s; else the recorded `load_s` or 360 s), and `clear()`.
  - `ovms_child.py`: a stdlib fake OVMS that sleeps N seconds, then serves health, config and a scripted chat. Used by WS6 tests.
- **Accept:**
  - Phase A notes are committed, and a real tool-call round trip works on NPU.
  - Unit tests cover argv (golden), env, sha mismatch, extraction guard, readiness transitions, child-crash detection, and stop/kill-tree (with a fake child).
  - `pytest -m live_npu` (with `AICHAT_LIVE_NPU=1`) loads the model, answers "What is 2+2?", and unloads, leaving no `ovms.exe` process afterwards.
- **Depends on:** WS0 for phase B only. **Recommended model: opus.**

### WS2: Core platform (general-purpose, **sonnet**)

- **Files:** `src/aichat/{paths.py, config.py, secrets.py, errors.py, logging_setup.py, logfiles.py, autostart.py, single_instance.py}`, `tests/unit/{test_paths.py, test_config.py, test_secrets.py, test_logging_redaction.py, test_autostart.py, test_single_instance.py}`.
- **Steps:**
  1. Implement §1.4 and §1.7 exactly: defaults, validation (hotkey syntax, device enum, idle ≥ 0, max_prompt_len 1024–8192, base_url via `normalize_base_url` rules), atomic save, `update_config` returning restart keys.
  2. Copy logging and logfiles per 1.3.
  3. `secrets`: env beats keyring, `register_secret` on every get and set, reject newline and oversize keys.
  4. `autostart`: argv `[pythonw, "-m", "aichat", "--hidden"]`, shim `AIChat.vbs` with `CurrentDirectory = app_home`.
  5. `single_instance`: bind port 47831 without `SO_REUSEADDR`, an accept thread that invokes an `on_show` callback, and `signal_existing()`.
- **Accept:**
  - The config round-trips. An unknown key produces a warning, not a crash.
  - With the env var set, `key_status` is `env` and `env_overrides_saved` is true when keyring also has a key.
  - A secret never appears in the log file (test writes and greps).
  - The VBS starts with the UTF-16 LE BOM and contains `--hidden`. Tests use `APPDATA` pointing at tmp.

### WS3: LLM client, providers and MiniMax (general-purpose, **opus**)

- **Files:** `src/aichat/llm/*`, `tests/fakes/openai_server.py`, `tests/unit/{test_llm_client.py, test_thinking.py, test_minimax.py, test_providers.py, test_probe.py}`, `tests/live/test_live_minimax.py`.
- **Steps:**
  1. `client.py`: an async version of CrucibleForge `stream_chat`. It yields events and treats `data: {"error":...}` frames as errors. It accepts a 200 response with non-SSE JSON: run `quirks.check_payload`, then raise `server`. Stall and wall timeouts, a cancel event, and `_merge_tool_call_deltas`. On completion: `split_thinking` on the raw content, fold inline think text into reasoning, apply the `_merge_reasoning` rule (empty content with reasoning present means show the reasoning), and parse `tool_calls` into `ToolCall` objects.
  2. `OvmsQuirks`: inject `chat_template_kwargs`, drop `n`, and use a fallback `<tool_call>\s*(\{.*?\})\s*</tool_call>` parser when `tool_calls` is empty but the content contains one. Strip it from the visible text.
  3. `MiniMaxQuirks`:
     - Send `reasoning_split: true`. Use `max_completion_tokens` instead of `max_tokens`. Drop `presence_penalty`, `frequency_penalty`, `logit_bias` and `n`. Clamp `temperature` to [0, 2].
     - Check `base_resp.status_code != 0` on **every** payload (HTTP 200 bodies and SSE chunks). Map the codes:
       - 1004/2049 → `region_or_key`, hint "International keys use api.minimax.io; China keys use api.minimaxi.com", action `open_settings`
       - 1002 → `rate_limit` (retryable)
       - 1008 → `balance`
       - 1039 → `context_overflow`
       - 2013 → `bad_request`
       - 2056 → `quota` ("plan window used up; resets within 5 h")
     - Read reasoning from `delta.reasoning_content` **and** `delta.reasoning_details` (a list; concatenate `text`, and keep the raw list merged by index).
     - `history_message` echoes the assistant message unchanged: `content` = the raw content as received, plus `tool_calls` and whichever of `reasoning_details`/`reasoning_content` arrived, stored in `extras`.
     - `finalize` strips `<minimax:tool_call>` XML from the visible text (ported DisPatch helpers).
  4. History serialisation across providers: extras go only back to the provider kind that produced them. Local gets content and tool_calls only.
  5. `providers.py`: `ProviderSpec`, `SEED_PROVIDERS`, region → base_url, `RemoteProvider` (resolves the key via `secrets` and raises `LLMError(no_key, action="add_key")`), and the `ProviderRegistry` CRUD over config. `LocalOvmsProvider` is a thin class taking a `manager` protocol; WS6 supplies the implementation.
  6. `probe.py`: DisPatch `_probe_openai` flow plus MiniMax `base_resp` handling. A 404 on `/models` returns the seeded list, then the probe runs a one-token chat on the chosen model and reports latency.
  7. The fake server (stdlib `ThreadingHTTPServer`) serves scripted SSE: split content, tool_calls split across chunks and indexes, `reasoning_content`, `reasoning_details`, inline `<think>`, a 200 response carrying `base_resp` 2049, 429 with `Retry-After`, a stalled stream, and a `/models` 404.
- **Accept:** all fake-server scenarios pass. MiniMax echo is proven: the second request body contains the first turn's `reasoning_details` and `tool_calls` unchanged. `pytest -m live_minimax` (skips unless `secrets.get_api_key("minimax")` resolves) streams a reply from `MiniMax-M3`, runs one tool round trip with `current_datetime`, and prints which reasoning field arrived. **The live run waits for the owner to enter a key.**

### WS4: Model library (general-purpose, **sonnet**)

- **Files:** `src/aichat/models/*`, `tests/fakes/hf_api.py`, `tests/unit/{test_registry.py, test_downloader.py, test_hf_search.py, test_catalog.py, test_diskspace.py}`.
- **Steps:**
  1. Registry scan and completeness check, adopting the existing Qwen3 folder (write the sidecar with `source:"adopted"` after a size check against `repo_files`, when online). Delete with `_assert_inside_model_dirs`, which also removes that model's `cache\ov\<slug>`.
  2. `hf_search`: `GET /api/models?search=&author=&sort=downloads&limit=30`, and `GET /api/models/{repo}/tree/main?recursive=true` (size, `lfs.oid`) for revision and files.
  3. Downloader:
     - Group = repo. Files download sequentially to `<dest>.part`, then publish.
     - `downloads.json` at 1 Hz. Progress events at 4 Hz.
     - Cancel deletes the partials. On restart, pending groups show as `paused` and are not auto-resumed.
     - Refuse to start unless free space ≥ total + 2 GB.
  4. Catalog load, and badge plus note for search results.
- **Accept:** `httpx.MockTransport` tests cover 206 resume, 200-ignores-Range restart, 416 recovery, size mismatch, sha mismatch, 429 backoff, cancel mid-file (the `.part` is removed), `.part` lock contention, adopt-by-size, dot-dir ignore, avoid-list badges, and path-traversal filenames rejected (`safe_filename`).

### WS5: Tools (general-purpose, **sonnet**)

- **Files:** `src/aichat/tools/*`, `tests/unit/{test_tools_registry.py, test_calculator.py, test_fetch_url.py, test_web_search.py, test_clock.py, test_htmltext.py}`.
- **Steps:**
  - `calculator`: an `ast` whitelist. Numbers, `+ - * / // % **`, unary operators, parentheses. Functions: `sqrt sin cos tan log log10 exp abs round floor ceil`; constants `pi e`. Expressions up to 200 characters, exponent magnitude ≤ 100, results capped at 1e100. No names, attributes or calls outside the whitelist.
  - `clock` (`current_datetime`): local ISO time, weekday, timezone name, UTC offset.
  - `web_search`: `DDGS(timeout=10).text(q, max_results=n)` via `asyncio.to_thread`. A module-level `_ddgs_factory` for mocking. A 2 s minimum interval, a 10-minute LRU cache (32 entries), and ddgs exceptions become `ToolResult(ok=False, "search unavailable/rate-limited; try again shortly")`. Output: a numbered `title — url — snippet[:200]` list.
  - `fetch_url`: http/https only. Resolve the host and block loopback, private, link-local, multicast and reserved addresses (this also protects the OVMS port). Up to 3 redirects, each re-checked. 10 s timeout. Stream-cap at 1 MB. Only `text/html`, `text/plain` and `application/json`. `htmltext` (stdlib `HTMLParser`) skips `script`, `style`, `noscript`, `nav`, `footer` and `svg`, and collapses whitespace. Returns title, URL and text, truncated.
  - `ToolRegistry`: OpenAI schemas, `assert_tool_allowed`, JSON-argument parsing with a one-pass repair (strip code fences and trailing commas). A bad-JSON or unknown-tool call returns a readable tool error and never raises.
- **Accept:** mocked ddgs covers success, a ratelimit exception and a timeout. `fetch_url` refuses `http://127.0.0.1:18611`, `file://` and `http://169.254.169.254`. Calculator fuzz inputs (`__import__`, `9**9**9`, `().__class__`) are rejected.

### WS6: Chat engine and local model manager (general-purpose, **opus**)

- **Files:** `src/aichat/chat/*`, `src/aichat/runtime/{manager.py, idle.py}`, `tests/unit/{test_engine.py, test_history.py, test_conversation.py, test_manager.py, test_idle.py}`.
- **Steps:**
  1. Manager:
     - An `asyncio.Lock` serialises load, unload and switch. `ensure_loaded` switches the model (stop, then start).
     - Status transitions `starting → compiling → ready` with 1 Hz ticks carrying `elapsed_s` and `expected_s` from `compile_cache`.
     - `lease()` takes the lock only to increment `in_flight` and check readiness (reloading if needed), then releases the lock. `Semaphore(1)` (NPU runs one request at a time) and touching `last_activity` happen at start and at end in `finally`.
     - A child exit sets state `error`, and the next request reloads once.
     - `aclose()` unloads.
  2. `IdleReaper` (the SF `_sweep_ttl` shape): every 15 s, `sweep_once` takes the lock, re-checks `state == ready`, `in_flight == 0` and `now - last_activity >= ttl`, then unloads with reason `idle` while holding the lock, so a request arriving mid-unload waits and then reloads. A TTL of 0 disables it. The reaper never touches `starting` or `compiling`.
  3. History: `fit_messages` works in this order:
     - truncate old tool results to 300 characters
     - drop the oldest whole turn groups, keeping an assistant-with-tool_calls message together with its tool messages
     - truncate the current tool results to the budget
     - if the system prompt, tools and the latest user message alone overflow, raise `context_overflow` with the hint "shorten the message or switch to a cloud model"
     
     The local budget is `local.max_prompt_len - 128`, estimated at 3.0 characters per token (or exact counts through `/v3/tokenize` if WS1 confirms it exists). Retry once with half the history on an OVMS context-overflow 400.
  4. Engine per §1.6. Conversation JSON persistence. Cancel.
- **Accept:**
  - Fake-clock idle tests: unload after exactly the TTL. No unload while `in_flight > 0` or during compile. A request racing the sweep either keeps the model or waits and reloads, and never hits a dead port.
  - Engine tests with the WS3 fake server and the WS1 fake child cover a tool loop (2 rounds), the max-rounds stop, the duplicate-call stop, `no_key` giving `add_key`, and 4096-budget trimming with 5 large search results.
- **Depends on:** the WS1 supervisor interface, WS3 and WS5 (it can start against the frozen interfaces).

### WS7: Desktop shell and bridge (general-purpose, **opus**)

- **Files:** `src/aichat/{__main__.py, app.py}`, `src/aichat/desktop/*`, `launchers/*.bat`, `tests/unit/{test_placement.py, test_hotkey_parse.py, test_bridge.py, test_events.py, test_webserver.py, test_icon.py}`, `tests/ui/test_ui_smoke.py`.
- **Steps:**
  1. **Day-one spike:** pywebview 6.2 with a hidden frameless on-top window, our static server, an ES-module page, a `js_api` round trip under the CSP header, `evaluate_js` from a non-UI thread, and a second window created after `start()`. Record the results in the PR.
  2. `webserver.py`: `ThreadingHTTPServer` bound to `127.0.0.1:0`, rooted at `web/`, with an explicit MIME map, `Cache-Control: no-cache`, CSP, `nosniff`, and path confinement.
  3. `win32util`:
     - `MonitorFromPoint(cursor)` and `GetMonitorInfoW(rcWork)` for the work area in physical pixels, and `GetDpiForWindow` for the scale.
     - A pure `place(work_rect, dpi, logical_w, logical_h, margin) -> rect`. Apply it with `SetWindowPos` on the native HWND (found from the pywebview window's native handle).
     - `DwmSetWindowAttribute(33, DWMWCP_ROUND=2)`, and `SetForegroundWindow` (with an `AllowSetForegroundWindow` and `AttachThreadInput` fallback).
  4. `popup.py`:
     - `show` (place, show, foreground, emit `popup.shown`), `hide`, `toggle`, and pin.
     - Hide on blur comes from a JS `blur` listener with a 150 ms debounce. It is ignored while pinned, while settings are opening, or while a native dialog is up.
     - Tray click after a blur-hide: a tray click within 400 ms of a blur-hide keeps it hidden (a toggle, not a flicker).
     - Escape hides.
  5. `settings_window.py`: a normal resizable window (900×700 logical), one instance only.
  6. `tray.py`/`icon.py`:
     - Status dot: green = ready, amber = starting/compiling, grey = unloaded or cloud, red = error.
     - Menu: status line, Open chat (default and left-click), Settings, Load/Unload model, Open logs folder, Start at login (checked), Quit.
     - Handlers run on `_spawn_thread`.
  7. `hotkey.py`: parse `Ctrl+Alt+Space` style strings into MOD and VK codes. `RegisterHotKey(NULL, id, mods | MOD_NOREPEAT, vk)` in its own thread with a `GetMessageW` loop. Re-register by posting `WM_APP` to that thread. A failure is reported to settings.
  8. `core_loop.py` and `bridge.py`/`events.py` per §1.5, plus the 50 ms delta coalescing.
  9. `app.py` wiring:
     - `main()`: single instance, `Paths` and config, logging, apply first-run autostart, start the static server and core loop (registry scan, manager, reaper, downloader), create the popup (`hidden=True`), start the tray and hotkey threads, then `webview.start(gui="edgechromium", private_mode=False, storage_path=paths.webview_dir)`.
     - On Quit: `manager.aclose()`, `icon.stop()`, destroy the windows.
     - `__main__` CLI: `aichat [--hidden|--show|--settings]`, `aichat runtime install|status`, `aichat autostart enable|disable|status`, `aichat doctor`.
- **Accept:**
  - Placement unit tests (100/150/200% DPI, taskbar at bottom/left/top, second monitor) produce a rect inside rcWork with the margin.
  - Hotkey parse tests pass.
  - `test_bridge` asserts `Api` exposes only the listed public names, and that `save_api_key` never returns the key.
  - The UI smoke test passes (see §5).
- **Depends on:** WS2, WS4, WS6, and WS8a's `bridge.js` contract.

### WS8a: Popup chat UI (general-purpose, **sonnet**). Can start right after WS0, against `dev-mock.js`.

- **Files:** `src/aichat/web/index.html`, `web/static/{ui-theme/**, vendor/**, js/markdown.js, js/util.js, js/i18n.js, js/bridge.js, js/dev-mock.js, js/chat.js, js/keycard.js, css/app.css, img/icon.svg}`, `tests/js/**`.
- **Steps:**
  1. Copy the verbatim files (checksum-compare against the sources in the PR).
  2. Write the i18n shim, `bridge.js` and `dev-mock.js` (which simulates streaming, tool chips, a compile timer and `no_key`).
  3. `chat.js` per §1.9:
     - The header model chip (dropdown of the providers' models and installed local models, status dot, "Compiling for NPU 1:23 / ~6:00", "Loading… ~10 s", "Unloads in 4 min").
     - Composer: Enter sends, Shift+Enter adds a newline, a Stop button, and the counter.
     - Collapsed think block, tool chips, error bubbles with action buttons (Add key, Open settings, Retry).
     - `keycard.js` inline card: "Add your MiniMax API key". A password input with a show/hide eye, a region toggle, and a "Save & test" button that calls `save_api_key` then `test_provider(pid, null)`. On success it re-sends the pending text. Clicking a link calls `open_external`.
  4. `app.css`: token-mapped port, compact popup layout (420×620), and `:focus-visible` on every control.
- **Accept:**
  - `node --test tests/js` passes (§5).
  - In the browser with dev-mock, streaming renders smoothly, a table, code and a footnote render, and the key card flow works.
  - `lint_colors.py` is clean.
  - Theme switch between LaserLloyd and LaserLloyd Light works through `data-families`.

### WS8b: Settings UI (general-purpose, **sonnet**). Parallel with WS8a.

- **Files:** `src/aichat/web/settings.html`, `web/static/js/settings.js`, `web/static/css/settings.css`. It consumes WS8a's `bridge.js`, `util.js` and ui-theme without editing them.
- **Tabs:**
  - **Models.** Installed list: select, load, unload, delete (confirm), clear compile cache, size, "compiled for NPU ✓", badge. Disk usage bar. Runtime card: OVMS installed/version, Install button with progress, VC++ status plus the winget instruction if missing. Search box (author filter defaulting to OpenVINO, toggleable), results with badges, and a details drawer with total size and a Download button. Downloads list with progress, speed, ETA, and Cancel/Resume.
  - **Providers.** The local-npu card (device NPU/GPU/CPU, `max_prompt_len` under an advanced section, "reload required" note). The MiniMax card:
    - Region segmented control: International / China / Custom URL.
    - Model select plus custom model text.
    - API key `type=password autocomplete=off spellcheck=false` with an eye toggle, and Save, Test and Remove buttons.
    - Status line, one of: "Saved in Windows Credential Manager" / "Using MINIMAX_API_KEY from the environment. It takes precedence over a saved key." / "No key".
    - Test result line with hint.
    - Add-custom-provider form.
  - **General.** Idle-unload minutes (0 = never), max prompt length (chars), hotkey capture field (records a combination and shows registration errors), theme `<select data-ui-theme-picker>`, Start at login, hide on blur, tool checkboxes, and "show reasoning".
  - **Logs.** Tail of 500 lines with a level filter, 2 s auto-refresh while visible, Open folder, Copy.
- The key field is cleared after Save and Test, and on tab switch. No key value is ever put into state or `console.log`.
- **Accept:** all flows work against dev-mock. `lint_colors.py` is clean.

### WS9: Integration, docs, doctor, live verification, repo (general-purpose, **opus**). Last.

- **Files:** `src/aichat/doctor.py`, `docs/SETUP.md`, `README.md` (full; WS0 wrote the skeleton), `tests/unit/test_doctor.py`, and fix-ups by coordination only.
- **Steps:**
  1. Merge in order and resolve interface drift.
  2. `doctor`: Python, WebView2 version (registry `EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` `pv`), VC++, NPU device, OVMS, model completeness, compiled marker, keyring backend, `MINIMAX_API_KEY` present (true/false only), whether the hotkey can be registered, autostart status, and free disk.
  3. Run the live NPU smoke, the UI smoke and the full checklist (§6).
  4. Ask the owner to enter the MiniMax key, then run `live_minimax`.
  5. **With owner confirmation**, create the private repo: `gh repo create LaserLloyd/AI-Chat --private --source . --remote origin --push`. Confirm CI is green on GitHub.

### Integration order

```
t0: WS0 ─┬─► WS2 ─┬─► WS3 ─┐
         │        ├─► WS4 ─┤
WS1-A ───┤        ├─► WS5 ─┼─► WS6 ─► WS7 ─► WS9
(now)    │        └─► WS1-B┘            ▲
         └─► WS8a, WS8b (dev-mock) ─────┘
```

Merge order: WS0 → WS2 → WS1-B → WS3 → WS5 → WS4 → WS6 → WS8a → WS8b → WS7 → WS9.

---

## 3. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **First NPU compile takes ~5–6 min** | Per-model `--cache_dir` plus a compiled marker, so the UI shows an honest "First-time NPU compile ~6 min (one-off)" countdown and "~10 s" afterwards. The popup stays usable, and the user can switch to MiniMax meanwhile. The first compile is done in WS1 phase A, so the owner never sees it. The reaper never unloads during compile. Killing mid-compile leads to a scoped cache clear if the next load fails. |
| **4096-token static prompt window** | `fit_messages` (drop whole turn groups, never split tool pairs), tool results capped at 1500 characters locally, search snippets at 200 characters × 5, 4 tool schemas kept short (under ~400 tokens), a conservative 3.0 chars/token estimate (or exact `/v3/tokenize`), one retry with half the history on overflow, and a clear error suggesting cloud. |
| **Tool-call parsing quirks (small Qwen)** | hermes3 parser plus a fallback `<tool_call>` regex, a one-pass JSON repair, bad arguments returned to the model as a tool error once, a 4-round cap, duplicate-call stop, and ignoring the NPU `finish_reason` limitation (stop/tool_calls only). The owner can disable tools per list. |
| **MiniMax think and reasoning output** | `reasoning_split: true`. Both `reasoning_content` and `reasoning_details` are handled, plus inline `<think>` via `split_thinking`. Collapsed in the UI. The full assistant message is echoed back unchanged. `<minimax:tool_call>` XML is stripped from the visible text. |
| **MiniMax errors on HTTP 200** | `check_payload` on every body and chunk. 1004/2049 gives a region-or-key hint and the region toggle. 2056 and 1002 are shown as waits, not failures. `/models` 404 falls back to the seeded list. |
| **ddgs rate limits and breakage** | Runs in a thread with a timeout, a 2 s spacing, a 10-minute cache and friendly failure text. The version is capped `<10`. Tests mock it. |
| **pywebview and pystray threading** | The main thread belongs to pywebview. pystray runs in its own thread. `evaluate_js` only runs on the dispatch thread. Loop work goes through `run_coroutine_threadsafe`. `Api` exposes no public attributes. The WS7 day-one spike proves it. |
| **DPI and work-area placement** | Physical-pixel math from `rcWork` of the cursor's monitor plus `GetDpiForWindow`, applied with `SetWindowPos` (not pywebview x/y). Re-placed on every show, which handles taskbar moves and monitor changes. Unit-tested pure function. |
| **Focus and hide-on-blur** | Foreground via `SetForegroundWindow` with fallbacks. JS focuses the composer on `popup.shown`. Blur debounce, a pin button, Escape to hide, and the tray-click-after-blur toggle guard. Hotkey presses grant foreground rights. |
| **Idle-unload race with an in-flight request** | A single manager lock plus the `in_flight` counter. The reaper re-checks under the lock. `lease()` increments before use, and requests arriving mid-unload wait and reload. The `finally` decrement follows the SF `_forward` discipline. |
| **Download resume** | SF `.part` + Range with the 206 check, restart on a 200, 416 recovery, sha256 against the LFS oid, an exclusive `.part` lock, `downloads.json`, and a manual Resume button. |
| **Disk usage** | Model 2.29 GB + compile cache (estimate 1–3 GB; WS1 measures) + OVMS (~112 MiB zip, extracted size recorded by WS1) + venv (~100–150 MB estimate). Settings shows each figure, with Delete model and Clear cache. Downloads need free space ≥ total + 2 GB. C: has 415 GB free now. |
| **Licences of copied vendor files** | `vendor/README.md` copied verbatim (MIT, BSD-3, Apache-2.0/MPL-2.0). `THIRD_PARTY_NOTICES.md` lists the vendor files, DisPatch/StudioForge/CrucibleForge (owner, MIT), UnifyingTheme `ui-theme/` (owner, private: **re-check before any public release**), OVMS (Apache-2.0, downloaded at runtime, not redistributed), Qwen (Apache-2.0, downloaded). |
| **Console flash, firewall prompt** | `CREATE_NO_WINDOW` on the OVMS spawn. `--rest_bind_address 127.0.0.1`. |
| **Orphaned `ovms.exe`** | Job object with kill-on-close, the atexit net, kill_process_tree, and doctor reports strays. |
| **python_off lacks template or tool support** | WS1 phase A decides. `ovms_variant = python_on` is the fallback (139 MB). |
| **Secrets leakage** | keyring only, env precedence, `register_secret` redaction, no key in events or returns, field cleared, `test_bridge` guard, and a grep test on the logs. |
| **Sleep/resume breaks the OVMS NPU context** | A health check before each lease. On failure: error state and one automatic reload. |
| **WebView2 blocked by CSP** | Verified in the WS7 spike. Fallback: drop the header CSP and use a meta CSP with the same policy minus `script-src` hashes. |
| **`python` is the Store stub** | All commands use `py -3.12`. Launchers call `.venv\Scripts\pythonw.exe` directly. |

---

## 4. Install and verification commands

No step needs admin (UAC) on this machine. VC++ v14.50 and WebView2 are present, and OVMS binds loopback.

```powershell
cd "%USERPROFILE%\Desktop\Projects\AI Chat"
py -3.12 -m uv venv --python 3.12 .venv
py -3.12 -m uv sync --extra dev                      # ~100–150 MB of wheels (estimate)
npm ci                                               # dev only: jsdom (~10 MB)
py -3.12 -m uv run ruff check . ; py -3.12 -m uv run ruff format --check .
py -3.12 -m uv run pytest -q                         # unit only (markers deselected)
node --test tests/js

# Runtime (LARGE: 117,195,695 bytes; python_on fallback 138,798,816 bytes)
py -3.12 -m uv run python -m aichat runtime install
py -3.12 -m uv run python -m aichat runtime status
py -3.12 -m uv run python -m aichat doctor

# Model: already on disk (2.29 GB). Fallback model, if needed (0.93 GB), via Settings → Models → Download.

# First NPU compile (~5–6 min, one-off) + live smoke
$env:AICHAT_LIVE_NPU = "1"; py -3.12 -m uv run pytest -m live_npu -s

# Run the app
py -3.12 -m uv run python -m aichat                 # shows popup
.\.venv\Scripts\pythonw.exe -m aichat --hidden      # as autostart runs it
py -3.12 -m uv run python -m aichat autostart enable    # writes %APPDATA%\...\Startup\AIChat.vbs (no admin)

# MiniMax (after the owner saves a key in Settings)
py -3.12 -m uv run pytest -m live_minimax -s
py -3.12 -c "import keyring; print(keyring.get_password('AIChat','minimax') is not None)"   # prints True/False only

# UI smoke (Windows desktop session)
py -3.12 -m uv run pytest -m ui -s

# Repo (after owner confirmation)
git init -b main; git add -A; git commit -m "AI Chat: initial"
gh repo create LaserLloyd/AI-Chat --private --source . --remote origin --push
gh run watch
```

Only if VC++ is ever missing: `winget install --id Microsoft.VCRedist.2015+.x64 -e` (**UAC**, about 25 MB). The app only shows this instruction and never runs it itself.

---

## 5. Testing

**Unit tests** (CI, ubuntu and windows). Windows-only modules are import-guarded and pure helpers are tested everywhere.

- **Config:** defaults file created, round-trip, env override, validation errors, restart keys.
- **Secrets:** fake keyring; env precedence; `env_overrides_saved`; reject bad keys; redaction of registered secrets in log output.
- **Registry:** scan the fixture tree, completeness, dot-dir ignore, adopt, delete confined to `models_dir`.
- **Downloader and HF search:** `httpx.MockTransport` cases from WS4.
- **LLM client:** the stdlib fake OpenAI server:
  - streamed content split mid-token
  - `tool_calls` split across chunks and indexes (merge)
  - `reasoning_content`, `reasoning_details` and inline `<think>`
  - `data:{"error"}` frames
  - a 200 non-SSE response with `base_resp` 2049
  - 429 with `Retry-After`
  - stall timeout and cancel
  - `/models` 404 falling back to seeds
- **MiniMax:** body has `reasoning_split` and `max_completion_tokens` and no penalties; the second-turn echo is byte-equal; the XML strip.
- **Idle and manager:** fake clock covering TTL exact, `in_flight` blocks, compile blocks, the race, TTL 0, crash then reload. Uses the `tests/fakes/ovms_child.py` fake child.
- **Engine:** tool loop, max rounds, duplicate stop, `no_key`, trimming under 4096 with big tool results.
- **Tools:** calculator safety; `fetch_url` SSRF blocks, size cap, content types, `htmltext`; `web_search` with ddgs mocked (ok, ratelimit, timeout, cache, spacing); clock format.
- **Desktop:** placement math, hotkey parse, `Api` surface and no-key-echo, event coalescing, static server MIME, CSP and path confinement, icon rendering at 16/32/64.
- **Supervisor:** argv golden, env, stop/kill with a fake child, exit-code description, sha and extraction guard.
- **Autostart:** shim encoding, content, remove.

**Markdown rendering check** (`tests/js/markdown.test.mjs`, node:test plus jsdom): load `marked.min.js` and `purify.min.js` into the jsdom window, import `markdown.js`, and render the fixtures. Assert:

- a GFM table becomes a `<table>`
- fenced code has the code-block header and copy button
- footnotes render
- `<script>` and `onerror` are removed
- a `javascript:` href is removed
- external links get `target=_blank rel~=noopener`
- `[[media:...]]` with `noMedia` does not produce `<img>`
- `renderMarkdown('')` does not throw

**Live NPU smoke** (`live_npu`, skipped unless `AICHAT_LIVE_NPU=1`; Windows): real OVMS and the model. Load (report compile vs cached time), stream "2+2", run one `current_datetime` tool round trip, time an idle unload with a 5 s TTL override, then assert no `ovms.exe` process remains.

**Live MiniMax smoke** (`live_minimax`, skipped unless a key resolves from env or keyring): `test_provider` succeeds; stream from `MiniMax-M3`; a tool round trip with the echoed message accepted (no 2013); log which reasoning field arrived and the key region. Never prints the key.

**UI smoke** (`ui`, Windows desktop). Launch the app in-process with a temp `AICHAT_HOME`, provider `fake` pointing at the fake OpenAI server, and a hidden start. Through `webview.start(func)`:

1. Toggle the popup.
2. Assert its rect is inside the work area at the bottom-right with the margin, and has rounded corners.
3. Send a message through `evaluate_js`.
4. Wait for `chat.done`.
5. Assert the DOM has a rendered table and a highlighted code block, and a collapsed `.think` element.
6. Select a keyless cloud provider and assert the key card appears.
7. Open settings; confirm that 4 tabs render and the theme switches.
8. Quit cleanly.

---

## 6. Final verification checklist

- [ ] `ruff check`, `ruff format --check`, `pytest -q` and `node --test tests/js` are green locally, and CI is green on ubuntu and windows (private repo `LaserLloyd/AI-Chat`).
- [ ] `aichat doctor` is all green: WebView2, VC++, NPU, OVMS 2026.4.0, model complete, compiled marker, keyring backend, hotkey free, autostart enabled.
- [ ] Tray icon appears at logon (after a reboot or sign-out) with no console window, hidden, and with no model loaded.
- [ ] The tray click and the `Ctrl+Alt+Space` hotkey each open the popup at the lower-right above the taskbar at 200% DPI. Composer is focused, corners are rounded. Escape, blur and a second tray click hide it. Pin keeps it open.
- [ ] Local: the first question auto-loads the model with a status countdown. The cached load takes seconds (time recorded). Answers stream with markdown. "What's today's date?" uses `current_datetime`. "Search the web for …" uses `web_search`. "Summarise <url>" uses `fetch_url`. "What is sqrt(2)*10?" uses the calculator.
- [ ] After 10 minutes idle the model unloads (tray dot turns grey, `ovms.exe` is gone). The next question reloads it. Unload during a long answer does not happen.
- [ ] Settings → Models: search "Qwen" shows badges. Download of the fallback shows progress. Cancel removes the `.part`. Resume works after an app restart. Select, load, unload and delete work, and delete refuses a loaded model.
- [ ] Settings → Providers → MiniMax: paste the key, eye toggle, Test succeeds, Save shows "Saved in Windows Credential Manager". Remove works. With `MINIMAX_API_KEY` set, the UI says the env var takes precedence. A wrong region shows the 1004/2049 hint.
- [ ] First-run: selecting MiniMax with no key shows the inline "Add your MiniMax API key" card in the popup. After saving, the pending message is sent.
- [ ] MiniMax replies stream, reasoning is collapsed, and a tool round trip works (echo accepted).
- [ ] General: idle timeout, max prompt length (the counter enforces it), device, hotkey change (conflict reported), theme (LaserLloyd default, the Light toggle, persists across both windows), and the autostart toggle all persist in `config.toml`.
- [ ] Logs tab shows recent lines. `Select-String` for the key over `%LOCALAPPDATA%\AIChat\logs\*` and `config.toml` finds nothing.
- [ ] Quit from the tray leaves no `ovms.exe` or `pythonw.exe` running. Launching twice brings up the existing popup.
- [ ] `THIRD_PARTY_NOTICES.md` and `web/static/vendor/README.md` are present. Every copied module carries an "adapted from" comment. StudioForge is unmodified (`git -C ..\StudioForge status` is clean).

### Critical Files for Implementation
- %USERPROFILE%\Desktop\Projects\StudioForge\src\studioforge\core\supervisor.py (lines 700–1001 job object and kill tree, 2461–2760 spawn, pump and readiness)
- %USERPROFILE%\Desktop\Projects\StudioForge\src\studioforge\core\downloader.py (lines 197–633 part file, retry and progress; 1288–1590 transfer and finish)
- %USERPROFILE%\Desktop\Projects\_reference\DisPatch_Chat\frontend\static\js\markdown.js, plus js\main.js (lines 1281–1960 and 4760–4870) and js\llm.js (lines 275–395)
- https://raw.githubusercontent.com/LaserLloyd/CrucibleForge/main/crucibleforge/api.py (`split_thinking`, `_merge_tool_call_deltas`, `stream_chat`) and %USERPROFILE%\Desktop\Projects\_reference\DisPatch_Chat\backend\app\llm_api.py (lines 226–488 and 857–913)
- %USERPROFILE%\Desktop\Projects\_reference\UnifyingTheme\ui-theme\ (verbatim bundle), %USERPROFILE%\Desktop\Projects\StudioForge\src\studioforge\tray\tray_app.py, and %USERPROFILE%\Desktop\Projects\StudioForge\src\studioforge\core\autostart.py

---

## 7. Review amendments (Fable review, 2026-09-30) — BINDING, these override sections 0–6

Verdict: APPROVE WITH REQUIRED CHANGES. All required changes below are adopted. Every agent must apply them.

### Required changes (adopted)
1. **OVMS package is `python_on` by default.** The C++-only `python_off` package cannot use tools and drops the system message (OVMS `docs/deploying_server_baremetal.md`, `docs/llm/reference.md`). Set `ovms_variant = "python_on"`, `ASSET = "ovms_windows_2026.4.0_python_on.zip"`, `ASSET_BYTES = 138_798_816`. `python_off` remains opt-in only if WS1 proves it renders the Qwen3 template with a system message and tools. WS1 Phase A records whether `python_on` needs a system Python and exactly what `setupvars.ps1` sets (`PYTHONHOME`, `PYTHONPATH`, `PATH`). `ovms_env()` strips the app venv's `VIRTUAL_ENV`, `PYTHONHOME` and `PYTHONPATH` before applying OVMS's values.
2. **`graph.pbtxt` ownership.** OVMS writes launch parameters into `graph.pbtxt` in the model dir.
   - WS1 Phase A tests whether relaunching with a different `--max_prompt_len` or `--target_device` takes effect or the stale graph wins.
   - `OvmsSupervisor.start()` hashes the `LaunchSpec` fields (device, max_prompt_len, parsers, cache_dir, variant, ovms_version) and compares with `state.json`. On a difference it regenerates the graph with `ovms --configure --model_path … --task text_generation …`, or deletes `graph.pbtxt`, before serving.
   - `models/registry.py` treats `graph.pbtxt` as expected: not a completeness file, ignored in size checks, never deleted on adopt.
   - Adopt verifies that the model dir is writable.
3. **`ProviderSpec` and `Timeouts` live in `config.py` (WS2).** `llm/*` imports them from `aichat.config`. This removes the backwards WS2→WS3 dependency.
4. **`__init__.py` files are empty and owned by WS0.** Nobody else edits them. `ToolResult`, `ToolNotAllowed`, `assert_tool_allowed` and `ToolRegistry` move to `tools/registry.py`. Same rule for `llm/` and `runtime/`: put shared types in named modules, never in `__init__.py`.
5. **Frameless popup uses `easy_drag=False`.** Otherwise every text-selection drag moves the window. If a drag handle is wanted, use `webview.settings['DRAG_REGION_SELECTOR']` on the header only. Verify in the WS7 day-one spike.
6. **Events are delivered with `window.run_js(...)`, not `evaluate_js`.** `evaluate_js` uses `eval`, which a strict CSP blocks, and it blocks the calling thread. Keep `evaluate_js` only for test DOM assertions. The WS7 spike exercises `run_js`, `evaluate_js` and a `js_api` round trip under the CSP header. Add `object-src 'none'` to the CSP.
7. **Validate `repo_id` before it becomes a path.**
   - Regex `^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$`; reject any `..` segment.
   - Assert `_is_under(models_dir)` on the resolved destination before the first byte is written.
   - Unit-test a traversal repo id.
8. **Token and path corrections.**
   - DisPatch `app.css` uses 55 tokens, 17 of which ui-theme defines by name. The token-map port still stands.
   - The colour linter is `UnifyingTheme\V26-09-16\tools\lint_colors.py`.
   - DisPatch `locales/en.json` nests strings under `"msg": {...}` (line 133), so the i18n shim flattens `msg.<key>` lookups.

### Recommended changes (adopted)
- **fetch_url:** protect against DNS rebinding by connecting to the vetted IP (`Host` header plus `extensions={"sni_hostname": host}`) and re-vetting on every redirect. Also reject IPv4-mapped IPv6 and `0.0.0.0`.
- **Calculator:** enforce the magnitude and exponent limits per AST node during evaluation, so `9**9**9` is rejected before the inner power runs. Catch `ArithmeticError` into a tool error.
- **Keys:**
  - `test_provider(pid, key)` calls `register_secret(key)` before any network call.
  - Production runs `webview.start(debug=False)`.
  - `api_key_env` must match `^[A-Z][A-Z0-9_]{0,63}$`.
- **OVMS zip integrity:** pin its sha256 as a constant once WS1 records it, rather than relying only on the sibling `.sha256` file.
- **Config cuts:** remove `enable_prefix_caching` (ignored on NPU), `local.port` and `[ui].themes`.
- **Unload during an in-flight request:** a user `unload_model()` during a request cancels the request (the engine reports `cancelled`), then unloads. `ensure_loaded` touches `last_activity`.
- **Scope decisions (lead):**
  - KEEP the add-custom-provider form, because the owner asked to "plug in an API model".
  - The hotkey setting is a plain text field that applies on save, with registration errors shown. No capture widget.
  - The in-process UI smoke test becomes a manual checklist item plus a minimal scripted launch check (`aichat --show` starts, the window rect is inside the work area, `aichat` quits cleanly). No pywebview-in-pytest target.
  - The ubuntu CI leg stays.
- **Markdown JS tests:** copy DisPatch `frontend/tests/markdown-behaviour.test.js` as the harness base.
- **WS8a lands first:** it commits `bridge.js`, `dev-mock.js` and `i18n.js` first, so WS8b can start.
- **DPI:** the WS7 spike asserts that `GetDpiForWindow` returns 192 at 200%. If it doesn't, call `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` before importing webview.
- **markdown.js load order:** `marked.setOptions` is guarded and the DOMPurify hooks are lazy, so the globals are not required at evaluation time. Keep the prescribed load order anyway, so that `breaks: true` applies.
- **Dependencies:** `uv lock` is the arbiter for version ranges. Loosen `pytest-asyncio` if needed.

### Owner decisions (made by the lead under the owner's standing instructions)
1. `python_on` is the default OVMS package (+21 MB).
2. **NPU model gate:** Qwen3-4B-int4-ov is gs128, while OVMS docs ask for channel-wise (`--group-size -1`) on NPU. WS1 tries it first. If the compile fails or tool calls are garbage, WS1 falls back automatically: to an NPU-optimised `-cw-` variant of a ≤4B model if one exists in the OpenVINO org, else to `Qwen2.5-1.5B-Instruct-int4-ov`. It reports what happened and updates `catalog.toml`'s recommended flag.
3. Scope cuts as listed above.
4. **Model assignments:** WS7 and WS9 run on **fable**, WS4 on **opus**. The rest are as planned.
5. **Repo:** private `LaserLloyd/AI-Chat`, as the owner requested. It stays private until the UnifyingTheme licence is reviewed.

### Execution rules for all agents
- All agents share one working tree, `%USERPROFILE%\Desktop\Projects\AI Chat`. **Edit only the files your workstream owns.** Do not run repo-wide `ruff format` or `ruff check --fix`. Run them on your own files only.
- Use `py -3.12 -m uv run ...` from the repo root, and never bare `python`. Do not add dependencies; ask the lead to change `pyproject.toml`.
- Do not `git commit`, create branches or push. The lead commits each workstream when it lands.
- Never type, print or log an API key. The owner enters keys in the app.
- Report at the end: files written, tests run with results, deviations from the plan, and open issues.
