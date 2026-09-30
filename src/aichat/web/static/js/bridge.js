// JS <-> Python bridge (PLAN 1.5).
//
//   import { api, on, off, ready } from './bridge.js';
//   const state = await api.call('get_state');
//   on('chat.delta', (evt) => ...);
//
// Under pywebview, api.call(name, ...args) forwards to window.pywebview.api[name] and
// resolves to the JSON result ({ok: true, ...} or {ok: false, error: {...}}). In a plain
// browser (no window.pywebview) dev-mock.js is loaded instead and implements every method.
// Python pushes events by calling window.__aichat.emit(evt); handlers registered with on()
// receive the event object. on('*', fn) receives every event.

const handlers = new Map();   // type -> Set<fn>
let readyPromise = null;

function dispatch(evt) {
  if (!evt || typeof evt.type !== 'string') return;
  for (const key of [evt.type, '*']) {
    const set = handlers.get(key);
    if (!set) continue;
    for (const fn of [...set]) {
      try { fn(evt); } catch (e) { console.error('[bridge] handler for', evt.type, 'failed:', e); }
    }
  }
}

// Installed at module evaluation so events emitted before the first on() are not lost
// to a missing global. Python's run_js guards with `window.__aichat &&`.
window.__aichat = { emit: dispatch };

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

function pywebviewReady() {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) { resolve(true); return; }
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    window.addEventListener('pywebviewready', () => finish(true), { once: true });
    // Plain browser: no pywebview will ever appear. Give an injected bridge a moment
    // (it is injected before page scripts run in WebView2, so this is generous).
    setTimeout(() => finish(!!(window.pywebview && window.pywebview.api)), 1500);
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
