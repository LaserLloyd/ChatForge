// JS <-> Python bridge (PLAN 1.5).
//
//   import { api, on, off, ready } from './bridge.js';
//   const state = await api.call('get_state');
//   on('chat.delta', (evt) => ...);
//
// Under pywebview, api.call(name, ...args) forwards to window.pywebview.api[name] and
// resolves to the JSON result ({ok: true, ...} or {ok: false, error: {...}}). In a plain
// browser (no window.pywebview) dev-mock.js is loaded instead and implements every method.
// Python pushes events by calling window.__chatforge.emit(evt); handlers registered with on()
// receive the event object. on('*', fn) receives every event.
//
// Inside the real app shell the mock is never used: on a cold start the bridge can be
// injected late, and the popup must not quietly run against fake data. The shell is WebView2
// (Windows), WebKitGTK (Linux GTK: pywebview registers the `jsBridge` message handler before
// the page runs, although window.pywebview itself arrives only after the load) or Qt
// WebEngine, plus any page that already has a window.pywebview. bridge.js keeps waiting
// and emits {type: 'bridge.unavailable'} once after CONNECT_TIMEOUT_MS so the page can say
// it is not connected; calls made meanwhile resolve once the bridge arrives.

const handlers = new Map();   // type -> Set<fn>
let readyPromise = null;

const MOCK_WAIT_MS = 1500;          // plain browser: how long to wait before using the mock
const CONNECT_TIMEOUT_MS = 15000;   // real shell: when to report that the bridge is missing
const POLL_MS = 250;

function dispatch(evt) {
  if (!evt || typeof evt.type !== 'string') return;
  run(handlers.get(evt.type), evt);
  run(handlers.get('*'), evt);
}

// Snapshot first, so a handler that adds or removes handlers does not change who runs in
// this dispatch. chat.delta fires once per streamed token: no key array per event, and a
// single handler (the usual case) is called without copying the set.
function run(set, evt) {
  if (!set || set.size === 0) return;
  if (set.size === 1) { call(set.values().next().value, evt); return; }
  for (const fn of [...set]) call(fn, evt);
}

function call(fn, evt) {
  try { fn(evt); } catch (e) { console.error('[bridge] handler for', evt.type, 'failed:', e); }
}

// Installed at module evaluation so events emitted before the first on() are not lost
// to a missing global. Python's run_js guards with `window.__chatforge &&`.
window.__chatforge = { emit: dispatch };

export function on(type, fn) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(fn);
  return () => off(type, fn);
}

export function off(type, fn) {
  const set = handlers.get(type);
  if (set) set.delete(fn);
}

/** Feed an event in from JS (used by dev-mock.js and tests). */
export function emit(evt) { dispatch(evt); }

const hasBridge = () => !!(window.pywebview && window.pywebview.api);

/** True inside the real app shell: WebView2, WebKitGTK or Qt WebEngine under pywebview
 *  (a plain browser has none of these, and no window.pywebview). */
function inAppShell() {
  const w = window;
  return !!(
    (w.chrome && w.chrome.webview) ||                                        // WebView2
    (w.webkit && w.webkit.messageHandlers && w.webkit.messageHandlers.jsBridge) || // WebKitGTK
    (w.qt && w.qt.webChannelTransport) ||                                    // Qt WebEngine
    w.pywebview                                                              // injected, api pending
  );
}

function pywebviewReady() {
  return new Promise((resolve) => {
    if (hasBridge()) { resolve(true); return; }
    let done = false;
    const timers = [];
    const finish = (v) => {
      if (done) return;
      done = true;
      for (const t of timers) { clearTimeout(t); clearInterval(t); }
      resolve(v);
    };
    window.addEventListener('pywebviewready', () => finish(true), { once: true });
    if (inAppShell()) {
      // The real app: wait for the bridge however long it takes. Poll as well, in case
      // pywebviewready fired before this listener was added.
      timers.push(setInterval(() => { if (hasBridge()) finish(true); }, POLL_MS));
      timers.push(setTimeout(() => { if (!done) dispatch({ type: 'bridge.unavailable' }); }, CONNECT_TIMEOUT_MS));
      return;
    }
    // Plain browser: no pywebview will ever appear. Give an injected bridge a moment.
    timers.push(setTimeout(() => finish(hasBridge()), MOCK_WAIT_MS));
  });
}

/** Resolves once the backend (pywebview or dev-mock) can be called. Returns true if real. */
export function ready() {
  if (!readyPromise) {
    readyPromise = (async () => {
      const real = await pywebviewReady();
      if (real) return true;
      const mock = await import('./dev-mock.js');
      mock.install({ emit: dispatch });
      console.info('[bridge] no pywebview: using dev-mock.js');
      return false;
    })();
  }
  return readyPromise;
}

function errorResult(code, message) {
  return { ok: false, error: { code, message, hint: '', action: null } };
}

export const api = {
  /** Call a Python Api method; always resolves to a JSON object, never rejects. */
  async call(name, ...args) {
    await ready();
    try {
      const fn = window.pywebview && window.pywebview.api && window.pywebview.api[name];
      if (typeof fn !== 'function') return errorResult('server', `Unknown bridge method: ${name}`);
      const res = await fn(...args);
      return res == null ? { ok: true } : res;
    } catch (e) {
      return errorResult('server', String((e && e.message) || e));
    }
  },
};
