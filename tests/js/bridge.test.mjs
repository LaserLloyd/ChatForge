// bridge.js + dev-mock.js contract tests (PLAN 1.5 / 1.6). Runs the mock the way a plain
// browser does (no window.pywebview) and checks the events and return shapes that chat.js
// and settings.js rely on, so a drift from the contract fails here first.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', '..', 'src', 'chatforge', 'web', 'static', 'js');
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

test('on / off / emit and window.__chatforge.emit', () => {
  const got = [];
  const fn = (e) => got.push(e.n);
  on('t.one', fn);
  emit({ type: 't.one', n: 1 });
  win.__chatforge.emit({ type: 't.one', n: 2 });
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
  const methods = ['get_state', 'send_message', 'regenerate', 'attach_files', 'attach_data', 'remove_attachment', 'open_document',
    'reveal_document', 'save_document', 'stop_generation', 'new_chat', 'select_model', 'load_model', 'unload_model',
    'hide_popup', 'set_sticky', 'start_resize', 'drag_resize', 'end_resize', 'reset_popup_size',
    'open_settings', 'open_external', 'save_api_key', 'remove_api_key', 'test_provider', 'refresh_models',
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
  assert.equal(s.selected.model, 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov');
  assert.ok(s.providers.find((p) => p.id === 'minimax'));
  const mm = s.providers.find((p) => p.id === 'minimax');
  // The bridge always sends key.required (desktop/bridge.py _key_status).
  assert.deepEqual(Object.keys(mm.key).sort(), ['env_name', 'env_overrides_saved', 'required', 'source']);
  assert.equal(mm.key.source, 'none');
  assert.equal(mm.key.required, true);
  assert.equal(s.limits.max_prompt_chars, 4000);
  assert.equal(s.theme, 'laserlloyd');
  assert.equal(s.runtime.state, 'unloaded');
  assert.ok(Array.isArray(s.conversation));
});

test('providers mirror config.seed_providers() and the views carry docs_url', async () => {
  const s = await api.call('get_state');
  assert.deepEqual(s.providers.map((p) => p.id), ['local-npu', 'minimax', 'studioforge', 'openai', 'deepseek']);
  for (const p of s.providers) {
    assert.ok('docs_url' in p, `${p.id} view lacks docs_url`);
    assert.equal(p.builtin, true, `${p.id} is seeded, so built in`);
    assert.equal(typeof p.key.required, 'boolean', p.id);
  }
  const by = Object.fromEntries(s.providers.map((p) => [p.id, p]));
  assert.equal(by['local-npu'].key.required, false);
  assert.equal(by.studioforge.key.required, false);
  assert.equal(by.studioforge.base_url, 'http://localhost:1234/v1');
  assert.equal(by.openai.docs_url, 'https://platform.openai.com/api-keys');
  assert.equal(by.deepseek.docs_url, 'https://platform.deepseek.com/api_keys');
  assert.equal(by.minimax.docs_url, null);
  const settings = await api.call('get_settings');
  assert.equal(settings.config.providers.studioforge.key_required, false);
  assert.equal(settings.config.providers.minimax.builtin, false);   // only local-npu is builtin in the config
  // A seeded provider cannot be removed even though its config says builtin: false.
  assert.equal((await api.call('remove_provider', 'openai')).ok, false);
});

test('local models mirror catalog.toml: Qwen2.5-1.5B recommended, Qwen3-4B avoided', async () => {
  const { models } = await api.call('list_models');
  assert.equal(models[0].id, 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov');
  assert.equal(models[0].catalog.npu, 'recommended');
  assert.equal(models.find((m) => m.id === 'OpenVINO/Qwen3-4B-int4-ov').catalog, null);
  const { results } = await api.call('search_models', 'qwen', 'OpenVINO');
  const badge = Object.fromEntries(results.map((r) => [r.id, r.badge]));
  assert.equal(badge['OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov'], 'recommended');
  assert.equal(badge['OpenVINO/Qwen3-4B-int4-ov'], 'avoid');
});

test('test_provider for the local runtime fails like the real probe', async () => {
  const r = await api.call('test_provider', 'local-npu', null, null);
  assert.equal(r.ok, false);
  assert.equal(r.code, 'bad_request');
  assert.match(r.hint, /Load/);
});

test('remove_provider emits settings.changed', async () => {
  const added = await api.call('upsert_provider', { id: 'my-server', display_name: 'My server', base_url: 'http://127.0.0.1:8080/v1', models: ['m1'] });
  assert.equal(added.ok, true);
  assert.equal(added.provider.key.required, false, 'a loopback server needs no key');
  const changed = collect(['settings.changed'], () => true);
  assert.equal((await api.call('remove_provider', 'my-server')).ok, true);
  assert.ok((await changed)[0].config.chat);
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
  assert.equal(err.provider, 'minimax');
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
  const text = events.filter((e) => e.type === 'chat.delta' && e.content).map((e) => e.content).join('');
  assert.match(text, /OpenVINO/);
  const done = events.at(-1);
  assert.equal(done.finish_reason, 'stop');
  assert.equal(typeof done.tok_per_s, 'number');
  // Like the engine: the final content, who answered, and how many model rounds it took.
  assert.equal(done.content, text);
  assert.equal(done.provider, 'minimax');
  assert.equal(done.model, 'MiniMax-M3');
  assert.equal(done.rounds, 2);
});

test('"refuse" streams a refusal, then chat.reset, a tool call and the answer', async () => {
  const run = collect(['chat.delta', 'chat.reset', 'chat.tool_call', 'chat.done', 'chat.error'],
    (e) => e.type === 'chat.done' || e.type === 'chat.error');
  await api.call('send_message', 'please refuse to answer this');
  const events = await run;
  const types = events.map((e) => e.type);
  assert.equal(types.at(-1), 'chat.done');
  const reset = types.indexOf('chat.reset');
  assert.ok(reset > 0, types.join());
  assert.equal(events[reset].reason, 'refusal');
  assert.ok(types.indexOf('chat.tool_call') > reset);
  assert.doesNotMatch(events.at(-1).content, /cannot browse/);
});

test('the markdown showcase streams a table, code and a footnote', async () => {
  const run = collect(['chat.delta', 'chat.done', 'chat.error'], (e) => e.type === 'chat.done' || e.type === 'chat.error');
  const r = await api.call('send_message', 'show me everything');
  const events = await run;
  const text = events.filter((e) => e.type === 'chat.delta' && e.content).map((e) => e.content).join('');
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
  assert.equal(events.at(-1).message, 'Stopped.');
  assert.equal(typeof events.at(-1).partial, 'boolean');
  // The partial reply is kept, marked stopped, like the engine's conversation snapshot.
  const { conversation } = await api.call('get_state');
  assert.equal(conversation.at(-1).stopped, true);
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

test('unloading the local model mid-reply stops it with its own message', async () => {
  const first = collect(['chat.delta', 'chat.error', 'chat.done'], (e) => !!e.content || e.type !== 'chat.delta');
  const run = collect(['chat.done', 'chat.error'], () => true);
  await api.call('send_message', 'a long answer please, long');
  await first;
  await api.call('unload_model');
  const end = (await run)[0];
  assert.equal(end.type, 'chat.error');
  assert.equal(end.code, 'cancelled');
  assert.equal(end.message, 'Stopped: the local model was unloaded.');
  assert.equal(end.partial, true);
});

test('"fallback" with the local model selected: chat.fallback, then the answer from MiniMax', async () => {
  const run = collect(['chat.fallback', 'chat.done', 'chat.error'], (e) => e.type === 'chat.done' || e.type === 'chat.error');
  await api.call('send_message', 'fallback please');
  const events = await run;
  const fb = events.find((e) => e.type === 'chat.fallback');
  assert.ok(fb, JSON.stringify(events));
  assert.deepEqual([fb.from_provider, fb.provider, fb.model], ['local-npu', 'minimax', 'MiniMax-M3']);
  const done = events.at(-1);
  assert.equal(done.type, 'chat.done');
  assert.equal(done.provider, 'minimax');
  assert.equal(done.model, 'MiniMax-M3');
});

test('settings: update validates, reports restart keys and emits settings.changed', async () => {
  const bad = await api.call('update_settings', { chat: { max_prompt_chars: 5 } });
  assert.equal(bad.ok, false);
  assert.ok(bad.errors['chat.max_prompt_chars']);
  // config.py: local.max_prompt_len is 1024..8192.
  for (const mpl of [512, 16384, 4096.5]) {
    const r = await api.call('update_settings', { local: { max_prompt_len: mpl } });
    assert.equal(r.ok, false, String(mpl));
    assert.match(r.errors['local.max_prompt_len'], /1024 to 8192/);
  }
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

// ------------------------------------------------------------ attachments ----

const b64 = (text) => Buffer.from(text, 'utf8').toString('base64');
const ATTACH_KEYS = ['chars', 'id', 'kind', 'name', 'size', 'truncated', 'warning'];

test('attach_data reads a text file and refuses what attachments.py refuses, with its messages', async () => {
  const r = await api.call('attach_data', 'C:\\Users\\you\\notes.md', `data:text/markdown;base64,${b64('# Notes\r\n\r\nhello')}`);
  assert.equal(r.ok, true);
  assert.deepEqual(r.errors, []);
  const [a] = r.attachments;
  assert.deepEqual(Object.keys(a).sort(), ATTACH_KEYS);
  assert.match(a.id, /^att_[\w-]{22}$/);
  assert.equal(a.name, 'notes.md', 'the folder is dropped from the name');
  assert.equal(a.kind, 'text');
  assert.equal(a.chars, '# Notes\n\nhello'.length);
  assert.equal(a.size, '# Notes\r\n\r\nhello'.length);
  assert.equal(a.truncated, false);
  assert.equal(a.warning, null);

  const refused = async (name, data) => {
    const res = await api.call('attach_data', name, data);
    assert.equal(res.ok, true, 'a refused file is an errors entry, not a failed call');
    assert.deepEqual(res.attachments, []);
    assert.equal(res.errors[0].name, name);
    return res.errors[0].message;
  };
  assert.equal(await refused('photo.png', b64('not really a png')),
    'The picture could not be read; it may be damaged or not really a PNG file.');
  assert.match(await refused('layers.psd', b64('8BPS')), /This kind of picture cannot be read/);
  assert.match(await refused('IMG_0001.HEIC', b64('x')), /HEIC photos cannot be read here/);
  assert.match(await refused('tool.exe', b64('MZ')), /\.exe files are not supported yet/);
  assert.match(await refused('fake.docx', b64('plain text')), /not a real Word document/);
  assert.equal(await refused('empty.txt', ''), 'There is no text in this file.');
  // Checked from the length of the base64 text, before anything is decoded.
  assert.equal(await refused('huge.log', 'A'.repeat(Math.ceil((20 * 1024 * 1024 + 3) / 3) * 4)), 'The file is larger than 20 MB.');

  // PDFs and the older Office, OpenDocument, RTF and email formats are read (made-up text here).
  for (const [name, kind, bytes] of [['scan.pdf', 'pdf', '%PDF-1.7'], ['old.doc', 'doc', 'OLE2 bytes'],
    ['budget.xls', 'xls', 'OLE2 bytes'], ['notes.odt', 'odt', 'PK\x03\x04 zip'], ['letter.rtf', 'rtf', '{\\rtf1 hi}'],
    ['mail.eml', 'eml', 'Subject: hi'], ['deck.ppt', 'ppt', 'OLE2 bytes']]) {
    const r = await api.call('attach_data', name, b64(bytes));
    assert.deepEqual(r.errors, [], name);
    assert.equal(r.attachments[0].kind, kind, name);
    await api.call('remove_attachment', r.attachments[0].id);
  }

  // Text past tools.attachment_max_chars is cut and marked.
  const big = await api.call('attach_data', 'big.csv', b64('a,b\n'.repeat(60000)));
  assert.equal(big.attachments[0].kind, 'data');
  assert.equal(big.attachments[0].chars, 200000);
  assert.equal(big.attachments[0].truncated, true);
  assert.equal(big.attachments[0].warning, 'Only the first 200,000 characters are used.');
});

test('attach_files stands in for the native dialog: sample pick, a set pick, cancel and the 10-file cap', async () => {
  const sample = await api.call('attach_files');
  assert.equal(sample.ok, true);
  assert.deepEqual(sample.attachments.map((a) => [a.name, a.kind, a.truncated]),
    [['Quarterly report.docx', 'docx', false], ['server.log', 'text', true], ['whiteboard.jpg', 'image', false]]);
  const photo = sample.attachments[2];
  assert.deepEqual([photo.width, photo.height, photo.thumb], [1568, 1176, null], 'a dialog pick has no bytes for a thumbnail');
  assert.deepEqual(sample.errors.map((e) => e.name), ['layers.psd']);

  win.__mock.nextPick = null;
  const cancelled = await api.call('attach_files');
  assert.deepEqual(cancelled, { ok: true, attachments: [], errors: [], cancelled: true });

  win.__mock.nextPick = Array.from({ length: 12 }, (_, i) => ({ name: `part${i + 1}.txt`, size: 100 }));
  const many = await api.call('attach_files');
  assert.equal(many.attachments.length, 10);
  assert.deepEqual(many.errors.map((e) => e.name), ['part11.txt', 'part12.txt']);
  assert.match(many.errors[0].message, /Only 10 files/);
  assert.equal((await api.call('attach_files')).attachments[0].name, 'Quarterly report.docx', 'nextPick is used once');

  const id = many.attachments[0].id;
  assert.deepEqual(await api.call('remove_attachment', id), { ok: true, removed: true });
  assert.deepEqual(await api.call('remove_attachment', id), { ok: true, removed: false });
});

test('pictures: a dropped PNG becomes a picture chip, and providers say which models see pictures', async () => {
  // A 2000 x 1000 PNG header (the mock reads the size, the backend would shrink it).
  const header = Buffer.alloc(33);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(header, 0);
  header.writeUInt32BE(13, 8);
  header.write('IHDR', 12, 'latin1');
  header.writeUInt32BE(2000, 16);
  header.writeUInt32BE(1000, 20);
  const url = `data:image/png;base64,${header.toString('base64')}`;
  const r = await api.call('attach_data', 'Pasted image 10.11.12.png', url);
  assert.deepEqual(r.errors, []);
  const [a] = r.attachments;
  assert.deepEqual([a.kind, a.chars, a.width, a.height, a.thumb], ['image', 0, 1568, 784, url]);

  const views = Object.fromEntries((await api.call('list_providers')).providers.map((p) => [p.id, p]));
  assert.equal(views.openai.vision['gpt-4o-mini'], true);
  assert.equal(views.deepseek.vision['deepseek-chat'], false);
  assert.equal(views.minimax.vision['MiniMax-M3'], true);
  assert.equal(views.minimax.vision['MiniMax-M2.7'], false);
  assert.equal((await api.call('select_model', 'openai', 'gpt-4o')).vision, true);
  assert.equal((await api.call('select_model', 'deepseek', 'deepseek-chat')).vision, false);
});

test('send_message with attachment ids: files-only messages, unknown ids, the cap, and ids forgotten after the reply', async () => {
  assert.equal((await api.call('select_model', 'minimax', 'MiniMax-M3')).ok, true);   // a saved key, no model load
  const { attachments } = await api.call('attach_data', 'notes.txt', b64('the meeting is at noon'));
  const id = attachments[0].id;

  assert.equal((await api.call('send_message', 'hi', ['att_unknown'])).error.code, 'not_found');
  assert.equal((await api.call('send_message', 'hi', Array.from({ length: 11 }, (_, i) => `att_${i}`))).error.code, 'bad_request');

  const run = collect(['chat.done', 'chat.error'], () => true);
  const sent = await api.call('send_message', '', [id]);
  assert.equal(sent.ok, true, 'a message can be files only');
  const done = (await run)[0];
  assert.equal(done.type, 'chat.done');
  assert.match(done.content, /notes\.txt/);

  const { conversation } = await api.call('get_state');
  const user = conversation.findLast((m) => m.role === 'user');
  assert.equal(user.content, '');
  assert.deepEqual(user.attachments, [{ name: 'notes.txt', kind: 'text', chars: 22, truncated: false }]);
  // Like Bridge._emit_then_release: answered, so the id is gone.
  assert.equal((await api.call('send_message', 'again', [id])).error.code, 'not_found');
});

test('"document": create_document saves a file; open/reveal accept only the documents folder', async () => {
  const run = collect(['chat.tool_call', 'chat.tool_result', 'chat.done', 'chat.error'], (e) => e.type === 'chat.done' || e.type === 'chat.error');
  await api.call('send_message', 'write a docx document of the meeting');
  const events = await run;
  assert.equal(events.at(-1).type, 'chat.done', JSON.stringify(events.at(-1)));
  const result = events.find((e) => e.type === 'chat.tool_result');
  assert.equal(result.name, 'create_document');
  const doc = result.document;
  assert.deepEqual(Object.keys(doc).sort(), ['kind', 'name', 'path', 'size']);
  assert.equal(doc.name, 'Meeting summary.docx');
  assert.equal(doc.kind, 'docx');
  assert.match(doc.path, /^C:\\Users\\you\\Documents\\ChatForge\\Meeting summary\.docx$/);
  assert.match(events.at(-1).content, /Meeting summary\.docx/);

  const { conversation } = await api.call('get_state');
  const reply = conversation.at(-1);
  assert.deepEqual(reply.documents, [doc]);
  assert.ok(reply.tools.every((t) => !('document' in t)), 'tools keep their {call_id, name, arguments, ok, summary} shape');

  // Never overwritten: the second one gets " (2)".
  const again = collect(['chat.tool_result'], () => true);
  const second = collect(['chat.done', 'chat.error'], () => true);
  await api.call('send_message', 'another docx document please');
  assert.equal((await again)[0].document.name, 'Meeting summary (2).docx');
  await second;

  assert.deepEqual(await api.call('open_document', doc.path), { ok: true, path: doc.path });
  assert.deepEqual(await api.call('reveal_document', doc.path), { ok: true, path: doc.path });
  const outside = await api.call('open_document', 'C:\\Windows\\notepad.exe');
  assert.equal(outside.error.code, 'bad_request');
  assert.match(outside.error.message, /Only files in the ChatForge documents folder/);
  assert.equal((await api.call('reveal_document', `${doc.path}\\..\\..\\secret.txt`)).error.code, 'bad_request');
  assert.equal((await api.call('open_document', 'C:\\Users\\you\\Documents\\ChatForge\\gone.md')).error.code, 'not_found');
  assert.equal((await api.call('open_document', '')).error.code, 'bad_request');

  // Download: the Save As dialog "saves" to Downloads under the file's name, or where
  // window.__mock.nextSave says (null = cancelled); only documents-folder files.
  assert.deepEqual(await api.call('save_document', doc.path), { ok: true, path: 'C:\\Users\\you\\Downloads\\Meeting summary.docx' });
  win.__mock.nextSave = 'D:\\Reports\\Minutes.docx';
  assert.deepEqual(await api.call('save_document', doc.path), { ok: true, path: 'D:\\Reports\\Minutes.docx' });
  win.__mock.nextSave = null;
  assert.deepEqual(await api.call('save_document', doc.path), { ok: true, cancelled: true });
  assert.ok(!('nextSave' in win.__mock), 'nextSave is used once');
  assert.equal((await api.call('save_document', 'C:\\Windows\\notepad.exe')).error.code, 'bad_request');
  assert.equal((await api.call('save_document', 'C:\\Users\\you\\Documents\\ChatForge\\gone.md')).error.code, 'not_found');
});

test('"xlsx" / "slides": create_document saves an Excel workbook or PowerPoint deck', async () => {
  for (const [words, name] of [['make an xlsx of the actions', 'Meeting summary.xlsx'], ['turn it into pptx slides', 'Meeting summary.pptx']]) {
    const run = collect(['chat.tool_result', 'chat.done', 'chat.error'], (e) => e.type === 'chat.done' || e.type === 'chat.error');
    await api.call('send_message', words);
    const events = await run;
    const result = events.find((e) => e.type === 'chat.tool_result');
    assert.equal(result.document.name, name);
    assert.equal(result.document.kind, name.endsWith('.xlsx') ? 'xlsx' : 'pptx');
    assert.match(events.at(-1).content, /Download/);
  }
});

test('"overflow": chat.context leaves the older messages out; get_state shows a notice there; the next message clears it', async () => {
  assert.equal((await api.call('select_model', 'studioforge', null)).ok, true);   // no key needed
  await api.call('new_chat');
  const done = () => collect(['chat.start', 'chat.context', 'chat.done', 'chat.error'], (e) => e.type === 'chat.done' || e.type === 'chat.error');
  for (const text of ['first: what time is it', 'second: what time is it']) {   // short replies
    const run = done();
    await api.call('send_message', text);
    const events = await run;
    assert.equal(events.at(-1).type, 'chat.done');
    assert.ok(!events.some((e) => e.type === 'chat.context'), 'a message that fits sent chat.context');
  }

  const run = done();
  const r = await api.call('send_message', 'overflow cut please');
  const events = await run;
  assert.deepEqual(events.map((e) => e.type), ['chat.start', 'chat.context', 'chat.done']);
  const [start, ctx] = events;
  let { conversation } = await api.call('get_state');
  const users = conversation.filter((m) => m.role === 'user');
  assert.equal(start.user_ts, users.at(-1).ts, 'chat.start names the stored time of the message');
  assert.deepEqual(ctx, { type: 'chat.context', request_id: r.request_id, dropped_messages: 2, first_kept_ts: users[1].ts, message_cut: true });
  const at = conversation.findIndex((m) => m.role === 'notice');
  assert.deepEqual(conversation[at], { role: 'notice', kind: 'context_cut', content: 'Older messages are past the context window (too long for context).' });
  assert.equal(conversation[at + 1].content, 'second: what time is it');
  assert.equal(conversation.filter((m) => m.role === 'notice').length, 1);
  assert.equal(users.at(-1).cut, true);

  const next = done();
  const r2 = await api.call('send_message', 'and the date today?');
  const after = await next;
  assert.deepEqual(after.find((e) => e.type === 'chat.context'),
    { type: 'chat.context', request_id: r2.request_id, dropped_messages: 0, first_kept_ts: null, message_cut: false });
  ({ conversation } = await api.call('get_state'));
  assert.ok(!conversation.some((m) => m.role === 'notice'));
  assert.equal(conversation.filter((m) => m.cut).length, 1, 'the cut message keeps its note');
});

test('quick actions: listed in get_state, sent by id with a ```text reply, kept by regenerate', async () => {
  assert.equal((await api.call('select_model', 'studioforge', null)).ok, true);   // needs no key
  const s = await api.call('get_state');
  assert.deepEqual(s.config.quick_actions.slice(0, 4).map((a) => a.id), ['proof', 'improve', 'check', 'insight']);
  assert.deepEqual(Object.keys(s.config.quick_actions[0]).sort(), ['hint', 'id', 'label', 'tools']);
  const bad = await api.call('send_message', 'x', [], 'no-such-action');
  assert.equal(bad.ok, false);
  assert.equal(bad.error.code, 'bad_request');

  const run = collect(['chat.done', 'chat.error'], () => true);
  await api.call('send_message', 'Teh report is done.', [], 'proof');
  const [done] = await run;
  assert.equal(done.type, 'chat.done');
  assert.match(done.content, /^```text\nThe report is done\.\n```\n\n- "Teh" → "The"$/);
  let { conversation } = await api.call('get_state');
  const user = conversation.at(-2);
  assert.deepEqual(user, { role: 'user', content: 'Teh report is done.', ts: user.ts, action: { id: 'proof', label: 'Proof this' } });

  const again = collect(['chat.done', 'chat.error'], () => true);
  await api.call('regenerate');
  const [redone] = await again;
  assert.equal(redone.type, 'chat.done');
  assert.match(redone.content, /^```text\n/);
  ({ conversation } = await api.call('get_state'));
  assert.deepEqual(conversation.at(-2).action, { id: 'proof', label: 'Proof this' });
});

test('quick actions: News insight fetches a bare link; Settings edits the list', async () => {
  const run = collect(['chat.tool_call', 'chat.done', 'chat.error'], (e) => e.type !== 'chat.tool_call');
  await api.call('send_message', 'https://news.example.com/budget', [], 'insight');
  const events = await run;
  assert.equal(events[0].type, 'chat.tool_call');
  assert.equal(events[0].name, 'fetch_url');
  assert.equal(events.at(-1).type, 'chat.done');
  assert.match(events.at(-1).content, /\*\*Credibility\*\*/);

  const st = await api.call('get_settings');
  assert.deepEqual(st.quick_actions.items.map((a) => a.id), st.quick_actions.defaults.map((a) => a.id));
  assert.deepEqual(Object.keys(st.quick_actions.items[0]).sort(),
    ['builtin', 'hint', 'id', 'instructions', 'label', 'match_style', 'tools']);
  const changed = collect(['settings.changed'], () => true);
  const res = await api.call('update_settings', {
    chat: { quick_actions: [{ id: 'proof', label: 'Proofread', instructions: '' }, { label: 'Haiku', instructions: 'Write a haiku.' }],
      hidden_quick_actions: ['translate'] },
  });
  assert.equal(res.ok, true);
  const items = Object.fromEntries(res.quick_actions.items.map((a) => [a.id, a]));
  assert.equal(items.proof.label, 'Proofread');
  assert.equal(items.proof.instructions, st.quick_actions.defaults[0].instructions, 'empty instructions keep the built-in ones');
  assert.equal(items['custom-haiku'].builtin, false);
  const [evt] = await changed;
  const ids = evt.config.quick_actions.map((a) => a.id);
  assert.equal(ids.at(-1), 'custom-haiku');
  assert.ok(!ids.includes('translate'));
  assert.equal((await api.call('update_settings', { chat: { quick_actions: [{ label: ' ' }] } })).ok, false);
  await api.call('update_settings', { chat: { quick_actions: [], hidden_quick_actions: [] } });
});

test('dispatch: type handlers then "*", snapshot semantics, single-handler fast path', () => {
  const log = [];
  const a = () => log.push('a');
  const b = () => log.push('b');
  const offA = on('perf.x', () => { log.push('first'); on('perf.x', b); off('perf.x', a); });
  on('perf.x', a);
  const star = on('*', (e) => { if (e.type === 'perf.x') log.push('star'); });
  emit({ type: 'perf.x' });
  // Both handlers present at dispatch ran (a was removed mid-dispatch, b added); '*' last.
  assert.deepEqual(log, ['first', 'a', 'star']);
  log.length = 0;
  emit({ type: 'perf.x' });                       // now: first, b (added), and a is gone
  assert.deepEqual(log, ['first', 'b', 'star']);
  offA(); star();
  const solo = [];
  const stop = on('perf.solo', (e) => { solo.push(e.n); throw new Error('boom'); });
  const origError = console.error; console.error = () => {};
  try { emit({ type: 'perf.solo', n: 1 }); } finally { console.error = origError; }   // a throwing sole handler is contained
  stop();
  emit({ type: 'perf.solo', n: 2 });
  assert.deepEqual(solo, [1]);
});
