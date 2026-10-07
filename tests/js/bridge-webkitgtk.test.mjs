// bridge.js under pywebview's GTK backend (WebKitGTK): the page has no chrome.webview, but
// pywebview registers the `jsBridge` script message handler before the page runs, and
// window.pywebview arrives only after the load. The bridge must wait for it, never fall back
// to dev-mock.js (fake data). Its own file, so bridge.js is evaluated fresh with these globals.
import test, { mock } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', '..', 'src', 'chatforge', 'web', 'static', 'js');
const { JSDOM } = createRequire(import.meta.url)('jsdom');

const win = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://127.0.0.1:8765/index.html' }).window;
win.webkit = { messageHandlers: { jsBridge: { postMessage() {} } } };
Object.assign(globalThis, { window: win, document: win.document, location: win.location, sessionStorage: win.sessionStorage });

const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

test('under WebKitGTK the bridge is waited for, never replaced by the dev mock', async () => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  try {
    const { ready, api, on } = await import(pathToFileURL(join(JS, 'bridge.js')).href);
    const notices = [];
    on('bridge.unavailable', (e) => notices.push(e.type));
    let real = null;
    ready().then((v) => { real = v; });
    let state = null;
    api.call('get_state').then((r) => { state = r; });

    mock.timers.tick(2000);   // past the plain-browser wait
    await settle();
    assert.equal(real, null, 'ready() resolved without the bridge');
    assert.equal(win.pywebview, undefined, 'the dev mock was installed under WebKitGTK');

    // pywebview injects window.pywebview after the load; the api follows (the poll sees it).
    win.pywebview = { api: { get_state: async () => ({ ok: true, real: true }) } };
    mock.timers.tick(300);
    await settle();
    assert.equal(real, true);
    assert.deepEqual(state, { ok: true, real: true });
    assert.deepEqual(notices, []);
  } finally {
    mock.timers.reset();
  }
});
