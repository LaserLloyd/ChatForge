// First-run cards (setupcard.js): the "Set up local AI" card repaints in place from
// bridge snapshots, offers Cancel while it runs, shows a failure with Try again, and the
// autostart card asks once. Runs against jsdom; the api is a recording stub.
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

const { createSetupCard, createAutostartCard, progressLine } = await import(pathToFileURL(join(JS, 'setupcard.js')).href);

const IDLE = {
  phase: 'idle', running: false, needed: true, percent: 0, speed_bps: 0, eta_s: null,
  model_label: 'Qwen2.5 1.5B Instruct INT4', model_licence: 'apache-2.0', model_bytes: 935999480, runtime_bytes: 138798816,
  runtime_installed: false, model_installed: false,
  device_note: { device: 'CPU', label: 'No Intel NPU found: runs on the CPU (slower)', slower: true }, failure: null,
};

function stubApi(replies = {}) {
  const calls = [];
  return {
    calls,
    async call(name, ...args) { calls.push([name, ...args]); return replies[name] ?? { ok: true }; },
  };
}
const $ = (card, sel) => card.node.querySelector(sel);
const visible = (n) => !!n && !n.closest('[hidden]');

test('idle: says what is downloaded, how big, which device, and offers the way out', () => {
  const card = createSetupCard({ api: stubApi() });
  card.update(IDLE);
  const text = card.node.textContent;
  assert.match(text, /local runtime \(139 MB\)/);
  assert.match(text, /Qwen2\.5 1\.5B Instruct INT4 \(0\.94 GB, apache-2\.0\)/);
  assert.match(text, /1\.07 GB in all/);
  assert.match(text, /runs on the CPU \(slower\)/);
  assert.equal(visible($(card, '.sc-bar')), false);
  const labels = [...card.node.querySelectorAll('button')].filter(visible).map((b) => b.textContent);
  assert.deepEqual(labels, ['Set up local AI', 'Use StudioForge', 'Use an API key', 'Not now']);
  assert.equal($(card, '.sc-device').dataset.slow, 'true');
});

test('start calls the bridge, running shows the bar and Cancel and hides the alternatives', async () => {
  const api = stubApi({ setup_local_start: { ok: true, ...IDLE, phase: 'model', running: true, percent: 42.4, speed_bps: 12e6, eta_s: 70, message: 'Downloading Qwen' } });
  const card = createSetupCard({ api });
  card.update(IDLE);
  [...card.node.querySelectorAll('button')].find((b) => b.textContent === 'Set up local AI').click();
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(api.calls[0], ['setup_local_start']);
  assert.equal(visible($(card, '.sc-bar')), true);
  assert.equal($(card, '.sc-bar').getAttribute('aria-valuenow'), '42');
  assert.equal($(card, '.sc-fill').style.width, '42.4%');
  assert.match($(card, '.sc-line').textContent, /42% · 12 MB\/s · about 70 s left/);
  const labels = [...card.node.querySelectorAll('button')].filter(visible).map((b) => b.textContent);
  assert.deepEqual(labels, ['Cancel']);
  assert.equal(card.node.getAttribute('aria-busy'), 'true');
});

test('progress repaints in place: the same nodes survive twenty updates', () => {
  const card = createSetupCard({ api: stubApi() });
  card.update({ ...IDLE, phase: 'runtime', running: true });
  const bar = $(card, '.sc-bar');
  const fill = $(card, '.sc-fill');
  for (let i = 1; i <= 20; i++) card.update({ percent: i * 4, phase: 'runtime', running: true });
  assert.equal($(card, '.sc-bar'), bar);
  assert.equal($(card, '.sc-fill'), fill);
  assert.equal(fill.style.width, '80%');
});

test('cancel goes to the bridge; verifying cannot be cancelled', async () => {
  const api = stubApi();
  const card = createSetupCard({ api });
  card.update({ ...IDLE, phase: 'model', running: true, percent: 50 });
  [...card.node.querySelectorAll('button')].find((b) => b.textContent === 'Cancel').click();
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(api.calls[0], ['setup_local_cancel']);
  card.update({ phase: 'verifying', running: true });
  assert.equal([...card.node.querySelectorAll('button')].find((b) => b.textContent === 'Cancel').disabled, true);
});

test('a failure shows the message and hint, and Try again', () => {
  const card = createSetupCard({ api: stubApi() });
  card.update({ ...IDLE, phase: 'error', failure: { message: 'Not enough disk space: 0.5 GB free, 3 GB needed', hint: 'Free some space.', code: 'insufficient_disk' } });
  assert.equal(visible($(card, '.sc-problem')), true);
  assert.match($(card, '.sc-problem').textContent, /Not enough disk space.*Free some space\./);
  assert.equal($(card, '.sc-title').textContent, 'Setup stopped');
  assert.ok([...card.node.querySelectorAll('button')].some((b) => visible(b) && b.textContent === 'Try again'));
  card.update({ phase: 'cancelled', failure: null });
  assert.equal($(card, '.sc-title').textContent, 'Setup cancelled');
});

test('the alternatives call back into the popup; Not now is remembered', async () => {
  const api = stubApi();
  const seen = [];
  const card = createSetupCard({ api, onUseStudioForge: () => seen.push('sf'), onUseKey: () => seen.push('key') });
  let dismissed = 0;
  card.onDismiss = () => { dismissed += 1; };
  card.update(IDLE);
  const click = (label) => [...card.node.querySelectorAll('button')].find((b) => b.textContent === label).click();
  click('Use StudioForge'); click('Use an API key'); click('Not now');
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(seen, ['sf', 'key']);
  assert.deepEqual(api.calls.map((c) => c[0]), ['setup_local_dismiss']);
  assert.equal(dismissed, 1);
});

test('progressLine omits what it does not know', () => {
  assert.equal(progressLine({ percent: 7.6 }), '8%');
  assert.equal(progressLine({ percent: 50, speed_bps: 2e6 }), '50% · 2 MB/s');
  assert.equal(progressLine({ percent: 50, speed_bps: 2e6, eta_s: 30 }), '50% · 2 MB/s · about 30 s left');
});

test('the autostart card asks once and reports the answer', async () => {
  const api = stubApi({ answer_autostart: { ok: true, enabled: true } });
  const answers = [];
  const card = createAutostartCard({ api, onAnswered: (v) => answers.push(v) });
  assert.match(card.node.textContent, /Start ChatForge when you sign in\?/);
  [...card.node.querySelectorAll('button')].find((b) => /^Yes/.test(b.textContent)).click();
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(api.calls[0], ['answer_autostart', true]);
  assert.deepEqual(answers, [true]);
});

test('the autostart card shows an error and lets the user try again', async () => {
  const api = stubApi({ answer_autostart: { ok: false, error: { message: 'Task Scheduler refused.' } } });
  const answers = [];
  const card = createAutostartCard({ api, onAnswered: (v) => answers.push(v) });
  const no = [...card.node.querySelectorAll('button')].find((b) => b.textContent === 'No thanks');
  no.click();
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(answers, []);
  assert.match(card.node.textContent, /Task Scheduler refused\./);
  assert.equal(no.disabled, false);
});
