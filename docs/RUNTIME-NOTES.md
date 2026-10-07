# Runtime notes: OVMS 2026.4 on the Intel NPU (WS1 Phase A)

These results were measured on 2026-09-30 on the target laptop:

- Core Ultra 5 226V with the Intel AI Boost NPU
- NPU driver 32.0.100.4841
- 16 GB RAM
- Windows 11 26H2, no admin
- VC++ x64 runtime v14.50.35719.00

`scripts/ovms_bringup.ps1` reproduces the whole bring-up.

This is a record of the Phase A measurements. The recommendations in it have since been applied: `catalog.toml` makes Qwen2.5-1.5B the recommended model and avoids Qwen3-4B, the `[chat] model` default is Qwen2.5-1.5B, and the `[local]` defaults below are the shipped ones (the config has since gained a few more keys, such as `precompile` and `npu_fallback_device`). The "WS" and "PLAN §" references point into [`PLAN.md`](PLAN.md), the original build plan.

## TL;DR

- **The default local model is `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov`, not Qwen3-4B.** Qwen3-4B-int4-ov compiles on the NPU but its output is garbage (see "Model gate" below). Qwen2.5-1.5B produces coherent output, and its hermes3 tool calls are clean.
- **Qwen2.5-1.5B on the NPU:**
  - The first compile takes **45 s**. A cached load takes **~3 s**.
  - Decoding runs at **42–51 tok/s**. Time to first token is **0.5–0.8 s** for short prompts.
  - The compile cache is **306 MiB**.
- **Use the `python_on` package.** It bundles its own embedded Python 3.12.10, so it needs no system Python.
- **Readiness:** `/v2/health/ready` returns 503 and then 200. At the same time, `/v1/config` returns `{}` and then `"state": "AVAILABLE"`. No `LOADING` state is ever shown.
- **`/v3/tokenize` exists.** Send `POST {"model", "text"}` and it returns `{"tokens": [...]}`.
- **`graph.pbtxt` is not written.** With `--task` on the command line, OVMS builds the graph in memory, and command-line flags win over a stale `graph.pbtxt`.
- **Pass `--max_prompt_len` only on the NPU.** The CPU and GPU plugins reject it and OVMS exits.

## Package, SHA-256 and sizes

| Item | Value |
|---|---|
| Asset | `ovms_windows_2026.4.0_python_on.zip` from `https://github.com/openvinotoolkit/model_server/releases/download/v2026.4.0/` |
| Size | 138,798,816 bytes |
| **SHA-256** (Get-FileHash, matches the release `.sha256`) | `5a022e44e794e6a9cb0f1c6c40822167dac53a36daf9af123c9411974cef1914` |
| `python_off` (not downloaded) | 117,195,695 bytes. SHA-256 from the release `.sha256`: `46d03114c97abfe05f2c5a8fde772c655aeef541ee254c23f402f81a616474e3` |
| Extracted to | `%LOCALAPPDATA%\ChatForge\runtime\ovms-2026.4.0\ovms\` (a single top-level `ovms\` folder in the zip) |
| Extracted size | 374,683,096 bytes (357 MiB), 2,949 files |
| Version string | `OpenVINO Model Server 2026.4.0.869b2186`, OpenVINO `2026.4.0-22959`, GenAI `2026.4.0.0-3407`, build flags `win_mp_on_py_on` |

The pinned values live in `chatforge.runtime.ovms_install.ASSETS`. The runtime marker `.chatforge-runtime.json` is written in `ovms-2026.4.0\`.

### python_on and python_off

- **`python_on` bundles an embeddable CPython 3.12.10.**
  - The interpreter is in `ovms\python\`: `python.exe`, `python312.dll`, `pyovms.pyd`, and `Lib\site-packages` with jinja2 3.1.6, MarkupSafe 3.0.2, pip and setuptools.
  - `python312._pth` isolates it. The log's `Python sys.path output` lists only the bundled folders.
  - **No system Python is needed**, and the app venv's Python can't leak in, as long as `PYTHONHOME` points at the bundle.
  - At startup the log shows `PythonInterpreterModule started`.
- **`python_off` was not tested.** PLAN §7 makes `python_on` the default because the docs say `python_off` cannot use tools and drops the system message. `python_off` stays opt-in and unvalidated.

## Environment (`setupvars.ps1` and `setupvars.bat`, 2026.4.0)

`setupvars.ps1` sets exactly these variables. `ovms_env()` in `ovms_supervisor.py` mirrors it.

| Var | Value (python_on, when `ovms\python` exists) |
|---|---|
| `OVMS_DIR` | `<ovms dir>` |
| `PYTHONHOME` | `<ovms dir>\python` |
| `SCRIPTS` | `<ovms dir>\python\Scripts` |
| `PATH` | `<ovms dir>;<PYTHONHOME>;<SCRIPTS>;<old PATH>` |
| `ESPEAK_DATA_PATH` | `<ovms dir>\espeak-ng-data` (if present) |

- Without `ovms\python` (python_off), `setupvars.ps1` only sets `PATH = <old PATH>;<ovms dir>`.
- **It never sets `PYTHONPATH`.** `setupvars.bat` is equivalent, except that it always sets `PYTHONHOME`.

`ovms_env()` also **strips** these variables:

- `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`, `PYTHONHOME`, `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONEXECUTABLE` and `__PYVENV_LAUNCHER__`.
- The venv's `Scripts` folder, removed from `PATH`.
- **`API_KEY`.** OVMS reads the `API_KEY` env var ("API key not provided via --api_key_file or API_KEY environment variable") and, if it is set, turns on auth for the generative endpoints.

## Working launch (validated)

```
ovms.exe --rest_port <P> --rest_bind_address 127.0.0.1
         --model_name OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov
         --model_path %LOCALAPPDATA%\ChatForge\models\OpenVINO\Qwen2.5-1.5B-Instruct-int4-ov
         --task text_generation --target_device NPU --max_prompt_len 4096
         --cache_dir %LOCALAPPDATA%\ChatForge\cache\ov\OpenVINO--Qwen2.5-1.5B-Instruct-int4-ov\NPU-4096
         --tool_parser hermes3 --log_level INFO
```

- **Working directory and environment:** the working directory is the OVMS dir, and the environment comes from `ovms_env()`. The process is spawned `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED`, assigned to the kill-on-close job, then resumed.
  - Windows still attaches a headless `conhost.exe` child. `kill_process_tree` removes it.
- **Parser flags:**
  - Add `--reasoning_parser qwen3` only for Qwen3-family models.
  - Qwen2.5 has no reasoning parser. `chat_template_kwargs: {"enable_thinking": false}` is accepted and ignored.
- **No gRPC `--port`:** "Port was not set. GRPC server will not be started."
- **Supported parsers in this build** (read from the binary):
  - Tool parsers: `hermes3`, `llama3`, `phi4`, `mistral`, `gptoss`, `devstral`, `qwen3coder`, `lfm2`, `gemma4`, `minicpm5`, `onyx`.
  - Reasoning parsers: `qwen3`, `gptoss`, `gemma4`, `onyx`.
- **`--max_prompt_len` on CPU or GPU fails:** `NotFound: Unsupported property MAX_PROMPT_LEN by CPU plugin`, then `state changed to: LOADING_PRECONDITION_FAILED`, then exit code 1. `build_argv` emits the flag only when the device contains `NPU`.
- **The NPU uses the stateful "Language Model Legacy" servable** (log: `Initializing Language Model Legacy servable`), not continuous batching.

### Prefix caching (do not pass `--enable_prefix_caching`)

`--enable_prefix_caching` **defaults to true**, and on the NPU it is **not ignored**. `ovms --configure` shows that it becomes:

```
plugin_config: '{"MAX_PROMPT_LEN":4096,"CACHE_DIR":"…","DEVICE_PROPERTIES":{"NPU":{"NPUW_LLM_ENABLE_PREFIX_CACHING":true}}}'
```

It works and it helps. With the same 2,505-token system prompt sent three times, time to first token was **1.94 s, then 0.67 s and 0.69 s**.

Leave it at the default: `LaunchSpec.enable_prefix_caching=None` emits nothing. PLAN §7 cuts the config key, and that is fine because the default is what we want. PLAN §7's reason ("ignored on NPU") is wrong.

### `--plugin_config '{"NPUW_LLM_GENERATE_HINT":"BEST_PERF"}'`

The flag is accepted and it helps modestly, at a large one-off compile cost:

| Qwen2.5-1.5B | First compile | Blob | Decode |
|---|---|---|---|
| default (FAST_COMPILE) | 44.5 s | 321 MB | 42–46 tok/s |
| BEST_PERF | 135 s | 434 MB | 48.3 tok/s (+~10 %) |

**Recommendation:** keep `extra_args = []`. BEST_PERF is a validated opt-in. A different plugin config writes a different blob into the same `--cache_dir`, and the launch hash changes. For Qwen3-4B, BEST_PERF took 295 s to compile, which is where the "5–6 min" folklore comes from.

## Readiness endpoints and state strings

| Endpoint | While loading | Ready |
|---|---|---|
| `GET /v2/health/live` | 200 as soon as REST is up (~3.5 s after spawn) | 200 |
| `GET /v2/health/ready` | **503** `{"error":"Server is not ready"}` | **200** (empty body) |
| `GET /v1/config` | **200 `{}`**: the model is absent, and **no `LOADING` state is shown** | `{"<model>": {"model_version_status": [{"version": "1", "state": "AVAILABLE", "status": {"error_code": "OK", "error_message": "OK"}}]}}` |
| `GET /v3/models`, `GET /v1/models` | `{"data":[],"object":"list"}` | `{"data":[{"id":"<model>","object":"model","created":…,"owned_by":"OVMS"}]}` |
| `GET /v3/models/<id>` | – | 200 with the model object |
| `POST /v3/chat/completions` | **404** `{"error":"Mediapipe graph definition with requested name is not found"}` (same as a wrong model name) | 200 |
| `GET /v2/models/<id>/ready`, `GET /metrics` | 400 `Invalid request URL` (the second needs `--metrics_enable`) | – |

- **Load failure:** the log shows `Mediapipe: <model> state changed to: LOADING_PRECONDITION_FAILED`, then `Couldn't start model manager`, then the process exits with **code 1**.
- **Busy REST port:** the log shows `FATAL , Bind address failed at 127.0.0.1:<P>` and then `Failed to start REST server`. The process exits with code 1 about **25 s** later, and until then health probes time out. The supervisor retries once on a new port.
- **The supervisor's rule:** ready means `/v2/health/ready == 200` **and** the `/v1/config` state for our model id is `AVAILABLE`.
- **Phases reported through `on_tick`:**
  - `starting`: REST is not up yet.
  - `compiling`: REST is up and the cache is cold.
  - `loading`: REST is up and the cache is warm.
  - `ready`
- **Timing:** on Windows, the first poll against a port that is not listening yet costs about 1 s, because refused connects are retried at the SYN level.

## Timings, sizes and speed

| Model on NPU (max_prompt_len 4096) | First compile | Cached load | Cache dir | Decode tok/s | Output |
|---|---|---|---|---|---|
| **Qwen2.5-1.5B-Instruct-int4-ov** (INT4_SYM gs128, 936 MB) | **44.5 s** | **2.7–3.2 s** | **321,066,765 B (306 MiB)**, one `.blob` | **42–51** | good |
| Qwen2.5-1.5B, max_prompt_len 2048 | 28.4 s | – | 291 MB | – | good |
| Qwen3-4B-int4-ov (INT4_SYM gs128, 2.29 GB) | 71.8 s (63.6 s with prefix caching off) | 4.75 s | 500 MB | 17.6–19.4 | **garbage** |
| Qwen3-4B + BEST_PERF | 295 s | – | 704 MB | – | **garbage** |

- **Memory:** `ovms.exe` with Qwen2.5-1.5B loaded had 1.9 GB RSS (657 MB private), 88 threads and 0.5 % CPU at idle. With Qwen3-4B it had a 4.1 GB working set.
- **Time to first token:** 0.5–0.8 s for prompts under 300 tokens, and 1.9 s for 2.5k tokens (0.7 s when the prefix is cached).
- **Prompt limit:** a prompt of 3,913 tokens was accepted at 4096. A prompt of 4,300 tokens returned **HTTP 400**:

  ```
  {"error":"Mediapipe execution failed. MP status - INVALID_ARGUMENT: CalculatorGraph::Run() failed: \nCalculator::Process() for node \"LLMExecutor\" failed: Input length exceeds the maximum allowed length"}
  ```

  WS3 should map this to `context_overflow`, and WS6 should retry with half the history.
- **Leftover data:** the Qwen3-4B NPU cache was deleted because it is useless. The Qwen3-4B model files are kept.

## Model gate (PLAN §7, owner decision 2): result

1. **Qwen3-4B-int4-ov compiles on the NPU, but its output is garbage.** Every NPU configuration tried failed, with default sampling:
   - "What is 2+2?" (with a pirate system prompt) gave `I'm\n\n**1.** The\n\n**2.** The …`.
   - A thinking-on prompt gave `undsundsunds…`.
   - A paragraph request gave `:center:center…`.
   - The configurations were the default launch, `--enable_prefix_caching false`, and `NPUW_LLM_GENERATE_HINT=BEST_PERF`.
   - **On CPU the same files answer correctly** (`Arrr… 2 plus 2 is... 4`, 24 tok/s), so the model files are fine and the fault is NPU-specific.
   - Tool calls are therefore unusable. No `reasoning_content` ever arrived, because the qwen3 parser had nothing to parse.
2. **I searched the OpenVINO HF org for NPU `-cw-` (channel-wise) models of 4B parameters or fewer.**
   - Found: `gemma-3-4b-it-int4-cw-ov`, `Phi-3-mini-4k-instruct-int4-cw-ov` and `Phi-3.5-mini-instruct-int4-cw-ov`. The rest are 7–8B, or embeddings.
   - **None has a tool parser in OVMS 2026.4.** There is no gemma3 or phi3 parser, and their chat templates don't render `tools`. So they cannot meet the tools requirement, and I didn't use them.
3. **Fallback `OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov` passes.**
   - Revision `4d14c299e35d8b74e3471f9e92bd1377fae50736`, 935,999,480 bytes. All 16 files were verified against the HF tree sizes and LFS sha256.
   - It is downloaded to `%LOCALAPPDATA%\ChatForge\models\OpenVINO\Qwen2.5-1.5B-Instruct-int4-ov`.
   - Its output is coherent, and the system message is honoured (pirate test).
   - It makes clean hermes3 tool calls, and the tool-result round trip works.
   - **It is the working default.** `catalog.toml` should make it `recommended` and set Qwen3-4B to `avoid` with the note "NPU output corrupt on OVMS 2026.4 (gs128)". The `[chat] model` default should change to match.

## Chat API behaviour (Qwen2.5-1.5B, `/v3/chat/completions`)

- **`/v1/chat/completions` works too.** It returns identical output. `/v1/models` also works. Use `/v3`.
- **Streaming chunk shape:**
  - Chunks look like `{"choices":[{"index":0,"logprobs":null,"delta":{…},"finish_reason":null}],"created":…,"model":"…","object":"chat.completion.chunk","usage":null}`.
  - **On the NPU there is no initial `{"role":"assistant"}` delta.** The first chunk already carries content. The CPU pipeline does send it.
  - Content arrives about one token per chunk.
- **Tool call over streaming** comes as exactly two chunks, then the finish chunk:

  ```
  delta: {"tool_calls":[{"id":"C8jIBZT5o","type":"function","index":0,"function":{"name":"current_datetime"}}]}
  delta: {"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]}
  delta: {}, finish_reason: "tool_calls"
  {"choices":[], … "usage":{"prompt_tokens":163,"completion_tokens":16,"total_tokens":179}}   (with stream_options.include_usage)
  data: [DONE]
  ```

  - Ids are 9-character alphanumeric strings.
  - The arguments come as one complete JSON string.
  - `content` is empty. No `<tool_call>` text leaked into the content in any test.
- **Non-streaming:** `message` always carries `"tool_calls": []`, even when empty, and `"content": ""` when a tool is called.
- **`finish_reason`** was only ever `stop`, `length` or `tool_calls`.
- **Tool round trip:** sending the assistant message with its `tool_calls`, followed by `{"role":"tool","tool_call_id","content"}`, gets a correct final answer. For example: "Today's date is Wednesday, September 30, 2026 in GMT Summer Time."
- **Quirks:**
  - **The small model over-uses tools** when the system prompt doesn't discourage them. "What is the capital of France?" with tools offered called `current_datetime`. With the PLAN §1.7 system prompt ("Use tools only when they help …") it answered "The capital of France is Paris." directly. That steering now lives in `TOOLS_SENTENCE` in `chat/prompts.py` ("… answer simple questions directly"), added whenever tools are offered.
  - `tool_choice: "required"` is accepted but was not enforced (the model gave empty content). `tool_choice: "none"` is honoured.
- **Error bodies** are always `{"error": "<text>"}`:
  - 400 `… n value cannot be greater than best_of` when `n` is 2. Never send `n`.
  - 400 `… Messages array cannot be empty`.
  - 400 `… Input length exceeds the maximum allowed length` on overflow.
  - 404 `Mediapipe graph definition with requested name is not found` for a wrong model, or while loading.
  - 412 `… model field is missing in JSON body`.
- **Reasoning:**
  - The field was never observed, because Qwen3 was unusable. OVMS documents it as `delta.reasoning_content`.
  - With Qwen2.5 and no reasoning parser, `chat_template_kwargs.enable_thinking=false` is harmless.
  - WS3 should keep handling both `reasoning_content` and inline `<think>`.

### `/v3/tokenize` (exists, and gives exact counts)

- `POST /v3/tokenize {"model":"<id>","text":"Hello world, how are you?"}` returns `{"tokens":[9707,1879,11,1246,525,498,30]}`.
- `text` can also be a list, which returns a list of token lists.
- `prompt` and `messages` are **not** accepted ("text field is required").
- It tokenizes raw text and does **not** apply the chat template. Special tokens in the text are recognised: `<|im_start|>user\nhi<|im_end|>` gives 5 tokens.
- For exact budgeting, WS6 can tokenize the rendered messages or add about 5 tokens of overhead per message. Tool schemas also count: 2 small tools added about 190 prompt tokens.

## `graph.pbtxt` behaviour (PLAN §7 required change 2)

- **OVMS does not write `graph.pbtxt`** when it is launched with `--model_path … --task text_generation`. The log says `Graph config created in memory from model_path`, and the model folder is unchanged after every load.
- **`ovms --configure` writes one.** It needs no server; `ovms --configure --model_path … --task text_generation --target_device NPU --max_prompt_len 2048 …` writes it. The file bakes `plugin_config` (`MAX_PROMPT_LEN`, `CACHE_DIR`, `NPUW_LLM_ENABLE_PREFIX_CACHING`), `device`, `max_num_seqs:256`, `enable_prefix_caching` and the parsers into the `LLMCalculatorOptions`.
- **A stale graph loses to the command line.** With a stale `graph.pbtxt` saying 2048 and NPU-2048, a launch with `--max_prompt_len 4096` and the NPU-4096 cache dir behaved like this:
  - It logged "Graph config created in memory".
  - It used the NPU-4096 cache (a warm 4.75 s load, with no NPU-2048 dir created).
  - It **accepted a 2,513-token prompt**.
- **A different flag takes effect directly.** Relaunching Qwen2.5 with `--max_prompt_len 2048` compiled a new blob into NPU-2048 in 28 s. It accepted 1,500 tokens and rejected 2,500 with the overflow 400. The test cache was then deleted; 4096 is the default.
- **What `OvmsSupervisor.start()` does:**
  - It hashes the LaunchSpec fields: model_path, device, max_prompt_len, parsers, cache_dir, prefix caching, extra_args, variant and ovms_version.
  - When a `graph.pbtxt` exists and the hash differs from `state.json["launch"][model_id]["hash"]`, it **deletes** the file. This is hygiene only, since the command line wins anyway.
  - The registry (WS4) should treat `graph.pbtxt` as optional, as §7 says.

## Recommended `config.toml` `[local]` defaults

```toml
[local]
device = "NPU"
idle_unload_minutes = 10
max_prompt_len = 4096          # validated; 2048 also works (28 s compile); NPU only
enable_thinking = false        # harmless for Qwen2.5 (ignored); needed for any future Qwen3
autoload_on_open = true
load_timeout_s = 900           # real first compile is 45 s; 900 leaves room for BEST_PERF/larger models
ovms_version = "2026.4.0"
ovms_variant = "python_on"
extra_args = []                # BEST_PERF validated as opt-in: +10 % tok/s, 3x compile time
# no enable_prefix_caching (OVMS default true = NPUW prefix caching, which helps)
# no port (auto from 18611)

[chat]
model = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"
```

The catalog entry should use `tool_parser = "hermes3"` and no `reasoning_parser`, with the note "First NPU compile ~45 s, then ~3 s". `compile_cache.DEFAULT_FIRST_COMPILE_S` (360 s) is only the ETA when nothing has been measured. The real first-compile time is recorded in `state.json` and in the cache marker.

## Files on disk after Phase A

- `%LOCALAPPDATA%\ChatForge\runtime\downloads\ovms_windows_2026.4.0_python_on.zip` (+ `.sha256`)
- `%LOCALAPPDATA%\ChatForge\runtime\ovms-2026.4.0\` (+ `.chatforge-runtime.json`, adopted)
- `%LOCALAPPDATA%\ChatForge\models\OpenVINO\Qwen2.5-1.5B-Instruct-int4-ov\` (new) and `…\Qwen3-4B-int4-ov\` (kept, unusable on the NPU)
- `%LOCALAPPDATA%\ChatForge\cache\ov\OpenVINO--Qwen2.5-1.5B-Instruct-int4-ov\NPU-4096\` (warm, with `.chatforge-compiled.json`, first compile 44.53 s)
- `%LOCALAPPDATA%\ChatForge\state.json`: the `compiled` and `launch` keys
