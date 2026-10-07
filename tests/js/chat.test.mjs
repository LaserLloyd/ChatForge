// Behavioural tests for the popup (chat.js) against a scripted bridge.
//
// index.html's markup is loaded into jsdom WITHOUT its scripts; marked and DOMPurify are
// evaluated into the window (as in markdown.test.mjs) and chat.js is imported by Node with
// jsdom's globals exposed. window.pywebview.api is defined by this file, so bridge.js takes
// the real path (no dev mock) and every reply is under the test's control: send_message
// resolves only when a test says so, which is how the round-trip races are reproduced.
// Events are pushed through window.__chatforge.emit, exactly like Python's run_js.
// Animation frames run only when a test flushes them, so a frame queued before chat.done
// can be run after it, as WebView2 does when both land in one event batch.
import test, { after, mock } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = join(HERE, '..', '..', 'src', 'chatforge', 'web');
const STATIC = join(WEB, 'static');
const { JSDOM } = createRequire(import.meta.url)('jsdom');

const dom = new JSDOM(readFileSync(join(WEB, 'index.html'), 'utf8'), {
  url: 'http://127.0.0.1:8765/index.html', runScripts: 'outside-only', pretendToBeVisual: true,
});
const win = dom.window;
for (const file of ['purify.min.js', 'marked.min.js']) win.eval(readFileSync(join(STATIC, 'vendor', file), 'utf8'));
win.Element.prototype.scrollTo = function scrollTo() {};

let frames = [];
function flushFrames() {
  const run = frames;
  frames = [];
  for (const fn of run) fn(performance.now());
}

for (const [k, v] of Object.entries({
  window: win, document: win.document, location: win.location, DOMPurify: win.DOMPurify, marked: win.marked,
  FileReader: win.FileReader,   // dropped and pasted files are read with it
  requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
})) {
  Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
}

// ------------------------------------------------------------ fake bridge ----

const key = (source, required, env) => ({ source, env_name: env, env_overrides_saved: false, required });
const PROVIDERS = [
  { id: 'local-npu', display_name: 'Local (NPU)', kind: 'ovms', models: ['OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov'],
    default_model: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov', region: null, base_url: null, builtin: true, docs_url: null, key: key('none', false, null) },
  { id: 'minimax', display_name: 'MiniMax', kind: 'openai', models: ['MiniMax-M3'], default_model: 'MiniMax-M3',
    region: 'international', base_url: 'https://api.minimax.io/v1', builtin: true, docs_url: null, key: key('none', true, 'MINIMAX_API_KEY'),
    vision: { 'MiniMax-M3': true } },
  { id: 'studioforge', display_name: 'StudioForge', kind: 'openai', models: ['qwen-27b'], default_model: 'qwen-27b',
    region: null, base_url: 'http://localhost:1234/v1', builtin: true, docs_url: null, key: key('none', false, 'STUDIOFORGE_API_KEY'),
    vision: { 'qwen-27b': true } },
];
const LOCAL = { provider: 'local-npu', model: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov' };
// get_state.config.quick_actions (chat/actions.py views): the first four built-in ones.
const QUICK = [
  { id: 'proof', label: 'Proof this', hint: 'Paste the text to proofread…', tools: false },
  { id: 'improve', label: 'Improve this', hint: 'Paste the text to improve…', tools: false },
  { id: 'check', label: 'Check me on this', hint: 'Paste a goal, plan or idea to check…', tools: false },
  { id: 'insight', label: 'News insight', hint: 'Paste a news article or a link…', tools: true },
];
const DOC = { name: 'Plan.docx', path: 'C:\\Users\\you\\Documents\\ChatForge\\Plan.docx', size: 14336, kind: 'docx' };
// The snapshot the popup boots with: a message with two files and the document its reply
// saved, the context divider (the model no longer sees that first turn), then a message cut to
// fit the context window and a stopped reply whose first tool call never got a result.
const SNAPSHOT = [
  { role: 'user', content: 'Make this into a plan', ts: 1699999990, attachments: [
    { name: 'notes.md', kind: 'text', chars: 1520, truncated: false },
    { name: 'huge.log', kind: 'text', chars: 200000, truncated: true },
  ] },
  { role: 'assistant', content: 'I saved **Plan.docx**.', ts: 1699999995, model: 'MiniMax-M3', documents: [DOC],
    tools: [{ call_id: 'c0', name: 'create_document', arguments: '{"filename":"Plan.docx"}', ok: true, summary: 'Saved Plan.docx' }] },
  { role: 'notice', kind: 'context_cut', content: 'Older messages are past the context window (too long for context).' },
  { role: 'user', content: 'look it up', ts: 1700000000, cut: true, action: { id: 'improve', label: 'Improve this' } },
  { role: 'assistant', content: 'Partial answer', ts: 1700000005, model: 'MiniMax-M3', stopped: true,
    tools: [
      { call_id: 'c1', name: 'web_search', arguments: '{"query":"npu"}', ok: null, summary: '' },
      { call_id: 'c2', name: 'calculator', arguments: '{"expression":"1/0"}', ok: false, summary: 'division by zero' },
    ] },
];

const calls = [];   // [method, ...args]
const sends = [];   // pending send_message replies: {text, ids, resolve}
const regens = [];  // pending regenerate replies: {resolve}
const picks = [];   // pending attach_files replies (the native dialog): {resolve}
const datas = [];   // pending attach_data replies: {name, data, resolve}
const saves = [];   // pending save_document replies (the Save As dialog): {path, resolve}
const clone = (o) => JSON.parse(JSON.stringify(o));
const ok = (extra = {}) => ({ ok: true, ...extra });
win.pywebview = {
  api: {
    get_state: async () => ok({
      config: { chat: { ...LOCAL, show_reasoning: 'collapsed', max_prompt_chars: 4000 }, ui: { theme: 'laserlloyd', sticky: false }, local: {},
        quick_actions: clone(QUICK) },
      providers: clone(PROVIDERS), selected: { ...LOCAL }, runtime: { state: 'ready', model_id: LOCAL.model },
      conversation: clone(SNAPSHOT), limits: { max_prompt_chars: 4000 }, theme: 'laserlloyd',
    }),
    // Called with one argument when there are no files, exactly as before attachments.
    send_message: (...args) => new Promise((resolve) => {
      calls.push(['send_message', ...args]);
      sends.push({ text: args[0], ids: args[1], resolve });
    }),
    regenerate: () => new Promise((resolve) => { calls.push(['regenerate']); regens.push({ resolve }); }),
    attach_files: () => new Promise((resolve) => { calls.push(['attach_files']); picks.push({ resolve }); }),
    attach_data: (name, data) => new Promise((resolve) => { calls.push(['attach_data', name, data]); datas.push({ name, data, resolve }); }),
    remove_attachment: async (id) => { calls.push(['remove_attachment', id]); return ok({ removed: true }); },
    open_document: async (path) => { calls.push(['open_document', path]); return docReply(path); },
    reveal_document: async (path) => { calls.push(['reveal_document', path]); return docReply(path); },
    // The Save As dialog: answered by the test through `saves` (pending replies).
    save_document: (path) => new Promise((resolve) => { calls.push(['save_document', path]); saves.push({ path, resolve }); }),
    stop_generation: async (id) => { calls.push(['stop_generation', id]); return ok(); },
    new_chat: async () => { calls.push(['new_chat']); return ok(); },
    list_providers: async () => ok({ providers: clone(PROVIDERS) }),
    select_model: async (provider, model) => ok({ selected: { provider, model } }),
    hide_popup: async (...args) => { calls.push(['hide_popup', ...args]); return ok(); },
    start_resize: async (...args) => { calls.push(['start_resize', ...args]); return ok({ resizing: true }); },
    drag_resize: async (...args) => { calls.push(['drag_resize', ...args]); return ok({ resizing: true }); },
    end_resize: async () => { calls.push(['end_resize']); return ok({ width: 500, height: 700 }); },
    reset_popup_size: async () => { calls.push(['reset_popup_size']); return ok({ width: 420, height: 620 }); },
    open_settings: async () => ok(),
    open_external: async () => ok(),
    set_sticky: async (flag) => { calls.push(['set_sticky', flag]); return ok({ sticky: !!flag }); },
    load_model: async () => ok(),
    unload_model: async () => ok(),
  },
};

/** desktop/bridge.py open_document / reveal_document: a moved file is not_found. */
function docReply(path) {
  if (/gone\.md$/.test(path)) return { ok: false, error: { code: 'not_found', message: 'That document no longer exists.', hint: '', action: null } };
  return ok({ path });
}

/** An attach reply view, as attachments.Pending.view() gives it. */
const view = (id, name, extra = {}) => ({ id, name, kind: 'text', chars: 1200, size: 2048, truncated: false, warning: null, ...extra });

await import(pathToFileURL(join(STATIC, 'js', 'chat.js')).href);
const $ = (id) => win.document.getElementById(id);
const S = () => win.__chat.S;
const emit = (evt) => win.__chatforge.emit(evt);
/** Let promise chains (api.call -> await ready() -> await fn()) run. Not a timer, so it
 *  still works while setTimeout is mocked. */
async function settle(rounds = 6) { for (let i = 0; i < rounds; i++) await new Promise((r) => setImmediate(r)); }
for (let i = 0; i < 100 && !S().providers.length; i++) await settle();
assert.ok(S().providers.length, 'chat.js did not boot');
const bootRows = $('messages').cloneNode(true);

after(() => { dom.window.close(); setTimeout(() => process.exit(0), 50).unref(); });

/** A clean popup: no request in flight, no messages, nothing in the composer, the local
 *  model selected. */
async function fresh(selected = LOCAL) {
  $('btn-new').click();
  await settle();
  emit({ type: 'settings.changed', config: { chat: { ...selected } } });
  await settle();
  for (const b of [...$('attach-list').querySelectorAll('.att-remove')]) b.click();
  $('attach-errors').querySelector('.att-err-close')?.click();
  $('input').value = '';
  $('input').dispatchEvent(new win.Event('input'));
  await settle();
  calls.length = 0;
  sends.length = 0;
  regens.length = 0;
  picks.length = 0;
  datas.length = 0;
  frames = [];
  S().ignore.clear();
}

/** Attach `views` through the paperclip (the dialog answers at once). */
async function attachViaDialog(views, errors = []) {
  $('attach').click();
  await settle();
  picks.shift().resolve(ok({ attachments: views, errors }));
  await settle();
}

/** A drag event carrying `files`, as WebView2 delivers one. */
function dragEvent(type, files = []) {
  const e = new win.Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(e, 'dataTransfer', { value: { types: ['Files'], files, dropEffect: 'none' } });
  return e;
}

const chipNames = (root) => [...root.querySelectorAll('.att-chip .att-name')].map((n) => n.textContent);
const errorLines = () => [...$('attach-errors').querySelectorAll('.att-err')].map((n) => n.textContent);

/** Send `text` and answer send_message with `id`. */
async function sendAs(text, id) {
  win.__chat.send(text);
  await settle();
  sends.shift().resolve(ok({ request_id: id }));
  await settle();
}

const lastAssistant = () => [...$('messages').querySelectorAll('.msg.assistant')].at(-1);
const metaText = (row) => [...row.querySelectorAll('.msg-time')].map((n) => n.textContent).join(' ');
const composerIdle = () => !$('send').classList.contains('hidden') && $('stop').classList.contains('hidden');

// ---------------------------------------------------------------- snapshot ----

test('snapshot: a tool call without a result yet is running, not failed; a stopped reply says so', () => {
  const reply = [...bootRows.querySelectorAll('.msg.assistant')].at(-1);
  const states = [...reply.querySelectorAll('.tool-chip')].map((c) => c.dataset.state);
  assert.deepEqual(states, ['running', 'fail']);
  assert.match(metaText(reply), /MiniMax-M3 · stopped/);
});

test('snapshot: a user message shows its files, and a reply the documents it saved', () => {
  const user = bootRows.querySelector('.msg.user');
  const chips = [...user.querySelectorAll('.msg-files .att-chip')];
  assert.deepEqual(chipNames(user), ['notes.md', 'huge.log']);
  assert.equal(chips[0].querySelector('.att-meta').textContent, '1.5k chars');
  assert.equal(chips[0].querySelector('.att-warn'), null);
  assert.ok(chips[1].querySelector('.att-warn'), 'a cut file has no "partial" badge');
  assert.match(chips[1].querySelector('.att-warn').getAttribute('aria-label'), /Only part of this file/);
  assert.equal(user.querySelector('.att-remove'), null, 'a sent file cannot be removed');
  assert.equal(user.querySelector('.bubble').textContent, 'Make this into a plan');

  const reply = bootRows.querySelector('.msg.assistant');
  const card = reply.querySelector('.doc-card');
  assert.ok(card, 'no document card from history');
  assert.equal(card.querySelector('.doc-name').textContent, 'Plan.docx');
  assert.equal(card.querySelector('.doc-meta').textContent, 'DOCX · 14 KB');
  assert.deepEqual([...card.querySelectorAll('button')].map((b) => b.textContent), ['Open', 'Download', 'Show in folder']);
  // Under the text that mentions it, above the time line.
  const kids = [...reply.querySelector('.msg-col').children].map((n) => n.className);
  assert.deepEqual(kids.slice(1, 4), ['bubble', 'docs', 'msg-time']);
});

test('snapshot: the context divider sits before the first message the model still sees, and a cut message says so', () => {
  const dividers = [...bootRows.querySelectorAll('.ctx-divider')];
  assert.equal(dividers.length, 1);
  assert.equal(dividers[0].textContent, 'Older messages are past the context window (too long for context).');
  assert.equal(dividers[0].getAttribute('role'), 'separator');
  const [first, second] = bootRows.querySelectorAll('.msg.user');
  assert.equal(dividers[0].nextElementSibling, second);
  assert.equal(second.dataset.ts, '1700000000');
  assert.equal(second.querySelector('.msg-time .ctx-cut').textContent.trim(), '(Too long for context)');
  assert.equal(first.querySelector('.ctx-cut'), null);
});

// ---------------------------------------------------------------- streaming ----

test('a stream frame queued before chat.done does not undo the finished reply', async () => {
  await fresh();
  await sendAs('table please', 'req_paint');
  const table = `| Runtime | Device | First load |\n|---|---|---|\n${'| OVMS 2026.4 | NPU | about six minutes |\n'.repeat(4)}`;
  assert.ok(table.length >= 160, 'the delta must be a burst, so its frame is queued at once');
  emit({ type: 'chat.start', request_id: 'req_paint', provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.delta', request_id: 'req_paint', content: table });
  emit({ type: 'chat.done', request_id: 'req_paint', content: table, model: LOCAL.model, provider: 'local-npu', tok_per_s: 40 });
  assert.equal(frames.length > 0, true, 'no frame was queued by the delta');
  const row = lastAssistant();
  assert.ok(row.querySelector('.table-scroll'), 'finalize did not enhance the table');
  flushFrames();
  assert.ok(row.querySelector('.table-scroll > table'), 'the queued frame repainted raw markdown over the finished reply');
  assert.equal(row.classList.contains('streaming'), false);
});

// ------------------------------------------------------ send round-trip races ----

test('New chat while send_message is in flight stops that request and ignores its events', async () => {
  await fresh();
  win.__chat.send('first question');
  await settle();
  $('btn-new').click();
  await settle();
  assert.equal(S().busy, false);
  sends.shift().resolve(ok({ request_id: 'req_old' }));
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'stop_generation'), [['stop_generation', 'req_old']]);
  assert.equal(S().reqId, null, 'the abandoned id was recorded');
  assert.ok(S().ignore.has('req_old'));

  // The next message must not adopt the old request's reply.
  win.__chat.send('second question');
  await settle();
  emit({ type: 'chat.delta', request_id: 'req_old', content: 'OLD REPLY' });
  sends.shift().resolve(ok({ request_id: 'req_new' }));
  await settle();
  emit({ type: 'chat.delta', request_id: 'req_old', content: 'OLD REPLY' });
  emit({ type: 'chat.delta', request_id: 'req_new', content: 'New reply.' });
  emit({ type: 'chat.done', request_id: 'req_new', content: 'New reply.', provider: 'local-npu', model: LOCAL.model });
  const text = $('messages').textContent;
  assert.match(text, /New reply\./);
  assert.doesNotMatch(text, /OLD REPLY/);
});

test('events that arrive before send_message answers (even a fallback) keep the request', async () => {
  await fresh();
  win.__chat.send('hello');
  await settle();
  emit({ type: 'chat.start', request_id: 'req_early', provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.fallback', request_id: 'req_early', from_provider: 'local-npu', provider: 'minimax', model: 'MiniMax-M3' });
  sends.shift().resolve(ok({ request_id: 'req_early' }));
  await settle();
  assert.equal(S().reqId, 'req_early');
  assert.equal(S().busy, true);
  assert.deepEqual(calls.filter((c) => c[0] === 'stop_generation'), []);
  emit({ type: 'chat.delta', request_id: 'req_early', content: 'From MiniMax.' });
  emit({ type: 'chat.done', request_id: 'req_early', content: 'From MiniMax.', provider: 'minimax', model: 'MiniMax-M3' });
  assert.match(metaText(lastAssistant()), /MiniMax-M3 · via MiniMax/);
});

test('Stop pressed before send_message answers is sent once the id arrives', async () => {
  await fresh();
  win.__chat.send('stop me');
  await settle();
  $('stop').click();
  await settle();
  assert.equal(S().stopping, true);
  assert.deepEqual(calls.filter((c) => c[0] === 'stop_generation'), [], 'no id yet, nothing to stop');
  sends.shift().resolve(ok({ request_id: 'req_stop' }));
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'stop_generation'), [['stop_generation', 'req_stop']]);
  emit({ type: 'chat.error', request_id: 'req_stop', code: 'cancelled', message: 'Stopped.', partial: false });
  assert.equal(S().busy, false);
  assert.ok(composerIdle(), 'the composer is still stuck on Stop');
});

test('the Stop safety net still fires when the id arrived after Stop was pressed', async () => {
  await fresh();
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    win.__chat.send('never cancelled');
    await settle();
    $('stop').click();
    await settle();
    sends.shift().resolve(ok({ request_id: 'req_lost' }));
    await settle();
    assert.equal(S().stopping, true);
    mock.timers.tick(8000);   // the cancelled event never comes
    await settle();
    assert.equal(S().busy, false);
    assert.equal(S().stopping, false);
    assert.ok(composerIdle(), 'the composer is still stuck on Stopping…');
    assert.ok(S().ignore.has('req_lost'));
  } finally {
    mock.timers.reset();
  }
});

// ------------------------------------------------------------- key handling ----

test('a provider whose key is optional (StudioForge) is ready without one', async () => {
  await fresh({ provider: 'studioforge', model: 'qwen-27b' });
  assert.equal($('status-dot').dataset.state, 'live');
  const empty = $('messages').querySelector('.empty-state');
  assert.ok(empty, 'no empty state');
  assert.doesNotMatch(empty.textContent, /Add your StudioForge key/);
  assert.ok(empty.querySelector('.qa-grid'), 'the quick actions should show instead of the key button');

  await fresh({ provider: 'minimax', model: 'MiniMax-M3' });   // a key is required and missing
  assert.equal($('status-dot').dataset.state, 'warning');
  assert.match($('messages').querySelector('.empty-state').textContent, /Add your MiniMax key/);
});

test('a key saved in Settings while the key card is open sends the message and clears the composer', async () => {
  await fresh({ provider: 'minimax', model: 'MiniMax-M3' });
  await sendAs('hello there', 'req_nokey');
  emit({ type: 'chat.error', request_id: 'req_nokey', code: 'no_key', action: 'add_key', provider: 'minimax',
    message: 'Add your MiniMax API key', hint: '' });
  await settle();
  assert.ok($('messages').querySelector('.key-card'), 'no key card');
  assert.equal($('input').value, 'hello there', 'the text goes back into the composer');

  emit({ type: 'key.status', provider_id: 'minimax', source: 'keyring', env_name: 'MINIMAX_API_KEY' });
  await settle();
  assert.equal($('messages').querySelector('.key-card'), null, 'the card stayed open');
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message').map((c) => c[1]), ['hello there', 'hello there']);
  assert.equal($('input').value, '', 'the re-sent message was left in the composer');
});

// ------------------------------------------------------------- reply meta ----

test('chat.reset after chat.fallback keeps the fallback model and "via" note', async () => {
  await fresh();
  await sendAs('what is new?', 'req_fb');
  emit({ type: 'chat.fallback', request_id: 'req_fb', from_provider: 'local-npu', provider: 'minimax', model: 'MiniMax-M3' });
  emit({ type: 'chat.reset', request_id: 'req_fb', reason: 'refusal' });
  emit({ type: 'chat.delta', request_id: 'req_fb', content: 'Here is the answer.' });
  emit({ type: 'chat.done', request_id: 'req_fb', tok_per_s: 50 });   // an engine that names neither
  const meta = metaText(lastAssistant());
  assert.match(meta, /MiniMax-M3/);
  assert.match(meta, /via MiniMax/);
  assert.doesNotMatch(meta, /Qwen2\.5/);
});

test('chat.done names the provider and model that answered', async () => {
  await fresh();
  await sendAs('hi', 'req_done');
  emit({ type: 'chat.delta', request_id: 'req_done', content: 'Hello.' });
  emit({ type: 'chat.done', request_id: 'req_done', content: 'Hello.', provider: 'local-npu', model: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov', tok_per_s: 45.2 });
  const meta = metaText(lastAssistant());
  assert.match(meta, /Qwen2\.5-1\.5B-Instruct-int4-ov · 45\.2 tok\/s/);
  assert.doesNotMatch(meta, /via/);
});

test('a reply stopped because the model unloaded says why', async () => {
  await fresh();
  await sendAs('long answer', 'req_unload');
  emit({ type: 'chat.delta', request_id: 'req_unload', content: 'Part of the answer' });
  emit({ type: 'chat.error', request_id: 'req_unload', code: 'cancelled', message: 'Stopped: the local model was unloaded.', partial: true });
  assert.match(metaText(lastAssistant()), /stopped: the local model was unloaded/);
  assert.ok(composerIdle());
});

// ------------------------------------------------------------- attachments ----
// (An error line's text is name + message: the ": " between them is CSS.)

const REPORT = view('att_a', 'report.docx', { kind: 'docx', chars: 12480, size: 48213 });
const LOG = view('att_b', 'server.log', { chars: 200000, size: 3500000, truncated: true,
  warning: 'Only the first 200,000 characters are used.' });
const NO_IMAGES = 'Images are not supported yet. Attach text, code, Word, Excel, PowerPoint or PDF files.';

test('the paperclip opens the file dialog; chips show size, characters, a partial badge and the refusals', async () => {
  await fresh();
  win.document.hasFocus = () => false;
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    $('attach').click();
    await settle();
    assert.deepEqual(calls.at(-1), ['attach_files']);
    assert.equal($('attach').disabled, true, 'a second dialog could be opened over the first');
    // The dialog takes the focus: the blur that follows must not hide the popup.
    win.dispatchEvent(new win.Event('blur'));
    mock.timers.tick(200);
    await settle();
    assert.deepEqual(calls.filter((c) => c[0] === 'hide_popup'), [], 'the popup hid behind its own file dialog');

    picks.shift().resolve(ok({ attachments: [REPORT, LOG], errors: [{ name: 'whiteboard.png', message: NO_IMAGES }] }));
    await settle();
    assert.equal($('attach').disabled, false);
    assert.equal($('attach-list').hidden, false);
    const chips = [...$('attach-list').querySelectorAll('.att-chip')];
    assert.deepEqual(chipNames($('attach-list')), ['report.docx', 'server.log']);
    assert.equal(chips[0].querySelector('.att-meta').textContent, '47 KB · 12.5k chars');
    assert.equal(chips[0].querySelector('.att-warn'), null);
    assert.equal(chips[1].querySelector('.att-meta').textContent, '3.3 MB · 200k chars');
    const badge = chips[1].querySelector('.att-warn');
    assert.equal(badge.getAttribute('aria-label'), 'Only the first 200,000 characters are used.');
    assert.match(badge.textContent, /partial/);
    assert.equal(chips[1].querySelector('.att-remove').getAttribute('aria-label'), 'Remove server.log');
    assert.equal($('attach-errors').hidden, false);
    assert.deepEqual(errorLines(), [`whiteboard.png${NO_IMAGES}`]);
    assert.equal($('send').disabled, false, 'files alone can be sent');

    // Once the dialog has closed, blurring the popup hides it again as usual.
    win.dispatchEvent(new win.Event('blur'));
    mock.timers.tick(200);
    await settle();
    assert.equal(calls.filter((c) => c[0] === 'hide_popup').length, 1);
  } finally {
    mock.timers.reset();
    delete win.document.hasFocus;
  }
});

test('a cancelled dialog changes nothing; a full composer refuses more files without opening it', async () => {
  await fresh();
  $('attach').click();
  await settle();
  picks.shift().resolve(ok({ attachments: [], errors: [], cancelled: true }));
  await settle();
  assert.equal($('attach-list').hidden, true);
  assert.equal($('attach-errors').hidden, true);

  // The dialog does not know what is already attached: past ten, the extras are given back.
  await attachViaDialog(Array.from({ length: 9 }, (_, i) => view(`att_${i}`, `part${i}.txt`)));
  await attachViaDialog([view('att_9', 'part9.txt'), view('att_10', 'part10.txt')]);
  assert.equal(S().files.length, 10);
  assert.deepEqual(calls.filter((c) => c[0] === 'remove_attachment'), [['remove_attachment', 'att_10']]);
  assert.deepEqual(errorLines(), ['part10.txtOnly 10 files can be attached to one message.']);

  calls.length = 0;
  $('attach').click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'attach_files'), [], 'the dialog opened for an 11th file');
  assert.deepEqual(errorLines(), ['Only 10 files can be attached to one message. Remove one to add another.']);
});

test('removing a chip forgets the file in the backend and keeps the keyboard in the list', async () => {
  await fresh();
  await attachViaDialog([REPORT, LOG]);
  const first = $('attach-list').querySelector('.att-remove');
  first.focus();
  first.click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'remove_attachment'), [['remove_attachment', 'att_a']]);
  assert.deepEqual(chipNames($('attach-list')), ['server.log']);
  assert.equal(win.document.activeElement, $('attach-list').querySelector('.att-remove'));
  $('attach-list').querySelector('.att-remove').click();
  await settle();
  assert.equal($('attach-list').hidden, true);
  assert.equal(win.document.activeElement, $('input'));
  assert.equal($('send').disabled, true, 'nothing left to send');
});

test('sending takes the chips with the message; Retry sends the same files again', async () => {
  await fresh();
  await attachViaDialog([REPORT, LOG]);
  $('input').value = 'Summarise these';
  $('input').dispatchEvent(new win.Event('input'));
  $('send').click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message'), [['send_message', 'Summarise these', ['att_a', 'att_b']]]);
  assert.equal($('attach-list').hidden, true, 'the chips stayed in the composer');
  assert.deepEqual(S().files, []);
  assert.deepEqual(calls.filter((c) => c[0] === 'remove_attachment'), [], 'sent files must stay in the backend');
  const user = [...$('messages').querySelectorAll('.msg.user')].at(-1);
  assert.deepEqual(chipNames(user), ['report.docx', 'server.log']);
  assert.equal(user.querySelector('.bubble').textContent, 'Summarise these');
  assert.ok(user.querySelector('.att-warn'), 'the sent message lost its "partial" badge');

  sends.shift().resolve(ok({ request_id: 'req_files' }));
  await settle();
  emit({ type: 'chat.error', request_id: 'req_files', code: 'server', message: 'The model server returned an error.', action: 'retry' });
  const retryBtn = [...$('messages').querySelectorAll('.err-actions button')].find((b) => b.textContent === 'Retry');
  retryBtn.click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message').at(-1), ['send_message', 'Summarise these', ['att_a', 'att_b']]);
  assert.equal($('messages').querySelectorAll('.msg.user').length, 1, 'Retry added a second user message');
});

test('a message of files only has no text bubble; text-only sends keep the one-argument call', async () => {
  await fresh();
  await attachViaDialog([REPORT]);
  $('input').dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message'), [['send_message', '', ['att_a']]]);
  const user = [...$('messages').querySelectorAll('.msg.user')].at(-1);
  assert.deepEqual(chipNames(user), ['report.docx']);
  assert.equal(user.querySelector('.bubble'), null);
  sends.shift().resolve(ok({ request_id: 'req_only_files' }));
  await settle();
  emit({ type: 'chat.done', request_id: 'req_only_files', content: 'Read it.', provider: 'local-npu', model: LOCAL.model });

  await sendAs('plain question', 'req_plain');
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message').at(-1), ['send_message', 'plain question']);
});

test('dropped files: an overlay while dragging, the 20 MB check before reading, then attach_data with a data: URL', async () => {
  await fresh();
  const notes = new win.File(['hello world'], 'notes.txt', { type: 'text/plain' });
  const huge = { name: 'disk.iso', size: 21 * 1024 * 1024 };   // never read
  win.document.body.dispatchEvent(dragEvent('dragenter', [notes, huge]));
  assert.equal($('drop-zone').hidden, false);
  const over = dragEvent('dragover', [notes, huge]);
  win.document.body.dispatchEvent(over);
  assert.equal(over.defaultPrevented, true, 'without preventDefault on dragover WebView2 opens the file instead');
  const drop = dragEvent('drop', [notes, huge]);
  $('messages').dispatchEvent(drop);
  assert.equal(drop.defaultPrevented, true);
  assert.equal($('drop-zone').hidden, true);
  assert.deepEqual(errorLines(), ['disk.isoThe file is larger than 20 MB.']);
  const loading = $('attach-list').querySelector('.att-chip');
  assert.equal(loading.dataset.state, 'loading');
  assert.match(loading.textContent, /Reading…/);
  assert.equal($('send').disabled, true, 'a file still being read could be sent');

  for (let i = 0; i < 50 && !datas.length; i++) await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'attach_data'), [['attach_data', 'notes.txt', 'data:text/plain;base64,aGVsbG8gd29ybGQ=']]);
  datas.shift().resolve(ok({ attachments: [view('att_n', 'notes.txt', { chars: 11, size: 11 })], errors: [] }));
  await settle();
  const chip = $('attach-list').querySelector('.att-chip');
  assert.equal(chip.dataset.state, 'ready');
  assert.equal(chip.querySelector('.att-meta').textContent, '11 B · 11 chars');
  assert.equal($('send').disabled, false);

  // A drag that only crosses the popup leaves no overlay behind.
  win.document.body.dispatchEvent(dragEvent('dragenter', [notes]));
  $('messages').dispatchEvent(dragEvent('dragenter', [notes]));
  $('messages').dispatchEvent(dragEvent('dragleave', [notes]));
  assert.equal($('drop-zone').hidden, false);
  win.document.body.dispatchEvent(dragEvent('dragleave', [notes]));
  assert.equal($('drop-zone').hidden, true);
});

test('a dropped file the backend refuses becomes an error, not a chip', async () => {
  await fresh();
  const pic = new win.File(['x'], 'photo.png', { type: 'image/png' });
  $('messages').dispatchEvent(dragEvent('drop', [pic]));
  for (let i = 0; i < 50 && !datas.length; i++) await settle();
  datas.shift().resolve(ok({ attachments: [], errors: [{ name: 'photo.png', message: NO_IMAGES }] }));
  await settle();
  assert.equal($('attach-list').hidden, true);
  assert.deepEqual(errorLines(), [`photo.png${NO_IMAGES}`]);
  $('attach-errors').querySelector('.att-err-close').click();
  assert.equal($('attach-errors').hidden, true);
});

test('pasting a file attaches it; pasting text that comes with a picture pastes the text', async () => {
  await fresh();
  const file = new win.File(['a,b\n1,2\n'], 'table.csv', { type: 'text/csv' });
  const paste = (text) => {
    const e = new win.Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(e, 'clipboardData', { value: { files: [file], items: [], getData: () => text } });
    $('input').dispatchEvent(e);
    return e;
  };
  const withText = paste('Quarter,Total');   // e.g. cells copied from Excel
  await settle();
  assert.equal(withText.defaultPrevented, false);
  assert.deepEqual(calls.filter((c) => c[0] === 'attach_data'), []);

  const fileOnly = paste('');
  assert.equal(fileOnly.defaultPrevented, true);
  for (let i = 0; i < 50 && !datas.length; i++) await settle();
  assert.equal(datas[0].name, 'table.csv');
  assert.match(datas[0].data, /^data:text\/csv;base64,/);
  datas.shift().resolve(ok({ attachments: [view('att_csv', 'table.csv', { kind: 'data' })], errors: [] }));
  await settle();
  assert.deepEqual(chipNames($('attach-list')), ['table.csv']);
});

// ---------------------------------------------------------------- pictures ----

const THUMB = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ==';
const PHOTO = view('att_p', 'whiteboard.jpg', { kind: 'image', chars: 0, size: 1258291, width: 1568, height: 1176, thumb: THUMB });
const NO_VISION = 'This model can’t see images — pick a vision model in the menu.';
const SEEING = { provider: 'studioforge', model: 'qwen-27b' };

test('a picture chip shows its thumbnail and size, and warns while the model cannot see it', async () => {
  await fresh();   // the local model cannot see pictures
  await attachViaDialog([PHOTO, REPORT]);
  const [chip, other] = [...$('attach-list').querySelectorAll('.att-chip')];
  const img = chip.querySelector('img.att-thumb');
  assert.equal(img.getAttribute('src'), THUMB);
  assert.equal(img.getAttribute('alt'), 'whiteboard.jpg', 'the alt text is the file name');
  assert.equal(chip.querySelector('.att-meta').textContent, '1568 × 1176 · 1.2 MB');
  assert.equal(chip.querySelector('.att-warn').getAttribute('aria-label'), NO_VISION);
  assert.equal(other.querySelector('.att-warn'), null, 'a document is not a picture');
  assert.equal($('attach-note').hidden, false);
  assert.equal($('attach-note').textContent, NO_VISION);

  // A model that sees pictures: no warning; back to the local model: the warning is back.
  emit({ type: 'settings.changed', config: { chat: { ...SEEING } } });
  await settle();
  assert.equal($('attach-note').hidden, true);
  assert.equal($('attach-list').querySelector('.att-warn'), null);
  emit({ type: 'settings.changed', config: { chat: { ...LOCAL } } });
  await settle();
  assert.equal($('attach-note').hidden, false);

  // Sent: the thumbnail heads the message, and the note goes with the chips.
  $('send').click();
  await settle();
  const user = [...$('messages').querySelectorAll('.msg.user')].at(-1);
  const sent = user.querySelector('.msg-files .att-image img.att-thumb');
  assert.equal(sent.getAttribute('src'), THUMB);
  assert.equal(sent.getAttribute('alt'), 'whiteboard.jpg');
  assert.equal(user.querySelector('.att-image .att-warn'), null, 'a sent message does not warn');
  assert.equal($('attach-note').hidden, true);
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message'), [['send_message', '', ['att_p', 'att_a']]]);
  sends.shift().resolve(ok({ request_id: 'req_pic' }));
  await settle();
  emit({ type: 'chat.phase', request_id: 'req_pic', phase: 'reading_image' });
  assert.match(lastAssistant().textContent, /Reading the text in the picture…/);
  emit({ type: 'chat.done', request_id: 'req_pic', content: 'A whiteboard.', provider: 'local-npu', model: LOCAL.model });
  await settle();
});

test('choosing a model that sees pictures from the menu clears the warning', async () => {
  await fresh();
  await attachViaDialog([PHOTO]);
  assert.equal($('attach-note').hidden, false);
  $('model-chip').click();
  await settle();
  const item = $('model-menu').querySelector('[data-provider="studioforge"][data-model="qwen-27b"]');
  assert.equal(item.querySelector('.mm-vision').getAttribute('aria-label'), 'sees pictures');
  assert.equal($('model-menu').querySelector('[data-provider="local-npu"] .mm-vision'), null);
  item.click();
  await settle();
  assert.equal($('attach-note').hidden, true);
  assert.equal($('attach-list').querySelector('.att-warn'), null);
});

test('a saved conversation shows picture thumbnails, and only real picture data', () => {
  win.__chat.renderConversation([{ role: 'user', content: 'see', ts: 1, attachments: [
    { name: 'a.png', kind: 'image', chars: 0, truncated: false, width: 800, height: 600, thumb: THUMB },
    { name: 'b.png', kind: 'image', chars: 0, truncated: false, width: null, height: null, thumb: 'javascript:alert(1)' },
  ] }]);
  const chips = [...$('messages').querySelectorAll('.msg.user .att-chip')];
  assert.equal(chips[0].querySelector('img').getAttribute('src'), THUMB);
  assert.equal(chips[0].querySelector('img').getAttribute('alt'), 'a.png');
  assert.equal(chips[0].querySelector('.att-meta').textContent, '800 × 600');
  assert.equal(chips[1].querySelector('img'), null, 'a thumbnail that is not picture data is never loaded');
  assert.ok(chips[1].querySelector('svg'), 'a picture icon instead');
});

test('pasting a screenshot attaches it under a readable name', async () => {
  await fresh();
  const shot = new win.File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'image.png', { type: 'image/png' });
  const e = new win.Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(e, 'clipboardData', { value: { files: [shot], items: [], getData: () => '' } });
  $('input').dispatchEvent(e);
  assert.equal(e.defaultPrevented, true);
  const loading = $('attach-list').querySelector('.att-chip');
  assert.equal(loading.dataset.state, 'loading');
  assert.ok(loading.classList.contains('att-image'), 'a picture is being read');
  for (let i = 0; i < 50 && !datas.length; i++) await settle();
  const { name, data } = datas[0];
  assert.match(name, /^Pasted image \d\d\.\d\d\.\d\d\.png$/);
  assert.match(data, /^data:image\/png;base64,/);
  datas.shift().resolve(ok({ attachments: [{ ...PHOTO, id: 'att_shot', name }], errors: [] }));
  await settle();
  assert.deepEqual(chipNames($('attach-list')), [name]);
  assert.equal($('attach-list').querySelector('img.att-thumb').getAttribute('alt'), name);
});

test('a key added after no_key sends the files again and takes them out of the composer', async () => {
  await fresh({ provider: 'minimax', model: 'MiniMax-M3' });
  await attachViaDialog([REPORT]);
  $('send').click();
  await settle();
  sends.shift().resolve(ok({ request_id: 'req_nokey_files' }));
  await settle();
  emit({ type: 'chat.error', request_id: 'req_nokey_files', code: 'no_key', action: 'add_key', provider: 'minimax',
    message: 'Add your MiniMax API key', hint: '' });
  await settle();
  assert.ok($('messages').querySelector('.key-card'), 'no key card');
  assert.deepEqual(chipNames($('attach-list')), ['report.docx'], 'the files go back into the composer');
  assert.equal($('messages').querySelector('.msg.user'), null);

  emit({ type: 'key.status', provider_id: 'minimax', source: 'keyring', env_name: 'MINIMAX_API_KEY' });
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message').at(-1), ['send_message', '', ['att_a']]);
  assert.equal($('attach-list').hidden, true, 'the re-sent files were left in the composer');
  assert.deepEqual(calls.filter((c) => c[0] === 'remove_attachment'), []);
});

test('a chip removed while the key card is open is not sent when the key arrives', async () => {
  await fresh({ provider: 'minimax', model: 'MiniMax-M3' });
  await attachViaDialog([REPORT, LOG]);
  $('send').click();
  await settle();
  sends.shift().resolve(ok({ request_id: 'req_nokey_removed' }));
  await settle();
  emit({ type: 'chat.error', request_id: 'req_nokey_removed', code: 'no_key', action: 'add_key', provider: 'minimax',
    message: 'Add your MiniMax API key', hint: '' });
  await settle();
  assert.deepEqual(chipNames($('attach-list')), ['report.docx', 'server.log']);
  $('attach-list').querySelectorAll('.att-remove')[1].click();   // server.log
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'remove_attachment'), [['remove_attachment', 'att_b']]);
  assert.deepEqual(S().lastFiles.map((f) => f.id), ['att_a'], 'Retry would send the removed file');

  emit({ type: 'key.status', provider_id: 'minimax', source: 'keyring', env_name: 'MINIMAX_API_KEY' });
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message').at(-1), ['send_message', '', ['att_a']]);
  assert.equal($('attach-list').hidden, true, 'the re-sent file was left in the composer');
});

// --------------------------------------------------------------- documents ----

test('a saved document shows as a card under the reply, with Open, Download and Show in folder', async () => {
  await fresh();
  await sendAs('write the plan as a Word file', 'req_doc');
  emit({ type: 'chat.tool_call', request_id: 'req_doc', call_id: 'd1', name: 'create_document', arguments: '{"filename":"Plan.docx"}' });
  emit({ type: 'chat.tool_result', request_id: 'req_doc', call_id: 'd1', name: 'create_document', ok: true,
    summary: 'Saved Plan.docx', document: DOC });
  const row = lastAssistant();
  const card = row.querySelector('.doc-card');
  assert.ok(card, 'no card from chat.tool_result');
  assert.equal(card.getAttribute('aria-label'), 'Document Plan.docx');
  emit({ type: 'chat.delta', request_id: 'req_doc', content: 'I saved Plan.docx.' });
  emit({ type: 'chat.done', request_id: 'req_doc', content: 'I saved Plan.docx.', provider: 'local-npu', model: LOCAL.model });
  // The text streamed after the card still sits above it.
  assert.equal(card.parentElement.previousElementSibling, row.querySelector('.bubble'));

  card.querySelector('.doc-open').click();
  await settle();
  card.querySelector('.doc-reveal').click();
  await settle();
  assert.deepEqual(calls.filter((c) => /_document$/.test(c[0])), [['open_document', DOC.path], ['reveal_document', DOC.path]]);
  assert.equal(card.querySelector('.doc-reveal').getAttribute('aria-label'), 'Show in folder: Plan.docx');
});

test('opening a document that was moved says so', async () => {
  await fresh();
  await sendAs('make notes', 'req_gone');
  const gone = { name: 'gone.md', path: 'C:\\Users\\you\\Documents\\ChatForge\\gone.md', size: 900, kind: 'text' };
  emit({ type: 'chat.tool_result', request_id: 'req_gone', call_id: 'g1', name: 'create_document', ok: true, summary: 'Saved gone.md', document: gone });
  emit({ type: 'chat.done', request_id: 'req_gone', content: 'Saved.', provider: 'local-npu', model: LOCAL.model });
  const card = lastAssistant().querySelector('.doc-card');
  assert.equal(card.querySelector('.doc-meta').textContent, 'MD · 900 B');
  card.querySelector('.doc-open').click();
  await settle();
  assert.equal($('toast').hidden, false);
  assert.equal($('toast').textContent, 'That document no longer exists.');
  assert.equal(card.querySelector('.doc-open').disabled, false);
});

test('Download on a document card: Save As, then "Saved to …"; cancel says nothing; errors are shown', async () => {
  await fresh();
  await sendAs('save the plan as Excel', 'req_dl');
  const sheet = { name: 'Plan.xlsx', path: 'C:\\Users\\you\\Documents\\ChatForge\\Plan.xlsx', size: 6200, kind: 'xlsx' };
  emit({ type: 'chat.tool_result', request_id: 'req_dl', call_id: 'x1', name: 'create_document', ok: true, summary: 'Saved Plan.xlsx', document: sheet });
  emit({ type: 'chat.done', request_id: 'req_dl', content: 'Saved.', provider: 'local-npu', model: LOCAL.model });
  const card = lastAssistant().querySelector('.doc-card');
  // Open, Download, Show in folder: real buttons, so Tab, Enter and Space reach them.
  const buttons = [...card.querySelectorAll('.doc-actions button')];
  assert.deepEqual(buttons.map((b) => b.textContent), ['Open', 'Download', 'Show in folder']);
  const save = card.querySelector('.doc-save');
  assert.equal(save.getAttribute('type'), 'button');
  assert.equal(save.getAttribute('aria-label'), 'Download Plan.xlsx');
  assert.ok(save.querySelector('svg'), 'the download icon');
  assert.equal(save.tabIndex, 0);
  $('toast').hidden = true;

  save.focus();   // a keyboard user: the focus comes back to Download after the dialog
  save.click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'save_document'), [['save_document', sheet.path]]);
  assert.equal(save.disabled, true, 'disabled while the dialog is open');
  // A browser drops the focus of a button that gets disabled (to the body); jsdom does not.
  $('input').focus();
  $('input').blur();
  assert.equal(win.document.activeElement, win.document.body);
  save.click();   // a second click while the dialog is up does nothing
  await settle();
  assert.equal(saves.length, 1);
  saves.shift().resolve({ ok: true, path: 'C:\\Users\\you\\Downloads\\Plan.xlsx' });
  await settle();
  assert.equal(save.disabled, false);
  assert.equal(win.document.activeElement, save, 'the focus is back on Download');
  assert.equal($('toast').hidden, false);
  assert.equal($('toast').textContent, 'Saved to Downloads\\Plan.xlsx');

  $('toast').hidden = true;
  save.click();
  await settle();
  saves.shift().resolve({ ok: true, cancelled: true });
  await settle();
  assert.equal($('toast').hidden, true, 'a cancelled Save As says nothing');
  assert.equal(save.disabled, false);

  save.click();
  await settle();
  saves.shift().resolve({ ok: false, error: { code: 'in_use', message: 'Plan.xlsx could not be saved there.', hint: '', action: null } });
  await settle();
  assert.equal($('toast').textContent, 'Plan.xlsx could not be saved there.');

  save.click();
  await settle();
  saves.shift().resolve({ ok: false });   // an error without a message
  await settle();
  assert.equal($('toast').textContent, 'The document could not be saved.');
  assert.equal(saves.length, 0);
});

// ---------------------------------------------------------------- context window ----

/** Send `text` as request `id`, the engine storing it at `ts`; `finish` answers it. */
async function turnAt(text, id, ts, finish = true) {
  await sendAs(text, id);
  emit({ type: 'chat.start', request_id: id, provider: 'local-npu', model: LOCAL.model, user_ts: ts });
  if (finish) emit({ type: 'chat.done', request_id: id, content: `Answer to ${text}.`, provider: 'local-npu', model: LOCAL.model });
}
const userRows = () => [...$('messages').querySelectorAll('.msg.user')];
const dividers = () => [...$('messages').querySelectorAll('.ctx-divider')];
const context = (id, dropped, first, cut = false) =>
  emit({ type: 'chat.context', request_id: id, dropped_messages: dropped, first_kept_ts: first, message_cut: cut });

test('chat.context puts one divider before the first message the model still sees, moves it, and clears it', async () => {
  await fresh();
  await turnAt('one', 'req_c1', 101.25);
  await turnAt('two', 'req_c2', 102.25);
  await turnAt('three', 'req_c3', 103.25, false);
  const [one, two, three] = userRows();
  assert.deepEqual([one, two, three].map((r) => r.dataset.ts), ['101.25', '102.25', '103.25']);

  context('req_c3', 2, 102.25);
  assert.equal(dividers().length, 1);
  assert.equal(two.previousElementSibling, dividers()[0]);
  assert.equal(dividers()[0].textContent, 'Older messages are past the context window (too long for context).');
  assert.equal(three.querySelector('.ctx-cut'), null);

  // A later round left out more and cut this message: the same divider moves down.
  context('req_c3', 4, 103.25, true);
  assert.equal(dividers().length, 1);
  assert.equal(three.previousElementSibling, dividers()[0]);
  assert.match(three.querySelector('.msg-time').textContent, /\(Too long for context\)$/);
  assert.equal(two.querySelector('.ctx-cut'), null);

  // Fits again (a bigger model): no divider, no note.
  context('req_c3', 0, null);
  assert.equal(dividers().length, 0);
  assert.equal(three.querySelector('.ctx-cut'), null);

  // A time this popup has no row for: the divider goes before the message being answered.
  context('req_c3', 4, 999);
  assert.equal(three.previousElementSibling, dividers()[0]);
  // The first row on screen: nothing above it is out of view.
  context('req_c3', 2, 101.25);
  assert.equal(dividers().length, 0);

  // Another request's event changes nothing.
  context('req_other', 2, 102.25);
  assert.equal(dividers().length, 0);
  emit({ type: 'chat.done', request_id: 'req_c3', content: 'Done.', provider: 'local-npu', model: LOCAL.model });
});

test('a failed request puts the divider back where it was; New chat removes it', async () => {
  await fresh();
  await turnAt('one', 'req_f1', 201.5);
  await turnAt('two', 'req_f2', 202.5, false);
  context('req_f2', 2, 202.5);
  emit({ type: 'chat.done', request_id: 'req_f2', content: 'Ok.', provider: 'local-npu', model: LOCAL.model });
  const [, two] = userRows();
  assert.equal(two.previousElementSibling, dividers()[0]);

  await turnAt('three', 'req_f3', 203.5, false);
  const three = userRows()[2];
  context('req_f3', 4, 203.5, true);
  assert.equal(three.previousElementSibling, dividers()[0]);
  assert.ok(three.querySelector('.ctx-cut'));
  emit({ type: 'chat.error', request_id: 'req_f3', code: 'server', message: 'The model server returned an error.', action: 'retry' });
  assert.equal(dividers().length, 1);
  assert.equal(two.previousElementSibling, dividers()[0], 'the rolled-back turn moved the divider for good');
  assert.equal(three.querySelector('.ctx-cut'), null);

  $('btn-new').click();
  await settle();
  assert.equal(dividers().length, 0);
});


// ---------------------------------------------------------------- regenerate ----

/** Send `text` and finish its reply `answer` under request `id`. */
async function answered(text, id, answer) {
  await sendAs(text, id);
  emit({ type: 'chat.start', request_id: id, provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.delta', request_id: id, content: answer });
  emit({ type: 'chat.done', request_id: id, content: answer, model: LOCAL.model, provider: 'local-npu' });
  await settle();
}

const visibleRegen = () => [...$('messages').querySelectorAll('.act-regen')].filter((b) => !b.hidden);
const visibleReplies = () => [...$('messages').querySelectorAll('.msg.assistant')].filter((r) => !r.hidden);

/** Click Regenerate on the latest reply and answer the call with `id`. */
async function regenerateAs(id) {
  visibleRegen()[0].click();
  await settle();
  regens.shift().resolve(ok({ request_id: id }));
  await settle();
}

test('Regenerate is offered on the latest reply only, and replaces it', async () => {
  await fresh();
  await answered('first', 'req_r1', 'One.');
  await answered('second', 'req_r2', 'Two.');
  const offered = visibleRegen();
  assert.equal(offered.length, 1);
  assert.ok(lastAssistant().contains(offered[0]), 'Regenerate is not on the latest reply');
  assert.equal(offered[0].textContent, 'Regenerate');

  await regenerateAs('req_r3');
  assert.deepEqual(calls.filter((c) => c[0] === 'regenerate'), [['regenerate']]);
  assert.equal(S().busy, true);
  assert.equal(visibleRegen().length, 0, 'Regenerate is offered while a reply runs');
  const hidden = [...$('messages').querySelectorAll('.msg.assistant')].filter((r) => r.hidden);
  assert.equal(hidden.length, 1, 'the old reply should be hidden while the new one streams');
  assert.equal(hidden[0].querySelector('.bubble').textContent.trim(), 'Two.');
  assert.ok(visibleReplies().at(-1).classList.contains('streaming'));

  emit({ type: 'chat.start', request_id: 'req_r3', provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.delta', request_id: 'req_r3', content: 'Two, again.' });
  emit({ type: 'chat.done', request_id: 'req_r3', content: 'Two, again.', model: LOCAL.model, provider: 'local-npu' });
  await settle();
  const replies = [...$('messages').querySelectorAll('.msg.assistant')];
  assert.deepEqual(replies.map((r) => r.querySelector('.bubble').textContent.trim()), ['One.', 'Two, again.']);
  assert.equal([...$('messages').querySelectorAll('.msg.user')].length, 2, 'Regenerate must not repeat the message');
  assert.ok(composerIdle());
  assert.equal(visibleRegen().length, 1);
});

test('a failed Regenerate brings the old reply back, and Retry regenerates again', async () => {
  await fresh();
  await answered('question', 'req_f1', 'Keep me.');
  await regenerateAs('req_f2');
  emit({ type: 'chat.start', request_id: 'req_f2', provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.delta', request_id: 'req_f2', content: 'half' });
  emit({ type: 'chat.error', request_id: 'req_f2', code: 'server', message: 'The model server failed.', hint: '',
    action: 'retry', restored: true });
  await settle();
  assert.deepEqual(visibleReplies().map((r) => r.querySelector('.bubble').textContent.trim()), ['Keep me.']);
  const error = $('messages').querySelector('.msg.error');
  assert.ok(error, 'no error row');
  assert.equal(visibleRegen().length, 0, 'Regenerate under an error row');
  [...error.querySelectorAll('button')].find((b) => b.textContent === 'Retry').click();
  await settle();
  assert.equal(calls.filter((c) => c[0] === 'regenerate').length, 2);
  assert.equal(sends.length, 0, 'Retry after a Regenerate must not send a new message');
});

test('a Regenerate stopped before any text keeps the old reply', async () => {
  await fresh();
  await answered('question', 'req_s1', 'Old answer.');
  await regenerateAs('req_s2');
  emit({ type: 'chat.start', request_id: 'req_s2', provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.tool_call', request_id: 'req_s2', call_id: 'c9', name: 'web_search', arguments: '{}' });
  emit({ type: 'chat.error', request_id: 'req_s2', code: 'cancelled', message: 'Stopped.', partial: false, restored: true });
  await settle();
  const replies = [...$('messages').querySelectorAll('.msg.assistant')];
  assert.equal(replies.length, 1);
  assert.equal(replies[0].hidden, false);
  assert.equal(replies[0].querySelector('.bubble').textContent.trim(), 'Old answer.');
  assert.equal(visibleRegen().length, 1);
});

test('an empty reply says so, and offers Regenerate', async () => {
  await fresh();
  await sendAs('status', 'req_e1');
  emit({ type: 'chat.start', request_id: 'req_e1', provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.delta', request_id: 'req_e1', content: '\n\n' });
  emit({ type: 'chat.done', request_id: 'req_e1', content: '', model: LOCAL.model, provider: 'local-npu' });
  await settle();
  const row = lastAssistant();
  assert.equal(row.querySelector('.bubble').textContent, '(The model returned an empty reply.)');
  assert.equal(row.querySelectorAll('.msg-act-btn').length, 1, 'nothing to copy, but Regenerate');
  assert.equal(visibleRegen().length, 1);
});

// ------------------------------------------------------------ recently used ----

test('the model menu starts with the recently used models that can still be chosen', async () => {
  await fresh();
  PROVIDERS[2].models.push('qwen-14b');
  try {
    emit({ type: 'settings.changed', config: { chat: { ...LOCAL, recent_models: [
      { provider: 'studioforge', model: 'qwen-27b' },
      { provider: 'minimax', model: 'MiniMax-M3' },          // no key: not offered
      { provider: 'local-npu', model: LOCAL.model },          // in use: marked in its own group instead
      { provider: 'studioforge', model: 'qwen-14b' },
      { provider: 'local-npu', model: 'OpenVINO/Deleted-int4-ov' },   // not installed any more
    ] } } });
    await settle();
    $('model-chip').click();
    await settle();
    const menu = $('model-menu');
    const first = menu.querySelector('.mm-group');
    assert.ok(first.classList.contains('mm-recent'), 'the first group is not Recently used');
    assert.equal(first.querySelector('.mm-head').textContent, 'Recently used');
    const items = [...first.querySelectorAll('.mm-item')];
    assert.deepEqual(items.map((b) => [b.querySelector('.mm-name').textContent, b.querySelector('.mm-sub').textContent]),
      [['qwen-27b', 'StudioForge'], ['qwen-14b', 'StudioForge']]);
    const checked = menu.querySelector('.mm-item[aria-checked="true"]');
    assert.equal(checked.dataset.model, LOCAL.model);
    assert.equal(win.document.activeElement, checked, 'the selected model gets the focus');
    // The provider groups follow, unchanged.
    const heads = [...menu.querySelectorAll('.mm-group:not(.mm-recent) .mm-head')].map((h) => h.textContent);
    assert.deepEqual(heads, ['Local (NPU)', 'StudioForge']);
    items[0].click();
    await settle();
    assert.deepEqual(S().selected, { provider: 'studioforge', model: 'qwen-27b' });
    assert.equal(menu.hidden, true);

    // One other model is not a section: it is in its provider's group anyway.
    emit({ type: 'settings.changed', config: { chat: { ...LOCAL, recent_models: [
      { provider: 'studioforge', model: 'qwen-27b' }, { provider: 'local-npu', model: LOCAL.model },
    ] } } });
    await settle();
    $('model-chip').click();
    await settle();
    assert.equal(menu.querySelector('.mm-recent'), null, 'a Recently used list of one is noise');
    $('model-chip').click();
  } finally {
    PROVIDERS[2].models.pop();
    await settle();
  }
});

test('no Recently used section before any model was used', async () => {
  await fresh();
  emit({ type: 'settings.changed', config: { chat: { ...LOCAL, recent_models: [] } } });
  await settle();
  $('model-chip').click();
  await settle();
  assert.equal($('model-menu').querySelector('.mm-recent'), null);
  $('model-chip').click();
  await settle();
});

// ------------------------------------------------------------- runtime status ----

test('the status line says when a model compiles in the background or runs off the NPU', async () => {
  await fresh();
  emit({ type: 'runtime.status', state: 'compiling', model_id: LOCAL.model, device: 'NPU', elapsed_s: 30,
    expected_s: 90, first_compile: true, background: true, device_fallback: null });
  await settle();
  assert.match($('status-text').textContent, /^Preparing Qwen2\.5-1\.5B-Instruct-int4-ov for NPU in the background/);
  emit({ type: 'runtime.status', state: 'ready', model_id: LOCAL.model, device: 'GPU', idle_timeout_s: 600,
    unload_at: Date.now() / 1000 + 600, background: false,
    device_fallback: { from: 'NPU', to: 'GPU', reason: 'This model gives wrong output on the NPU.' } });
  await settle();
  // Calm while ready: no row for a countdown of minutes; the chip's title has it, and the device.
  assert.equal($('status-line').hidden, true);
  assert.match($('model-chip').title, /Unloads in 10 min\. Runs on GPU instead of NPU: This model gives wrong output/);
  emit({ type: 'runtime.status', state: 'ready', model_id: LOCAL.model, device: 'GPU', idle_timeout_s: 600,
    unload_at: Date.now() / 1000 + 30, background: false,
    device_fallback: { from: 'NPU', to: 'GPU', reason: 'This model gives wrong output on the NPU.' } });
  await settle();
  assert.equal($('status-line').hidden, false, 'the last minute before the unload is shown');
  assert.match($('status-text').textContent, /^Unloads in (?:29|30) s · on GPU$/);
  assert.match($('status-line').title, /Runs on GPU instead of NPU: This model gives wrong output/);
  emit({ type: 'runtime.status', state: 'ready', model_id: LOCAL.model, device: 'NPU', idle_timeout_s: 0,
    unload_at: null, background: false, device_fallback: null });
  await settle();
  assert.equal($('status-line').hidden, true);
});
// ------------------------------------------------------------- quick actions ----

const press = (target, k, extra = {}) => target.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...extra }));
const qaChips = () => [...$('messages').querySelectorAll('.empty-state .qa-chip')];
const pill = () => $('action-bar').querySelector('.action-pill');

test('snapshot: a message sent with a quick action shows the action over its bubble', () => {
  const [plain, withAction] = bootRows.querySelectorAll('.msg.user');
  assert.equal(plain.querySelector('.msg-action'), null);
  const tag = withAction.querySelector('.msg-col').firstElementChild;
  assert.equal(tag.className, 'msg-action');
  assert.equal(tag.textContent, 'Improve this');
  assert.equal(withAction.querySelector('.bubble').textContent, 'look it up');
});

test('an empty chat shows the quick actions, Proof / Improve / Check me / News insight first, after every Clear chat', async () => {
  await fresh();
  assert.deepEqual(qaChips().map((b) => b.textContent), ['Proof this', 'Improve this', 'Check me on this', 'News insight']);
  assert.equal(qaChips()[0].title, 'Paste the text to proofread…');
  assert.match($('messages').querySelector('.empty-state').textContent, /Pick a quick action, then paste your text/);
  await sendAs('hello', 'req_qa0');
  assert.equal(qaChips().length, 0, 'the chips go once there is a message');
  emit({ type: 'chat.done', request_id: 'req_qa0', content: 'Hi.', model: LOCAL.model, provider: 'local-npu' });
  $('btn-new').click();
  await settle();
  assert.equal(qaChips().length, 4, 'Clear chat brings them back');
  // Settings changed the list: the chips follow.
  emit({ type: 'settings.changed', config: { quick_actions: [...QUICK.slice(1), { id: 'custom-haiku', label: 'Haiku', hint: '', tools: false }] } });
  await settle();
  assert.deepEqual(qaChips().map((b) => b.textContent), ['Improve this', 'Check me on this', 'News insight', 'Haiku']);
  emit({ type: 'settings.changed', config: { quick_actions: clone(QUICK) } });
  await settle();
});

test('a chip puts the action in the composer, keeps the typed text, and Enter sends it with the action', async () => {
  await fresh();
  const input = $('input');
  input.value = 'Teh cat sat.';
  input.dispatchEvent(new win.Event('input'));
  qaChips()[0].click();
  assert.equal($('action-bar').hidden, false);
  assert.equal(pill().querySelector('.action-pill-label').textContent, 'Proof this');
  assert.equal(input.placeholder, 'Paste the text to proofread…');
  assert.equal(input.value, 'Teh cat sat.', 'the typed text stays');
  assert.equal(win.document.activeElement, input, 'the box is ready for a paste');

  press(input, 'Enter');
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message'), [['send_message', 'Teh cat sat.', [], 'proof']]);
  assert.equal($('action-bar').hidden, true, 'the pill went with the message');
  assert.equal(input.placeholder, 'Message ChatForge');
  const user = [...$('messages').querySelectorAll('.msg.user')].at(-1);
  assert.equal(user.querySelector('.msg-col').firstElementChild.textContent, 'Proof this');
  assert.equal(user.querySelector('.bubble').textContent, 'Teh cat sat.');

  sends.shift().resolve(ok({ request_id: 'req_qa1' }));
  await settle();
  const fixed = 'The cat sat on the mat, and then it sat on the other mat for a very long time indeed.';
  const reply = `\`\`\`text\n${fixed}\n\`\`\`\n\n- Fixed "Teh"`;
  emit({ type: 'chat.done', request_id: 'req_qa1', content: reply, model: LOCAL.model, provider: 'local-npu' });
  // The corrected text is a prose block: wrapped from the start, Copy copies it exactly.
  const block = lastAssistant().querySelector('.code-block-wrapper');
  assert.ok(block.classList.contains('wrapped'), 'a ```text block should wrap by default');
  assert.equal(block.querySelector('.code-block-wrap').getAttribute('aria-pressed'), 'true');
  assert.equal(block.querySelector('.code-block-copy').dataset.code, fixed);
});

test('Retry after an error sends the message again with its quick action', async () => {
  await fresh();
  win.__chat.send('Plan: get fit', { action: QUICK[2] });
  await settle();
  sends.shift().resolve(ok({ request_id: 'req_qa2' }));
  await settle();
  emit({ type: 'chat.error', request_id: 'req_qa2', code: 'server', message: 'boom', action: 'retry' });
  await settle();
  [...$('messages').querySelectorAll('.err-actions button')].find((b) => b.textContent === 'Retry').click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'send_message').at(-1), ['send_message', 'Plan: get fit', [], 'check']);
  sends.shift().resolve(ok({ request_id: 'req_qa3' }));
  await settle();
  emit({ type: 'chat.done', request_id: 'req_qa3', content: 'ok', model: LOCAL.model, provider: 'local-npu' });
});

test('the quick-actions menu: arrow keys, Escape closes only the menu, picking one fills the composer', async () => {
  await fresh();
  const button = $('quick');
  assert.equal(button.getAttribute('aria-haspopup'), 'menu');
  button.click();
  const menu = $('quick-menu');
  assert.equal(menu.hidden, false);
  assert.equal(button.getAttribute('aria-expanded'), 'true');
  const items = [...menu.querySelectorAll('[role="menuitem"]')];
  assert.deepEqual(items.map((b) => (b.querySelector('.qm-label') || b).textContent),
    ['Proof this', 'Improve this', 'Check me on this', 'News insight', 'Edit quick actions…']);
  assert.equal(items[3].querySelector('.qm-tag').textContent, 'web', 'an action that looks things up says so');
  assert.equal(win.document.activeElement, items[0]);
  press(menu, 'ArrowDown');
  assert.equal(win.document.activeElement, items[1]);
  press(menu, 'End');
  assert.equal(win.document.activeElement, items[4]);
  press(menu, 'ArrowDown');
  assert.equal(win.document.activeElement, items[0], 'the keys wrap around');
  press(win.document.activeElement, 'Escape');
  assert.equal(menu.hidden, true);
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  assert.equal(win.document.activeElement, button);
  await settle();
  assert.equal(calls.filter((c) => c[0] === 'hide_popup').length, 0, 'Escape in the menu must not hide the popup');

  press(button, 'ArrowDown');   // opens it from the keyboard too
  assert.equal(menu.hidden, false);
  [...menu.querySelectorAll('[role="menuitem"]')][2].click();
  assert.equal(menu.hidden, true);
  assert.equal(pill().querySelector('.action-pill-label').textContent, 'Check me on this');
  assert.equal($('input').placeholder, 'Paste a goal, plan or idea to check…');
  // The current action is marked when the menu opens again, and gets the focus.
  button.click();
  const current = menu.querySelector('.qm-item.is-current');
  assert.equal(current.querySelector('.qm-label').textContent, 'Check me on this');
  assert.equal(win.document.activeElement, current);
  win.document.body.click();   // a click elsewhere closes it
  assert.equal(menu.hidden, true);
});

test('the pill comes out with its × or Backspace in an empty box, and goes when Settings removes the action', async () => {
  await fresh();
  qaChips()[1].click();
  assert.ok(pill());
  pill().querySelector('.action-pill-remove').click();
  assert.equal($('action-bar').hidden, true);
  assert.equal($('input').placeholder, 'Message ChatForge');

  qaChips()[1].click();
  const input = $('input');
  input.value = 'x';
  press(input, 'Backspace');
  assert.ok(pill(), 'Backspace with text in the box edits the text');
  input.value = '';
  press(input, 'Backspace');
  assert.equal(pill(), null);

  qaChips()[0].click();
  emit({ type: 'settings.changed', config: { quick_actions: [{ ...QUICK[0], label: 'Proofread', hint: 'Paste it…' }, ...QUICK.slice(1)] } });
  await settle();
  assert.equal(pill().querySelector('.action-pill-label').textContent, 'Proofread', 'a renamed action follows');
  assert.equal(input.placeholder, 'Paste it…');
  emit({ type: 'settings.changed', config: { quick_actions: QUICK.slice(1) } });
  await settle();
  assert.equal(pill(), null, 'a removed action leaves the composer');
  emit({ type: 'settings.changed', config: { quick_actions: clone(QUICK) } });
  await settle();
});

// ---------------------------------------------------------------- resizing ----

const RESIZE_CALLS = new Set(['start_resize', 'drag_resize', 'end_resize', 'reset_popup_size']);
const resizeCalls = () => calls.filter((c) => RESIZE_CALLS.has(c[0]));
/** A pointer event as WebView2 sends it (jsdom: devicePixelRatio 1). */
function pointer(type, target, init) {
  target.dispatchEvent(new win.PointerEvent(type, {
    bubbles: true, cancelable: true, pointerId: 1, isPrimary: true, button: 0, ...init,
  }));
}

test('the grip with a mouse: the page starts and ends the resize, the app follows the cursor', async () => {
  await fresh();
  const grip = $('resize-grip');
  pointer('pointerdown', grip, { pointerType: 'mouse', clientX: 5, clientY: 6, screenX: 1493, screenY: 414 });
  assert.equal(win.document.documentElement.dataset.resizing, 'top-left');
  pointer('pointermove', win, { pointerType: 'mouse', clientX: 40, clientY: 50, screenX: 1300, screenY: 200 });
  pointer('pointerup', win, { pointerType: 'mouse', clientX: 5, clientY: 6, screenX: 1300, screenY: 200 });
  await settle();
  assert.deepEqual(resizeCalls(), [['start_resize', 'top-left', 5, 6, true], ['end_resize']]);
  assert.equal(win.document.documentElement.dataset.resizing, undefined);
});

test('a finger on an edge sends its moves in order, only the latest while one is on its way', async () => {
  await fresh();
  const edge = win.document.querySelector('[data-resize="left"]');
  pointer('pointerdown', edge, { pointerType: 'touch', clientX: 2, clientY: 300, screenX: 1490, screenY: 700 });
  for (const x of [1480, 1470, 1460]) pointer('pointermove', win, { pointerType: 'touch', screenX: x, screenY: 690 });
  pointer('pointerup', win, { pointerType: 'touch', screenX: 1460, screenY: 690 });
  pointer('pointermove', win, { pointerType: 'touch', screenX: 1400, screenY: 690 });   // after the drag
  await settle();
  assert.deepEqual(resizeCalls(), [
    ['start_resize', 'left', 2, 300, false],
    ['drag_resize', -10, -10],
    ['drag_resize', -30, -10],
    ['end_resize'],
  ]);
});

test('only a primary press starts a resize; a double-click on the grip restores the default size', async () => {
  await fresh();
  const grip = $('resize-grip');
  pointer('pointerdown', grip, { pointerType: 'mouse', button: 2 });
  pointer('pointerdown', win.document.querySelector('[data-resize="top"]'), { pointerType: 'mouse', isPrimary: false, pointerId: 2 });
  await settle();
  assert.deepEqual(resizeCalls(), []);
  grip.dispatchEvent(new win.MouseEvent('dblclick', { bubbles: true, cancelable: true }));
  await settle();
  assert.deepEqual(resizeCalls(), [['reset_popup_size']]);
});

test('a press after a release that never arrived ends the old resize first', async () => {
  await fresh();
  const top = win.document.querySelector('[data-resize="top"]');
  pointer('pointerdown', top, { pointerType: 'mouse', clientX: 200, clientY: 3 });
  pointer('pointerdown', $('resize-grip'), { pointerType: 'mouse', pointerId: 2, clientX: 4, clientY: 4 });
  pointer('pointerup', win, { pointerType: 'mouse', pointerId: 2 });
  await settle();
  assert.deepEqual(resizeCalls(), [
    ['start_resize', 'top', 200, 3, true], ['end_resize'],
    ['start_resize', 'top-left', 4, 4, true], ['end_resize'],
  ]);
});

test('a cancelled pointer or a lost capture ends the drag', async () => {
  await fresh();
  const grip = $('resize-grip');
  pointer('pointerdown', grip, { pointerType: 'pen', clientX: 4, clientY: 4, screenX: 100, screenY: 100 });
  pointer('pointercancel', win, { pointerType: 'pen' });
  pointer('pointerdown', grip, { pointerType: 'mouse', pointerId: 2, clientX: 4, clientY: 4 });
  grip.dispatchEvent(new win.PointerEvent('lostpointercapture', { pointerId: 2 }));
  await settle();
  assert.deepEqual(resizeCalls(), [
    ['start_resize', 'top-left', 4, 4, false], ['end_resize'],
    ['start_resize', 'top-left', 4, 4, true], ['end_resize'],
  ]);
  assert.equal(win.document.documentElement.dataset.resizing, undefined);
});

test('the popup coming back during a drag whose release was missed leaves the resizing state', async () => {
  await fresh();
  pointer('pointerdown', $('resize-grip'), { pointerType: 'mouse', clientX: 4, clientY: 4 });
  assert.equal(win.document.documentElement.dataset.resizing, 'top-left');
  emit({ type: 'popup.shown' });   // e.g. Escape mid-drag, the release landed elsewhere
  await settle();
  assert.equal(win.document.documentElement.dataset.resizing, undefined);
  assert.deepEqual(resizeCalls(), [['start_resize', 'top-left', 4, 4, true], ['end_resize']]);
  pointer('pointerup', win, { pointerType: 'mouse' });   // the late release changes nothing
  await settle();
  assert.equal(resizeCalls().length, 2);
});

// ------------------------------------------------------------------ sticky ----

test('sticky: a blur never hides the popup, Escape does, and the pin turns it off and on', async () => {
  await fresh();
  const pin = $('btn-pin');
  const hides = () => calls.filter((c) => c[0] === 'hide_popup').length;
  const blur = async () => { win.dispatchEvent(new win.Event('blur')); mock.timers.tick(200); await settle(); };
  assert.equal(pin.getAttribute('aria-pressed'), 'false');   // the boot config turned it off
  emit({ type: 'settings.changed', config: { ui: { sticky: true } } });
  assert.equal(pin.getAttribute('aria-pressed'), 'true');
  assert.match(pin.title, /^Sticky/);
  win.document.hasFocus = () => false;
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    await blur();
    assert.equal(hides(), 0, 'a sticky popup hid when it lost the focus');
    win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await settle();
    assert.equal(hides(), 1, 'Escape still puts it away');

    pin.click();
    await settle();
    assert.deepEqual(calls.filter((c) => c[0] === 'set_sticky'), [['set_sticky', false]]);
    assert.equal(pin.getAttribute('aria-pressed'), 'false');
    await blur();
    assert.equal(hides(), 2, 'not sticky: a blur hides it');
    // ...saying so, so the app checks sticky and its own dialogs as well.
    assert.deepEqual(calls.filter((c) => c[0] === 'hide_popup').at(-1), ['hide_popup', 'blur']);

    pin.click();
    await settle();
    assert.deepEqual(calls.filter((c) => c[0] === 'set_sticky').at(-1), ['set_sticky', true]);
    assert.equal(pin.getAttribute('aria-pressed'), 'true');
  } finally {
    mock.timers.reset();
    delete win.document.hasFocus;
    emit({ type: 'settings.changed', config: { ui: { sticky: false } } });
  }
});

test('Clear chat sits in the composer footer as a labelled ghost button, not in the header', () => {
  const b = $('btn-new');
  assert.ok(b.classList.contains('btn-clear'), 'keeps the caution colour hook');
  assert.ok(b.classList.contains('ui-btn') && b.classList.contains('ui-btn--ghost') && b.classList.contains('ui-btn--sm'));
  assert.equal(b.closest('.composer-foot'), document.querySelector('.composer-foot'));
  assert.equal(b.closest('.hdr'), null, 'not next to Close');
  assert.equal(b.textContent.trim(), 'Clear chat');
  assert.match(b.getAttribute('aria-label'), /^Clear chat/);
  assert.ok(b.title);
  for (const id of ['btn-pin', 'btn-settings', 'btn-close']) {
    assert.ok($(id).closest('.hdr-actions'), `${id} stays in the header`);
    assert.ok($(id).classList.contains('ui-btn--icon'));
  }
  assert.ok(document.querySelector('.hdr .model-chip'));
});


// ------------------------------------------------- popup: shortcuts, undo, edit, copy ----

const api = win.pywebview.api;
const fail = (code, message) => ({ ok: false, error: { code, message, hint: '', action: null } });
let dropReply = fail('empty', 'There is no message to edit.');
let undoReply = fail('nothing_to_undo', 'There is nothing to bring back.');
api.open_settings = async () => { calls.push(['open_settings']); return ok(); };
api.open_logs_folder = async () => { calls.push(['open_logs_folder']); return ok({ path: 'C:\\logs' }); };
api.open_external = async (url) => {
  calls.push(['open_external', url]);
  return /^(?:https?:\/\/|mailto:)/i.test(url) ? ok() : fail('bad_request', 'Only http(s) and mailto: links can be opened.');
};
api.drop_last_turn = async () => { calls.push(['drop_last_turn']); return dropReply; };
api.undo_clear = async () => { calls.push(['undo_clear']); return undoReply; };

const clipboardWrites = [];   // ['text', text] or ['rich', {html, text}]
const blobText = (blob) => new Promise((resolve) => {
  const fr = new win.FileReader();
  fr.onload = () => resolve(String(fr.result));
  fr.readAsText(blob);
});
/** navigator.clipboard as WebView2 has it; `rich: false` is a browser without ClipboardItem. */
function stubClipboard({ rich = true } = {}) {
  clipboardWrites.length = 0;
  const clip = {
    writeText: async (text) => { clipboardWrites.push(['text', text]); },
    ...(rich ? { write: async (items) => {
      const it = items[0].items;
      clipboardWrites.push(['rich', { html: await blobText(it['text/html']), text: await blobText(it['text/plain']) }]);
    } } : {}),
  };
  Object.defineProperty(win.navigator, 'clipboard', { value: clip, configurable: true });
  if (rich) win.ClipboardItem = class { constructor(items) { this.items = items; } };
  else delete win.ClipboardItem;
}
const toastParts = () => ({ text: $('toast').hidden ? '' : $('toast').childNodes[0]?.textContent ?? '', action: $('toast').hidden ? null : $('toast').querySelector('.toast-act') });

test('the attachment list has room for three rows and scrolls the newest chip into view', async () => {
  const css = readFileSync(join(STATIC, 'css', 'app.css'), 'utf8');
  const px = Number(/\.attach-list \{[^}]*max-height: (\d+)px/.exec(css)[1]);
  assert.ok(px >= 3 * 28 + 2 * 6, `max-height ${px}px clips the third row of 28px chips`);
  await fresh();
  const list = $('attach-list');
  Object.defineProperty(list, 'scrollHeight', { configurable: true, get: () => 250 });
  try {
    await attachViaDialog([view('a1', 'one.txt'), view('a2', 'two.txt'), view('a3', 'three.txt')]);
    assert.deepEqual(chipNames(list), ['one.txt', 'two.txt', 'three.txt']);
    assert.equal(list.scrollTop, 250, 'the list is not scrolled to the end');
  } finally {
    delete list.scrollHeight;
  }
});

test('shortcuts: Ctrl+N, Ctrl+L, /, Ctrl+, Ctrl+M and Ctrl+Shift+C, named in the titles', async () => {
  await fresh();
  stubClipboard();
  win.__chat.renderConversation(clone(SNAPSHOT));
  const body = win.document.body;
  const down = (target, k, extra = {}) => {
    const e = new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...extra });
    target.dispatchEvent(e);
    return e;
  };
  assert.match($('btn-settings').title, /\(Ctrl\+,\)/);
  assert.match($('btn-new').title, /\(Ctrl\+N\)/);
  assert.match($('model-chip').title, /\(Ctrl\+M\)/);
  assert.match($('stop').title, /Esc/);
  assert.match(lastAssistant().querySelector('.act-copy-reply').title, /Ctrl\+Shift\+C/);

  $('input').blur();
  assert.ok(down(body, 'l', { ctrlKey: true }).defaultPrevented);
  assert.equal(win.document.activeElement, $('input'));
  $('input').blur();
  assert.ok(down(body, '/').defaultPrevented);
  assert.equal(win.document.activeElement, $('input'));
  assert.equal(down($('input'), '/').defaultPrevented, false, 'a slash typed in the box is a slash');

  down(body, ',', { ctrlKey: true });
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'open_settings'), [['open_settings']]);

  down(body, 'm', { ctrlKey: true });
  assert.equal(S().menuOpen, true);
  assert.equal($('model-menu').hidden, false);
  down(win.document.activeElement, 'Escape');
  assert.equal(S().menuOpen, false);

  down(body, 'C', { ctrlKey: true, shiftKey: true });
  await settle();
  assert.equal(clipboardWrites.length, 1);
  assert.equal(clipboardWrites[0][0], 'rich');
  assert.equal(clipboardWrites[0][1].text, 'Partial answer');
  assert.match(clipboardWrites[0][1].html, /Partial answer/);
  assert.equal(toastParts().text, 'Reply copied');

  down(body, 'n', { ctrlKey: true });
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'new_chat'), [['new_chat']]);
  assert.equal($('messages').querySelector('.msg'), null);
});

test('Esc stops a running reply first and hides on the second press; Enter says it is still replying', async () => {
  await fresh();
  await sendAs('tell me a story', 'req_e1');
  const hides = () => calls.filter((c) => c[0] === 'hide_popup').length;
  $('input').value = 'and then?';
  press($('input'), 'Enter');
  assert.equal(toastParts().text, 'Still replying. Press Esc to stop.');
  assert.equal(sends.length, 0, 'Enter sent a second message while a reply ran');
  assert.equal($('input').value, 'and then?', 'the draft stays');

  win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'stop_generation'), [['stop_generation', 'req_e1']]);
  assert.equal(hides(), 0, 'the first Esc hid the popup instead of stopping');
  win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  await settle();
  assert.equal(hides(), 1, 'the second Esc hides');
  emit({ type: 'chat.error', request_id: 'req_e1', code: 'cancelled', message: 'Stopped.' });
  await settle();
  assert.ok(composerIdle());
  $('input').value = '';
});

test('Clear chat: a toast with Undo for six seconds that brings the chat back; an empty chat clears silently', async () => {
  await fresh();
  const clear = async () => { $('btn-new').click(); await settle(); };
  await clear();
  assert.equal(toastParts().action, null, 'clearing an empty chat is not worth a toast');

  win.__chat.renderConversation(clone(SNAPSHOT));
  undoReply = ok({ conversation: clone(SNAPSHOT) });
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    await clear();
    assert.equal($('messages').querySelector('.msg'), null);
    assert.equal(toastParts().text, 'Chat cleared');
    assert.equal(toastParts().action.textContent, 'Undo');
    mock.timers.tick(5900);
    assert.equal($('toast').hidden, false, 'the toast went before six seconds');
    mock.timers.tick(200);
    assert.equal($('toast').hidden, true, 'the toast stays longer than six seconds');
  } finally {
    mock.timers.reset();
  }

  win.__chat.renderConversation(clone(SNAPSHOT));
  await clear();
  toastParts().action.click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'undo_clear'), [['undo_clear']]);
  assert.equal(userRows().length, 2, 'the conversation did not come back');
  assert.equal($('toast').hidden, true);
  assert.equal(win.document.activeElement, $('input'));

  // Sending ends the chance: the toast goes with it.
  await clear();
  assert.ok(toastParts().action);
  await sendAs('new start', 'req_u1');
  assert.equal($('toast').hidden, true);
  emit({ type: 'chat.error', request_id: 'req_u1', code: 'cancelled', message: 'Stopped.' });
  await settle();

  // A refusal is said, not hidden.
  win.__chat.renderConversation(clone(SNAPSHOT));
  undoReply = fail('nothing_to_undo', 'There is nothing to bring back.');
  await clear();
  toastParts().action.click();
  await settle();
  assert.equal(toastParts().text, 'There is nothing to bring back.');

  const css = readFileSync(join(STATIC, 'css', 'app.css'), 'utf8');
  assert.match(css, /\.btn-clear, \.btn-clear:hover:not\(:disabled\) \{ color: var\(--text-secondary\); \}/, 'neutral at rest');
  assert.match(css, /\.btn-clear:hover:not\(:disabled\), \.btn-clear:focus-visible:not\(:disabled\) \{ color: var\(--warning-text\); \}/);
});

test('Edit and resend: Up in an empty box and Edit take the last message back into the composer', async () => {
  await fresh();
  stubClipboard();
  const convo = () => [
    { role: 'user', content: 'first', ts: 1 },
    { role: 'assistant', content: 'One.', ts: 2, model: LOCAL.model },
    { role: 'user', content: 'Fix teh text', ts: 3, action: { id: 'proof', label: 'Proof this' },
      attachments: [{ name: 'notes.md', kind: 'text', chars: 10, truncated: false }] },
    { role: 'assistant', content: 'Done.', ts: 4, model: LOCAL.model },
  ];
  const edits = () => [...$('messages').querySelectorAll('.act-edit')].filter((b) => !b.hidden);
  win.__chat.renderConversation(convo());
  assert.equal(edits().length, 1, 'Edit is on the last message only');
  assert.equal(edits()[0].closest('.msg.user').querySelector('.bubble').textContent, 'Fix teh text');
  const copies = userRows().map((r) => r.querySelector('.msg-act-btn:not(.act-edit)'));
  assert.equal(copies.length, 2, 'Copy is on every message of yours');
  copies[1].click();
  await settle();
  assert.deepEqual(clipboardWrites, [['text', 'Fix teh text']]);

  // Up with something typed is just a cursor key.
  $('input').value = 'x';
  press($('input'), 'ArrowUp');
  await settle();
  assert.equal(calls.filter((c) => c[0] === 'drop_last_turn').length, 0);
  // Edit never overwrites a draft.
  edits()[0].click();
  await settle();
  assert.equal(calls.filter((c) => c[0] === 'drop_last_turn').length, 0);
  assert.equal($('input').value, 'x');
  $('input').value = '';

  // The attachments are gone from the bridge: the popup says so.
  dropReply = ok({ removed: { content: 'Fix teh text', attachments: [{ name: 'notes.md', kind: 'text', chars: 10, truncated: false }], action: { id: 'proof', label: 'Proof this' } },
    conversation: convo().slice(0, 2) });
  const e = new win.KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true, cancelable: true });
  $('input').dispatchEvent(e);
  await settle();
  assert.ok(e.defaultPrevented);
  assert.deepEqual(calls.filter((c) => c[0] === 'drop_last_turn'), [['drop_last_turn']]);
  assert.equal($('input').value, 'Fix teh text');
  assert.equal(pill().querySelector('.action-pill-label').textContent, 'Proof this');
  assert.equal(toastParts().text, 'Attachments were removed, add them again');
  assert.equal(userRows().length, 1, 'the conversation is the one the bridge returned');
  assert.equal(win.document.activeElement, $('input'));
  pill().querySelector('.action-pill-remove').click();
  $('input').value = '';

  // Attachments the bridge still holds (they carry ids) come back as chips.
  win.__chat.renderConversation(convo());
  dropReply = ok({ removed: { content: 'Fix teh text', action: null,
    attachments: [{ id: 'keep1', name: 'notes.md', kind: 'text', chars: 10, size: 100, truncated: false }] },
  conversation: convo().slice(0, 2) });
  edits()[0].click();
  await settle();
  assert.equal($('input').value, 'Fix teh text');
  assert.deepEqual(chipNames($('attach-list')), ['notes.md']);
  assert.equal(pill(), null);
  $('attach-list').querySelector('.att-remove').click();
  $('input').value = '';

  // The bridge refuses while a reply streams or when there is nothing: said in a toast.
  win.__chat.renderConversation(convo());
  dropReply = fail('busy', 'Wait for the reply to finish, or press Stop.');
  press($('input'), 'ArrowUp');
  await settle();
  assert.equal(toastParts().text, 'Wait for the reply to finish, or press Stop.');
  assert.equal(userRows().length, 2);
  // While a reply runs the popup does not even ask.
  await sendAs('another', 'req_ed1');
  assert.equal(edits().length, 0, 'Edit is offered while a reply runs');
  const before = calls.filter((c) => c[0] === 'drop_last_turn').length;
  press($('input'), 'ArrowUp');
  await settle();
  assert.equal(calls.filter((c) => c[0] === 'drop_last_turn').length, before);
  emit({ type: 'chat.error', request_id: 'req_ed1', code: 'cancelled', message: 'Stopped.' });
  await settle();
});

test('Copy puts the rendered reply (HTML) and its Markdown on the clipboard; Copy as Markdown only the Markdown', async () => {
  await fresh();
  stubClipboard();
  const md = 'Hello **world**\n\n```js\nlet a = 1;\n```\n\nSee [docs](https://example.com/x).';
  await answered('q', 'req_c1', md);
  const row = lastAssistant();
  assert.ok(row.querySelector('.code-block-copy'), 'the fixture has a code block with its Copy button');
  row.querySelector('.act-copy-reply').click();
  await settle();
  assert.equal(clipboardWrites.length, 1);
  const [kind, got] = clipboardWrites[0];
  assert.equal(kind, 'rich');
  assert.equal(got.text, md);
  assert.match(got.html, /<strong>world<\/strong>/);
  assert.match(got.html, /let a/);
  assert.doesNotMatch(got.html, /code-block-copy|<button|tool-chip/, 'the chrome went onto the clipboard');
  assert.equal(win.document.activeElement, $('input'), 'the focus fell to the body after Copy');
  assert.equal(row.querySelector('.act-copy-reply').classList.contains('ok'), true);

  row.querySelector('.act-copy-md').click();
  await settle();
  assert.deepEqual(clipboardWrites[1], ['text', md]);

  stubClipboard({ rich: false });   // no ClipboardItem: plain text
  row.querySelector('.act-copy-reply').click();
  await settle();
  assert.deepEqual(clipboardWrites, [['text', md]]);

  // Links say where they go, and open in the default app with a word about it.
  const link = row.querySelector('a[href]');
  assert.equal(link.title, 'https://example.com/x');
  link.click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'open_external'), [['open_external', 'https://example.com/x']]);
  assert.equal(toastParts().text, 'Opened in your browser');
  const mail = win.document.createElement('a');
  mail.href = 'mailto:me@example.com';
  row.querySelector('.bubble').append(mail);
  mail.click();
  await settle();
  assert.equal(calls.filter((c) => c[0] === 'open_external').at(-1)[1], 'mailto:me@example.com');
  const odd = win.document.createElement('a');
  odd.href = 'ftp://example.com/file';
  row.querySelector('.bubble').append(odd);
  odd.click();
  await settle();
  assert.equal(calls.filter((c) => c[0] === 'open_external').length, 2, 'an unsupported link reached the bridge');
  assert.equal(toastParts().text, 'That link type is not supported');
});

test('after Regenerate and Retry the focus is on Stop, never on the page body', async () => {
  await fresh();
  await answered('q', 'req_f10', 'An answer.');
  visibleRegen()[0].focus();
  await regenerateAs('req_f11');
  assert.equal(win.document.activeElement, $('stop'));
  emit({ type: 'chat.start', request_id: 'req_f11', provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.error', request_id: 'req_f11', code: 'server', message: 'Boom.', action: 'retry' });
  await settle();
  const retryBtn = [...$('messages').querySelectorAll('.msg.error .ui-btn')].find((b) => b.textContent === 'Retry');
  retryBtn.focus();
  retryBtn.click();
  await settle();
  assert.equal(S().busy, true);
  assert.equal(win.document.activeElement, $('stop'));
  emit({ type: 'chat.error', request_id: 'req_f11', code: 'cancelled', message: 'Stopped.' });
  await settle();
});

test('an error row offers Open logs and Copy details next to Retry', async () => {
  await fresh();
  stubClipboard();
  await sendAs('hello', 'req_x1');
  emit({ type: 'chat.start', request_id: 'req_x1', provider: 'local-npu', model: LOCAL.model });
  emit({ type: 'chat.error', request_id: 'req_x1', code: 'server', message: 'The model stopped answering.', hint: 'Try again.', action: 'retry' });
  await settle();
  const row = $('messages').querySelector('.msg.error');
  const labels = [...row.querySelectorAll('.err-actions .ui-btn')].map((b) => b.textContent);
  assert.deepEqual(labels, ['Retry', 'Open logs', 'Copy details']);
  assert.doesNotMatch(row.textContent, /See the logs/);
  [...row.querySelectorAll('.ui-btn')].find((b) => b.textContent === 'Open logs').click();
  await settle();
  assert.deepEqual(calls.filter((c) => c[0] === 'open_logs_folder'), [['open_logs_folder']]);
  [...row.querySelectorAll('.ui-btn')].find((b) => b.textContent === 'Copy details').click();
  await settle();
  assert.equal(clipboardWrites[0][0], 'text');
  assert.match(clipboardWrites[0][1], /The model stopped answering\.\nTry again\.\nCode: server\nModel: Local \(NPU\) \/ OpenVINO\/Qwen2\.5-1\.5B-Instruct-int4-ov\nTime: /);
  assert.equal(toastParts().text, 'Details copied');
  $('messages').querySelector('.msg.error').remove();
});

test('the model menu is a menu of radio items with aria-checked, and the chip shows the model alone', async () => {
  await fresh();
  const chip = $('model-chip');
  assert.equal(chip.getAttribute('aria-haspopup'), 'menu');
  assert.equal($('model-menu').getAttribute('role'), 'menu');
  assert.equal($('chip-label').textContent, 'Qwen2.5-1.5B', 'the chip shows the model without its build suffix');
  assert.match(chip.title, /^Local \(NPU\) · Qwen2\.5-1\.5B-Instruct-int4-ov \(/, 'the title keeps the provider and the full id');
  assert.equal(S().selected.model, LOCAL.model, 'the data was shortened');

  chip.click();
  await settle();
  const menu = $('model-menu');
  const radios = [...menu.querySelectorAll('[role="menuitemradio"]')];
  assert.equal(radios.length, 2);
  assert.equal(menu.querySelectorAll('[role="option"]').length, 0);
  assert.deepEqual(radios.map((b) => b.getAttribute('aria-checked')), ['true', 'false']);
  assert.equal(radios[0].querySelector('.mm-name').textContent, 'Qwen2.5-1.5B-Instruct-int4-ov', 'the menu keeps full names');
  assert.equal(menu.textContent.includes('✓'), false, 'a hidden check mark is still text for a screen reader');
  assert.equal(win.document.activeElement, radios[0]);
  press(menu, 'ArrowDown');
  assert.equal(win.document.activeElement, radios[1]);
  press(win.document.activeElement, 'Escape');
  assert.equal(menu.hidden, true);

  chip.click();
  await settle();
  menu.querySelector('[data-model="qwen-27b"]').click();
  await settle();
  assert.equal($('chip-label').textContent, 'qwen-27b');
  assert.match(chip.title, /^StudioForge · qwen-27b/);
  await fresh();
});

test('a long model list gets a filter box', async () => {
  await fresh();
  const extra = Array.from({ length: 9 }, (_, i) => `extra-model-${i}`);
  PROVIDERS[2].models.push(...extra);
  try {
    emit({ type: 'settings.changed', config: { chat: { ...LOCAL } } });
    await settle();
    $('model-chip').click();
    await settle();
    const box = $('model-menu').querySelector('.mm-filter');
    assert.ok(box, 'ten models and no filter');
    box.value = 'extra-model-3';
    box.dispatchEvent(new win.Event('input', { bubbles: true }));
    const shown = [...$('model-menu').querySelectorAll('.mm-item[data-model]')].filter((b) => !b.hidden);
    assert.deepEqual(shown.map((b) => b.dataset.model), ['extra-model-3']);
    $('model-chip').click();
  } finally {
    PROVIDERS[2].models.length -= extra.length;
    emit({ type: 'settings.changed', config: { chat: { ...LOCAL } } });
    await settle();
  }
});

test('popup.shown closes a model or quick-actions menu left open, before it focuses the box', async () => {
  await fresh();
  $('model-chip').click();
  await settle();
  assert.equal(S().menuOpen, true);
  emit({ type: 'popup.shown' });
  await settle();
  assert.equal($('model-menu').hidden, true);
  assert.equal($('model-chip').getAttribute('aria-expanded'), 'false');
  assert.equal(win.document.activeElement, $('input'));

  $('quick').click();
  assert.equal($('quick-menu').hidden, false);
  emit({ type: 'popup.shown' });
  await settle();
  assert.equal($('quick-menu').hidden, true);
  assert.equal($('quick').getAttribute('aria-expanded'), 'false');
  assert.equal(win.document.activeElement, $('input'));
});

test('a finished reply is announced with its first sentence and is a Tab stop; the quick menu pins its footer', async () => {
  await fresh();
  await answered('q', 'req_a1', 'The meeting is on Friday. Bring the notes.');
  await new Promise((r) => setTimeout(r, 60));
  assert.equal($('announcer').textContent, 'Reply ready. The meeting is on Friday.');
  const row = lastAssistant();
  assert.equal(row.tabIndex, 0);
  assert.equal(row.getAttribute('aria-label'), 'Latest reply');
  await answered('q2', 'req_a2', 'Second.');
  assert.equal(row.hasAttribute('tabindex'), false, 'only the latest reply is a Tab stop');
  assert.equal(lastAssistant().tabIndex, 0);

  $('quick').click();
  const menu = $('quick-menu');
  const foot = menu.querySelector('.qm-foot .qm-edit');
  assert.ok(foot, 'Edit quick actions… is not in a footer outside the scrolling list');
  assert.equal(menu.querySelector('.qm-scroll .qm-edit'), null);
  assert.ok(menu.querySelector('.qm-body .qm-fade'));
  press(menu, 'Escape');
});

test('a paste that goes over the limit is kept and offers Attach as text; the counter says how far over', async () => {
  await fresh();
  const input = $('input');
  const big = 'word '.repeat(1000);   // 5000 characters, limit 4000
  const paste = new win.Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(paste, 'clipboardData', { value: { getData: (t) => (t === 'text/plain' ? big : ''), files: [], items: [] } });
  input.value = 'intro ';
  input.setSelectionRange(6, 6);
  input.dispatchEvent(paste);
  assert.equal(paste.defaultPrevented, false, 'the paste was dropped');
  assert.equal(toastParts().text, 'That paste is over the 4,000 character limit');
  assert.equal(toastParts().action.textContent, 'Attach as text');
  // The browser inserts it...
  input.value = `intro ${big}`;
  input.dispatchEvent(new win.Event('input'));
  assert.equal($('char-over').hidden, false);
  assert.equal($('char-over').textContent, `${(6 + 5000 - 4000).toLocaleString()} characters over the limit`);
  assert.equal($('send').disabled, true);
  // ...and Attach as text moves it into pasted-text.txt.
  toastParts().action.click();
  await settle();
  assert.equal(input.value, 'intro ');
  assert.equal($('char-over').hidden, true);
  assert.equal(datas.length, 1);
  assert.equal(datas[0].name, 'pasted-text.txt');
  datas.shift().resolve(ok({ attachments: [view('pt1', 'pasted-text.txt', { chars: 5000 })], errors: [] }));
  await settle();
  assert.deepEqual(chipNames($('attach-list')), ['pasted-text.txt']);

  // A paste that fits is left alone.
  const small = new win.Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(small, 'clipboardData', { value: { getData: (t) => (t === 'text/plain' ? 'short' : ''), files: [], items: [] } });
  $('toast').hidden = true;
  input.dispatchEvent(small);
  assert.equal($('toast').hidden, true);
});
