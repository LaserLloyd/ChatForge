// Development mock of the Python bridge (PLAN 1.5 / 1.6).
//
// Loaded by bridge.js only when window.pywebview is absent (a plain browser served with
//   py -3.12 -m http.server -d src\chatforge\web 8765 ).
// install({emit}) defines window.pywebview.api with EVERY method in the contract, backed by
// realistic fake data, and pushes the same events Python would:
//   chat.start / chat.context / chat.phase / chat.delta / chat.tool_call / chat.tool_result /
//   chat.reset / chat.fallback / chat.done / chat.error, runtime.status (1 Hz while
//   starting/compiling),
//   download.progress, runtime.install, key.status, settings.changed.
// Providers mirror config.seed_providers() and the local models mirror
// src/chatforge/models/catalog.toml, so the mock shows what a fresh install shows.
//
// Magic words in a message (chat popup):
//   "search ..."  -> web_search tool call + result, then an answer
//   "date"/"time" -> current_datetime tool call
//   "calc"/"sqrt" -> calculator tool call
//   "refuse"      -> a "cannot look that up" reply, chat.reset, then a tool call and the answer
//   "fallback"    -> (local model selected) the load fails, chat.fallback to
//                    chat.fallback_provider (MiniMax when unset), then the answer from there
//   "error"       -> chat.error{server, retry}
//   "long"        -> a long answer (scroll testing)
//   "overflow"    -> chat.context: the model's window holds only the previous turn and this
//                    one, so the older messages are left out (the popup shows a divider and
//                    get_state a "notice" item); "overflow cut" also cuts this message's end.
//                    Any later message fits again and clears it, like the engine.
//   "document"/"docx" -> create_document tool call; its chat.tool_result carries the saved
//                    file ({name, path, size, kind}), and the reply mentions it
//   files attached and no other magic word -> an answer about the attached files
//   anything else -> the markdown showcase (table, code, footnote, callout), with reasoning
//                    when the selected provider is remote
// A remote provider that needs a key and has none -> chat.error{no_key, add_key} (StudioForge
// needs none). save_api_key then test_provider succeed unless the typed key starts with "bad"
// (then region_or_key).
//
// Attachments mirror src/chatforge/attachments.py: attach_data reads text files for real and
// estimates the text of Office files; images, old Office formats, programs and (without
// pypdf) PDFs are refused with the same messages; 20 MB per file, 10 per message, text cut
// at tools.attachment_max_chars. attach_files stands in for the native dialog: it "picks"
// window.__mock.nextPick when set ([{name, size, chars?, error?}], or null to cancel), else
// SAMPLE_PICK. Documents are "saved" under MOCK_DOCS_DIR; open_document/reveal_document
// accept only those.
//
// No secret is ever stored: the mock remembers only THAT a key was saved.

const MOCK_KEY = 'chatforge.mock.v1';
const TIME_SCALE_FIRST_COMPILE_S = 12;   // real seconds a "6 minute" first compile takes
const TIME_SCALE_CACHED_S = 3;           // real seconds a cached load takes

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now() / 1000;
const clone = (o) => JSON.parse(JSON.stringify(o));
const ok = (extra = {}) => ({ ok: true, ...extra });
const fail = (code, message, hint = '', action = null) => ({ ok: false, error: { code, message, hint, action } });

// ---------------------------------------------------------------- state ----

/** A provider spec with ProviderSpec's defaults filled in, as get_settings returns it. */
function providerSpec(id, fields) {
  return {
    id, kind: 'openai', display_name: id, base_url: null, region: null, api_key_env: null,
    models: [], default_model: null, quirks: [], supports_tools: true, max_output_tokens: 2048,
    temperature: null, extra_body: {}, timeouts: { connect_s: 10, stall_s: 90, wall_s: 600 },
    builtin: false, key_required: null, docs_url: null, context_tokens: null, model_context: {},
    ...fields,
  };
}

// config.seed_providers(). Only local-npu is builtin in the config; every seeded id is shown
// as built in and cannot be removed (SEED_IDS).
const SEED_PROVIDERS = {
  'local-npu': providerSpec('local-npu', { kind: 'ovms', display_name: 'Local (NPU)', builtin: true, quirks: ['ovms'] }),
  minimax: providerSpec('minimax', {
    display_name: 'MiniMax', region: 'international', base_url: 'https://api.minimax.io/v1', api_key_env: 'MINIMAX_API_KEY',
    models: ['MiniMax-M3', 'MiniMax-M2.7-highspeed', 'MiniMax-M2.7', 'MiniMax-M3.1-Flash-Preview'],
    default_model: 'MiniMax-M3', quirks: ['minimax'], max_output_tokens: 4096, temperature: 1.0,
    extra_body: { reasoning_split: true }, timeouts: { connect_s: 10, stall_s: 90, wall_s: 600 },
    context_tokens: 1000000,
  }),
  studioforge: providerSpec('studioforge', {
    display_name: 'StudioForge', base_url: 'http://localhost:1234/v1', api_key_env: 'STUDIOFORGE_API_KEY',
    models: ['unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S'], default_model: 'unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S',
    max_output_tokens: 4096, timeouts: { connect_s: 10, stall_s: 300, wall_s: 900 }, key_required: false,
  }),
  openai: providerSpec('openai', {
    display_name: 'OpenAI', base_url: 'https://api.openai.com/v1', api_key_env: 'OPENAI_API_KEY',
    models: ['gpt-4o-mini', 'gpt-4o'], default_model: 'gpt-4o-mini', quirks: ['openai'], max_output_tokens: 4096,
    docs_url: 'https://platform.openai.com/api-keys',
  }),
  deepseek: providerSpec('deepseek', {
    display_name: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', api_key_env: 'DEEPSEEK_API_KEY',
    models: ['deepseek-chat', 'deepseek-reasoner'], default_model: 'deepseek-chat', quirks: ['deepseek'],
    max_output_tokens: 4096, docs_url: 'https://platform.deepseek.com/api_keys',
  }),
};
const SEED_IDS = new Set(Object.keys(SEED_PROVIDERS));

// config.py LocalCfg.max_prompt_len
const MPL_MIN = 1024;
const MPL_MAX = 8192;

const DEFAULT_CONFIG = {
  schema_version: 3,
  chat: {
    provider: 'local-npu',
    model: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov',
    system_prompt: 'You are ChatForge, a concise desktop assistant. Answer briefly, and work things out step by step when a question needs it.',
    instructions: '',
    max_prompt_chars: 4000,
    max_tool_rounds: 6,
    fallback_provider: '',
    fallback_model: '',
    auto_refresh_models: true,
    recent_models: [],
    temperature: 0.7,
    max_output_tokens: 1024,
    show_reasoning: 'collapsed',
    persist_conversation: true,
  },
  local: {
    device: 'NPU', idle_unload_minutes: 10, max_prompt_len: 4096, enable_thinking: false,
    autoload_on_open: true, load_timeout_s: 900, ovms_version: '2026.4.0', ovms_variant: 'python_on',
    extra_args: [],
  },
  providers: SEED_PROVIDERS,
  tools: {
    // config.ToolsCfg.enabled: every tool.
    enabled: ['web_search', 'news_search', 'fetch_url', 'weather', 'wikipedia', 'exchange_rate',
      'current_datetime', 'calculator', 'create_document'],
    location: '', units: 'metric', web_search_max_results: 5, tool_result_max_chars_local: 1500, tool_result_max_chars_cloud: 6000,
    block_private_addresses: true, attachment_max_chars: 200000, documents_dir: '',
  },
  ui: { theme: 'laserlloyd', hotkey: 'Ctrl+Alt+C', hide_on_blur: true, show_on_reply: true, width: 420, height: 620, margin: 12 },
  startup: { autostart: true },
  logging: { level: 'INFO' },
  hf: { endpoint: 'https://huggingface.co', default_author: 'OpenVINO' },
};

// src/chatforge/models/catalog.toml: Qwen2.5-1.5B is the one catalog entry (recommended);
// Qwen3-4B-int4-ov is on the avoid list, so an installed copy has no catalog entry (the
// registry reports catalog: null for it, like any model outside the catalog).
const CATALOG_NOTE = 'Default. Verified on the Lunar Lake NPU: ~45 s first compile, ~3 s cached load, 42-51 tok/s, clean tool calls.';
const LOCAL_MODELS = [
  {
    id: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov', path: 'C:\\Users\\you\\AppData\\Local\\ChatForge\\models\\OpenVINO\\Qwen2.5-1.5B-Instruct-int4-ov',
    size_bytes: 930000000, complete: true, missing: [],
    catalog: { id: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov', label: 'Qwen2.5 1.5B Instruct INT4', npu: 'recommended', approx_gb: 0.93,
      tool_parser: 'hermes3', reasoning_parser: null, thinking_toggle: false, licence: 'apache-2.0', note: CATALOG_NOTE },
    last_used_at: now() - 3600, compiled: { NPU: true, GPU: false, CPU: false },
  },
  {
    id: 'OpenVINO/Qwen3-4B-int4-ov', path: 'C:\\Users\\you\\AppData\\Local\\ChatForge\\models\\OpenVINO\\Qwen3-4B-int4-ov',
    size_bytes: 2290000000, complete: true, missing: [],
    catalog: null,
    last_used_at: null, compiled: { NPU: false, GPU: false, CPU: false },
  },
];

function loadPersisted() {
  try {
    const raw = sessionStorage.getItem(MOCK_KEY);
    if (raw) return JSON.parse(raw);
  } catch { /* storage unavailable */ }
  return null;
}

function createState() {
  if (new URLSearchParams(location.search).get('mock') === 'reset') {
    try { sessionStorage.removeItem(MOCK_KEY); } catch { /* ignore */ }
  }
  const persisted = loadPersisted();
  const st = {
    config: clone(DEFAULT_CONFIG),
    keySaved: {},            // provider_id -> true (never the key itself)
    envKey: {},              // provider_id -> true if the env var "is set"
    conversation: [],
    contextStartTs: null,     // Conversation.context_start_ts: where the model's view starts
    models: clone(LOCAL_MODELS),
    runtime: {
      state: 'unloaded', model_id: null, device: 'NPU', elapsed_s: 0, expected_s: 0, first_compile: false,
      idle_timeout_s: 600, unload_at: null, error: null,
    },
    runtimeInstalled: true,
    pinned: false,
    autostart: true,
    downloads: {},
    logs: [],
    attachments: new Map(),   // pending attachment id -> {view, text}
    documents: [],            // paths of the documents create_document "saved"
  };
  if (persisted) {
    if (persisted.config) st.config = { ...st.config, ...persisted.config };
    st.keySaved = persisted.keySaved || {};
    st.conversation = persisted.conversation || [];
    st.contextStartTs = persisted.contextStartTs ?? null;
    st.documents = persisted.documents || [];
    if (persisted.compiled) for (const m of st.models) if (m.id in persisted.compiled) m.compiled = persisted.compiled[m.id];
    if (persisted.providers) st.config.providers = persisted.providers;
  }
  return st;
}

let S = null;
let emitFn = () => {};
const timers = new Set();
const requests = new Map();   // request_id -> { cancelled }
let rid = 0;
let toolSeq = 0;

function persist() {
  try {
    sessionStorage.setItem(MOCK_KEY, JSON.stringify({
      config: S.config, keySaved: S.keySaved, conversation: S.conversation.slice(-40), contextStartTs: S.contextStartTs,
      providers: S.config.providers, documents: S.documents,
      compiled: Object.fromEntries(S.models.map((m) => [m.id, m.compiled])),
    }));
  } catch { /* storage unavailable */ }
}

function emit(evt) { emitFn(evt); }

function log(level, msg) {
  const d = new Date();
  const ts = d.toISOString().replace('T', ' ').slice(0, 23);
  S.logs.push(`${ts} ${level.padEnd(7)} ${msg}`);
  if (S.logs.length > 2000) S.logs.shift();
}

// ------------------------------------------------------------ providers ----

function isLoopbackUrl(url) {
  let host = '';
  try { host = new URL(String(url || '')).hostname.toLowerCase(); } catch { return false; }
  return ['127.0.0.1', 'localhost', '[::1]', '::1'].includes(host) || host.endsWith('.localhost');
}

/** llm/providers.key_required: remote providers need a key unless they declare it optional
 *  (StudioForge) or their base URL is loopback. */
function keyRequired(spec) {
  if (!spec || spec.kind === 'ovms') return false;
  if (spec.key_required != null) return !!spec.key_required;
  return !isLoopbackUrl(spec.base_url);
}

function keyStatus(pid) {
  const spec = S.config.providers[pid];
  const envName = spec && spec.api_key_env ? spec.api_key_env : null;
  const saved = !!S.keySaved[pid];
  const env = !!(envName && S.envKey[envName]);
  if (spec && spec.kind === 'ovms') return { source: 'none', env_name: null, env_overrides_saved: false, required: false };
  return {
    source: env ? 'env' : (saved ? 'keyring' : 'none'),
    env_name: envName,
    env_overrides_saved: env && saved,
    required: keyRequired(spec),
  };
}

function providerView(pid) {
  const spec = S.config.providers[pid];
  const local = spec.kind === 'ovms';
  const installed = S.models.filter((m) => m.complete).map((m) => m.id);
  return {
    id: pid,
    display_name: spec.display_name || pid,
    kind: spec.kind,
    models: local ? installed : (spec.models || []),
    default_model: local
      ? (installed.includes(S.config.chat.model) ? S.config.chat.model : installed[0] || null)
      : (spec.default_model || (spec.models || [])[0] || null),
    region: spec.region || null,
    base_url: spec.base_url || null,
    builtin: !!spec.builtin || SEED_IDS.has(pid),
    docs_url: spec.docs_url || null,
    key: keyStatus(pid),
  };
}

function uiConfig() {
  const c = S.config;
  return {
    chat: { provider: c.chat.provider, model: c.chat.model, show_reasoning: c.chat.show_reasoning,
      max_prompt_chars: c.chat.max_prompt_chars, recent_models: clone(c.chat.recent_models || []) },
    ui: clone(c.ui),
    local: { device: c.local.device, idle_unload_minutes: c.local.idle_unload_minutes,
      autoload_on_open: c.local.autoload_on_open },
  };
}

function isLocal(pid) { return (S.config.providers[pid] || {}).kind === 'ovms'; }

const RECENT_MODELS_KEPT = 3;

/** Like the bridge: put provider/model first in chat.recent_models (newest first, unique,
 *  at most three). False when it is first already. */
function rememberModel(provider, model) {
  const recent = S.config.chat.recent_models || [];
  if (recent[0] && recent[0].provider === provider && recent[0].model === model) return false;
  S.config.chat.recent_models = [{ provider, model },
    ...recent.filter((r) => r.provider !== provider || r.model !== model)].slice(0, RECENT_MODELS_KEPT);
  return true;
}

// autostart.task_script(): the .vbs the "ChatForge" logon task in Task Scheduler runs.
const AUTOSTART_SCRIPT = 'C:\\Users\\you\\AppData\\Local\\ChatForge\\ChatForge.vbs';

/** get_autostart's reply: autostart.status() with the logon task, as the bridge reports it
 *  (AutostartStatus.describe() is the description). */
function autostartView(detail = '') {
  const path = S.autostart ? AUTOSTART_SCRIPT : null;
  const description = `autostart ${S.autostart ? 'enabled' : 'not enabled'} via Task Scheduler`
    + (path ? ` (${path})` : '') + (detail ? ` - ${detail}` : '');
  return { enabled: S.autostart, mode: 'task-scheduler', mechanism: 'Task Scheduler', path, detail, duplicate: null, description };
}

// -------------------------------------------------------------- runtime ----

let loadTimer = null;
let idleTimer = null;

function pushRuntime() { emit({ type: 'runtime.status', ...S.runtime }); }

function touchIdle() {
  clearTimeout(idleTimer);
  const mins = S.config.local.idle_unload_minutes;
  S.runtime.idle_timeout_s = mins * 60;
  if (S.runtime.state !== 'ready' || !mins) { S.runtime.unload_at = null; return; }
  S.runtime.unload_at = now() + mins * 60;
  idleTimer = setTimeout(() => unload('idle'), mins * 60 * 1000);
}

function unload(reason) {
  clearInterval(loadTimer); clearTimeout(idleTimer);
  // Like the engine: a reply still coming from the local model stops with its own message.
  for (const req of requests.values()) {
    if (req.local && !req.cancelled) { req.cancelled = true; req.cancelReason = 'unloaded'; }
  }
  S.runtime = { ...S.runtime, state: 'unloaded', elapsed_s: 0, expected_s: 0, first_compile: false,
    unload_at: null, error: null, model_id: null };
  log('INFO', `local model unloaded (${reason})`);
  pushRuntime();
}

function startLoad(modelId) {
  const model = S.models.find((m) => m.id === modelId) || S.models[0];
  if (S.runtime.state === 'ready' && S.runtime.model_id === model.id) return Promise.resolve(true);
  if (loadTimer) return loadPromise;
  const dev = S.config.local.device;
  const first = !model.compiled[dev];
  const realTotal = first ? TIME_SCALE_FIRST_COMPILE_S : TIME_SCALE_CACHED_S;
  const expected = first ? 360 : 10;
  S.runtime = { ...S.runtime, state: 'starting', model_id: model.id, device: dev, elapsed_s: 0,
    expected_s: expected, first_compile: first, unload_at: null, error: null,
    idle_timeout_s: S.config.local.idle_unload_minutes * 60 };
  log('INFO', `loading ${model.id} on ${dev} (${first ? 'first compile' : 'cached'})`);
  pushRuntime();
  const t0 = Date.now();
  loadPromise = new Promise((resolve) => {
    loadTimer = setInterval(() => {
      const real = (Date.now() - t0) / 1000;
      S.runtime.elapsed_s = Math.min(expected, expected * (real / realTotal));
      if (real > 0.9 && S.runtime.state === 'starting') S.runtime.state = 'compiling';
      if (real >= realTotal) {
        clearInterval(loadTimer); loadTimer = null;
        model.compiled[dev] = true;
        S.runtime.state = 'ready';
        S.runtime.elapsed_s = expected;
        touchIdle(); persist();
        log('INFO', `${model.id} ready`);
        pushRuntime();
        resolve(true);
        return;
      }
      pushRuntime();
    }, 1000);
  });
  return loadPromise;
}
let loadPromise = Promise.resolve(true);

// ---------------------------------------------------------- attachments ----
// src/chatforge/attachments.py, as much as the popup needs: the same limits, kinds and messages.

const MAX_FILES = 10;
const MAX_FILE_BYTES = 20 * 1024 * 1024;
const MAX_PENDING = 50;
const PDF_NEEDS_PYPDF = 'Reading PDFs needs the pypdf package: py -3.12 -m uv add pypdf';
const IMAGES_UNSUPPORTED = 'Images are not supported yet. Attach text, code, Word, Excel, PowerPoint or PDF files.';
const extSet = (s) => new Set(s.split(' '));
const KIND_EXTS = {
  html: extSet('.html .htm .xhtml'),
  data: extSet('.json .jsonl .csv .tsv .xml .yaml .yml .toml .ini .cfg .conf .env .reg'),
  code: extSet('.py .pyw .js .mjs .cjs .jsx .ts .tsx .css .scss .sql .ps1 .sh .bash .bat .cmd .c .h .cpp .cs .java .go .rs .rb .php .swift .kt .lua .vue .svg'),
  text: extSet('.txt .text .md .markdown .rst .log .nfo .srt .vtt'),
};
const OFFICE_KINDS = { '.docx': 'docx', '.docm': 'docx', '.xlsx': 'xlsx', '.xlsm': 'xlsx', '.pptx': 'pptx', '.pptm': 'pptx' };
const IMAGE_EXTS = extSet('.png .jpg .jpeg .gif .bmp .webp .tif .tiff .ico .heic .heif .avif .psd');
const LEGACY_FORMATS = {
  '.doc': ['Word', '.docx'], '.xls': ['Excel', '.xlsx'], '.ppt': ['PowerPoint', '.pptx'], '.rtf': ['Word', '.docx'],
  '.odt': ['Word or LibreOffice', '.docx'], '.ods': ['Excel or LibreOffice', '.xlsx'], '.odp': ['PowerPoint or LibreOffice', '.pptx'],
};
const BINARY_EXTS = extSet('.exe .dll .msi .zip .7z .rar .gz .tar .iso .mp3 .wav .mp4 .mkv .mov .avi .db .sqlite .ttf .woff .pyc .bin');
const OFFICE_NAMES = { docx: 'Word document', xlsx: 'Excel workbook', pptx: 'PowerPoint file' };

// What attach_files "picks" unless window.__mock.nextPick says otherwise: a Word file, a log
// too long to use whole and a picture, so one click shows chips, the "partial" badge and an error.
const SAMPLE_PICK = [
  { name: 'Quarterly report.docx', size: 48213 },
  { name: 'server.log', size: 3407872, chars: 3400000 },
  { name: 'whiteboard.png', size: 1843200 },
];

/** A file that cannot be attached; the message is attachments.py's. */
class Refused extends Error {}

/** clean_name(): no folders, no control characters. */
function cleanName(name) {
  const base = String(name == null ? '' : name).split(/[\\/]/).pop();
  return Array.from(base).filter((ch) => ch >= ' ' && ch !== '\x7f').join('').trim() || 'file';
}

/** The lower-case extension; the whole name for dot-files such as ".gitignore". */
function fileExt(name) {
  const lower = String(name || '').toLowerCase();
  const dot = lower.lastIndexOf('.');
  if (dot === 0) return lower;
  return dot > 0 ? lower.slice(dot) : '';
}

function kindFor(name) {
  const ext = fileExt(name);
  if (OFFICE_KINDS[ext]) return OFFICE_KINDS[ext];
  if (ext === '.pdf') return 'pdf';
  for (const [kind, exts] of Object.entries(KIND_EXTS)) if (exts.has(ext)) return kind;
  return null;
}

function decodeText(bytes) {
  try {
    if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder('utf-16le').decode(bytes.subarray(2));
    if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder('utf-16be').decode(bytes.subarray(2));
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes);   // drops a UTF-8 BOM
  } catch {
    return new TextDecoder('windows-1252').decode(bytes);
  }
}

/** decode_base64(): plain base64 or a data: URL, the size checked before decoding. */
function decodeBase64(data) {
  let text = String(data || '').trim();
  if (text.startsWith('data:')) { const comma = text.indexOf(','); text = comma >= 0 ? text.slice(comma + 1) : ''; }
  const padding = text.length - text.replace(/=+$/, '').length;
  if (Math.floor((text.length * 3) / 4) - padding > MAX_FILE_BYTES) throw new Refused('The file is larger than 20 MB.');
  let bin;
  try { bin = atob(text); } catch { throw new Refused('The file data could not be read.'); }
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

/** extract_text(): {kind, chars, truncated, warning, text}. With `bytes` (attach_data) text
 *  files are read for real; Office files are not unzipped and a dialog pick has no bytes,
 *  so their text is made up from the size (or `chars`). Throws Refused. */
function extract(name, size, bytes = null, chars = null) {
  if (size > MAX_FILE_BYTES) throw new Refused('The file is larger than 20 MB.');
  const ext = fileExt(name);
  const bom16 = !!bytes && ((bytes[0] === 0xff && bytes[1] === 0xfe) || (bytes[0] === 0xfe && bytes[1] === 0xff));
  const binary = !!bytes && !bom16 && bytes.includes(0);
  let kind = kindFor(name);
  if (!kind) {
    if (IMAGE_EXTS.has(ext)) throw new Refused(IMAGES_UNSUPPORTED);
    if (LEGACY_FORMATS[ext]) {
      const [program, newer] = LEGACY_FORMATS[ext];
      throw new Refused(`${ext} files are not supported. Save it as ${newer} in ${program} and attach that.`);
    }
    if (BINARY_EXTS.has(ext) || binary) {
      throw new Refused(`${ext ? `${ext} files are` : 'This type of file is'} not supported yet. Attach text, code, Word, Excel, PowerPoint or PDF files.`);
    }
    kind = 'text';
  }
  if (kind === 'pdf') throw new Refused(PDF_NEEDS_PYPDF);
  const office = !!OFFICE_NAMES[kind];
  let text;
  if (bytes && !office) {
    if (binary) throw new Refused('This file is not text, so it cannot be read.');
    text = decodeText(bytes).replace(/\r\n?/g, '\n').replace(/^\n+|\n+$/g, '');
    chars = text.length;
  } else {
    if (office && bytes && !(bytes[0] === 0x50 && bytes[1] === 0x4b)) {   // not a zip ("PK")
      throw new Refused(`The file could not be read: it is damaged, password-protected or not a real ${OFFICE_NAMES[kind]}.`);
    }
    chars = chars != null ? Number(chars) : Math.round(size * (office ? 0.3 : 1));
    text = `(the text of ${name})`;
  }
  if (!chars || !text.trim()) throw new Refused('There is no text in this file.');
  const limit = Number(S.config.tools.attachment_max_chars) || 200000;
  const truncated = chars > limit;
  return {
    kind, chars: Math.min(chars, limit), truncated, text: text.slice(0, limit),
    warning: truncated ? `Only the first ${limit.toLocaleString('en-US')} characters are used.` : null,
  };
}

/** "att_" + 22 random URL-safe characters, like secrets.token_urlsafe(16). */
function newAttachmentId() {
  const bytes = new Uint8Array(16);
  if (globalThis.crypto && globalThis.crypto.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return `att_${btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')}`;
}

/** Bridge._attach(): run each [name, load] (load returns {ex, size}) and keep what reads.
 *  A file that cannot be read is an `errors` entry, not a failed call. */
function attachReply(loaders) {
  const attachments = [];
  const errors = [];
  loaders.forEach(([name, load], i) => {
    if (i >= MAX_FILES) { errors.push({ name, message: `Only ${MAX_FILES} files can be attached to one message.` }); return; }
    try {
      const { ex, size } = load();
      const view = { id: newAttachmentId(), name, kind: ex.kind, chars: ex.chars, size, truncated: ex.truncated, warning: ex.warning };
      S.attachments.set(view.id, { view, text: ex.text });
      while (S.attachments.size > MAX_PENDING) S.attachments.delete(S.attachments.keys().next().value);
      attachments.push(clone(view));
    } catch (e) {
      if (!(e instanceof Refused)) console.error('[dev-mock] attach failed', e);
      errors.push({ name, message: e instanceof Refused ? e.message : 'The file could not be read.' });
    }
  });
  log('INFO', `attached ${attachments.length} file(s), ${errors.length} refused`);
  return ok({ attachments, errors });
}

/** Bridge._attachment_ids(): a list of unique ids (null or one id accepted). */
function attachmentIds(value) {
  if (value == null) return [];
  const list = typeof value === 'string' ? [value] : value;
  if (!Array.isArray(list)) throw new Refused('attachment_ids must be a list of ids.');
  return [...new Set(list.filter(Boolean).map(String))];
}

/** Like Bridge._emit_then_release: the files are forgotten once the reply is done or stopped. */
function releaseAttachments(req) {
  for (const id of req.attachmentIds || []) S.attachments.delete(id);
}

// ------------------------------------------------------------ documents ----
// tools/documents.py: create_document saves into the documents folder and never overwrites;
// open_document / reveal_document accept only files in that folder.

const MOCK_DOCS_DIR = 'C:\\Users\\you\\Documents\\ChatForge';

function saveDocument(filename, content) {
  const dot = filename.lastIndexOf('.');
  const stem = dot > 0 ? filename.slice(0, dot) : filename;
  const ext = dot > 0 ? filename.slice(dot) : '.md';
  const taken = (n) => S.documents.some((p) => p.toLowerCase() === `${MOCK_DOCS_DIR}\\${n}`.toLowerCase());
  let name = `${stem}${ext}`;
  for (let i = 2; taken(name); i++) name = `${stem} (${i})${ext}`;
  const path = `${MOCK_DOCS_DIR}\\${name}`;
  S.documents.push(path);
  const bytes = new TextEncoder().encode(content).length;
  const size = ext.toLowerCase() === '.docx' ? 2600 + bytes : bytes;   // a docx is a zip of XML parts
  log('INFO', `document_saved ${name} (${size} bytes)`);
  return { name, path, size, kind: kindFor(name) };
}

/** resolve_document(): the known path, or the bridge's refusal. */
function checkDocument(path) {
  const raw = String(path || '').trim();
  if (!raw || raw.includes('\0')) return fail('bad_request', 'No document was given.');
  const full = (/^[A-Za-z]:[\\/]|^[\\/]{2}/.test(raw) ? raw : `${MOCK_DOCS_DIR}\\${raw}`).replace(/\//g, '\\');
  const inside = !full.split('\\').includes('..') && full.toLowerCase().startsWith(`${MOCK_DOCS_DIR.toLowerCase()}\\`);
  if (!inside) {
    return fail('bad_request', 'Only files in the ChatForge documents folder can be opened from the chat.',
      `The documents folder is ${MOCK_DOCS_DIR}.`);
  }
  const known = S.documents.find((p) => p.toLowerCase() === full.toLowerCase());
  if (!known) return fail('not_found', 'That document no longer exists.', 'It may have been moved, renamed or deleted.');
  return ok({ path: known });
}

// ---------------------------------------------------------------- chat -----

const SHOWCASE = `Here is a quick tour of what I can render.

## A small table

| Runtime | Device | First load | Cached load |
|---|:---:|---:|---:|
| OVMS 2026.4 | NPU | ~6 min | ~10 s |
| OVMS 2026.4 | GPU | ~40 s | ~4 s |
| MiniMax | cloud | n/a | n/a |

## Some code

\`\`\`python
def fib(n: int) -> int:
    """Return the n-th Fibonacci number."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

print([fib(i) for i in range(10)])
\`\`\`

Inline \`code\`, **bold**, *italic* and a [link](https://example.com/docs) all work.[^1]

> [!NOTE]
> Compiled NPU graphs are cached per model, so only the first load is slow.

- First item
- Second item with a nested list
  - Nested item

And a task list:

- [x] A finished task
- [ ] An open task

[^1]: Footnotes render as a marker plus a list at the end of the message.
`;

const LONG_TEXT = Array.from({ length: 14 }, (_, i) =>
  `### Section ${i + 1}\n\nThis is paragraph ${i + 1} of a long answer, used to exercise scrolling and the scroll-to-bottom badge while the text is still streaming in.`).join('\n\n');

const REASONING = 'The user wants a demo of rendering. I should include a table, a code block and a footnote, keep it compact, and mention the compile cache. Let me structure it with headings.';

function chunk(text) {
  const out = [];
  let i = 0;
  while (i < text.length) {
    const n = 4 + Math.floor(Math.random() * 12);
    out.push(text.slice(i, i + n));
    i += n;
  }
  return out;
}

/** Stream `text` as deltas of `field`, adding each chunk to `record[field]` so a stopped
 *  reply keeps exactly what was shown. False when the request was cancelled. */
async function streamText(req, record, text, field = 'content', delay = 30) {
  for (const c of chunk(text)) {
    if (req.cancelled) return false;
    emit({ type: 'chat.delta', request_id: req.id, [field]: c });
    record[field] += c;
    await sleep(delay);
  }
  return true;
}

function pickScript(text, hasFiles = false) {
  const s = text.toLowerCase();
  if (/\berror\b/.test(s)) return 'error';
  if (/\bfallback\b/.test(s)) return 'fallback';
  if (/\brefuse\b/.test(s)) return 'refuse';
  if (/\bsearch\b/.test(s)) return 'search';
  if (/\b(date|time|today)\b/.test(s)) return 'clock';
  if (/\b(calc|sqrt|math)\b/.test(s)) return 'calc';
  if (/\b(document|docx)\b/.test(s)) return 'document';
  if (/\boverflow\b/.test(s)) return 'overflow';
  if (/\blong\b/.test(s)) return 'long';
  if (hasFiles) return 'files';
  return 'showcase';
}

/** One tool call and its result. `save` (create_document) writes the file when the tool
 *  runs, so a stopped call saves nothing and the name (" (2)" when taken) is known only then. */
async function toolRound(req, name, args, resultOk, summary, save = null) {
  const call_id = `call_${++toolSeq}`;
  emit({ type: 'chat.phase', request_id: req.id, phase: 'calling_tool' });
  emit({ type: 'chat.tool_call', request_id: req.id, call_id, name, arguments: JSON.stringify(args).slice(0, 300) });
  await sleep(900);
  if (req.cancelled) return null;
  const document = save ? save() : null;
  if (document) summary = `Saved ${document.name}`;
  emit({ type: 'chat.tool_result', request_id: req.id, call_id, name, ok: resultOk, summary, ...(document ? { document } : {}) });
  await sleep(200);
  return { call_id, name, arguments: JSON.stringify(args), ok: resultOk, summary, ...(document ? { document } : {}) };
}

const KIND_WORDS = { text: 'text', code: 'code', data: 'data', html: 'a web page', docx: 'a Word document',
  xlsx: 'an Excel workbook', pptx: 'a PowerPoint deck', pdf: 'a PDF' };

function filesAnswer(files) {
  const one = files.length === 1;
  const lines = files.map((f) => `- **${f.name}**: ${KIND_WORDS[f.kind] || 'text'}, ${f.chars.toLocaleString('en-US')} characters`
    + `${f.truncated ? ' (only the first part was attached)' : ''}`);
  return `I read ${one ? 'the attached file' : `the ${files.length} attached files`}:\n\n${lines.join('\n')}\n\n`
    + `Ask me anything about ${one ? 'it' : 'them'}. (Mock answer: the text itself is not read.)`;
}

const DOC_MARKDOWN = '# Meeting summary\n\n## Decisions\n\n- Ship the popup on Friday\n- Keep the local model as the default\n\n'
  + '## Next steps\n\n1. **Alex**: write the release notes\n2. **Sam**: test on the NPU laptop\n';

// conversation.CONTEXT_CUT_NOTICE
const CONTEXT_CUT_NOTICE = 'Older messages are past the context window (too long for context).';

/** conversation.items(): the context_cut notice goes before the first user message the model
 *  still sees (sent at or after contextStartTs), unless that is the first message. */
function conversationItems() {
  const items = clone(S.conversation);
  if (S.contextStartTs == null) return items;
  const at = items.findIndex((m) => m.role === 'user' && m.ts != null && m.ts >= S.contextStartTs);
  if (at > 0) items.splice(at, 0, { role: 'notice', kind: 'context_cut', content: CONTEXT_CUT_NOTICE });
  return items;
}

/** The engine's context fitting, faked: with `overflow` only the previous turn and this one
 *  fit, and `cut` also cuts this message's end. Otherwise everything fits, which clears an
 *  earlier cut (chat.context with dropped_messages 0), as the engine does. */
function reportContext(req, overflow, cut) {
  const users = S.conversation.filter((m) => m.role === 'user');
  const current = users[users.length - 1];
  const first = overflow && users.length > 1 ? users[users.length - 2] : current;
  const dropped = overflow ? S.conversation.indexOf(first) : 0;   // items before it: close enough
  const messageCut = !!(overflow && cut);
  if (!dropped && !messageCut && S.contextStartTs == null) return;
  S.contextStartTs = dropped ? first.ts : null;
  if (messageCut && current) current.cut = true;
  persist();
  emit({ type: 'chat.context', request_id: req.id, dropped_messages: dropped,
    first_kept_ts: dropped ? first.ts : null, message_cut: messageCut });
}

const OVERFLOW_ANSWER = 'This conversation is longer than the model\'s context window, so the oldest messages were left out '
  + 'of the prompt. The divider above shows where my view of it starts. (Mock answer.)';

/** Like the engine: a message refused before it runs, or one that fails, is taken back out
 *  of the conversation, so Retry re-sends cleanly. */
function unrecord(text) {
  const last = S.conversation[S.conversation.length - 1];
  if (last && last.role === 'user' && last.content === text) S.conversation.pop();
}

/** A failed message is taken back out; a failed regenerate puts the reply it was replacing
 *  back instead. The extra chat.error fields: `{restored: true}` for the latter. */
function undo(req, text) {
  if (!req.restore) { unrecord(text); return {}; }
  S.conversation.push(...req.restore);
  req.restore = null;
  persist();
  return { restored: true };
}

async function runChat(text, req) {
  let pid = S.config.chat.provider;
  let model = S.config.chat.model;
  let local = isLocal(pid);
  req.local = local;
  emit({ type: 'chat.start', request_id: req.id, provider: pid, model, user_ts: req.userTs });
  log('INFO', `chat.start ${pid}/${model} (${text.length} chars, ${(req.files || []).length} files)`);
  const script = pickScript(text, !!(req.files && req.files.length));

  // "fallback": the local model fails to load and the message runs again elsewhere.
  if (local && script === 'fallback') {
    emit({ type: 'chat.phase', request_id: req.id, phase: 'loading_model' });
    await sleep(800);
    if (req.cancelled) return finishCancelled(req);
    const fb = S.config.providers[S.config.chat.fallback_provider] ? S.config.chat.fallback_provider : 'minimax';
    const fbModel = S.config.chat.fallback_model || providerView(fb).default_model;
    log('WARNING', `local model failed to load; falling back to ${fb}/${fbModel}`);
    emit({ type: 'chat.fallback', request_id: req.id, from_provider: pid, provider: fb, model: fbModel,
      reason: 'The local model failed to load.' });
    pid = fb; model = fbModel; local = false; req.local = false;
  }

  const spec = S.config.providers[pid];
  if (!local && keyRequired(spec) && keyStatus(pid).source === 'none') {
    const extra = undo(req, text);
    const env = spec.api_key_env ? ` (or set ${spec.api_key_env})` : '';
    emit({ type: 'chat.error', request_id: req.id, code: 'no_key', message: `Add your ${spec.display_name} API key`,
      hint: `Paste the key in the card below or in Settings → Providers${env}.`, action: 'add_key', provider: pid, ...extra });
    return;
  }

  if (local && S.runtime.state !== 'ready') {
    emit({ type: 'chat.phase', request_id: req.id, phase: 'loading_model' });
    await startLoad(model);
    if (req.cancelled) return finishCancelled(req);
  }

  if (script === 'error') {
    await sleep(500);
    const extra = undo(req, text);
    emit({ type: 'chat.error', request_id: req.id, code: 'server', message: 'The model server returned an error.',
      hint: 'See the logs for details.', action: 'retry', provider: pid, ...extra });
    return;
  }

  reportContext(req, script === 'overflow', /\bcut\b/i.test(text));

  const record = { role: 'assistant', content: '', reasoning: '', tools: [], ts: now(), model };
  const t0 = Date.now();
  let rounds = 1;

  if (!local || script === 'showcase') {
    emit({ type: 'chat.phase', request_id: req.id, phase: 'thinking' });
    if (!local && !(await streamText(req, record, REASONING, 'reasoning', 12))) return finishCancelled(req, record);
  }

  let answer;
  if (script === 'refuse') {
    // The engine's refusal recovery: the streamed text (and its reasoning) is dropped,
    // a tool runs, and the model answers again.
    emit({ type: 'chat.phase', request_id: req.id, phase: 'generating' });
    if (!(await streamText(req, record, 'I cannot browse the internet, so I cannot look that up for you.', 'content', 18))) {
      return finishCancelled(req, record);
    }
    emit({ type: 'chat.reset', request_id: req.id, reason: 'refusal' });
    record.content = ''; record.reasoning = '';
    rounds += 1;
    const r = await toolRound(req, 'web_search', { query: text.replace(/\brefuse\b/i, '').trim() || 'openvino release' }, true, '5 results');
    if (!r) return finishCancelled(req, record);
    record.tools.push(r);
    answer = 'Here is what I found: the latest **OpenVINO** release notes list NPU improvements for LLM serving (mock result).';
  } else if (script === 'search') {
    const r = await toolRound(req, 'web_search', { query: text.replace(/^search( the web)?( for)?/i, '').trim() || 'openvino npu' },
      true, '5 results');
    if (!r) return finishCancelled(req, record);
    record.tools.push(r);
    answer = 'I found five results. The most relevant is the **OpenVINO Model Server** documentation, which explains how to serve LLMs on an NPU.[^1]\n\n[^1]: Source: https://docs.openvino.ai (mock result).';
  } else if (script === 'clock') {
    const r = await toolRound(req, 'current_datetime', {}, true, new Date().toISOString().slice(0, 16).replace('T', ' '));
    if (!r) return finishCancelled(req, record);
    record.tools.push(r);
    answer = `Today is **${new Date().toDateString()}**.`;
  } else if (script === 'calc') {
    const r = await toolRound(req, 'calculator', { expression: 'sqrt(2)*10' }, true, '14.142135623730951');
    if (!r) return finishCancelled(req, record);
    record.tools.push(r);
    answer = 'sqrt(2) * 10 = **14.1421...**';
  } else if (script === 'document') {
    const filename = /\b(docx|word)\b/i.test(text) ? 'Meeting summary.docx' : 'Meeting summary.md';
    const r = await toolRound(req, 'create_document', { filename, content: DOC_MARKDOWN }, true, '',
      () => saveDocument(filename, DOC_MARKDOWN));
    if (!r) return finishCancelled(req, record);
    const { document, ...call } = r;
    record.tools.push(call);
    record.documents = [document];
    answer = `I saved **${document.name}** in your ChatForge documents folder. Use **Open** on the card below, or **Show in folder** to find it.`;
  } else if (script === 'files') {
    answer = filesAnswer(req.files);
  } else if (script === 'overflow') {
    answer = OVERFLOW_ANSWER;
  } else if (script === 'long') {
    answer = LONG_TEXT;
  } else {
    answer = SHOWCASE;
  }

  emit({ type: 'chat.phase', request_id: req.id, phase: 'generating' });
  if (!(await streamText(req, record, answer, 'content', 18))) return finishCancelled(req, record);

  S.conversation.push(record);
  const elapsed = (Date.now() - t0) / 1000;
  rounds += record.tools.length;
  releaseAttachments(req);
  emit({ type: 'chat.done', request_id: req.id, finish_reason: 'stop',
    usage: { prompt_tokens: 210, completion_tokens: Math.round(answer.length / 3.5) },
    elapsed_s: elapsed, tok_per_s: local ? 14.2 : 62.5,
    content: answer, model, rounds, provider: pid });
  if (local) { touchIdle(); pushRuntime(); }
  persist();
}

function startChat(text, req) {
  setTimeout(() => runChat(text, req).catch((e) => {
    console.error('[dev-mock] chat failed', e);
    emit({ type: 'chat.error', request_id: req.id, code: 'server', message: 'Mock failure: ' + e.message, hint: '',
      action: 'retry', ...undo(req, text) });
  }).finally(() => requests.delete(req.id)), 0);
}

function finishCancelled(req, record) {
  const partial = !!(record && (record.content || record.reasoning));
  if (partial) S.conversation.push({ ...record, stopped: true });
  // A regenerate stopped before it said anything keeps the reply it was replacing.
  const extra = partial || !req.restore ? {} : undo(req, '');
  releaseAttachments(req);
  const unloaded = req.cancelReason === 'unloaded';
  emit({ type: 'chat.error', request_id: req.id, code: 'cancelled',
    message: unloaded ? 'Stopped: the local model was unloaded.' : 'Stopped.', hint: null, action: null, partial, ...extra });
}

// ------------------------------------------------------------ downloads ----

// What search_models returns after catalog.annotate(): catalog entries keep their badge,
// avoid-list matches are "avoid" with the reason, anything else is "untested".
const SEARCH_DB = [
  { id: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov', downloads: 91345, likes: 22, last_modified: '2026-06-18T09:00:00Z', badge: 'recommended', note: CATALOG_NOTE },
  { id: 'OpenVINO/Qwen3-4B-int4-ov', downloads: 48210, likes: 31, last_modified: '2026-08-02T10:11:00Z', badge: 'avoid', note: 'Compiles on the Lunar Lake NPU but produces garbage output (fine on CPU).' },
  { id: 'OpenVINO/Qwen2.5-7B-Instruct-int4-ov', downloads: 30544, likes: 18, last_modified: '2026-04-21T10:00:00Z', badge: 'untested', note: 'Untested on NPU. Tools use the hermes3 parser.' },
  { id: 'OpenVINO/Qwen3-8B-int4-ov', downloads: 20117, likes: 12, last_modified: '2026-07-11T15:30:00Z', badge: 'avoid', note: 'Too large for 16 GB shared memory.' },
  { id: 'OpenVINO/Qwen3-1.7B-int4-ov', downloads: 15022, likes: 9, last_modified: '2026-05-30T12:00:00Z', badge: 'avoid', note: 'Asymmetric INT4 export; does not compile on NPU.' },
  { id: 'OpenVINO/Mistral-7B-Instruct-v0.3-int4-ov', downloads: 7315, likes: 6, last_modified: '2026-03-02T08:00:00Z', badge: 'untested', note: 'Untested on NPU. Runs with tools disabled.' },
  { id: 'OpenVINO/Phi-3.5-mini-instruct-fp16-ov', downloads: 3210, likes: 4, last_modified: '2026-02-11T08:00:00Z', badge: 'untested',
    note: 'Untested on NPU. Runs with tools disabled. The NPU needs a symmetric INT4 OpenVINO export; this name does not say int4, so it may not compile on NPU.' },
];

function startDownload(repo_id) {
  const gid = `dl_${Date.now().toString(36)}`;
  const total = 930_000_000;
  const d = { group_id: gid, download_id: gid, repo_id, status: 'downloading', downloaded_bytes: 0, total_bytes: total,
    speed_bps: 0, eta_s: null, file: 'openvino_model.bin', files_done: 0, files_total: 6, error: null };
  S.downloads[gid] = d;
  const tick = setInterval(() => {
    if (d.status !== 'downloading') { clearInterval(tick); timers.delete(tick); return; }
    d.speed_bps = 190_000_000 + Math.random() * 20_000_000;
    d.downloaded_bytes = Math.min(total, d.downloaded_bytes + d.speed_bps / 4);
    d.eta_s = (total - d.downloaded_bytes) / d.speed_bps;
    d.files_done = Math.floor((d.downloaded_bytes / total) * 6);
    if (d.downloaded_bytes >= total) {
      d.status = 'done'; d.speed_bps = 0; d.eta_s = 0; d.files_done = 6;
      clearInterval(tick); timers.delete(tick);
      if (!S.models.find((m) => m.id === repo_id)) {
        S.models.push({ id: repo_id, path: `C:\\Users\\you\\AppData\\Local\\ChatForge\\models\\${repo_id.replace('/', '\\')}`,
          size_bytes: total, complete: true, missing: [], catalog: null, last_used_at: null, compiled: { NPU: false } });
      }
    }
    emit({ type: 'download.progress', ...d });
  }, 250);
  timers.add(tick);
  d._tick = tick;
  return gid;
}

function publicDownload(d) { const { _tick, ...rest } = d; return rest; }

// ------------------------------------------------------------------ API ----

function makeApi() {
  return {
    async get_state() {
      await sleep(30);
      return ok({
        config: uiConfig(),
        providers: Object.keys(S.config.providers).map(providerView),
        selected: { provider: S.config.chat.provider, model: S.config.chat.model },
        runtime: clone(S.runtime),
        conversation: conversationItems(),
        limits: { max_prompt_chars: S.config.chat.max_prompt_chars },
        theme: S.config.ui.theme,
      });
    },

    async send_message(text, attachment_ids) {
      text = String(text || '');
      let ids;
      try { ids = attachmentIds(attachment_ids); } catch (e) { return fail('bad_request', e.message); }
      if (!text.trim() && !ids.length) return fail('bad_request', 'Empty message.');
      if (ids.length > MAX_FILES) {
        return fail('bad_request', `Attach at most ${MAX_FILES} files to one message.`, 'Remove some files and send again.');
      }
      if (text.length > S.config.chat.max_prompt_chars) {
        return fail('context_overflow', `Message is longer than ${S.config.chat.max_prompt_chars} characters.`,
          'Shorten the message.');
      }
      if (ids.some((id) => !S.attachments.has(id))) {
        return fail('not_found', 'An attached file is no longer available. Attach it again.');
      }
      const files = ids.map((id) => clone(S.attachments.get(id).view));
      const user = { role: 'user', content: text, ts: now() };
      const req = { id: `req_${++rid}`, cancelled: false, files, attachmentIds: ids, userTs: user.ts };
      requests.set(req.id, req);
      // conversation.items(): {name, kind, chars, truncated} per file, only when there are some.
      if (files.length) user.attachments = files.map(({ name, kind, chars, truncated }) => ({ name, kind, chars, truncated }));
      S.conversation.push(user);
      if (rememberModel(S.config.chat.provider, S.config.chat.model)) emit({ type: 'settings.changed', config: uiConfig() });
      persist();
      startChat(text, req);
      return ok({ request_id: req.id });
    },

    // Answer the latest user message again: its reply is replaced; a failed or empty
    // regenerate puts the old reply back (chat.error restored: true).
    async regenerate() {
      const req = { id: `req_${++rid}`, cancelled: false, files: [], attachmentIds: [] };
      requests.set(req.id, req);
      let at = S.conversation.length - 1;
      while (at >= 0 && S.conversation[at].role !== 'user') at -= 1;
      if (at < 0) {
        setTimeout(() => {
          requests.delete(req.id);
          emit({ type: 'chat.error', request_id: req.id, code: 'bad_request', message: 'There is no message to answer again',
            hint: null, action: null });
        }, 0);
        return ok({ request_id: req.id });
      }
      const user = S.conversation[at];
      req.restore = S.conversation.splice(at + 1);
      req.userTs = user.ts;
      req.files = (user.attachments || []).map(clone);
      if (rememberModel(S.config.chat.provider, S.config.chat.model)) emit({ type: 'settings.changed', config: uiConfig() });
      persist();
      startChat(user.content || '', req);
      return ok({ request_id: req.id });
    },

    // The native dialog: picks window.__mock.nextPick once when set (null = cancelled),
    // else SAMPLE_PICK.
    async attach_files() {
      await sleep(400);
      const custom = !!window.__mock && Object.prototype.hasOwnProperty.call(window.__mock, 'nextPick');
      const pick = custom ? window.__mock.nextPick : SAMPLE_PICK;
      if (custom) delete window.__mock.nextPick;
      if (!pick || !pick.length) return ok({ attachments: [], errors: [], cancelled: true });
      return attachReply(pick.map((p) => {
        const name = cleanName(p.name);
        const size = Number(p.size) || 0;
        return [name, () => {
          if (p.error) throw new Refused(p.error);
          return { ex: extract(name, size, null, p.chars == null ? null : p.chars), size };
        }];
      }));
    },

    async attach_data(name, base64_data) {
      const fileName = cleanName(name);
      return attachReply([[fileName, () => {
        const bytes = decodeBase64(base64_data);
        return { ex: extract(fileName, bytes.length, bytes), size: bytes.length };
      }]]);
    },

    async remove_attachment(attachment_id) {
      return ok({ removed: S.attachments.delete(String(attachment_id || '')) });
    },

    async open_document(path) {
      const r = checkDocument(path);
      if (r.ok) log('INFO', `open_document ${r.path.split('\\').pop()}`);
      return r;
    },

    async reveal_document(path) {
      const r = checkDocument(path);
      if (r.ok) log('INFO', `reveal_document ${r.path.split('\\').pop()}`);
      return r;
    },

    async stop_generation(request_id) {
      const r = requests.get(request_id);
      if (r) r.cancelled = true;
      return ok();
    },

    async new_chat() { S.conversation = []; S.contextStartTs = null; persist(); return ok(); },

    async select_model(provider_id, model_id) {
      if (!S.config.providers[provider_id]) return fail('not_found', `Unknown provider ${provider_id}.`);
      S.config.chat.provider = provider_id;
      S.config.chat.model = model_id || providerView(provider_id).default_model;
      rememberModel(S.config.chat.provider, S.config.chat.model);
      persist();
      emit({ type: 'settings.changed', config: uiConfig() });
      if (isLocal(provider_id) && S.config.local.autoload_on_open && S.runtime.state === 'unloaded') {
        startLoad(S.config.chat.model);
      }
      return ok({ selected: { provider: provider_id, model: S.config.chat.model } });
    },

    async load_model() {
      const id = isLocal(S.config.chat.provider) ? S.config.chat.model : S.models[0].id;
      startLoad(id);
      return ok();
    },
    async unload_model() { unload('user'); return ok(); },

    async hide_popup() { log('DEBUG', 'hide_popup'); return ok(); },
    async set_pinned(flag) { S.pinned = !!flag; return ok({ pinned: S.pinned }); },
    async open_settings() {
      try { window.open('settings.html', 'chatforge-settings', 'width=900,height=700'); } catch { /* popup blocked */ }
      return ok();
    },
    async open_external(url) {
      if (!/^https?:\/\//i.test(String(url))) return fail('bad_request', 'Only http(s) links can be opened.');
      try { window.open(url, '_blank', 'noopener'); } catch { /* popup blocked */ }
      return ok();
    },

    async save_api_key(provider_id, key) {
      if (!S.config.providers[provider_id]) return fail('not_found', `Unknown provider ${provider_id}.`);
      key = String(key || '').trim();
      if (!key) return fail('bad_request', 'The key is empty.');
      if (/[\r\n]/.test(key) || key.length > 512) return fail('bad_request', 'That does not look like an API key.');
      S.keySaved[provider_id] = true;          // remember THAT, never the value
      S.__lastKeyWasBad = key.toLowerCase().startsWith('bad');
      persist();
      const st = keyStatus(provider_id);
      emit({ type: 'key.status', provider_id, source: st.source, env_name: st.env_name });
      return ok({ key: st });
    },
    async remove_api_key(provider_id) {
      delete S.keySaved[provider_id];
      persist();
      const st = keyStatus(provider_id);
      emit({ type: 'key.status', provider_id, source: st.source, env_name: st.env_name });
      return ok({ key: st });
    },

    async test_provider(provider_id, key_or_null, model_or_null) {
      const spec = S.config.providers[provider_id];
      if (!spec) return fail('not_found', `Unknown provider ${provider_id}.`);
      await sleep(700);
      const seeded = [...(spec.models || [])];
      // llm/probe.test_provider: the same failure shape for every refusal.
      const probeFail = (code, error, hint, action = null, latency_s = null) => ({ ok: false, models: seeded, latency_s, error, hint, code, action });
      if (spec.kind === 'ovms') {
        return probeFail('bad_request', 'The local runtime is tested by loading a model', 'Use Load in Settings → Models.');
      }
      const typed = key_or_null != null && String(key_or_null).trim() !== '';
      const have = typed || keyStatus(provider_id).source !== 'none';
      if (!have && keyRequired(spec)) {
        const env = spec.api_key_env ? ` (or set ${spec.api_key_env})` : '';
        return probeFail('no_key', `Add your ${spec.display_name} API key`, `Paste the key in the card below or in Settings → Providers${env}.`, 'add_key');
      }
      const bad = typed ? String(key_or_null).toLowerCase().startsWith('bad') : (have && !!S.__lastKeyWasBad);
      if (bad) {
        return probeFail('region_or_key', 'MiniMax rejected the key (status 2049).',
          'International keys use api.minimax.io; China keys use api.minimaxi.com.', null, 0.31);
      }
      const chosen = (model_or_null || '').trim() || spec.default_model || seeded[0] || '';
      return ok({ models: seeded, models_source: 'api', model: chosen, served_model: chosen, latency_s: 0.42,
        error: null, hint: null, code: null });
    },

    // Mock: a provider with a key "publishes" its list plus one new model; no key = skipped
    // (refresh all) or no_key (one provider).
    async refresh_models(provider_id_or_null) {
      await sleep(500);
      const ids = provider_id_or_null ? [provider_id_or_null]
        : Object.keys(S.config.providers).filter((id) => S.config.providers[id].kind !== 'ovms');
      const results = {};
      for (const id of ids) {
        const spec = S.config.providers[id];
        if (!spec) return fail('not_found', `Unknown provider ${id}.`);
        if (spec.kind === 'ovms') return fail('bad_request', 'Local models are managed in Settings → Models.');
        const st = keyStatus(id);
        if (st.source === 'none' && st.required) {
          if (provider_id_or_null) results[id] = { ok: false, models: spec.models || [], added: [], removed: [], error: 'No API key.', hint: 'Add your key first.', code: 'no_key' };
          continue;
        }
        const before = [...(spec.models || [])];
        const extra = `${before[0] ? before[0].split('-')[0] : id}-New-Preview`;
        const after = before.includes(extra) ? before : [...before, extra];
        spec.models = after;
        results[id] = { ok: true, models: after, added: after.filter((m) => !before.includes(m)), removed: [], error: null, hint: null, code: null };
      }
      persist();
      emit({ type: 'settings.changed', config: uiConfig() });
      return ok({ results, providers: Object.keys(S.config.providers).map(providerView) });
    },

    async get_settings() {
      return ok({ config: clone(S.config), restart_required: [], errors: {} });
    },
    async update_settings(patch) {
      const errors = {};
      const restart = [];
      const next = clone(S.config);
      const merge = (dst, src, path) => {
        for (const [k, v] of Object.entries(src || {})) {
          const p = path ? `${path}.${k}` : k;
          if (v && typeof v === 'object' && !Array.isArray(v) && dst[k] && typeof dst[k] === 'object') merge(dst[k], v, p);
          else { if (JSON.stringify(dst[k]) !== JSON.stringify(v)) dst[k] = v; }
        }
      };
      merge(next, patch, '');
      const mpc = next.chat.max_prompt_chars;
      if (!Number.isInteger(mpc) || mpc < 200 || mpc > 4000000) errors['chat.max_prompt_chars'] = 'Must be a whole number from 200 to 4000000.';
      if (!['NPU', 'GPU', 'CPU'].includes(next.local.device)) errors['local.device'] = 'Must be NPU, GPU or CPU.';
      const mpl = next.local.max_prompt_len;
      if (!Number.isInteger(mpl) || mpl < MPL_MIN || mpl > MPL_MAX) errors['local.max_prompt_len'] = `Must be a whole number from ${MPL_MIN} to ${MPL_MAX}.`;
      if (!(next.local.idle_unload_minutes >= 0)) errors['local.idle_unload_minutes'] = 'Must be 0 or more.';
      if (Object.keys(errors).length) return { ok: false, config: clone(S.config), restart_required: [], errors,
        error: { code: 'bad_request', message: 'Some settings are invalid.', hint: '', action: null } };
      for (const key of ['local.device', 'local.max_prompt_len', 'local.extra_args', 'local.ovms_variant']) {
        const [a, b] = key.split('.');
        if (JSON.stringify(S.config[a][b]) !== JSON.stringify(next[a][b])) restart.push(key);
      }
      S.config = next;
      if (S.runtime.state === 'ready') touchIdle();
      persist();
      emit({ type: 'settings.changed', config: uiConfig() });
      return ok({ config: clone(S.config), restart_required: restart, errors: {} });
    },

    async list_providers() { return ok({ providers: Object.keys(S.config.providers).map(providerView) }); },
    async upsert_provider(spec) {
      const id = String((spec && spec.id) || '').trim();
      if (!/^[a-z][a-z0-9-]{1,31}$/.test(id)) return fail('bad_request', 'Provider id must be lowercase letters, digits and dashes.');
      if (id === 'local-npu') return fail('bad_request', 'The local provider cannot be replaced.');
      if (spec.api_key_env && !/^[A-Z][A-Z0-9_]{0,63}$/.test(spec.api_key_env)) return fail('bad_request', 'Invalid environment variable name.');
      const prev = S.config.providers[id] || {};
      const { id: _drop, ...rest } = spec;
      S.config.providers[id] = providerSpec(id, { ...prev, ...rest, builtin: prev.builtin || false });
      persist();
      return ok({ provider: providerView(id) });
    },
    async remove_provider(id) {
      const p = S.config.providers[id];
      if (!p) return fail('not_found', 'No such provider.');
      if (p.builtin || SEED_IDS.has(id)) return fail('bad_request', 'Built-in providers cannot be removed.');
      delete S.config.providers[id]; delete S.keySaved[id];
      if (S.config.chat.provider === id) { S.config.chat.provider = 'local-npu'; S.config.chat.model = S.models[0].id; }
      if (S.config.chat.fallback_provider === id) { S.config.chat.fallback_provider = ''; S.config.chat.fallback_model = ''; }
      persist();
      emit({ type: 'settings.changed', config: uiConfig() });
      return ok();
    },

    async list_models() {
      return ok({ models: S.models.map((m) => ({ ...clone(m), loaded: S.runtime.state === 'ready' && S.runtime.model_id === m.id,
        selected: S.config.chat.model === m.id })) });
    },
    async delete_model(id) {
      if (S.runtime.model_id === id && S.runtime.state !== 'unloaded') return fail('bad_request', 'The model is loaded. Unload it first.');
      if (Object.values(S.downloads).some((d) => d.repo_id === id && d.status === 'downloading')) return fail('bad_request', 'The model is downloading.');
      S.models = S.models.filter((m) => m.id !== id);
      persist();
      return ok();
    },
    async clear_compile_cache(id) {
      const m = S.models.find((x) => x.id === id);
      if (!m) return fail('not_found', 'No such model.');
      m.compiled = { NPU: false, GPU: false, CPU: false };
      persist();
      return ok();
    },

    async search_models(query, author_or_null) {
      await sleep(350);
      const q = String(query || '').toLowerCase();
      const author = author_or_null ? String(author_or_null).toLowerCase() : null;
      const results = SEARCH_DB.filter((r) => (!q || r.id.toLowerCase().includes(q))
        && (!author || r.id.toLowerCase().startsWith(author + '/')));
      return ok({ results });
    },
    async repo_details(repo_id) {
      await sleep(250);
      const files = [
        { path: 'openvino_model.bin', size: 2263625445, sha256: 'ab12' + '0'.repeat(60) },
        { path: 'openvino_model.xml', size: 4400000, sha256: null },
        { path: 'openvino_tokenizer.bin', size: 5600000, sha256: null },
        { path: 'openvino_tokenizer.xml', size: 20000, sha256: null },
        { path: 'openvino_detokenizer.xml', size: 18000, sha256: null },
        { path: 'config.json', size: 1200, sha256: null },
      ];
      const total = files.reduce((n, f) => n + f.size, 0);
      return ok({ repo_id, files, total_bytes: total, fits_disk: true, free_bytes: 415_000_000_000 });
    },
    async start_download(repo_id) {
      if (!/^[A-Za-z0-9][\w.-]{0,95}\/[A-Za-z0-9][\w.-]{0,95}$/.test(String(repo_id)) || String(repo_id).includes('..')) {
        return fail('bad_request', 'Invalid repository id.');
      }
      return ok({ download_id: startDownload(repo_id) });
    },
    async cancel_download(id) {
      const d = S.downloads[id];
      if (!d) return fail('not_found', 'No such download.');
      d.status = 'cancelled';
      emit({ type: 'download.progress', ...publicDownload(d) });
      return ok();
    },
    async resume_download(id) {
      const d = S.downloads[id];
      if (!d) return fail('not_found', 'No such download.');
      if (d.status === 'cancelled' || d.status === 'paused' || d.status === 'error') { delete S.downloads[id]; return ok({ download_id: startDownload(d.repo_id) }); }
      return ok({ download_id: id });
    },
    async list_downloads() { return ok({ downloads: Object.values(S.downloads).map(publicDownload) }); },

    async runtime_install() {
      let done = 0;
      const total = 138_798_816;
      const tick = setInterval(() => {
        done = Math.min(total, done + 9_000_000);
        const status = done >= total ? 'done' : 'downloading';
        emit({ type: 'runtime.install', status, downloaded_bytes: done, total_bytes: total, speed_bps: 36_000_000,
          eta_s: (total - done) / 36_000_000, error: null });
        if (done >= total) { clearInterval(tick); timers.delete(tick); S.runtimeInstalled = true; }
      }, 250);
      timers.add(tick);
      return ok();
    },
    async runtime_status() {
      return ok({ installed: S.runtimeInstalled, version: '2026.4.0', variant: 'python_on',
        exe: 'C:\\Users\\you\\AppData\\Local\\ChatForge\\runtime\\ovms-2026.4.0\\ovms\\ovms.exe', vcredist: true });
    },

    async get_logs(n = 200, level = 'INFO') {
      const order = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };
      const min = order[String(level || 'INFO').toUpperCase()] ?? 1;
      const lines = S.logs.filter((l) => (order[l.slice(24).trim().split(/\s+/)[0]] ?? 1) >= min).slice(-n);
      return ok({ lines });
    },
    async open_logs_folder() { return ok(); },

    async get_autostart() { return ok(autostartView()); },
    async set_autostart(flag) {
      const was = S.autostart;
      S.autostart = !!flag; S.config.startup.autostart = S.autostart; persist();
      // autostart.disable() says whether there was anything to remove. The bridge's
      // set_autostart reply has no duplicate field.
      const { duplicate: _drop, ...view } = autostartView(S.autostart ? '' : (was ? 'removed' : 'was not enabled'));
      return ok(view);
    },

    async set_hotkey(spec) {
      const s = String(spec || '').trim();
      if (!/^((ctrl|alt|shift|win)\+)+[a-z0-9]+$|^((ctrl|alt|shift|win)\+)+(space|f\d{1,2})$/i.test(s)) return fail('bad_request', `"${s}" is not a valid hotkey.`);
      if (/^ctrl\+alt\+delete$/i.test(s) || /^win\+l$/i.test(s)) return fail('conflict', `${s} is in use by another app.`);
      S.config.ui.hotkey = s; persist();
      emit({ type: 'settings.changed', config: uiConfig() });
      return ok();
    },

    async disk_usage() {
      return ok({ models: 3_220_000_000, cache: 1_480_000_000, runtime: 420_000_000, logs: 1_200_000, free: 415_000_000_000 });
    },
  };
}

// -------------------------------------------------------------- install ----

/** Called by bridge.js. Defines window.pywebview.api and starts feeding events. */
export function install({ emit: emitter }) {
  if (window.pywebview && window.pywebview.api) return;
  emitFn = emitter;
  S = createState();
  log('INFO', 'dev-mock started');
  log('INFO', 'config loaded');
  window.pywebview = { api: makeApi(), __mock: true };
  // Dev helpers from the console.
  window.__mock = {
    setEnvKey(name, on = true) { S.envKey[name] = !!on; },
    reset() { try { sessionStorage.removeItem(MOCK_KEY); } catch { /* ignore */ } location.reload(); },
    state: () => S,
    emit,
  };
  // popup.shown a moment after load, like the real shell.
  setTimeout(() => emit({ type: 'popup.shown' }), 300);
  window.addEventListener('focus', () => emit({ type: 'popup.shown' }));
}
