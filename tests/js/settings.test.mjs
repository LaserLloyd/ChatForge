// Smoke test for the settings window: load settings.html in jsdom against dev-mock.js and
// check that the four tabs render, follow the ARIA tabs pattern and get their content.
//
// jsdom does not run <script type="module">, so the page markup is loaded WITHOUT scripts and
// settings.js is imported by Node with jsdom's window/document exposed as globals. bridge.js
// then finds no window.pywebview and installs dev-mock.js, exactly like a plain browser.

import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test, before, after, mock } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.resolve(here, '../../src/chatforge/web');
const html = readFileSync(path.join(web, 'settings.html'), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let dom;
let doc;
// What the bundle's scripts would provide (jsdom runs none): the theme runtime, the themed
// confirm/toast, and a window that can scroll.
const themeList = [
  { slug: 'laserlloyd', name: 'LaserLloyd', ground: 'dark', themeColor: '#080a0d', swatch: '#2ea8ff' },
  { slug: 'daylight', name: 'Daylight', ground: 'light', themeColor: '#ffffff', swatch: '#2c56c9' },
  { slug: 'forest', name: 'Forest', ground: 'oled', themeColor: '#000000', swatch: '#b5d59b' },
];
let themeNow = 'laserlloyd';
const themeListeners = [];
const confirms = [];
let confirmAnswer = true;
const scroll = { y: 0, calls: [] };
let copied = null;

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
  w.UITheme = {
    list: () => themeList,
    current: () => themeNow,
    set: (slug) => { themeNow = slug; for (const f of themeListeners) f({ slug }); return slug; },
    onChange: (fn) => { themeListeners.push(fn); return () => {}; },
    mountPicker: () => null,
  };
  w.UIComponents = {
    confirm: async (o) => { confirms.push(o); return confirmAnswer; },
    toast: (message, opts = {}) => {
      let region = doc.getElementById('bundle-toasts');
      if (!region) { region = doc.createElement('div'); region.id = 'bundle-toasts'; doc.body.append(region); }
      const n = doc.createElement('div');
      n.className = `ui-toast${opts.kind ? ` ui-toast--${opts.kind}` : ''}`;
      n.textContent = message;
      region.append(n);
      return n;
    },
  };
  w.scrollTo = (x, y) => { scroll.y = y; scroll.calls.push(y); };
  Object.defineProperty(w, 'scrollY', { get: () => scroll.y, configurable: true });
  Object.defineProperty(globalThis, 'navigator', { value: { clipboard: { writeText: async (t) => { copied = t; } } }, configurable: true, writable: true });
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

test('the page is built from ThemeForge ui-* components, with none of the old duplicates left', () => {
  const tabs = $('tab-models').parentElement;
  assert.ok(tabs.classList.contains('ui-tabs'));
  assert.ok([...tabs.children].every((t) => t.classList.contains('ui-tab')));
  // aria-selected is the only selected-state signal (ui-tab styles it).
  assert.equal(doc.querySelectorAll('.ui-tab[aria-selected="true"]').length, 1);
  assert.ok($('panel-models').querySelector('.ui-card .ui-card__title'));
  // The OVMS download bar is the native element (role and value come with it).
  assert.equal($('rt-bar').tagName, 'PROGRESS');
  assert.ok($('rt-bar').classList.contains('ui-progress'));
  assert.equal($('rt-bar').getAttribute('aria-label'), 'OVMS download');
  // Boolean settings are switches; the tool list uses checkboxes.
  for (const id of ['g-autostart', 'g-sticky', 'g-reply', 'auto-refresh', 'log-auto']) {
    assert.equal($(id).getAttribute('role'), 'switch', id);
    assert.ok($(id).classList.contains('ui-switch'), id);
    assert.ok($(id).closest('label').querySelector('.ui-switch-state'), id);
  }
  assert.ok($('tool-calculator').classList.contains('ui-checkbox'));
  // Fields: label + control + error from the bundle.
  assert.ok($('g-idle').classList.contains('ui-input'));
  assert.ok($('g-reasoning').classList.contains('ui-select'));
  assert.ok($('g-personality').classList.contains('ui-textarea'));
  assert.ok($('err-local.max_prompt_len').classList.contains('ui-error'));
  // Class names settings.css used to define for itself must not come back.
  const legacy = '.tabs, .tab, .card, .card-head, .field, .input, .btn, .btn-primary, .btn-danger, .btn-danger-ghost, .chip, .err, .check, .row, .bar, .hint-box, .warn-note, .small, .note, .h2, .adv';
  assert.equal(doc.querySelectorAll(legacy).length, 0, [...doc.querySelectorAll(legacy)].map((n) => n.className).join(' | '));
});

test('the runtime\'s <select data-ui-theme-picker> stays, hidden, behind the swatch grid', () => {
  const sel = $('g-theme');
  assert.equal(sel.tagName, 'SELECT');
  assert.ok(sel.hasAttribute('data-ui-theme-picker'));
  assert.ok(sel.classList.contains('ui-select'));
  assert.equal(sel.getAttribute('aria-label'), 'Theme');
  assert.equal(sel.hidden, true);
  assert.equal($('g-theme-grid').getAttribute('role'), 'radiogroup');
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
  for (const label of ['Use this model', 'Clear cache', 'Delete model']) {
    assert.ok([...rows[0].querySelectorAll('button')].some((b) => b.textContent.trim() === label), label);
  }
  assert.ok($('disk-bar').children.length === 4);
  // Badges are ui-badge with a status variant: Recommended is success, Untested is warning.
  assert.ok(rows[0].querySelector('.ui-badge--success'));
  assert.ok(rows[1].querySelector('.ui-badge--warning'));
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

test('a typed key is masked but kept when switching tabs, and wiped when the window goes', () => {
  const key = doc.querySelector('input[data-key-input]');
  key.value = 'dummy-not-a-real-key';
  key.type = 'text';
  $('tab-general').click();
  assert.equal(key.value, 'dummy-not-a-real-key');
  assert.equal(key.type, 'password');
  assert.equal(key.parentElement.querySelector('[data-eye]').textContent, 'Show');
  dom.window.dispatchEvent(new dom.window.Event('pagehide'));
  assert.equal(key.value, '');
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
  // ui.sticky and ui.show_on_reply: both on by default, side by side.
  assert.equal($('g-sticky').checked, true);
  assert.match($('g-sticky').closest('label').textContent, /Sticky popup/);
  assert.equal($('g-reply').checked, true);
  assert.match($('g-reply').closest('label').textContent, /Show the popup when a reply finishes/);
  assert.equal($('g-sticky').closest('.switches'), $('g-reply').closest('.switches'));
});

test('the sticky checkbox saves ui.sticky', async () => {
  const api = window.pywebview.api;
  const submit = async () => {
    $('g-msg').textContent = '';
    $('general-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
    for (let i = 0; i < 30 && $('g-msg').textContent !== 'Saved.'; i++) await sleep(20);
    assert.equal($('g-msg').textContent, 'Saved.');
  };
  $('g-sticky').checked = false;
  await submit();
  assert.equal((await api.get_settings()).config.ui.sticky, false);
  $('g-sticky').checked = true;
  await submit();
  assert.equal((await api.get_settings()).config.ui.sticky, true);
});

test('the popup pin changing sticky while General has unsaved edits is not undone by Save', async () => {
  const api = window.pywebview.api;
  const submit = async () => {
    $('g-msg').textContent = '';
    $('general-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
    for (let i = 0; i < 30 && $('g-msg').textContent !== 'Saved.'; i++) await sleep(20);
    assert.equal($('g-msg').textContent, 'Saved.');
  };
  assert.equal($('g-sticky').checked, true);
  $('g-reply').checked = false;   // an unsaved edit elsewhere on the tab
  $('general-form').dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  await api.set_sticky(false);   // the pin in the popup
  for (let i = 0; i < 30 && $('g-sticky').checked; i++) await sleep(10);
  assert.equal($('g-sticky').checked, false, 'the box did not follow the pin');
  assert.equal($('g-reply').checked, false, 'the unsaved edit was thrown away');
  await submit();
  const ui = (await api.get_settings()).config.ui;
  assert.equal(ui.sticky, false);
  assert.equal(ui.show_on_reply, false);
  $('g-sticky').checked = true;
  $('g-reply').checked = true;
  await submit();
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
  for (let i = 0; i < 40 && !/Saved\.|Nothing was saved|Give|Write/.test($('g-msg').textContent + $('err-chat.quick_actions').textContent); i++) await sleep(20);
}

test('General lists the quick actions: the built-in ones, Proof / Improve / Check me / News insight first', () => {
  $('tab-general').click();
  $('g-mpc').value = '4000';   // an earlier test left an invalid value here
  assert.deepEqual(qaNames().slice(0, 4), ['Proof this', 'Improve this', 'Check me on this', 'News insight']);
  assert.equal(qaRows().length, 12);
  const proof = qaRows()[0];
  assert.match(proof.querySelector('.ui-badge').textContent, /Built in/);
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
  assert.match(last.querySelector('.ui-badge').textContent, /Custom/);
  const state = await api.get_state();
  assert.equal(state.config.quick_actions[0].label, 'Proofread');
  assert.ok(!state.config.quick_actions.some((a) => a.id === 'translate'));
});

test('Restore defaults brings back the built-in quick actions once saved', async () => {
  const api = window.pywebview.api;
  confirmAnswer = true;
  $('qa-restore').click();
  assert.ok(await until(() => /Press Save to keep them/.test($('g-msg').textContent)), $('g-msg').textContent);
  assert.equal(qaRows().length, 12);
  assert.equal(qaNames()[0], 'Proof this');
  await saveGeneral();
  assert.equal($('g-msg').textContent, 'Saved.');
  const chat = (await api.get_settings()).config.chat;
  assert.deepEqual(chat.quick_actions, []);
  assert.deepEqual(chat.hidden_quick_actions, []);
});

// ------------------------------------------------- save, unsaved changes, hotkey ----

const input = (node) => node.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
const change = (node) => node.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
const tick = () => new Promise((r) => setImmediate(r));
const toasts = () => [...doc.querySelectorAll('.ui-toast')];
const key = (code, k, mods = {}) => new dom.window.KeyboardEvent('keydown', { code, key: k, bubbles: true, cancelable: true, ...mods });

test('a refused hotkey says "Saved, except the hotkey" and keeps only the hotkey unsaved', async () => {
  const api = window.pywebview.api;
  $('tab-general').click();
  $('g-idle').value = '7'; input($('g-idle'));
  $('g-hotkey').value = 'Win+L'; input($('g-hotkey'));
  await saveGeneral();
  for (let i = 0; i < 40 && !/except the hotkey/.test($('g-msg').textContent); i++) await sleep(20);
  assert.match($('g-msg').textContent, /^Saved, except the hotkey: Win\+L is in use by another app/);
  assert.doesNotMatch($('g-msg').textContent, /Some settings were not saved/);
  const cfg = (await api.get_settings()).config;
  assert.equal(cfg.local.idle_unload_minutes, 7, 'the rest was saved');
  assert.equal(cfg.ui.hotkey, 'Ctrl+Alt+C', 'the refused hotkey was not');
  assert.match($('err-ui.hotkey').textContent, /in use/);
  assert.equal($('g-hotkey').value, 'Win+L', 'the typed hotkey is still there');
  assert.equal($('g-dirty').hidden, false, 'and still counts as unsaved');
  // A hotkey Windows accepts is applied before the rest and saves cleanly.
  $('g-hotkey').value = 'Ctrl+Alt+K'; input($('g-hotkey'));
  await saveGeneral();
  assert.equal($('g-msg').textContent, 'Saved.');
  assert.equal((await api.get_settings()).config.ui.hotkey, 'Ctrl+Alt+K');
  assert.equal($('err-ui.hotkey').textContent, '');
  $('g-hotkey').value = 'Ctrl+Alt+C'; $('g-idle').value = '10'; input($('g-idle'));
  await saveGeneral();
  assert.equal($('g-msg').textContent, 'Saved.');
});

test('unsaved changes: marker, Revert, a Settings saved toast and a leave warning', async () => {
  assert.equal($('g-dirty').hidden, true);
  assert.equal($('g-revert').disabled, true);
  $('g-location').value = 'Lisbon'; input($('g-location'));
  assert.equal($('g-dirty').hidden, false);
  assert.match($('g-dirty').textContent, /Unsaved changes/);
  assert.equal($('g-revert').disabled, false);
  assert.equal($('tab-general').dataset.dirty, 'true');
  const leave = new dom.window.Event('beforeunload', { cancelable: true });
  dom.window.dispatchEvent(leave);
  assert.equal(leave.defaultPrevented, true, 'leaving with unsaved edits is questioned');

  $('g-revert').click();
  assert.equal($('g-location').value, '');
  assert.equal($('g-dirty').hidden, true);
  assert.equal($('g-revert').disabled, true);
  const quiet = new dom.window.Event('beforeunload', { cancelable: true });
  dom.window.dispatchEvent(quiet);
  assert.equal(quiet.defaultPrevented, false);

  // The theme and Start at login apply at once: they are not unsaved edits.
  $('theme-daylight').checked = true; change($('theme-daylight'));
  assert.equal($('g-dirty').hidden, true);
  $('theme-laserlloyd').checked = true; change($('theme-laserlloyd'));

  $('g-location').value = 'Lisbon'; input($('g-location'));
  await saveGeneral();
  assert.equal($('g-msg').textContent, 'Saved.');
  assert.equal($('g-dirty').hidden, true);
  assert.ok(toasts().some((t) => t.textContent.includes('Settings saved')));
  assert.ok($('g-save').closest('.savebar'), 'Save lives in the sticky bar');
  $('g-location').value = ''; input($('g-location'));
  await saveGeneral();
});

test('Record captures the next key combination; Esc cancels, Backspace clears, Reset restores', () => {
  const rec = $('g-hotkey-record');
  $('g-hotkey').value = 'Ctrl+Alt+C'; input($('g-hotkey'));
  assert.equal($('g-hotkey-reset').hidden, true, 'Reset is only offered when the hotkey differs');
  rec.click();
  assert.equal(rec.getAttribute('aria-pressed'), 'true');
  const ev = key('KeyK', 'k', { ctrlKey: true, shiftKey: true });
  doc.dispatchEvent(ev);
  assert.equal(ev.defaultPrevented, true);
  assert.equal($('g-hotkey').value, 'Ctrl+Shift+K');
  assert.equal(rec.getAttribute('aria-pressed'), 'false');
  assert.equal($('g-dirty').hidden, false);
  assert.equal($('g-hotkey-reset').hidden, false);

  rec.click();
  doc.dispatchEvent(key('ControlLeft', 'Control', { ctrlKey: true }));   // a modifier alone keeps waiting
  assert.equal(rec.getAttribute('aria-pressed'), 'true');
  doc.dispatchEvent(key('Digit5', '5', { altKey: true, metaKey: true }));
  assert.equal($('g-hotkey').value, 'Alt+Win+5');
  rec.click();
  doc.dispatchEvent(key('F9', 'F9', { ctrlKey: true }));
  assert.equal($('g-hotkey').value, 'Ctrl+F9');
  rec.click();
  doc.dispatchEvent(key('Space', ' ', { ctrlKey: true, altKey: true }));
  assert.equal($('g-hotkey').value, 'Ctrl+Alt+Space');

  rec.click();
  doc.dispatchEvent(key('F23', 'F23', { metaKey: true, shiftKey: true }));
  assert.equal($('g-hotkey').value, 'Copilot', 'the Copilot key is Meta+Shift+F23');
  $('g-hotkey-reset').click();
  $('g-hotkey-copilot').click();
  assert.equal($('g-hotkey').value, 'Copilot');
  assert.match($('g-hotkey-copilot-hint').textContent, /Windows only\. Replaces the PowerToys remap\./);
  $('g-hotkey-reset').click();
  rec.click();
  doc.dispatchEvent(key('Space', ' ', { ctrlKey: true, altKey: true }));
  rec.click();
  doc.dispatchEvent(key('Escape', 'Escape'));
  assert.equal(rec.getAttribute('aria-pressed'), 'false');
  assert.equal($('g-hotkey').value, 'Ctrl+Alt+Space', 'Esc left the field alone');
  rec.click();
  doc.dispatchEvent(key('Backspace', 'Backspace'));
  assert.equal($('g-hotkey').value, '');
  $('g-hotkey-reset').click();
  assert.equal($('g-hotkey').value, 'Ctrl+Alt+C');
  assert.equal($('g-hotkey-reset').hidden, true);
  rec.click();
  doc.dispatchEvent(key('KeyK', 'k'));   // no modifier: refused, still recording
  assert.match($('err-ui.hotkey').textContent, /Ctrl, Alt, Shift or Win/);
  rec.click();   // pressing Record again stops
  assert.equal(rec.getAttribute('aria-pressed'), 'false');
  $('g-revert').click();
});

// ------------------------------------------------------------- quick actions ----

test('Restore defaults asks first; names and instructions get a counter and a preview', async () => {
  confirms.length = 0;
  confirmAnswer = false;
  qaRows()[0].querySelector('.qa-label').value = 'Proofread now'; input(qaRows()[0].querySelector('.qa-label'));
  $('qa-restore').click();
  await tick();
  assert.equal(confirms.length, 1);
  assert.equal(confirms[0].danger, true);
  assert.equal(qaNames()[0], 'Proofread now', 'declined: nothing changed');
  confirmAnswer = true;

  const row = qaRows()[0];
  const counter = row.querySelector('.qa-head .qa-count');
  assert.equal(counter.hidden, true);
  row.querySelector('.qa-label').value = 'x'.repeat(36); input(row.querySelector('.qa-label'));
  assert.equal(counter.hidden, false);
  assert.equal(counter.textContent, '36/40');
  const text = row.querySelector('.qa-instructions');
  const textCount = row.querySelector('.qa-more .qa-count');
  assert.equal(textCount.hidden, true);
  text.value = 'a'.repeat(3300); input(text);
  assert.equal(textCount.textContent, '3300/4000');
  assert.equal(textCount.hidden, false);
  text.value = 'Proofread   the\ntext.'; input(text);
  assert.equal(row.querySelector('.qa-preview').textContent, 'Proofread the text.');
  $('g-revert').click();
});

test('quick actions can be moved up and down; the new order is what Save writes', async () => {
  const api = window.pywebview.api;
  const before = qaNames();
  assert.equal(qaRows()[0].querySelector('.qa-up').disabled, true);
  assert.equal(qaRows().at(-1).querySelector('.qa-down').disabled, true);
  assert.equal(qaRows()[1].querySelector('.qa-up').getAttribute('aria-label'), `Move ${before[1]} up`);
  qaRows()[0].querySelector('.qa-down').click();
  assert.deepEqual(qaNames().slice(0, 2), [before[1], before[0]]);
  assert.equal(doc.activeElement.className.includes('qa-down'), true, 'focus follows the moved row');
  assert.equal($('g-dirty').hidden, false);
  await saveGeneral();
  assert.equal($('g-msg').textContent, 'Saved.');
  const items = (await api.get_settings()).quick_actions.items;
  assert.equal(items[0].label, before[1]);
  assert.equal((await api.get_settings()).config.chat.quick_actions.length, 12);
  qaRows()[1].querySelector('.qa-up').click();
  assert.deepEqual(qaNames(), before);
  await saveGeneral();
  assert.deepEqual((await api.get_settings()).config.chat.quick_actions, [], 'the built-in order needs no entries');
});

// --------------------------------------------------- fallback, tools, theme grid ----

test('the fallback card warns when the chosen provider has no key', async () => {
  $('tab-general').click();
  const sel = $('g-fallback');
  const warn = $('g-fallback-warn');
  assert.equal(warn.hidden, true);
  sel.value = 'openai'; change(sel);
  assert.equal(warn.hidden, false);
  assert.match(warn.textContent, /^No key saved for OpenAI/);
  sel.value = 'studioforge'; change(sel);   // its key is optional
  assert.equal(warn.hidden, true);
  sel.value = ''; change(sel);
  assert.equal(warn.hidden, true);
  assert.match(doc.querySelector('#h-fallback').closest('section').textContent, /leaves this PC/);
  $('g-revert').click();
});

test('every tool says what it sends where; the five local tools carry a Local badge', () => {
  const items = [...doc.querySelectorAll('.tool-list .tool')];
  assert.equal(items.length, 9);
  for (const li of items) assert.ok(li.querySelector('.ui-help').textContent.length > 10, li.textContent);
  const local = items.filter((li) => li.querySelector('[data-local-tool]')).map((li) => li.querySelector('[data-tool]').value);
  assert.deepEqual(local, ['web_search', 'fetch_url', 'weather', 'current_datetime', 'calculator']);
  assert.match(items[0].textContent, /DuckDuckGo/);
  assert.match($('tool-weather').closest('.tool').textContent, /Open-Meteo/);
});

test('the theme is a grid of swatch cards that calls UITheme.set and saves ui.theme', async () => {
  const api = window.pywebview.api;
  const radios = [...doc.querySelectorAll('#g-theme-grid input[type="radio"]')];
  assert.deepEqual(radios.map((r) => r.value), ['laserlloyd', 'daylight', 'forest']);
  assert.equal(radios.filter((r) => r.checked).length, 1);
  assert.equal($('theme-laserlloyd').checked, true);
  const card = $('theme-daylight').closest('label');
  assert.equal(card.querySelector('.theme-swatch').style.getPropertyValue('--sw-accent'), '#2c56c9');
  assert.match(card.textContent, /Daylight/);
  $('theme-daylight').checked = true; change($('theme-daylight'));
  assert.equal(themeNow, 'daylight');
  assert.ok(await until(async () => false) || true);
  for (let i = 0; i < 30 && (await api.get_settings()).config.ui.theme !== 'daylight'; i++) await sleep(20);
  assert.equal((await api.get_settings()).config.ui.theme, 'daylight');
  $('theme-laserlloyd').checked = true; change($('theme-laserlloyd'));
  for (let i = 0; i < 30 && (await api.get_settings()).config.ui.theme !== 'laserlloyd'; i++) await sleep(20);
  assert.equal((await api.get_settings()).config.ui.theme, 'laserlloyd');
});

// ------------------------------------------------------------ provider cards ----

test('provider cards are built once: a typed key and a test result survive a tab switch', async () => {
  $('tab-providers').click();
  await until(() => doc.querySelector('.provider-card'));
  const card = doc.querySelector('.provider-card[data-id="openai"]');
  const keyIn = card.querySelector('input[data-key-input]');
  keyIn.value = 'typed-not-a-real-key';
  card.querySelector('.test-line').textContent = 'Connected in 0.42 s.';
  $('tab-logs').click();
  $('tab-providers').click();
  await sleep(150);
  assert.equal(doc.querySelector('.provider-card[data-id="openai"]'), card, 'the same card element');
  assert.equal(keyIn.value, 'typed-not-a-real-key');
  assert.equal(keyIn.type, 'password');
  assert.equal(card.querySelector('.test-line').textContent, 'Connected in 0.42 s.');
  keyIn.value = '';
  card.querySelector('.test-line').textContent = '';
});

test('base URL, context window and reply length get their own error and aria-invalid', async () => {
  const card = doc.querySelector('.provider-card[data-id="openai"]');
  const url = card.querySelector('input[type="url"]');
  const err = (inp) => doc.getElementById(inp.getAttribute('aria-describedby').split(' ').find((i) => i.endsWith('-err')));
  url.value = 'nope'; change(url);
  await sleep(10);
  assert.match(err(url).textContent, /full http\(s\) URL/);
  assert.equal(url.getAttribute('aria-invalid'), 'true');
  input(url);   // typing clears it
  assert.equal(url.getAttribute('aria-invalid'), null);

  const ctx = card.querySelector('input[id$="-ctx"]');
  ctx.value = '100'; change(ctx);
  await sleep(10);
  assert.match(err(ctx).textContent, /2048 to 10000000/);
  assert.equal(ctx.getAttribute('aria-invalid'), 'true');
  ctx.value = ''; change(ctx);
  await sleep(30);
  assert.equal(err(ctx).textContent, '');
  assert.equal(ctx.getAttribute('aria-invalid'), null);

  const out = card.querySelector('input[id$="-out"]');
  out.value = '10'; change(out);
  await sleep(10);
  assert.match(err(out).textContent, /256 to 200000/);
  assert.equal(out.getAttribute('aria-invalid'), 'true');
  out.value = '2048'; change(out);
  await sleep(30);
  assert.equal(out.getAttribute('aria-invalid'), null);
});

test('plain http to another machine is warned about; localhost and https are not', () => {
  const card = doc.querySelector('.provider-card[data-id="openai"]');
  const url = card.querySelector('input[type="url"]');
  const warn = doc.getElementById(url.getAttribute('aria-describedby').split(' ').find((i) => i.endsWith('-warn')));
  url.value = 'http://192.168.1.20:8080/v1'; input(url);
  assert.equal(warn.hidden, false);
  assert.match(warn.textContent, /plain http/);
  url.value = 'http://localhost:1234/v1'; input(url);
  assert.equal(warn.hidden, true);
  url.value = 'https://api.openai.com/v1'; input(url);
  assert.equal(warn.hidden, true);
});

test('the key card says Save & test, toggles Show/Hide, and Remove key is a danger button', () => {
  const card = doc.querySelector('.provider-card[data-id="openai"]');
  const labels = [...card.querySelectorAll('.ui-row .ui-btn')].map((b) => b.textContent.trim());
  assert.deepEqual(labels.slice(0, 4), ['Save key', 'Save & test', 'Refresh models', 'Remove key']);
  const eye = card.querySelector('[data-eye]');
  const keyIn = card.querySelector('input[data-key-input]');
  assert.equal(eye.textContent, 'Show');
  eye.click();
  assert.equal(keyIn.type, 'text');
  assert.equal(eye.textContent, 'Hide');
  assert.equal(eye.getAttribute('aria-pressed'), 'true');
  eye.click();
  assert.equal(eye.textContent, 'Show');
  assert.ok([...card.querySelectorAll('button')].find((b) => b.textContent === 'Remove key').classList.contains('ui-btn--danger'));
  assert.ok(card.querySelector('h3.ui-card__title'));
  assert.equal($('h-cloud').tagName, 'H2');
});

test('a save that removes the key because the address changed says so', async () => {
  const api = window.pywebview.api;
  await api.save_api_key('deepseek', 'not-a-real-key-123');
  const card = doc.querySelector('.provider-card[data-id="deepseek"]');
  await until(() => /Credential Manager/.test(card.querySelector('.key-status').textContent) || true);
  const url = card.querySelector('input[type="url"]');
  url.value = 'https://other.example.com/v1'; change(url);
  assert.ok(await until(() => toasts().some((t) => /Key removed: the address changed/.test(t.textContent))));
  assert.ok(await until(() => /No key/.test(card.querySelector('.key-status').textContent)), card.querySelector('.key-status').textContent);
  assert.ok(toasts().find((t) => /Key removed/.test(t.textContent)).classList.contains('ui-toast--warning'));
  url.value = 'https://api.deepseek.com/v1'; change(url);
  await sleep(50);
});

// ------------------------------------------------------------------- toasts ----

test('error toasts stay until dismissed, success toasts hide; hovering holds the timer', async () => {
  const api = window.pywebview.api;
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    $('rt-vc-copy').click();   // copying is unavailable here: an info toast
    await tick(); await tick();
    const info = toasts().at(-1);
    assert.match(info.textContent, /Command copied|Could not copy/);
    assert.equal(info.getAttribute('role'), 'status');
    info.dispatchEvent(new dom.window.Event('mouseenter'));
    mock.timers.tick(10000);
    assert.ok(info.isConnected, 'held while hovered');
    info.dispatchEvent(new dom.window.Event('mouseleave'));
    mock.timers.tick(3999);
    assert.ok(info.isConnected);
    mock.timers.tick(1);
    assert.equal(info.isConnected, false, 'auto-hid after 4 s');

    const orig = api.open_config_folder;
    api.open_config_folder = async () => ({ ok: false, error: { code: 'x', message: 'Explorer would not open.' } });
    $('tab-logs').click();
    $('log-open-config').click();
    await tick(); await tick();
    const bad = toasts().at(-1);
    assert.match(bad.textContent, /Explorer would not open/);
    assert.equal(bad.getAttribute('role'), 'alert');
    mock.timers.tick(120000);
    assert.ok(bad.isConnected, 'an error stays until dismissed');
    bad.querySelector('.toast-close').click();
    assert.equal(bad.isConnected, false);
    api.open_config_folder = orig;
  } finally {
    mock.timers.reset();
  }
});

// --------------------------------------------------------------------- logs ----

test('logs: filter, level colours, Copy diagnostics and Open config folder', async () => {
  const api = window.pywebview.api;
  $('tab-logs').click();
  window.__mock.state().logs.push('2026-10-07 10:00:00.000 WARNING  something odd happened');
  window.__mock.state().logs.push('2026-10-07 10:00:01.000 ERROR    something broke');
  $('log-refresh').click();
  assert.ok(await until(() => doc.querySelector('.ll-warn')));
  assert.match(doc.querySelector('.ll-warn').textContent, /something odd/);
  assert.match(doc.querySelector('.ll-error').textContent, /something broke/);
  $('log-filter').value = 'broke'; input($('log-filter'));
  assert.equal(doc.querySelectorAll('#log-view .ll').length, 1);
  assert.match($('log-count').textContent, /1 of the last \d+ lines? match/);
  $('log-filter').value = ''; input($('log-filter'));
  assert.match($('log-count').textContent, /^The last \d+ lines/);
  assert.match($('log-view').getAttribute('aria-label'), /last 500 lines/);

  copied = null;
  $('log-diagnostics').click();
  assert.ok(await until(() => copied !== null));
  assert.match(copied, /ChatForge diagnostics/);
  assert.doesNotMatch(copied, /api_key"/);
  let opened = 0;
  const orig = api.open_config_folder;
  api.open_config_folder = async () => { opened++; return { ok: true }; };
  $('log-open-config').click();
  await tick();
  assert.equal(opened, 1);
  api.open_config_folder = orig;
  $('tab-models').click();
});

test('dropping a file on the window is cancelled instead of navigating', () => {
  for (const type of ['dragover', 'drop']) {
    const ev = new dom.window.Event(type, { bubbles: true, cancelable: true });
    doc.body.dispatchEvent(ev);
    assert.equal(ev.defaultPrevented, true, type);
  }
});

// ------------------------------------------------------------ scroll position ----

test('each tab comes back at the scroll position it was left at', () => {
  $('tab-models').click();
  scroll.y = 240;
  $('tab-general').click();
  assert.equal(scroll.y, 0, 'a tab never visited starts at the top');
  scroll.y = 700;
  $('tab-logs').click();
  scroll.y = 30;
  $('tab-models').click();
  assert.equal(scroll.y, 240);
  $('tab-general').click();
  assert.equal(scroll.y, 700);
  $('tab-logs').click();
  assert.equal(scroll.y, 30);
  $('tab-models').click();
});

// ------------------------------------------------------- models: use, loading ----

test('a model row has one primary "Use this model"; no dashed Active button; load progress shows', async () => {
  $('tab-models').click();
  await until(() => doc.querySelectorAll('#model-list .model-row').length === 2);
  const row = doc.querySelector('#model-list .model-row');
  const buttons = [...row.querySelectorAll('button')].map((b) => b.textContent.trim());
  assert.ok(!buttons.includes('Active') && !buttons.includes('Set active'), buttons.join());
  assert.equal(row.querySelectorAll('.ui-btn--primary').length, 1);
  const use = [...row.querySelectorAll('button')].find((b) => b.textContent === 'Use this model');
  assert.ok(use.classList.contains('ui-btn--primary'));
  assert.ok([...row.querySelectorAll('button')].find((b) => b.textContent === 'Delete model').classList.contains('ui-btn--danger'));
  assert.match(row.querySelector('.m-title').textContent, /Active/, 'the Active badge is the only marker');
  use.click();
  assert.ok(await until(() => doc.querySelector('[data-load]')), 'loading started');
  const line = () => (doc.querySelector('[data-load] .load-text') || { textContent: '' }).textContent;
  assert.ok(await until(() => /of about 10 s$/.test(line())), line());
  assert.match(line(), /^(Starting the runtime|Loading onto NPU): \d+ s of about 10 s$/);
  assert.ok(await until(() => [...doc.querySelectorAll('#model-list button')].some((b) => b.textContent === 'Unload')), 'loaded');
  [...doc.querySelectorAll('#model-list button')].find((b) => b.textContent === 'Unload').click();
  await until(() => [...doc.querySelectorAll('#model-list button')].some((b) => b.textContent === 'Use this model'));
});

test('disk usage is hidden when everything is zero', async () => {
  const api = window.pywebview.api;
  const orig = api.disk_usage;
  api.disk_usage = async () => ({ ok: true, models: 0, cache: 0, runtime: 0, logs: 0, free: 5e9 });
  $('rt-recheck').click();
  assert.ok(await until(() => $('disk-bar').hidden));
  assert.match($('disk-legend').textContent, /Nothing stored yet/);
  api.disk_usage = orig;
  $('rt-recheck').click();
  assert.ok(await until(() => !$('disk-bar').hidden));
});

// ------------------------------------------------------- downloads and drawer ----

test('a failed download shows a friendly reason with the raw error in a title; Clear finished clears it', async () => {
  assert.equal($('dl-clear').hidden, true);
  window.__mock.emit({ type: 'download.progress', group_id: 'dl_fail', repo_id: 'OpenVINO/Broken-int4-ov', status: 'error',
    downloaded_bytes: 10, total_bytes: 100, error: 'getaddrinfo ENOTFOUND huggingface.co' });
  assert.ok(await until(() => doc.querySelector('.dl-row')));
  const row = doc.querySelector('.dl-row[data-id="dl_fail"]');
  const text = row.querySelector('.dl-bottom span');
  assert.match(text.textContent, /^The connection to Hugging Face failed\./);
  assert.equal(text.title, 'getaddrinfo ENOTFOUND huggingface.co');
  assert.equal($('dl-clear').hidden, false);
  $('dl-clear').click();
  assert.equal(doc.querySelector('.dl-row[data-id="dl_fail"]'), null);
  assert.equal($('dl-clear').hidden, true);
  assert.equal($('download-empty').hidden, false);
});

test('a model rated Avoid shows a danger callout and asks before downloading', async () => {
  const api = window.pywebview.api;
  const row = [...doc.querySelectorAll('#search-results .result-row')].find((r) => /Avoid/.test(r.textContent) && !/Installed/.test(r.textContent));
  row.querySelector('button').click();
  assert.ok(await until(() => $('drawer-download')));
  assert.ok($('drawer-body').querySelector('.ui-callout--danger'));
  assert.equal($('drawer-download').textContent, 'Download anyway');
  const count = async () => (await api.list_downloads()).downloads.length;
  const before = await count();
  confirms.length = 0;
  confirmAnswer = false;
  $('drawer-download').click();
  await tick();
  assert.match(confirms[0].title, /rated Avoid/);
  assert.equal(confirms[0].danger, true);
  assert.equal(await count(), before, 'declined: nothing was downloaded');
  confirmAnswer = true;
  $('drawer-close').click();
});

// ---------------------------------------------------------------- first run ----

test('first run: a checklist with one-click recommended download; Cancel asks first', async () => {
  const st = window.__mock.state();
  st.models = [];
  st.runtimeInstalled = false;
  st.downloads = {};
  $('tab-models').click();
  $('rt-recheck').click();
  assert.ok(await until(() => !$('firstrun').hidden), 'the checklist appears');
  assert.equal($('fr-step-1').dataset.state, 'current');
  assert.equal($('fr-step-2').dataset.state, 'todo');
  assert.equal($('fr-step-3').dataset.state, 'todo');
  assert.equal($('fr-load').disabled, true);
  assert.match($('fr-text-3').textContent, /Waiting: install the runtime and download a model first/);
  assert.equal($('fr-download').textContent, 'Download recommended model');
  assert.match($('fr-progress').textContent, /0 of 3/);

  $('fr-download').click();
  assert.ok(await until(() => doc.querySelector('.dl-row')), 'the download started');
  assert.match(doc.querySelector('.dl-row').textContent, /Qwen2\.5-1\.5B-Instruct-int4-ov/);
  assert.equal($('fr-download').disabled, true);
  // Cancel asks, and "Keep downloading" keeps it.
  confirms.length = 0;
  confirmAnswer = false;
  [...doc.querySelectorAll('.dl-row button')].find((b) => b.textContent === 'Cancel').click();
  await tick(); await tick();
  assert.equal(confirms[0].message, 'Cancel and delete the partial download?');
  assert.equal(confirms[0].danger, true);
  assert.match(doc.querySelector('.dl-row .ui-badge').textContent, /Downloading/);
  confirmAnswer = true;

  $('fr-install').click();
  const long = async (fn, ms = 20000) => { for (let i = 0; i < ms / 100 && !fn(); i++) await sleep(100); return fn(); };
  assert.ok(await long(() => $('fr-step-1').dataset.state === 'done'), 'runtime installed');
  assert.ok(await long(() => $('fr-step-2').dataset.state === 'done'), 'model downloaded');
  assert.equal($('fr-step-3').dataset.state, 'current');
  assert.equal($('fr-load').disabled, false);
  assert.match($('fr-progress').textContent, /2 of 3/);
  $('fr-load').click();
  assert.ok(await long(() => /Compiling|Starting|Loading onto/.test($('fr-text-3').textContent)), $('fr-text-3').textContent);
  assert.ok(await long(() => $('firstrun').hidden, 30000), 'the checklist goes away once the model is loaded');
  assert.ok(toasts().some((t) => /model is loaded/.test(t.textContent)));
});

test('vcredist null (not applicable) hides the Visual C++ row; false still warns', async () => {
  const api = window.pywebview.api;
  const orig = api.runtime_status;
  const row = () => $('rt-vc').closest('div');
  $('tab-models').click();
  assert.equal(row().hidden, false);
  api.runtime_status = async () => ({ ok: true, installed: true, version: '2026.4.0', vcredist: null, platform: 'linux' });
  $('rt-recheck').click();
  assert.ok(await until(() => row().hidden));
  assert.equal($('rt-vc-help').hidden, true);
  assert.doesNotMatch($('rt-msg').textContent, /Visual C\+\+/);
  api.runtime_status = async () => ({ ok: true, installed: true, version: '2026.4.0', vcredist: false, platform: 'win32' });
  $('rt-recheck').click();
  assert.ok(await until(() => !row().hidden && !$('rt-vc-help').hidden));
  assert.equal($('rt-vc').textContent, 'Missing');
  api.runtime_status = orig;
  $('rt-recheck').click();
  assert.ok(await until(() => $('rt-vc-help').hidden));
});
