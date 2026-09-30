// bridge.js + dev-mock.js contract tests (PLAN 1.5 / 1.6). Runs the mock the way a plain
// browser does (no window.pywebview) and checks the events and return shapes that chat.js
// and settings.js rely on, so a drift from the contract fails here first.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', '..', 'src', 'aichat', 'web', 'static', 'js');
const { JSDOM } = createRequire(import.meta.url)('jsdom');

const win = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://127.0.0.1:8765/index.html' }).window;
Object.assign(globalThis, { window: win, document: win.document, location: win.location, sessionStorage: win.sessionStorage });

const bridge = await import(pathToFileURL(join(JS, 'bridge.js')).href);
const { api, on, off, emit } = bridge;

/** Collect events of the given types until `until(evt)` is true (or 20 s). */
function collect(types, until) {
  const seen = [];
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { unsub(); reject(new Error(`timeout; saw ${JSON.stringify(seen.map((e) => e.type))}`)); }, 20000);
    const offs = types.map((t) => on(t, (e) => {
      seen.push(e);
      if (until(e)) { clearTimeout(timer); unsub(); resolve(seen); }
    }));
    function unsub() { offs.forEach((f) => f()); }
  });
}

test('on / off / emit and window.__aichat.emit', () => {
  const got = [];
  const fn = (e) => got.push(e.n);
  on('t.one', fn);
  emit({ type: 't.one', n: 1 });
  win.__aichat.emit({ type: 't.one', n: 2 });
  off('t.one', fn);
  emit({ type: 't.one', n: 3 });
  assert.deepEqual(got, [1, 2]);
  const all = [];
  const unsub = on('*', (e) => all.push(e.type));
  emit({ type: 't.two' });
  unsub();
  emit({ type: 't.three' });
  assert.deepEqual(all, ['t.two']);
});

test('a throwing handler does not stop the others', () => {
  const got = [];
  const bad = on('t.err', () => { throw new Error('boom'); });
  const good = on('t.err', () => got.push('ok'));
  const origError = console.error; console.error = () => {};
  try { emit({ type: 't.err' }); } finally { console.error = origError; bad(); good(); }
  assert.deepEqual(got, ['ok']);
});

test('without pywebview the dev mock supplies every contract method', async () => {
  const state = await api.call('get_state');
  assert.equal(state.ok, true);
  assert.ok(win.pywebview && win.pywebview.__mock, 'dev-mock was not installed');
  const methods = ['get_state', 'send_message', 'stop_generation', 'new_chat', 'select_model', 'load_model', 'unload_model',
    'hide_popup', 'set_pinned', 'open_settings', 'open_external', 'save_api_key', 'remove_api_key', 'test_provider',
    'get_settings', 'update_settings', 'list_providers', 'upsert_provider', 'remove_provider', 'list_models',
    'delete_model', 'clear_compile_cache', 'search_models', 'repo_details', 'start_download', 'cancel_download',
    'resume_download', 'list_downloads', 'runtime_install', 'runtime_status', 'get_logs', 'open_logs_folder',
    'get_autostart', 'set_autostart', 'set_hotkey', 'disk_usage'];
  for (const m of methods) assert.equal(typeof win.pywebview.api[m], 'function', m);
  assert.deepEqual(Object.keys(win.pywebview.api).filter((k) => !methods.includes(k)), [], 'extra public methods');
});

test('get_state shape', async () => {
  const s = await api.call('get_state');
  assert.ok(s.config.chat && s.config.ui);
  assert.equal(s.selected.provider, 'local-npu');
  assert.ok(s.providers.find((p) => p.id === 'minimax'));
  const mm = s.providers.find((p) => p.id === 'minimax');
  assert.deepEqual(Object.keys(mm.key).sort(), ['env_name', 'env_overrides_saved', 'source']);
  assert.equal(mm.key.source, 'none');
  assert.equal(s.limits.max_prompt_chars, 4000);
  assert.equal(s.theme, 'laserlloyd');
  assert.equal(s.runtime.state, 'unloaded');
  assert.ok(Array.isArray(s.conversation));
});

test('an unknown bridge method resolves to an error object, never rejects', async () => {
  const r = await api.call('nope');
  assert.equal(r.ok, false);
  assert.equal(typeof r.error.message, 'string');
});

test('keyless cloud provider: no_key, then save + test succeed, then a streamed reply with reasoning and a tool call', async () => {
  assert.equal((await api.call('select_model', 'minimax', 'MiniMax-M3')).ok, true);

  // 1. no key -> chat.error{no_key, add_key}
  const first = collect(['chat.start', 'chat.error'], (e) => e.type === 'chat.error');
  const sent = await api.call('send_message', 'hello');
  assert.equal(sent.ok, true);
  const ev1 = await first;
  const err = ev1.at(-1);
  assert.equal(err.code, 'no_key');
  assert.equal(err.action, 'add_key');
  assert.equal(err.request_id, sent.request_id);

  // 2. the key is saved but never echoed; the test then succeeds
  const secret = 'test-key-not-real-123456';
  const saved = await api.call('save_api_key', 'minimax', secret);
  assert.equal(saved.ok, true);
  assert.equal(saved.key.source, 'keyring');
  assert.ok(!JSON.stringify(saved).includes(secret), 'save_api_key echoed the key');
  assert.ok(!JSON.stringify(await api.call('get_state')).includes(secret), 'get_state leaked the key');
  assert.ok(!JSON.stringify(await api.call('get_settings')).includes(secret), 'get_settings leaked the key');
  const tested = await api.call('test_provider', 'minimax', null, null);
  assert.equal(tested.ok, true);
  assert.ok(tested.models.length >= 1);

  // 3. a bad key is reported as region_or_key with a hint
  const bad = await api.call('test_provider', 'minimax', 'bad-key', null);
  assert.equal(bad.ok, false);
  assert.equal(bad.code, 'region_or_key');
  assert.match(bad.hint, /api\.minimax\.io/);

  // 4. a streamed reply: start, phases, reasoning deltas, tool call/result, content deltas, done
  const run = collect(['chat.start', 'chat.phase', 'chat.delta', 'chat.tool_call', 'chat.tool_result', 'chat.done', 'chat.error'],
    (e) => e.type === 'chat.done' || e.type === 'chat.error');
  const r = await api.call('send_message', 'search the web for openvino npu');
  assert.equal(r.ok, true);
  const events = await run;
  const types = events.map((e) => e.type);
  assert.equal(types[0], 'chat.start');
  assert.equal(types.at(-1), 'chat.done', JSON.stringify(events.at(-1)));
  assert.ok(events.every((e) => e.request_id === r.request_id));
  assert.ok(events.some((e) => e.type === 'chat.delta' && e.reasoning), 'no reasoning deltas');
  const call = events.find((e) => e.type === 'chat.tool_call');
  const result = events.find((e) => e.type === 'chat.tool_result');
  assert.equal(call.name, 'web_search');
  assert.ok(call.arguments.length <= 300);
  assert.equal(result.call_id, call.call_id);
  assert.equal(result.ok, true);
  assert.ok(types.indexOf('chat.tool_call') < types.indexOf('chat.tool_result'));
  const phases = events.filter((e) => e.type === 'chat.phase').map((e) => e.phase);
  assert.ok(phases.includes('calling_tool') && phases.includes('generating'), phases.join());
  const text = events.filter((e) => e.content).map((e) => e.content).join('');
  assert.match(text, /OpenVINO/);
  const done = events.at(-1);
  assert.equal(done.finish_reason, 'stop');
  assert.equal(typeof done.tok_per_s, 'number');
});

test('the markdown showcase streams a table, code and a footnote', async () => {
  const run = collect(['chat.delta', 'chat.done', 'chat.error'], (e) => e.type === 'chat.done' || e.type === 'chat.error');
  const r = await api.call('send_message', 'show me everything');
  const events = await run;
  const text = events.filter((e) => e.content).map((e) => e.content).join('');
  assert.match(text, /\| Runtime \| Device/);
  assert.match(text, /```python/);
  assert.match(text, /\[\^1\]:/);
  assert.equal(events.at(-1).type, 'chat.done');
  assert.ok(r.request_id);
});

test('stop_generation ends a stream with code cancelled', async () => {
  const run = collect(['chat.delta', 'chat.done', 'chat.error'], (e) => e.type === 'chat.done' || e.type === 'chat.error');
  const r = await api.call('send_message', 'a long answer please, long');
  await new Promise((res) => setTimeout(res, 250));
  await api.call('stop_generation', r.request_id);
  const events = await run;
  assert.equal(events.at(-1).type, 'chat.error');
  assert.equal(events.at(-1).code, 'cancelled');
});

test('send_message rejects empty and over-long text', async () => {
  assert.equal((await api.call('send_message', '   ')).ok, false);
  const long = await api.call('send_message', 'x'.repeat(4001));
  assert.equal(long.ok, false);
  assert.equal(long.error.code, 'context_overflow');
});

test('local model load emits runtime.status through starting/compiling to ready with a compile ETA', async () => {
  await api.call('select_model', 'local-npu', 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov');   // already compiled: fast path
  const run = collect(['runtime.status'], (e) => e.state === 'ready');
  await api.call('load_model');
  const events = await run;
  assert.ok(events.length >= 2);
  assert.ok(['starting', 'compiling'].includes(events[0].state));
  assert.equal(events[0].model_id, 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov');
  for (const k of ['state', 'model_id', 'device', 'elapsed_s', 'expected_s', 'first_compile', 'idle_timeout_s', 'unload_at', 'error']) {
    assert.ok(k in events[0], `runtime.status lacks ${k}`);
  }
  assert.equal(events.at(-1).state, 'ready');
  assert.equal(typeof events.at(-1).unload_at, 'number');
  await api.call('unload_model');
});

test('settings: update validates, reports restart keys and emits settings.changed', async () => {
  const bad = await api.call('update_settings', { chat: { max_prompt_chars: 5 } });
  assert.equal(bad.ok, false);
  assert.ok(bad.errors['chat.max_prompt_chars']);
  const changed = collect(['settings.changed'], () => true);
  const good = await api.call('update_settings', { ui: { theme: 'laserlloyd-light' }, local: { device: 'GPU' } });
  assert.equal(good.ok, true);
  assert.deepEqual(good.restart_required, ['local.device']);
  assert.equal((await changed)[0].config.ui.theme, 'laserlloyd-light');
});

test('models: search badges, download progress events, delete refusals', async () => {
  const res = await api.call('search_models', 'qwen', 'OpenVINO');
  assert.equal(res.ok, true);
  assert.ok(new Set(res.results.map((r) => r.badge)).size >= 3);
  assert.ok(res.results.every((r) => ['recommended', 'supported', 'untested', 'avoid'].includes(r.badge)));
  const det = await api.call('repo_details', 'OpenVINO/x-int4-ov');
  assert.ok(det.total_bytes > 0 && det.fits_disk === true);
  assert.equal((await api.call('start_download', '../evil/repo')).ok, false);
  const prog = collect(['download.progress'], (e) => e.status === 'done');
  const dl = await api.call('start_download', 'OpenVINO/Mistral-7B-Instruct-v0.3-int4-ov');
  assert.equal(dl.ok, true);
  const events = await prog;
  assert.ok(events.length > 2);
  assert.equal(events[0].group_id, dl.download_id);
  assert.equal(events[0].repo_id, 'OpenVINO/Mistral-7B-Instruct-v0.3-int4-ov');
  assert.ok(events.at(-1).downloaded_bytes >= events.at(-1).total_bytes);
  const logs = await api.call('get_logs', 50, 'INFO');
  assert.ok(Array.isArray(logs.lines) && logs.lines.length > 0);
});

test('open_external accepts only http(s)', async () => {
  assert.equal((await api.call('open_external', 'file:///C:/Windows/system32')).ok, false);
  assert.equal((await api.call('open_external', 'javascript:alert(1)')).ok, false);
});
