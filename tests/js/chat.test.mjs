// Behavioural tests for the popup (chat.js) against a scripted bridge.
//
// index.html's markup is loaded into jsdom WITHOUT its scripts; marked and DOMPurify are
// evaluated into the window (as in markdown.test.mjs) and chat.js is imported by Node with
// jsdom's globals exposed. window.pywebview.api is defined by this file, so bridge.js takes
// the real path (no dev mock) and every reply is under the test's control: send_message
// resolves only when a test says so, which is how the round-trip races are reproduced.
// Events are pushed through window.__aichat.emit, exactly like Python's run_js.
// Animation frames run only when a test flushes them, so a frame queued before chat.done
// can be run after it, as WebView2 does when both land in one event batch.
import test, { after, mock } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = join(HERE, '..', '..', 'src', 'aichat', 'web');
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
    region: 'international', base_url: 'https://api.minimax.io/v1', builtin: true, docs_url: null, key: key('none', true, 'MINIMAX_API_KEY') },
  { id: 'studioforge', display_name: 'StudioForge', kind: 'openai', models: ['qwen-27b'], default_model: 'qwen-27b',
    region: null, base_url: 'http://localhost:1234/v1', builtin: true, docs_url: null, key: key('none', false, 'STUDIOFORGE_API_KEY') },
];
const LOCAL = { provider: 'local-npu', model: 'OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov' };
const DOC = { name: 'Plan.docx', path: 'C:\\Users\\you\\Documents\\AI Chat\\Plan.docx', size: 14336, kind: 'docx' };
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
  { role: 'user', content: 'look it up', ts: 1700000000, cut: true },
  { role: 'assistant', content: 'Partial answer', ts: 1700000005, model: 'MiniMax-M3', stopped: true,
    tools: [
      { call_id: 'c1', name: 'web_search', arguments: '{"query":"npu"}', ok: null, summary: '' },
      { call_id: 'c2', name: 'calculator', arguments: '{"expression":"1/0"}', ok: false, summary: 'division by zero' },
    ] },
];

const calls = [];   // [method, ...args]
const sends = [];   // pending send_message replies: {text, ids, resolve}
const picks = [];   // pending attach_files replies (the native dialog): {resolve}
const datas = [];   // pending attach_data replies: {name, data, resolve}
const clone = (o) => JSON.parse(JSON.stringify(o));
const ok = (extra = {}) => ({ ok: true, ...extra });
win.pywebview = {
  api: {
    get_state: async () => ok({
      config: { chat: { ...LOCAL, show_reasoning: 'collapsed', max_prompt_chars: 4000 }, ui: { theme: 'laserlloyd', hide_on_blur: true }, local: {} },
      providers: clone(PROVIDERS), selected: { ...LOCAL }, runtime: { state: 'ready', model_id: LOCAL.model },
      conversation: clone(SNAPSHOT), limits: { max_prompt_chars: 4000 }, theme: 'laserlloyd',
    }),
    // Called with one argument when there are no files, exactly as before attachments.
    send_message: (...args) => new Promise((resolve) => {
      calls.push(['send_message', ...args]);
      sends.push({ text: args[0], ids: args[1], resolve });
    }),
    attach_files: () => new Promise((resolve) => { calls.push(['attach_files']); picks.push({ resolve }); }),
    attach_data: (name, data) => new Promise((resolve) => { calls.push(['attach_data', name, data]); datas.push({ name, data, resolve }); }),
    remove_attachment: async (id) => { calls.push(['remove_attachment', id]); return ok({ removed: true }); },
    open_document: async (path) => { calls.push(['open_document', path]); return docReply(path); },
    reveal_document: async (path) => { calls.push(['reveal_document', path]); return docReply(path); },
    stop_generation: async (id) => { calls.push(['stop_generation', id]); return ok(); },
    new_chat: async () => { calls.push(['new_chat']); return ok(); },
    list_providers: async () => ok({ providers: clone(PROVIDERS) }),
    select_model: async (provider, model) => ok({ selected: { provider, model } }),
    hide_popup: async () => { calls.push(['hide_popup']); return ok(); },
    open_settings: async () => ok(),
    open_external: async () => ok(),
    set_pinned: async (flag) => ok({ pinned: !!flag }),
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
const emit = (evt) => win.__aichat.emit(evt);
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
  assert.deepEqual([...card.querySelectorAll('button')].map((b) => b.textContent), ['Open', 'Show in folder']);
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
  assert.ok(empty.querySelector('.suggestions'), 'suggestions should show instead of the key button');

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

test('a saved document shows as a card under the reply, with Open and Show in folder', async () => {
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
  const gone = { name: 'gone.md', path: 'C:\\Users\\you\\Documents\\AI Chat\\gone.md', size: 900, kind: 'text' };
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
