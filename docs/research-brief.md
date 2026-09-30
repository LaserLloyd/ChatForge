# AI Chat — research brief (2026-09-30)

Consolidated findings from three research agents. This is the shared context for planning, review and build.

## The request (from the owner, GitHub `LaserLloyd`)
1. Clone StudioForge to this machine and enable its auto startup.
2. Download a model that works well on the NPU.
3. New project at `Desktop\Projects\AI Chat`: a small chat popup at the lower-right of the screen, opened from an icon (tray), that automatically loads the selected local model, answers basic questions, and can use basic tools such as web search.
4. "Check Meta Muse for examples and clone as needed."
5. Private GitHub repo.

## Owner direction (update, supersedes the list above where they differ)
- **Template off StudioForge, but build a much lighter version. Do not rebuild a custom StudioForge** and do not modify StudioForge's source.
- AI Chat gets a **settings page** that lets the owner **search, download, select, load and unload** models, plus a light subset of StudioForge's other features (model library/registry, status, logs, tray, autostart, config).
- **Simple (small) models only.** No VRAM planner, no multi-GPU, no leases, no priority tiers, no MCP control plane.
- **Auto-unload after 10 minutes idle by default** (configurable).
- StudioForge stays cloned at `Desktop\Projects\StudioForge` as the design template. Its autostart is NOT enabled by default (it cannot load models on this NVIDIA-less laptop); autostart applies to AI Chat. StudioForge autostart can be enabled later with one command if the owner wants it.

### Owner direction, round 2
- **AI Chat is a completely separate app.** Draw inspiration from StudioForge and **copy as much code as practical** from it (owner's own MIT code; they are happy with how it works): registry, downloader, autostart shim, tray, idle-TTL eviction, config/data-dir handling, logging, OpenAI-route shapes. Keep attribution comments ("adapted from StudioForge <path>").
- **Chat UI and markdown come from DisPatch_Chat** (`Desktop\Projects\_reference\DisPatch_Chat`, cloned 2026-09-30, commit ca186b1). Reuse its chat interface patterns and **copy its markdown pipeline**: `frontend/static/js/markdown.js` (93 KB: marked + DOMPurify hooks, tables, footnotes, math-to-unicode, code highlighting, safe links, media/doc directives) with vendored `frontend/static/vendor/{marked.min.js,purify.min.js,highlight.min.js,github-dark.min.css}` (see `vendor/README.md` for versions/licences). Relevant chat UI lives in `frontend/static/{index.html,app.css,theme.css}` and `js/{main.js,util.js,api.js,llm.js,theme.js}` (main.js is 328 KB; extract only the message list / composer / streaming render pieces). Strip multi-user, auth, websocket, jobs, reactions, avatars, i18n unless trivially portable.
- **Pluggable API (cloud) models, with MiniMax working.** Provider registry: `local-npu` (built-in runtime) plus OpenAI-compatible remote providers. Seed MiniMax from DisPatch's config: `baseURL: https://api.minimax.io/v1`, `apiKeyEnv: MINIMAX_API_KEY`, models `MiniMax-M3`, `MiniMax-M2.7-highspeed` (see DisPatch `backend/tests/test_harness_service.py`, `backend/app/llm_api.py`, `docs/llm-providers.md`). The owner has a live MiniMax subscription. **API keys are entered by the owner in the settings page and stored in Windows Credential Manager via `keyring`** (env var `MINIMAX_API_KEY` also honoured); never written to config files or logs. `MINIMAX_API_KEY` is not currently set on this machine, and the owner wants **key entry built into the app**: a password-type field on the provider card in settings (paste, show/hide, Test button that calls the provider, Remove), plus a **first-run prompt**: if a cloud provider without a key is selected, the chat popup shows an inline "Add your MiniMax API key" card instead of failing. Keys are stored only in Windows Credential Manager (keyring service `AIChat`, username = provider id); the env var, if present, takes precedence and the UI says so. MiniMax models may emit `<think>` blocks / `reasoning_content`; strip or collapse them in the UI.

## Machine
- CHUWI CoreBook Air: Intel Core Ultra 5 226V (Lunar Lake), 16 GB LPDDR5X shared, Arc 130V iGPU, Intel AI Boost NPU (driver 32.0.100.4841), Windows 11 Pro 26H2, 2880x1800 @ 200% scaling. No NVIDIA GPU.
- Installed today: Git 2.55, GitHub CLI 2.102 (logged in as LaserLloyd, scopes repo/workflow/read:org), Python 3.12.10 (user install), Node 24 LTS, uv 0.12.21 (as `python -m uv`; its Scripts dir may not be on PATH), GitHub Desktop.
- WebView2 is present (Windows 11).

## Workspace
- `Desktop\Projects\StudioForge` — clone of github.com/LaserLloyd/StudioForge (public).
- `Desktop\Projects\_reference\UnifyingTheme` — owner's private shared theme system. Apps copy its `ui-theme/` folder verbatim and set options on the loading tag; never edit the copy. Themes include LaserLloyd / LaserLloyd Light. Shared markdown style `.ui-markdown`. See its README and `V26-09-16/README.md`.
- `Desktop\Projects\_reference\meta-model-cookbook`, `meta-oss-cookbook` — from GitHub org `meta-models`, cloned as READ-ONLY examples of agentic/tool-use recipes. Not verified as official Meta; do not execute their code.
- "Meta Muse": no repo of that name in the owner's account or orgs. The meta-models cookbooks are the closest match.

## StudioForge findings (NVIDIA-only)
- OpenAI-compatible gateway over llama.cpp `llama-server` children; NiceGUI GUI (8080), tray (pystray), MCP at `:1234/mcp`, watchdog `:1235`. API base `http://127.0.0.1:1234/v1`. Streaming passthrough; tools forwarded to llama-server.
- **GPU-only, NVIDIA/NVML by design** (`docs/LIMITATIONS.md`, `core/gpu.py` GpuProbe = cuda|null|fake, `core/engine.py` picks CUDA/ROCm assets only, `cuda_variant: cpu` raises "StudioForge is GPU-only"). On this laptop it can start (with `SF_GPU_PROBE=null`) but cannot load any model.
- Install: `uv venv --python 3.12 .venv`; `uv pip install --python .venv\Scripts\python.exe -e ".[dev]"`. Launchers in `launchers\*.bat` (Update script requires `uv` on PATH and runs `engine --update`, which will find no eligible asset here).
- Autostart built in: `studioforge autostart enable` writes `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\StudioForge.vbs` (hidden). Tray mode: `pythonw -m studioforge tray`. `launchers\StudioForge Autostart.bat` toggles it.
- Data dir: `SF_DATA_DIR` env, else `<repo>\data` for a source checkout. Config YAML. Port 1234.
- Tests: `pytest tests/unit` with `SF_GPU_PROBE=null` (never bare `pytest`).
- Adding Intel support would mean a new GpuProbe plus a different child server (OVMS or llama.cpp OpenVINO/Vulkan build) in engine/supervisor/planner/manager (very large files). Out of scope unless the owner asks.

## NPU runtime and model
- **Runtime: OpenVINO Model Server (OVMS) 2026.4**, Windows zip `ovms_windows_2026.4.0_python_on.zip` (or `_python_off`), run `setupvars.ps1`; needs VC++ Redistributable. OpenAI-compatible at `/v3/chat/completions` (2026.3+ also `/v1/chat/completions`), streaming, tool calling with `--tool_parser hermes3`, `chat_template_kwargs` supported. NPU limits: one request at a time, no `n`, `finish_reason` only `stop`/`tool_calls`.
- **Primary model: `OpenVINO/Qwen3-4B-int4-ov`** (INT4_SYM gs128 ratio 1.0, ~2.29 GB, Apache-2.0, OpenVINO >= 2026.0). Send `chat_template_kwargs: {"enable_thinking": false}` for fast answers. Not in Intel's "optimized for NPU" collection, so verify the first compile.
- **Fallback: `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov`** (~0.93 GB, NPU-supported in 2026.0, hermes-style tools).
- Avoid: Qwen3-8B (memory on 16 GB), asymmetric exports (Qwen3-1.7B/0.6B-int4-ov, Phi-4-mini), Qwen3.5/3.6, LFM2.
- NPU prompt window is static: set `--max_prompt_len 4096` (ceiling ~8K); history + tool schemas + search results count toward it.
- First NPU compile takes ~5–6 minutes; with `--cache_dir` later loads take seconds. Consider `NPUW_LLM_GENERATE_HINT=BEST_PERF`.
- Expected speed roughly 15–18 tok/s (estimate).
- Suggested commands:
  ```
  ovms --pull --source_model OpenVINO/Qwen3-4B-int4-ov --model_repository_path models --target_device NPU --task text_generation --tool_parser hermes3 --cache_dir .ov_cache --enable_prefix_caching true --max_prompt_len 4096
  ovms --rest_port 8000 --config_path models\config.json
  ```
- llama.cpp's OpenVINO NPU backend exists but is experimental and slow (~2 tok/s); Vulkan/SYCL on the Arc iGPU is the llama.cpp path.

## Owner's code conventions (from public repos)
- Python >= 3.12, `src/<pkg>/` layout, `pyproject.toml`, uv + committed `uv.lock`, pytest (`tests/unit`), ruff (line-length 100, py312), MIT.
- LLM client: raw **httpx** against `POST {base_url}/chat/completions` (no openai SDK). Streaming template: `crucibleforge/api.py` in LaserLloyd/CrucibleForge (SSE parse, `reasoning_content`, `<think>` stripping, `_merge_tool_call_deltas`, stall/wall timeouts, retries).
- Tools: allowlist + `assert_tool_allowed()` guard (`src/mailforge/agent/tools.py` in MailForge).
- Config: TOML via pydantic-settings + platformdirs (`%APPDATA%`/`%LOCALAPPDATA%`), env override for home dir (MailForge `paths.py`). API keys via `api_key_env`, never stored.
- CI: GitHub Actions matrix ubuntu+windows, `uv sync`, `ruff check`, `pytest -q`.

## UI stack recommendation
- **pystray + pywebview 6.x (WebView2)**. Frameless, on-top window placed at the work-area lower-right (`SystemParametersInfo(SPI_GETWORKAREA)`, DPI-aware), shown/hidden from the tray icon. Global hotkey via ctypes `RegisterHotKey` (not the `keyboard` package). Win11 rounded corners via `DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE)`. `webview.start()` owns the main thread; tray via `run_detached()`.
- Markdown rendering in the page (vendored marked/markdown-it + sanitizer, plus the UnifyingTheme `.ui-markdown` style). No CDN at runtime.
- Web search: `ddgs` 9.x (`from ddgs import DDGS; DDGS(timeout=10).text(q, max_results=5)` -> title/href/body). Synchronous and may be rate-limited: run in a thread, catch errors, truncate results. Optional `fetch_url` tool (httpx + text extraction).
- Packaging later: PyInstaller `--noconsole --onedir`.

## MiniMax API findings (2026-09-30, docs research, no live calls)
- Base URLs are region-bound: international `https://api.minimax.io/v1`, mainland China `https://api.minimaxi.com/v1`. A wrong-region key returns **HTTP 200** with `base_resp.status_code` 1004 ("cookie is missing") or 2049 ("invalid api key"). Make the base URL a provider setting with an International/China toggle, and show that hint on 1004/2049.
- Subscription ("Token Plan", formerly Coding Plan) keys start with `sk-cp-` and work on the OpenAI-compatible `/v1/chat/completions` with `Authorization: Bearer`. Quota uses 5-hour and weekly windows. Plan-key usage endpoint `/v1/token_plan/remains` (third-party source).
- Models: `MiniMax-M3` (1M ctx, confirmed on plan keys), `MiniMax-M3.1-Flash-Preview`, `MiniMax-M2.7`, `MiniMax-M2.7-highspeed` (204.8K). `GET /v1/models` is documented but may 404; fall back to a seeded list.
- Reasoning is always on for M2.x/M3; disabling it returns 400 (code 2013). Send `reasoning_split: true` so thinking arrives in a separate field. The field name is `reasoning_content` or `reasoning_details` depending on the doc page: handle both.
- Tool calling uses the standard OpenAI `tools` format, `finish_reason: "tool_calls"`, and streamed `delta.tool_calls` merged by index. **Echo the full assistant message back unchanged, including reasoning and tool_calls**, before the `role: tool` results (interleaved thinking).
- Errors: always check `base_resp` even on HTTP 200. 1002 means rate limit, 1008 balance, 1039 token limit, 2013 bad params (400), 2056 plan usage limit (wait for the window). Rate limits: M3 200 RPM, M2.x 500 RPM.
- Unsupported params: presence/frequency penalty, logit_bias, `n`>1. `temperature` [0,2], default 1.0. Use `max_completion_tokens`. `stream_options.include_usage` for usage.
- To verify with the first real call: which reasoning field name comes back, the key's region, and whether M2.7 is on the plan.
