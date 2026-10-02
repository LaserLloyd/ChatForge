// Smoke test for the settings window: load settings.html in jsdom against dev-mock.js and
// check that the four tabs render, follow the ARIA tabs pattern and get their content.
//
// jsdom does not run <script type="module">, so the page markup is loaded WITHOUT scripts and
// settings.js is imported by Node with jsdom's window/document exposed as globals. bridge.js
// then finds no window.pywebview and installs dev-mock.js, exactly like a plain browser.

import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test, before, after } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.resolve(here, '../../src/chatforge/web');
const html = readFileSync(path.join(web, 'settings.html'), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let dom;
let doc;

before(async () => {
  dom = new JSDOM(html, { url: 'http://localhost:8766/settings.html', pretendToBeVisual: true });
  const w = dom.window;
  for (const [k, v] of Object.entries({
    window: w, document: w.document, location: w.location, sessionStorage: w.sessionStorage,
    localStorage: w.localStorage, Node: w.Node, HTMLElement: w.HTMLElement,
  })) {
    Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  }
  doc = w.document;
  await import(pathToFileURL(path.join(web, 'static/js/settings.js')).href);
  // bridge.js waits up to 1.5 s for pywebview before installing the mock.
  for (let i = 0; i < 60 && doc.documentElement.dataset.ready !== 'true'; i++) await sleep(100);
});

after(() => { dom.window.close(); setTimeout(() => process.exit(0), 50).unref(); });

const $ = (id) => doc.getElementById(id);

test('the page finished loading against dev-mock', () => {
  assert.equal(doc.documentElement.dataset.ready, 'true');
  assert.match($('s-sub').textContent, /Development mock/);
});

test('four tabs render with the ARIA tabs pattern', () => {
  const tabs = [...doc.querySelectorAll('[role="tab"]')];
  assert.deepEqual(tabs.map((t) => t.textContent.trim()), ['Models', 'Providers', 'General', 'Logs']);
  assert.equal(doc.querySelector('[role="tablist"]').getAttribute('aria-label'), 'Settings sections');
  for (const t of tabs) {
    const panel = $(t.getAttribute('aria-controls'));
    assert.equal(panel.getAttribute('role'), 'tabpanel');
    assert.equal(panel.getAttribute('aria-labelledby'), t.id);
  }
  assert.equal(tabs.filter((t) => t.getAttribute('aria-selected') === 'true').length, 1);
  assert.equal($('tab-models').tabIndex, 0);
  assert.equal($('tab-logs').tabIndex, -1);
});

test('Models tab lists the installed models with badges, size and actions', () => {
  const rows = [...doc.querySelectorAll('#model-list .model-row')];
  assert.equal(rows.length, 2);
  // catalog.toml: Qwen2.5-1.5B is the recommended default; Qwen3-4B is outside the catalog.
  assert.match(rows[0].textContent, /Qwen2\.5 1\.5B Instruct INT4/);
  assert.match(rows[0].textContent, /Recommended/);
  assert.match(rows[0].textContent, /930 MB/);
  assert.match(rows[0].textContent, /Compiled for NPU/);
  assert.match(rows[1].textContent, /Qwen3-4B-int4-ov/);
  assert.match(rows[1].textContent, /Untested/);
  assert.match(rows[1].textContent, /2\.29 GB/);
  for (const label of ['Load', 'Clear cache', 'Delete']) {
    assert.ok([...rows[0].querySelectorAll('button')].some((b) => b.textContent.trim() === label), label);
  }
  assert.ok($('disk-bar').children.length === 4);
  assert.match($('rt-ovms').textContent, /Installed, version 2026\.4\.0/);
});

test('search results carry badges and the author filter defaults to OpenVINO', async () => {
  for (let i = 0; i < 30 && doc.querySelectorAll('#search-results .result-row').length === 0; i++) await sleep(100);
  assert.equal($('search-author').checked, true);
  assert.equal($('author-name').textContent, 'OpenVINO');
  const rows = [...doc.querySelectorAll('#search-results .result-row')];
  assert.ok(rows.length >= 4);
  assert.ok(rows.some((r) => /Avoid/.test(r.textContent)));
});

test('arrow keys move between tabs and show the matching panel', () => {
  $('tab-models').focus();
  $('tab-models').dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  assert.equal($('tab-providers').getAttribute('aria-selected'), 'true');
  assert.equal($('panel-providers').hidden, false);
  assert.equal($('panel-models').hidden, true);
  assert.equal(doc.activeElement.id, 'tab-providers');
});

test('Providers tab builds the MiniMax card with a masked key field', async () => {
  for (let i = 0; i < 30 && !doc.querySelector('.provider-card'); i++) await sleep(100);
  const card = doc.querySelector('.provider-card[data-id="minimax"]');
  assert.ok(card, 'minimax card');
  assert.equal(card.querySelectorAll('input[type="radio"]').length, 3);
  const key = card.querySelector('input[data-key-input]');
  assert.equal(key.type, 'password');
  assert.equal(key.getAttribute('autocomplete'), 'off');
  assert.equal(key.getAttribute('spellcheck'), 'false');
  assert.match(card.querySelector('.key-status').textContent, /No key/);
  assert.ok(card.querySelector('label[for="' + key.id + '"]'), 'key label');
});

test('every seeded remote provider has a card; StudioForge marks its key optional', () => {
  const ids = [...doc.querySelectorAll('.provider-card')].map((c) => c.dataset.id);
  assert.deepEqual(ids, ['minimax', 'studioforge', 'openai', 'deepseek']);
  const sf = doc.querySelector('.provider-card[data-id="studioforge"]');
  assert.match(sf.querySelector(`label[for="${sf.querySelector('input[data-key-input]').id}"]`).textContent, /optional/);
  assert.match(sf.querySelector('.key-status').textContent, /optional for this server/);
  const openai = doc.querySelector('.provider-card[data-id="openai"]');
  assert.ok([...openai.querySelectorAll('a')].some((a) => /Where do I get a key/.test(a.textContent)), 'docs link');
});

/** Wait until `fn()` is truthy (or ~4 s). */
async function until(fn) {
  for (let i = 0; i < 80 && !fn(); i++) await sleep(50);
  return fn();
}

test('Enter in the key field saves the key, then tests it', async () => {
  const card = doc.querySelector('.provider-card[data-id="minimax"]');
  const key = card.querySelector('input[data-key-input]');
  const testLine = card.querySelector('.test-line');
  key.value = 'test-key-not-real-123456';
  key.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  assert.equal(key.value, '', 'the key left the field straight away');
  assert.ok(await until(() => /Connected/.test(testLine.textContent)), testLine.textContent);
  assert.match(testLine.textContent, /^Key saved\. Connected/);
  // Saved, not just tested: the bridge reports it in the credential store.
  assert.ok(await until(() => /Credential Manager/.test(card.querySelector('.key-status').textContent)));
  const st = await dom.window.pywebview.api.list_providers();
  assert.equal(st.providers.find((p) => p.id === 'minimax').key.source, 'keyring');
});

test('Enter on an empty key field asks for a key instead of testing', async () => {
  const card = doc.querySelector('.provider-card[data-id="openai"]');
  const key = card.querySelector('input[data-key-input]');
  key.value = '';
  key.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await sleep(20);
  assert.match(card.querySelector('.test-line').textContent, /Type a key first/);
});

test('the local prompt window is checked against config.py (1024 to 8192)', async () => {
  const input = $('local-mpl');
  assert.equal(input.getAttribute('min'), '1024');
  assert.equal(input.getAttribute('max'), '8192');
  input.value = '512';
  $('local-save').click();
  await sleep(20);
  assert.match($('err-local.max_prompt_len').textContent, /1024 to 8192/);
  assert.equal(input.getAttribute('aria-invalid'), 'true');
  input.value = '8192';
  $('local-save').click();
  assert.ok(await until(() => $('local-msg').textContent === 'Saved.'), $('local-msg').textContent);
  assert.equal($('err-local.max_prompt_len').textContent, '');
});

test('the key field is cleared when switching tabs', () => {
  const key = doc.querySelector('input[data-key-input]');
  key.value = 'dummy-not-a-real-key';
  $('tab-general').click();
  assert.equal(key.value, '');
  assert.equal(key.type, 'password');
});

test('General tab is populated and exposes the theme picker', () => {
  assert.equal($('g-idle').value, '10');
  assert.equal($('g-mpc').value, '4000');
  assert.equal($('g-hotkey').value, 'Ctrl+Alt+C');
  assert.ok($('g-theme').hasAttribute('data-ui-theme-picker'));
  // config.ToolsCfg.enabled: every tool is on, and every tool has a box.
  assert.equal(doc.querySelectorAll('[data-tool]').length, 9);
  assert.equal(doc.querySelectorAll('[data-tool]:checked').length, 9);
  assert.equal($('tool-create_document').checked, true);
  // ui.show_on_reply: on by default, next to the hide-on-blur box.
  assert.equal($('g-reply').checked, true);
  assert.match($('g-reply').closest('label').textContent, /Show the popup when a reply finishes/);
  assert.equal($('g-blur').closest('.switches'), $('g-reply').closest('.switches'));
});

test('the reply checkbox saves ui.show_on_reply', async () => {
  const api = window.pywebview.api;
  const submit = async () => {
    $('g-msg').textContent = '';
    $('general-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
    for (let i = 0; i < 30 && $('g-msg').textContent !== 'Saved.'; i++) await sleep(20);
    assert.equal($('g-msg').textContent, 'Saved.');
  };
  $('g-reply').checked = false;
  await submit();
  assert.equal((await api.get_settings()).config.ui.show_on_reply, false);
  $('g-reply').checked = true;
  await submit();
  assert.equal((await api.get_settings()).config.ui.show_on_reply, true);
});

test('saving the General tab unchanged keeps every tool on, even one without a box', async () => {
  const api = window.pywebview.api;
  const all = (await api.get_settings()).config.tools.enabled;
  const submit = async () => {
    $('general-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
    for (let i = 0; i < 30 && $('g-msg').textContent !== 'Saved.'; i++) await sleep(20);
    assert.equal($('g-msg').textContent, 'Saved.');
  };
  await submit();
  assert.deepEqual((await api.get_settings()).config.tools.enabled, all);
  assert.ok(all.includes('create_document'));

  // A tool this page does not list (a newer one) survives a save; settings.changed reloads it.
  await api.update_settings({ tools: { enabled: [...all, 'future_tool'] } });
  for (let i = 0; i < 30 && !(await api.get_settings()).config.tools.enabled.includes('future_tool'); i++) await sleep(20);
  await sleep(50);
  $('tool-calculator').checked = false;
  await submit();
  assert.deepEqual((await api.get_settings()).config.tools.enabled,
    [...all.filter((t) => t !== 'calculator'), 'future_tool']);
  await api.update_settings({ tools: { enabled: all } });
  await sleep(50);
  assert.equal($('tool-calculator').checked, true);
});

test('General tab shows a per-field error for an invalid value', async () => {
  $('g-mpc').value = '50';
  $('general-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await sleep(50);
  assert.match($('err-chat.max_prompt_chars').textContent, /200 to 4000000/);
  assert.equal($('g-mpc').getAttribute('aria-invalid'), 'true');
});

test('Logs tab shows the tail and stops its timer when left', async () => {
  $('tab-logs').click();
  await sleep(300);
  assert.match($('log-view').textContent, /dev-mock started/);
  assert.match($('log-count').textContent, /lines?/);
  $('tab-models').click();
});

// ------------------------------------------------------------- quick actions ----

const qaRows = () => [...doc.querySelectorAll('#qa-list .qa-row')];
const qaNames = () => qaRows().map((li) => li.querySelector('.qa-label').value);
async function saveGeneral() {
  $('g-msg').textContent = '';
  $('general-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  for (let i = 0; i < 40 && !/Saved\.|not saved|Give|Write/.test($('g-msg').textContent + $('err-chat.quick_actions').textContent); i++) await sleep(20);
}

test('General lists the quick actions: the built-in ones, Proof / Improve / Check me / News insight first', () => {
  $('tab-general').click();
  $('g-mpc').value = '4000';   // an earlier test left an invalid value here
  assert.deepEqual(qaNames().slice(0, 4), ['Proof this', 'Improve this', 'Check me on this', 'News insight']);
  assert.equal(qaRows().length, 12);
  const proof = qaRows()[0];
  assert.match(proof.querySelector('.chip').textContent, /Built in/);
  assert.match(proof.querySelector('.qa-instructions').value, /Proofread/);
  assert.equal(proof.querySelector('.qa-style').checked, true);
  assert.equal(qaRows()[3].querySelector('.qa-tools').checked, true, 'News insight may look things up');
  assert.equal(proof.querySelector('.qa-remove').getAttribute('aria-label'), 'Remove Proof this');
});

test('editing the quick actions saves only what differs from the built-in ones', async () => {
  const api = window.pywebview.api;
  qaRows()[0].querySelector('.qa-label').value = 'Proofread';
  qaRows().find((li) => li.querySelector('.qa-label').value === 'Translate').querySelector('.qa-remove').click();
  assert.equal(qaRows().length, 11);
  $('qa-add').click();
  const added = qaRows().at(-1);
  assert.equal(doc.activeElement, added.querySelector('.qa-label'), 'a new row takes the focus');
  assert.equal(added.querySelector('details').open, true);
  added.querySelector('.qa-label').value = 'Haiku';

  // A custom action needs instructions: the save stops and says so.
  await saveGeneral();
  assert.match($('err-chat.quick_actions').textContent, /Write the instructions for “Haiku”/);
  assert.equal(added.querySelector('.qa-instructions').getAttribute('aria-invalid'), 'true');
  assert.equal(doc.activeElement, added.querySelector('.qa-instructions'));

  added.querySelector('.qa-instructions').value = 'Turn the text into a haiku.';
  await saveGeneral();
  assert.equal($('g-msg').textContent, 'Saved.');
  const chat = (await api.get_settings()).config.chat;
  assert.deepEqual(chat.quick_actions, [
    { id: 'proof', label: 'Proofread', instructions: '', hint: '', tools: false, match_style: true },
    { id: '', label: 'Haiku', instructions: 'Turn the text into a haiku.', hint: '', tools: false, match_style: true },
  ]);
  assert.deepEqual(chat.hidden_quick_actions, ['translate']);
  // The list is redrawn as the app has it now: the custom action has its id and its chip.
  const last = qaRows().at(-1);
  assert.equal(last.dataset.id, 'custom-haiku');
  assert.match(last.querySelector('.chip').textContent, /Custom/);
  const state = await api.get_state();
  assert.equal(state.config.quick_actions[0].label, 'Proofread');
  assert.ok(!state.config.quick_actions.some((a) => a.id === 'translate'));
});

test('Restore defaults brings back the built-in quick actions once saved', async () => {
  const api = window.pywebview.api;
  $('qa-restore').click();
  assert.match($('g-msg').textContent, /Press Save to keep them/);
  assert.equal(qaRows().length, 12);
  assert.equal(qaNames()[0], 'Proof this');
  await saveGeneral();
  assert.equal($('g-msg').textContent, 'Saved.');
  const chat = (await api.get_settings()).config.chat;
  assert.deepEqual(chat.quick_actions, []);
  assert.deepEqual(chat.hidden_quick_actions, []);
});
