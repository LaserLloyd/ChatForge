// Development mock of the Python bridge (PLAN 1.5 / 1.6).
//
// Loaded by bridge.js only when window.pywebview is absent (a plain browser served with
//   py -3.12 -m http.server -d src\aichat\web 8765 ).
// install({emit}) defines window.pywebview.api with EVERY method in the contract, backed by
// realistic fake data, and pushes the same events Python would:
//   chat.start / chat.phase / chat.delta / chat.tool_call / chat.tool_result / chat.done /
//   chat.error, runtime.status (1 Hz while starting/compiling), download.progress,
//   runtime.install, key.status, settings.changed.
//
// Magic words in a message (chat popup):
//   "search ..."  -> web_search tool call + result, then an answer
//   "date"/"time" -> current_datetime tool call
//   "calc"/"sqrt" -> calculator tool call
//   "error"       -> chat.error{server, retry}
//   "long"        -> a long answer (scroll testing)
//   anything else -> the markdown showcase (table, code, footnote, callout), with reasoning
//                    when the selected provider is cloud
// Cloud provider (MiniMax) with no saved key -> chat.error{no_key, add_key}. save_api_key then
// test_provider succeed unless the typed key starts with "bad" (then region_or_key).
//
// No secret is ever stored: the mock remembers only THAT a key was saved.

const MOCK_KEY = 'aichat.mock.v1';
const TIME_SCALE_FIRST_COMPILE_S = 12;   // real seconds a "6 minute" first compile takes
const TIME_SCALE_CACHED_S = 3;           // real seconds a cached load takes

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now() / 1000;
const clone = (o) => JSON.parse(JSON.stringify(o));
const ok = (extra = {}) => ({ ok: true, ...extra });
const fail = (code, message, hint = '', action = null) => ({ ok: false, error: { code, message, hint, action } });

// ---------------------------------------------------------------- state ----

const DEFAULT_CONFIG = {
  schema_version: 1,
  chat: {
    provider: 'local-npu',
    model: 'OpenVINO/Qwen3-4B-int4-ov',
    system_prompt: 'You are AI Chat, a concise desktop assistant.',
    max_prompt_chars: 4000,
    max_tool_rounds: 4,
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
  providers: {
    'local-npu': { kind: 'ovms', display_name: 'Local (NPU)', builtin: true, quirks: ['ovms'] },
    minimax: {
      kind: 'openai', display_name: 'MiniMax', region: 'international',
      base_url: 'https://api.minimax.io/v1', api_key_env: 'MINIMAX_API_KEY',
      models: ['MiniMax-M3', 'MiniMax-M2.7-highspeed', 'MiniMax-M2.7', 'MiniMax-M3.1-Flash-Preview'],
      default_model: 'MiniMax-M3', quirks: ['minimax'], supports_tools: true, max_output_tokens: 4096,
      temperature: 1.0, builtin: true,
    },
  },
  tools: {
    enabled: ['web_search', 'fetch_url', 'current_datetime', 'calculator'],
    web_search_max_results: 5, tool_result_max_chars_local: 1500, tool_result_max_chars_cloud: 6000,
    block_private_addresses: true,
  },
  ui: { theme: 'laserlloyd', hotkey: 'Ctrl+Alt+C', hide_on_blur: true, width: 420, height: 620, margin: 12 },
  startup: { autostart: true },
  logging: { level: 'INFO' },
  hf: { endpoint: 'https://huggingface.co', default_author: 'OpenVINO' },
};

const LOCAL_MODELS = [
  {
    id: 'OpenVINO/Qwen3-4B-int4-ov', path: 'C:\\Users\\you\\AppData\\Local\\AIChat\\models\\OpenVINO\\Qwen3-4B-int4-ov',
    size_bytes: 2290000000, complete: true, missing: [],
    catalog: { label: 'Qwen3 4B INT4', npu: 'recommended', note: 'Default. First NPU compile ~5-6 min.' },
    last_used_at: now() - 3600, compiled: { NPU: false, GPU: false, CPU: false },
  },
  {
    id: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov', path: 'C:\\Users\\you\\AppData\\Local\\AIChat\\models\\OpenVINO\\Qwen2.5-1.5B-Instruct-int4-ov',
    size_bytes: 930000000, complete: true, missing: [],
    catalog: { label: 'Qwen2.5 1.5B Instruct INT4', npu: 'supported', note: 'Fallback; faster, weaker.' },
    last_used_at: null, compiled: { NPU: true, GPU: false, CPU: false },
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
  };
  if (persisted) {
    if (persisted.config) st.config = { ...st.config, ...persisted.config };
    st.keySaved = persisted.keySaved || {};
    st.conversation = persisted.conversation || [];
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
      config: S.config, keySaved: S.keySaved, conversation: S.conversation.slice(-40),
      providers: S.config.providers,
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

function keyStatus(pid) {
  const spec = S.config.providers[pid];
  const envName = spec && spec.api_key_env ? spec.api_key_env : null;
  const saved = !!S.keySaved[pid];
  const env = !!(envName && S.envKey[envName]);
  if (spec && spec.kind === 'ovms') return { source: 'none', env_name: null, env_overrides_saved: false };
  return {
    source: env ? 'env' : (saved ? 'keyring' : 'none'),
    env_name: envName,
    env_overrides_saved: env && saved,
  };
}

function providerView(pid) {
  const spec = S.config.providers[pid];
  const local = spec.kind === 'ovms';
  return {
    id: pid,
    display_name: spec.display_name || pid,
    kind: spec.kind,
    models: local ? S.models.filter((m) => m.complete).map((m) => m.id) : (spec.models || []),
    default_model: local ? S.models[0]?.id || null : (spec.default_model || (spec.models || [])[0] || null),
    region: spec.region || null,
    base_url: spec.base_url || null,
    builtin: !!spec.builtin,
    key: keyStatus(pid),
  };
}

function uiConfig() {
  const c = S.config;
  return {
    chat: { provider: c.chat.provider, model: c.chat.model, show_reasoning: c.chat.show_reasoning,
      max_prompt_chars: c.chat.max_prompt_chars },
    ui: clone(c.ui),
    local: { device: c.local.device, idle_unload_minutes: c.local.idle_unload_minutes,
      autoload_on_open: c.local.autoload_on_open },
  };
}

function isLocal(pid) { return (S.config.providers[pid] || {}).kind === 'ovms'; }

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

async function streamText(req, text, field = 'content', delay = 30) {
  for (const c of chunk(text)) {
    if (req.cancelled) return false;
    emit({ type: 'chat.delta', request_id: req.id, [field]: c });
    await sleep(delay);
  }
  return true;
}

function pickScript(text) {
  const s = text.toLowerCase();
  if (/\berror\b/.test(s)) return 'error';
  if (/\bsearch\b/.test(s)) return 'search';
  if (/\b(date|time|today)\b/.test(s)) return 'clock';
  if (/\b(calc|sqrt|math)\b/.test(s)) return 'calc';
  if (/\blong\b/.test(s)) return 'long';
  return 'showcase';
}

async function toolRound(req, name, args, resultOk, summary) {
  const call_id = `call_${++toolSeq}`;
  emit({ type: 'chat.phase', request_id: req.id, phase: 'calling_tool' });
  emit({ type: 'chat.tool_call', request_id: req.id, call_id, name, arguments: JSON.stringify(args).slice(0, 300) });
  await sleep(900);
  if (req.cancelled) return null;
  emit({ type: 'chat.tool_result', request_id: req.id, call_id, name, ok: resultOk, summary });
  await sleep(200);
  return { call_id, name, arguments: JSON.stringify(args), ok: resultOk, summary };
}

async function runChat(text, req) {
  const pid = S.config.chat.provider;
  const model = S.config.chat.model;
  const local = isLocal(pid);
  emit({ type: 'chat.start', request_id: req.id, provider: pid, model });
  log('INFO', `chat.start ${pid}/${model} (${text.length} chars)`);

  if (!local && keyStatus(pid).source === 'none') {
    // Like the engine: a message refused for a missing key is not recorded in the conversation.
    const last = S.conversation[S.conversation.length - 1];
    if (last && last.role === 'user' && last.content === text) S.conversation.pop();
    emit({ type: 'chat.error', request_id: req.id, code: 'no_key',
      message: `No API key for ${S.config.providers[pid].display_name}.`,
      hint: 'Add your key to start chatting.', action: 'add_key' });
    return;
  }

  if (local && S.runtime.state !== 'ready') {
    emit({ type: 'chat.phase', request_id: req.id, phase: 'loading_model' });
    await startLoad(model);
    if (req.cancelled) return finishCancelled(req);
  }

  const script = pickScript(text);
  if (script === 'error') {
    await sleep(500);
    emit({ type: 'chat.error', request_id: req.id, code: 'server', message: 'The model server returned an error.',
      hint: 'See the logs for details.', action: 'retry' });
    return;
  }

  const record = { role: 'assistant', content: '', reasoning: '', tools: [], ts: now() };
  const t0 = Date.now();

  if (!local || script === 'showcase') {
    emit({ type: 'chat.phase', request_id: req.id, phase: 'thinking' });
    if (!local) {
      if (!(await streamText(req, REASONING, 'reasoning', 12))) return finishCancelled(req, record);
      record.reasoning = REASONING;
    }
  }

  let answer;
  if (script === 'search') {
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
  } else if (script === 'long') {
    answer = LONG_TEXT;
  } else {
    answer = SHOWCASE;
  }

  emit({ type: 'chat.phase', request_id: req.id, phase: 'generating' });
  const done = await streamText(req, answer, 'content', 18);
  record.content = answer;
  if (!done) return finishCancelled(req, record);

  S.conversation.push(record);
  const elapsed = (Date.now() - t0) / 1000;
  emit({ type: 'chat.done', request_id: req.id, finish_reason: 'stop',
    usage: { prompt_tokens: 210, completion_tokens: Math.round(answer.length / 3.5) },
    elapsed_s: elapsed, tok_per_s: local ? 14.2 : 62.5 });
  if (local) { touchIdle(); pushRuntime(); }
  persist();
}

function finishCancelled(req, record) {
  if (record && (record.content || record.reasoning)) S.conversation.push(record);
  emit({ type: 'chat.error', request_id: req.id, code: 'cancelled', message: 'Stopped.', hint: '', action: null });
}

// ------------------------------------------------------------ downloads ----

const SEARCH_DB = [
  { id: 'OpenVINO/Qwen3-4B-int4-ov', downloads: 48210, likes: 31, last_modified: '2026-08-02T10:11:00Z', badge: 'recommended', note: 'Default. First NPU compile ~5-6 min.' },
  { id: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov', downloads: 91345, likes: 22, last_modified: '2026-06-18T09:00:00Z', badge: 'supported', note: 'Fallback; faster, weaker.' },
  { id: 'OpenVINO/Qwen3-8B-int4-ov', downloads: 20117, likes: 12, last_modified: '2026-07-11T15:30:00Z', badge: 'avoid', note: 'Too large for 16 GB shared memory' },
  { id: 'OpenVINO/Qwen3-1.7B-int4-ov', downloads: 15022, likes: 9, last_modified: '2026-05-30T12:00:00Z', badge: 'avoid', note: 'Asymmetric export, NPU-incompatible' },
  { id: 'OpenVINO/Mistral-7B-Instruct-v0.3-int4-ov', downloads: 7315, likes: 6, last_modified: '2026-03-02T08:00:00Z', badge: 'untested', note: 'Not in the catalog; runs with tools disabled.' },
  { id: 'OpenVINO/Phi-3.5-mini-instruct-fp16-ov', downloads: 3210, likes: 4, last_modified: '2026-02-11T08:00:00Z', badge: 'untested', note: 'No int4 in the name: the NPU needs symmetric INT4.' },
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
        S.models.push({ id: repo_id, path: `C:\\Users\\you\\AppData\\Local\\AIChat\\models\\${repo_id.replace('/', '\\')}`,
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
        conversation: clone(S.conversation),
        limits: { max_prompt_chars: S.config.chat.max_prompt_chars },
        theme: S.config.ui.theme,
      });
    },

    async send_message(text) {
      text = String(text || '');
      if (!text.trim()) return fail('bad_request', 'Empty message.');
      if (text.length > S.config.chat.max_prompt_chars) {
        return fail('context_overflow', `Message is longer than ${S.config.chat.max_prompt_chars} characters.`,
          'Shorten the message.');
      }
      const req = { id: `req_${++rid}`, cancelled: false };
      requests.set(req.id, req);
      S.conversation.push({ role: 'user', content: text, ts: now() });
      persist();
      setTimeout(() => runChat(text, req).catch((e) => {
        console.error('[dev-mock] chat failed', e);
        emit({ type: 'chat.error', request_id: req.id, code: 'server', message: 'Mock failure: ' + e.message, hint: '', action: 'retry' });
      }).finally(() => requests.delete(req.id)), 0);
      return ok({ request_id: req.id });
    },

    async stop_generation(request_id) {
      const r = requests.get(request_id);
      if (r) r.cancelled = true;
      return ok();
    },

    async new_chat() { S.conversation = []; persist(); return ok(); },

    async select_model(provider_id, model_id) {
      if (!S.config.providers[provider_id]) return fail('not_found', `Unknown provider ${provider_id}.`);
      S.config.chat.provider = provider_id;
      S.config.chat.model = model_id || providerView(provider_id).default_model;
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
      try { window.open('settings.html', 'aichat-settings', 'width=900,height=700'); } catch { /* popup blocked */ }
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
      if (spec.kind === 'ovms') {
        return S.runtimeInstalled ? ok({ models: S.models.map((m) => m.id), latency_s: 0.02 })
          : { ok: false, models: [], latency_s: 0, error: 'OVMS is not installed.', hint: 'Install it in Settings > Models.', code: 'unreachable' };
      }
      const typed = key_or_null != null && String(key_or_null).trim() !== '';
      const have = typed || keyStatus(provider_id).source !== 'none';
      if (!have) {
        return { ok: false, models: [], latency_s: 0, error: 'No API key.', hint: 'Add your key first.', code: 'no_key' };
      }
      const bad = typed ? String(key_or_null).toLowerCase().startsWith('bad') : !!S.__lastKeyWasBad;
      if (bad) {
        return { ok: false, models: [], latency_s: 0.31, code: 'region_or_key',
          error: 'MiniMax rejected the key (status 2049).',
          hint: 'International keys use api.minimax.io; China keys use api.minimaxi.com.' };
      }
      return ok({ models: spec.models || [], latency_s: 0.42 });
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
      if (!Number.isInteger(mpc) || mpc < 200 || mpc > 100000) errors['chat.max_prompt_chars'] = 'Must be a whole number from 200 to 100000.';
      if (!['NPU', 'GPU', 'CPU'].includes(next.local.device)) errors['local.device'] = 'Must be NPU, GPU or CPU.';
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
      S.config.providers[id] = { kind: 'openai', quirks: [], models: [], ...prev, ...rest, builtin: prev.builtin || false };
      persist();
      return ok({ provider: providerView(id) });
    },
    async remove_provider(id) {
      const p = S.config.providers[id];
      if (!p) return fail('not_found', 'No such provider.');
      if (p.builtin) return fail('bad_request', 'Built-in providers cannot be removed.');
      delete S.config.providers[id]; delete S.keySaved[id];
      if (S.config.chat.provider === id) { S.config.chat.provider = 'local-npu'; S.config.chat.model = S.models[0].id; }
      persist();
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
        exe: 'C:\\Users\\you\\AppData\\Local\\AIChat\\runtime\\ovms-2026.4.0\\ovms\\ovms.exe', vcredist: true });
    },

    async get_logs(n = 200, level = 'INFO') {
      const order = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };
      const min = order[String(level || 'INFO').toUpperCase()] ?? 1;
      const lines = S.logs.filter((l) => (order[l.slice(24).trim().split(/\s+/)[0]] ?? 1) >= min).slice(-n);
      return ok({ lines });
    },
    async open_logs_folder() { return ok(); },

    async get_autostart() { return ok({ enabled: S.autostart, mode: 'startup-folder', path: 'C:\\Users\\you\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\AIChat.vbs' }); },
    async set_autostart(flag) { S.autostart = !!flag; S.config.startup.autostart = S.autostart; persist(); return ok({ enabled: S.autostart }); },

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
