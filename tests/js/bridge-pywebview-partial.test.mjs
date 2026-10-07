// A page that already has window.pywebview (no api yet, no chrome.webview, no WebKit handler)
// is the real shell too: it waits for the `pywebviewready` event instead of using the mock,
// (a plain browser, with none of the markers, still gets dev-mock.js: see bridge.test.mjs).
import test, { mock } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', '..', 'src', 'chatforge', 'web', 'static', 'js');
const { JSDOM } = createRequire(import.meta.url)('jsdom');

const win = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://127.0.0.1:8765/index.html' }).window;
win.pywebview = { token: 'x' };   // injected, api pending
Object.assign(globalThis, { window: win, document: win.document, location: win.location, sessionStorage: win.sessionStorage });

const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

test('a page with window.pywebview but no api yet waits for pywebviewready', async () => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  try {
    const { ready } = await import(pathToFileURL(join(JS, 'bridge.js')).href);
    let real = null;
    ready().then((v) => { real = v; });
    mock.timers.tick(5000);
    await settle();
    assert.equal(real, null);
    win.pywebview.api = { get_state: async () => ({ ok: true }) };
    win.dispatchEvent(new win.Event('pywebviewready'));
    await settle();
    assert.equal(real, true);
  } finally {
    mock.timers.reset();
  }
});
