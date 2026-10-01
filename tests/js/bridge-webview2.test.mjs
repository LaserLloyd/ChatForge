// bridge.js inside WebView2 (window.chrome.webview present): a late bridge must never be
// replaced by dev-mock.js. Its own file, so bridge.js is evaluated fresh with chrome.webview
// set. Timers are mocked: the 15 s "not connected" notice is checked without waiting.
import test, { mock } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', '..', 'src', 'aichat', 'web', 'static', 'js');
const { JSDOM } = createRequire(import.meta.url)('jsdom');

const win = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://aichat.localhost/index.html' }).window;
win.chrome = { webview: { postMessage() {} } };
Object.assign(globalThis, { window: win, document: win.document, location: win.location, sessionStorage: win.sessionStorage });

const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

test('inside WebView2 a late bridge is waited for, never replaced by the dev mock', async () => {
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
    assert.equal(win.pywebview, undefined, 'the dev mock was installed inside WebView2');
    assert.deepEqual(notices, []);

    mock.timers.tick(13500);  // ~15 s: the page is told it is not connected
    await settle();
    assert.deepEqual(notices, ['bridge.unavailable']);
    assert.equal(real, null);

    // The bridge arrives late (a cold start): calls made while waiting now go through.
    win.pywebview = { api: { get_state: async () => ({ ok: true, real: true }) } };
    win.dispatchEvent(new win.Event('pywebviewready'));
    await settle();
    assert.equal(real, true);
    assert.deepEqual(state, { ok: true, real: true });
    mock.timers.tick(60000);
    await settle();
    assert.deepEqual(notices, ['bridge.unavailable'], 'the notice repeated after connecting');
  } finally {
    mock.timers.reset();
  }
});
