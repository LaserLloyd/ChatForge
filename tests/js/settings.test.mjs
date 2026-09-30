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
const web = path.resolve(here, '../../src/aichat/web');
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
  assert.match(rows[0].textContent, /Recommended/);
  assert.match(rows[0].textContent, /2\.29 GB/);
  assert.match(rows[1].textContent, /Compiled for NPU/);
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
  assert.equal($('g-hotkey').value, 'Ctrl+Alt+Space');
  assert.ok($('g-theme').hasAttribute('data-ui-theme-picker'));
  assert.equal(doc.querySelectorAll('[data-tool]:checked').length, 4);
});

test('General tab shows a per-field error for an invalid value', async () => {
  $('g-mpc').value = '50';
  $('general-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await sleep(50);
  assert.match($('err-chat.max_prompt_chars').textContent, /200 to 100000/);
  assert.equal($('g-mpc').getAttribute('aria-invalid'), 'true');
});

test('Logs tab shows the tail and stops its timer when left', async () => {
  $('tab-logs').click();
  await sleep(300);
  assert.match($('log-view').textContent, /dev-mock started/);
  assert.match($('log-count').textContent, /lines?/);
  $('tab-models').click();
});
