// Settings > General cards for add-on settings (extensions-ui.js): rendered from the
// declarative field list, saved through set_extension_settings, hidden when there is nothing.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', '..', 'src', 'chatforge', 'web', 'static', 'js');
const { JSDOM } = createRequire(import.meta.url)('jsdom');
const win = new JSDOM('<!doctype html><html><body><div id="ext-cards" hidden></div></body></html>', { url: 'http://127.0.0.1:8765/settings.html' }).window;
Object.assign(globalThis, { window: win, document: win.document, location: win.location, sessionStorage: win.sessionStorage });

const { renderExtensions } = await import(pathToFileURL(join(JS, 'extensions-ui.js')).href);

const VOICE = {
  name: 'voice', ok: true, version: '1.0.0', error: '',
  values: { enabled: false, rate: 1.0, engine: 'stock', device: '', threads: 4 },
  sections: [{
    extension: 'voice', id: 'main', title: 'Voice', description: 'Talk to ChatForge',
    fields: [
      { key: 'enabled', label: 'Voice on', type: 'bool', default: false, choices: [], min: null, max: null, help: '' },
      { key: 'rate', label: 'Speaking rate', type: 'float', default: 1.0, choices: [], min: 0.5, max: 2, help: 'Words per second, relative' },
      { key: 'engine', label: 'Engine', type: 'choice', default: 'stock', choices: ['stock', 'rig'], min: null, max: null, help: '' },
      { key: 'device', label: 'Microphone', type: 'text', default: '', choices: [], min: null, max: null, help: '' },
      { key: 'threads', label: 'Threads', type: 'int', default: 4, choices: [], min: 1, max: 32, help: '' },
    ],
  }],
};

function stub(extensions, saveReply = { ok: true, values: {} }) {
  const calls = [];
  return { calls, async call(name, ...args) {
    calls.push([name, ...args]);
    return name === 'get_extensions' ? { ok: true, extensions } : saveReply;
  } };
}

test('nothing installed: the container stays hidden', async () => {
  const box = document.getElementById('ext-cards');
  await renderExtensions(stub([]), box);
  assert.equal(box.hidden, true);
  assert.equal(box.childElementCount, 0);
});

test('a section becomes a card with a control per field, typed and filled from the values', async () => {
  const box = document.getElementById('ext-cards');
  await renderExtensions(stub([VOICE]), box);
  assert.equal(box.hidden, false);
  const card = box.querySelector('.ext-card');
  assert.equal(card.querySelector('.ui-card__title').textContent, 'Voice');
  const by = (k) => card.querySelector(`[data-key="${k}"]`);
  assert.equal(by('enabled').type, 'checkbox');
  assert.equal(by('enabled').checked, false);
  assert.equal(by('rate').type, 'number');
  assert.equal(by('rate').min, '0.5');
  assert.equal(by('rate').max, '2');
  assert.equal(by('engine').tagName, 'SELECT');
  assert.deepEqual([...by('engine').options].map((o) => o.value), ['stock', 'rig']);
  assert.equal(by('threads').value, '4');
  assert.equal(card.querySelectorAll('label').length, 5);
  assert.ok([...card.querySelectorAll('label')].every((l) => document.getElementById(l.htmlFor)), 'every label points at its control');
});

test('Save sends typed values to set_extension_settings and says so', async () => {
  const box = document.getElementById('ext-cards');
  const api = stub([VOICE], { ok: true, values: { enabled: true } });
  const saved = [];
  await renderExtensions(api, box, (name, values) => saved.push([name, values]));
  const card = box.querySelector('.ext-card');
  card.querySelector('[data-key="enabled"]').checked = true;
  card.querySelector('[data-key="rate"]').value = '1.25';
  card.querySelector('[data-key="engine"]').value = 'rig';
  card.querySelector('[data-key="threads"]').value = '8';
  card.querySelector('button').click();
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(api.calls[1], ['set_extension_settings', 'voice', { enabled: true, rate: 1.25, engine: 'rig', device: '', threads: 8 }]);
  assert.match(card.querySelector('[role="status"]').textContent, /Saved/);
  assert.deepEqual(saved, [['voice', { enabled: true }]]);
});

test('a refused save shows Python\'s message', async () => {
  const box = document.getElementById('ext-cards');
  const api = stub([VOICE], { ok: false, error: { message: 'Speaking rate: at most 2' } });
  await renderExtensions(api, box);
  const card = box.querySelector('.ext-card');
  card.querySelector('button').click();
  await new Promise((r) => setTimeout(r, 5));
  assert.match(card.querySelector('[role="status"]').textContent, /at most 2/);
});

test('an add-on that failed to load is reported, not rendered', async () => {
  const box = document.getElementById('ext-cards');
  await renderExtensions(stub([{ name: 'broken', ok: false, error: 'No module named foo', sections: [], values: {} }]), box);
  assert.equal(box.hidden, false);
  assert.match(box.textContent, /"broken" could not be loaded: No module named foo/);
  assert.equal(box.querySelector('.ext-card'), null);
});

test('text from an add-on is never parsed as markup', async () => {
  const box = document.getElementById('ext-cards');
  const evil = JSON.parse(JSON.stringify(VOICE));
  evil.sections[0].title = '<img src=x onerror=alert(1)>';
  evil.sections[0].fields[0].label = '<b>bold</b>';
  await renderExtensions(stub([evil]), box);
  assert.equal(box.querySelector('img'), null);
  assert.equal(box.querySelector('b'), null);
  assert.equal(box.querySelector('.ui-card__title').textContent, '<img src=x onerror=alert(1)>');
});
