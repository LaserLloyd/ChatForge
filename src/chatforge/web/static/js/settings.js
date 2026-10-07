// Settings window (PLAN 2 WS8b). Four tabs: Models, Providers, General, Logs.
//
// Consumes bridge.js (api.call / on / ready), util.js (el) and the ui-theme runtime.
// No innerHTML anywhere: all text goes through el() / textContent. API keys are read from
// the input, cleared from the field, and passed straight to the bridge. They are never held
// in module state and never logged.

import { el } from './util.js';
import { api, on, ready } from './bridge.js';

const $ = (id) => document.getElementById(id);
const REGION_URLS = {
  international: 'https://api.minimax.io/v1',
  china: 'https://api.minimaxi.com/v1',
};
const REGION_LABELS = [['international', 'International'], ['china', 'China'], ['custom', 'Custom URL']];
const BADGES = {
  recommended: ['Recommended', 'ok'],
  supported: ['Supported', 'info'],
  untested: ['Untested', 'warn'],
  avoid: ['Avoid', 'bad'],
};
const ID_RE = /^[a-z][a-z0-9-]{1,31}$/;
// config.py LocalCfg.max_prompt_len
const MPL_MIN = 1024;
const MPL_MAX = 8192;
const ENV_RE = /^[A-Z][A-Z0-9_]{0,63}$/;
const ACTIVE_DL = new Set(['downloading', 'queued', 'starting', 'verifying', 'running', 'active']);
const RESUMABLE_DL = new Set(['paused', 'cancelled', 'canceled', 'error', 'failed']);

const S = {
  cfg: null,                 // full config from get_settings
  views: [],                 // provider views from list_providers
  selected: { provider: 'local-npu', model: null },
  runtime: { state: 'unloaded', model_id: null },
  rtInfo: null,              // runtime_status()
  models: [],
  installedIds: new Set(),
  tab: 'models',
  generalDirty: false,
  searchToken: 0,
  drawerToken: 0,
  confirmDelete: null,
  installing: false,
  tabScroll: {},             // tab -> window.scrollY when it was left
  recommended: null,         // catalog's recommended model id (via search_models)
};

// ------------------------------------------------------------------ helpers ----

function fmtBytes(n) {
  if (n == null || !isFinite(n)) return '-';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let v = Number(n);
  while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(v >= 10 ? 1 : 2)} ${u[i]}`;
}
function fmtSpeed(bps) { return bps > 0 ? `${fmtBytes(bps)}/s` : '-'; }
function fmtEta(s) {
  if (s == null || !isFinite(s) || s <= 0) return '-';
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
function fmtNum(n) { return n == null ? '-' : Number(n).toLocaleString('en-US'); }
function fmtAgo(ts) {
  if (!ts) return 'never used';
  const d = Date.now() / 1000 - ts;
  if (d < 90) return 'used just now';
  if (d < 5400) return `used ${Math.round(d / 60)} min ago`;
  if (d < 129600) return `used ${Math.round(d / 3600)} h ago`;
  return `used ${Math.round(d / 86400)} days ago`;
}
function fmtDate(iso) {
  const t = Date.parse(iso || '');
  return Number.isNaN(t) ? '' : new Date(t).toLocaleDateString('en-CA');
}
const isOk = (res) => Array.isArray(res) || (res && res.ok !== false);
function pick(res, key) {
  if (Array.isArray(res)) return res;
  if (!res) return [];
  return res[key] || res.results || res.items || [];
}
function errText(res) {
  const e = res && res.error;
  if (!e) return 'Something went wrong.';
  if (typeof e === 'string') return e;
  return e.hint ? `${e.message} ${e.hint}` : (e.message || 'Something went wrong.');
}
const cssEsc = (v) => ((window.CSS && window.CSS.escape) ? window.CSS.escape(String(v)) : String(v).replace(/["\\]/g, '\\$&'));
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function shortName(id) { return String(id).split('/').pop(); }
function fmtDur(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  return s < 90 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s`;
}

/** Unsaved edits on the General tab: the marker beside Save, Revert, the leave warning. */
function setDirty(flag) {
  S.generalDirty = !!flag;
  $('g-dirty').hidden = !flag;
  $('g-revert').disabled = !flag;
  $('tab-general').dataset.dirty = flag ? 'true' : 'false';
}

const TOAST_KINDS = { ok: 'success', success: 'success', warn: 'warning', bad: 'danger', danger: 'danger', info: 'info' };
const TOAST_MS = 4000;
/** A toast. Errors ("bad") stay until dismissed and are announced as alerts; the rest
 *  hide after a few seconds, and the timer waits while the pointer or focus is on them.
 *  The node comes from the bundle's UIComponents.toast() when it is loaded. */
function toast(msg, kind = 'info') {
  const variant = TOAST_KINDS[kind] || 'info';
  const sticky = variant === 'danger';
  const UC = window.UIComponents;
  const host = $('toast');
  let node;
  if (UC && typeof UC.toast === 'function') {
    node = UC.toast(msg, { kind: variant, timeout: 0 });
  } else {
    host.hidden = false;
    node = el('div', { class: `ui-toast ui-toast--${variant}`, text: msg });
    host.append(node);
  }
  node.dataset.kind = variant;
  node.classList.add('s-toast');
  node.setAttribute('role', sticky ? 'alert' : 'status');
  let timer = null;
  const dismiss = () => {
    clearTimeout(timer);
    node.remove();
    if (host && !host.querySelector('.ui-toast')) host.hidden = true;
  };
  const arm = () => { clearTimeout(timer); if (!sticky) timer = setTimeout(dismiss, TOAST_MS); };
  const hold = () => clearTimeout(timer);
  node.append(el('button', { type: 'button', class: 'ui-btn ui-btn--sm ui-btn--ghost ui-btn--icon toast-close', 'aria-label': 'Dismiss', text: '\u00d7', onclick: dismiss }));
  node.addEventListener('mouseenter', hold);
  node.addEventListener('mouseleave', arm);
  node.addEventListener('focusin', hold);
  node.addEventListener('focusout', arm);
  arm();
  return node;
}

/** The bundle's themed confirm dialog, or the browser's when it is not loaded. */
async function confirmAction({ title, message, confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false }) {
  const UC = window.UIComponents;
  if (UC && typeof UC.confirm === 'function') return UC.confirm({ title, message, confirmLabel, cancelLabel, danger });
  return window.confirm(`${title ? `${title}\n\n` : ''}${message}`);
}

/** The reply to a save carried key_removed: the stored key was for the old address. */
function noteKeyRemoved(res) {
  if (!res || !res.key_removed) return false;
  toast('Key removed: the address changed. Enter the key again.', 'warn');
  refreshKeyStatuses();
  return true;
}

// Badge kinds used in this file -> ThemeForge .ui-badge variants (neutral is the default).
const BADGE_VARIANT = { ok: 'success', info: 'info', warn: 'warning', bad: 'danger', accent: 'accent', neutral: '' };
function badgeClass(kind = 'neutral') {
  const v = BADGE_VARIANT[kind];
  return v ? `ui-badge ui-badge--${v}` : 'ui-badge';
}
function chip(text, kind = 'neutral') { return el('span', { class: badgeClass(kind), text }); }
function badgeChip(badge) {
  const b = BADGES[badge];
  return b ? chip(b[0], b[1]) : null;
}
function btn(text, opts = {}) {
  const { onclick, cls = '', ...attrs } = opts;
  const b = el('button', { type: 'button', class: `ui-btn ui-btn--sm ${cls}`.trim(), text, ...attrs });
  if (onclick) b.addEventListener('click', onclick);
  return b;
}
function progressBar(label) {
  // A native <progress>: it has the progressbar role and exposes value/max itself.
  const bar = el('progress', { class: 'ui-progress', max: '100', value: '0', 'aria-label': label });
  bar.set = (pct) => { bar.value = Math.max(0, Math.min(100, pct)); };
  return bar;
}
function legacyCopy(text) {
  // Fallback for WebView2 contexts where navigator.clipboard is unavailable or denied.
  const ta = el('textarea', { 'aria-hidden': 'true', tabindex: '-1', readonly: '' });
  ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;';
  ta.value = text;
  const prev = document.activeElement;
  document.body.append(ta);
  ta.select();
  let ok_ = false;
  try { ok_ = document.execCommand('copy'); } catch { ok_ = false; }
  ta.remove();
  if (prev && prev.focus) prev.focus();
  return ok_;
}
async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  }
  return legacyCopy(text);
}
function localProviderId() {
  const v = S.views.find((p) => p.kind === 'ovms');
  return v ? v.id : 'local-npu';
}

// ------------------------------------------------------------------- errors ----

function clearErrors(scope = document) {
  for (const p of scope.querySelectorAll('.ui-error')) p.textContent = '';
  for (const i of scope.querySelectorAll('[aria-invalid]')) i.removeAttribute('aria-invalid');
}
function setError(key, msg) {
  const p = $(`err-${key}`);
  if (p) p.textContent = msg || '';
  const input = document.querySelector(`[data-field="${key}"]`) || $(key);
  if (input) { if (msg) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid'); }
}
/** Show a {field: message} map. Returns messages for fields with no slot on screen. */
function showFieldErrors(errors) {
  const leftover = [];
  for (const [k, v] of Object.entries(errors || {})) {
    if ($(`err-${k}`)) setError(k, v); else leftover.push(`${k}: ${v}`);
  }
  return leftover;
}

// --------------------------------------------------------------------- tabs ----

const TAB_IDS = ['models', 'providers', 'general', 'logs'];

/** Show/Hide toggle state of a key field's button (text, label and pressed state). */
function setEye(eye, show) {
  eye.setAttribute('aria-pressed', String(show));
  eye.setAttribute('aria-label', show ? 'Hide API key' : 'Show API key');
  eye.textContent = show ? 'Hide' : 'Show';
}
/** Mask every key field again. The text stays, so a typed key survives a tab switch. */
function maskKeyInputs() {
  for (const i of document.querySelectorAll('input[data-key-input]')) {
    i.type = 'password';
    const eye = i.parentElement && i.parentElement.querySelector('[data-eye]');
    if (eye) setEye(eye, false);
  }
}
/** Wipe every key field (the window is going away). */
function clearKeyInputs() {
  for (const i of document.querySelectorAll('input[data-key-input]')) i.value = '';
  maskKeyInputs();
}

function restoreScroll(name) {
  const y = S.tabScroll[name] || 0;
  try { window.scrollTo(0, y); } catch { /* no layout */ }
}

function selectTab(name, focus = false) {
  if (!TAB_IDS.includes(name)) return;
  maskKeyInputs();
  if (name !== S.tab) {
    S.tabScroll[S.tab] = window.scrollY || 0;
    stopHotkeyRecording();
  }
  S.tab = name;
  for (const t of TAB_IDS) {
    const tab = $(`tab-${t}`);
    const on_ = t === name;
    tab.setAttribute('aria-selected', String(on_));
    tab.tabIndex = on_ ? 0 : -1;
    $(`panel-${t}`).hidden = !on_;
    if (on_ && focus) tab.focus({ preventScroll: true });
  }
  restoreScroll(name);
  if (name === 'logs') { refreshLogs(); syncLogTimer(); } else { syncLogTimer(); }
  if (name === 'models') { loadModels().then(() => restoreScroll(name)); loadDisk(); refreshRuntime(); }
  if (name === 'providers') loadProviders().then(() => restoreScroll(name));
  if (name === 'general') { refreshAutostart(); refreshFallbackWarning(); }
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => { if (S.tab === name) restoreScroll(name); });
}

function initTabs() {
  const list = document.querySelector('[role="tablist"]');
  for (const t of TAB_IDS) $(`tab-${t}`).addEventListener('click', () => selectTab(t));
  list.addEventListener('keydown', (e) => {
    const i = TAB_IDS.indexOf(S.tab);
    let n = null;
    if (e.key === 'ArrowRight') n = (i + 1) % TAB_IDS.length;
    else if (e.key === 'ArrowLeft') n = (i - 1 + TAB_IDS.length) % TAB_IDS.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = TAB_IDS.length - 1;
    if (n == null) return;
    e.preventDefault();
    selectTab(TAB_IDS[n], true);
  });
}

// ------------------------------------------------------------------- models ----

async function loadModels() {
  const res = await api.call('list_models');
  if (!isOk(res)) { toast(errText(res), 'bad'); return; }
  S.models = pick(res, 'models');
  S.modelsLoaded = true;
  S.installedIds = new Set(S.models.map((m) => m.id));
  renderModels();
}

function rtStateFor(id) {
  const rt = S.runtime || {};
  if (rt.model_id !== id) return 'idle';
  if (rt.state === 'ready') return 'loaded';
  if (rt.state === 'starting' || rt.state === 'compiling') return 'loading';
  if (rt.state === 'unloading') return 'unloading';
  return 'idle';
}

function renderModels() {
  const list = $('model-list');
  const focusKey = document.activeElement && list.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
  clear(list);
  $('model-empty').hidden = S.models.length > 0;
  const device = (S.cfg && S.cfg.local && S.cfg.local.device) || 'NPU';
  const active = S.selected.model;
  $('active-note').textContent = active ? `Active model: ${shortName(active)}` : 'No active model';
  const installed = !S.rtInfo || S.rtInfo.installed !== false;

  for (const m of S.models) {
    const cat = m.catalog || null;
    const badge = cat && cat.npu ? cat.npu : 'untested';
    const rt = rtStateFor(m.id);
    const isActive = active === m.id && S.selected.provider === localProviderId();
    const compiled = !!(m.compiled && m.compiled[device]);
    const key = (a) => `${m.id}:${a}`;
    const name = shortName(m.id);
    const whyId = `why-${m.id.replace(/[^\w-]/g, '_')}`;

    const title = el('div', { class: 'm-title' }, [
      el('strong', { class: 'm-name', text: (cat && cat.label) || name }),
      badgeChip(badge),
      isActive ? chip('Active', 'accent') : null,
      rt === 'loaded' ? chip('Loaded', 'ok') : null,
      rt === 'loading' ? chip('Loading', 'warn') : null,
      !m.complete ? chip('Incomplete', 'bad') : null,
    ]);
    const sub = el('div', { class: 'm-sub ui-text-secondary' }, [
      el('span', { class: 'ui-mono ui-small', text: m.id }),
      el('span', { class: 'ui-num', text: fmtBytes(m.size_bytes) }),
      el('span', { class: compiled ? 'compiled-yes' : 'ui-text-tertiary', text: compiled ? `Compiled for ${device} ✓` : `Not compiled for ${device} yet` }),
      el('span', { class: 'ui-text-tertiary', text: fmtAgo(m.last_used_at) }),
    ]);
    const notes = [];
    if (cat && cat.note) notes.push(el('div', { class: 'm-note ui-text-tertiary', text: cat.note }));
    if (!m.complete) {
      notes.push(el('div', { class: 'm-note danger-text', text: `Missing files: ${(m.missing || []).join(', ') || 'unknown'}. Delete it and download again.` }));
    }
    if (rt === 'loading') {
      notes.push(el('div', { class: 'm-load', 'data-load': m.id }, [
        el('progress', { class: 'ui-progress', max: '100', 'aria-label': `Loading ${name}` }),
        el('span', { class: 'load-text ui-small ui-num ui-text-secondary', role: 'status' }),
      ]));
    }

    let actions;
    let why = null;
    if (S.confirmDelete === m.id) {
      actions = el('div', { class: 'm-actions confirm', role: 'group', 'aria-label': `Confirm deleting ${name}` }, [
        el('span', { class: 'danger-text ui-small', text: `Delete ${name} and its files?` }),
        btn('Delete model', { cls: 'ui-btn--danger', 'data-fk': key('confirm'), onclick: () => doDelete(m.id) }),
        btn('Cancel', { 'data-fk': key('cancel'), onclick: () => { S.confirmDelete = null; renderModels(); focusFk(key('delete')); } }),
      ]);
    } else {
      // Why "Use this model" is disabled, said under the buttons instead of in a tooltip.
      if (rt !== 'loaded' && rt !== 'loading') {
        if (!installed) why = 'The runtime is not installed yet. Install it first (Local runtime, below).';
        else if (!m.complete) why = 'Some files are missing. Delete it and download again.';
      }
      const blocked = rt === 'loading' || (rt !== 'loaded' && !!why);
      actions = el('div', { class: 'm-actions' }, [
        rt === 'loaded'
          ? btn('Unload', { 'data-fk': key('unload'), 'aria-label': `Unload ${name}`, onclick: () => doUnload() })
          : btn(rt === 'loading' ? 'Loading…' : 'Use this model', {
            cls: 'ui-btn--primary',
            'data-fk': key('load'),
            'aria-label': rt === 'loading' ? `Loading ${name}` : `Use ${name}: make it the active model and load it`,
            disabled: blocked ? '' : null,
            'aria-describedby': why ? whyId : null,
            onclick: () => doLoad(m.id),
          }),
        btn('Clear cache', { cls: 'ui-btn--ghost', 'data-fk': key('cache'), 'aria-label': `Clear compile cache for ${name}`, disabled: rt === 'loaded' ? '' : null, onclick: () => doClearCache(m.id) }),
        btn('Delete model', { cls: 'ui-btn--danger', 'data-fk': key('delete'), 'aria-label': `Delete ${name}`, onclick: () => { S.confirmDelete = m.id; renderModels(); focusFk(key('confirm')); } }),
      ]);
    }
    const side = el('div', { class: 'm-side' }, [actions, why ? el('p', { class: 'm-why ui-small ui-text-secondary', id: whyId, text: why }) : null]);
    list.append(el('li', { class: 'model-row ui-well', 'data-id': m.id }, [el('div', { class: 'm-main' }, [title, sub, ...notes]), side]));
  }
  if (focusKey) focusFk(focusKey);
  paintLoad();
  renderFirstRun();
}

/** Elapsed / expected seconds of the load in progress, from runtime.status (updated in place). */
function loadLine() {
  const rt = S.runtime || {};
  const exp = Number(rt.expected_s) || 0;
  const done = Number(rt.elapsed_s) || 0;
  const dev = rt.device || 'NPU';
  const what = rt.state === 'compiling'
    ? (rt.first_compile ? `Compiling for ${dev}, the first time for this model` : `Loading onto ${dev}`)
    : 'Starting the runtime';
  let text = `${what}: ${fmtDur(done)}`;
  if (exp) text += done > exp ? `, longer than the expected ${fmtDur(exp)}` : ` of about ${fmtDur(exp)}`;
  return { text, pct: exp ? Math.min(100, (done / exp) * 100) : null };
}
function paintLoad() {
  const rt = S.runtime || {};
  const line = loadLine();
  for (const box of document.querySelectorAll('[data-load]')) {
    if (box.dataset.load !== rt.model_id) continue;
    const bar = box.querySelector('progress');
    if (line.pct == null) bar.removeAttribute('value'); else bar.value = line.pct;
    box.querySelector('.load-text').textContent = line.text;
  }
  if (!$('firstrun').hidden) paintFirstRunLoad(line);
}

function focusFk(key) {
  const node = document.querySelector(`[data-fk="${cssEsc(key)}"]`);
  if (node && !node.disabled) node.focus();
}

/** "Use this model": select it, then load it (doLoad selects first). */
async function doLoad(id) {
  if (S.selected.model !== id || S.selected.provider !== localProviderId()) {
    const r = await api.call('select_model', localProviderId(), id);
    if (!isOk(r)) { toast(errText(r), 'bad'); return; }
    S.selected = { provider: localProviderId(), model: id };
  }
  const res = await api.call('load_model');
  if (!isOk(res)) { toast(errText(res), 'bad'); return; }
  // load_model's own runtime.status events may already have arrived; keep their numbers.
  if (!(S.runtime.model_id === id && ['starting', 'compiling'].includes(S.runtime.state))) {
    S.runtime = { ...S.runtime, state: 'starting', model_id: id, elapsed_s: 0, expected_s: 0 };
  }
  renderModels();
  toast('Loading the model. The first NPU compile can take several minutes.');
}
async function doUnload() {
  const res = await api.call('unload_model');
  if (!isOk(res)) { toast(errText(res), 'bad'); return; }
  toast('Model unloaded.');
}
async function doClearCache(id) {
  const res = await api.call('clear_compile_cache', id);
  if (!isOk(res)) { toast(errText(res), 'bad'); return; }
  toast('Compile cache cleared. The next load compiles again.');
  await loadModels(); loadDisk();
}
async function doDelete(id) {
  const res = await api.call('delete_model', id);
  S.confirmDelete = null;
  if (!isOk(res)) { toast(errText(res), 'bad'); renderModels(); return; }
  toast(`${shortName(id)} deleted.`);
  await loadModels(); loadDisk();
  const first = document.querySelector('#model-list .ui-btn');
  if (first) first.focus(); else $('search-q').focus();
}

// --------------------------------------------------------------- disk usage ----

async function loadDisk() {
  const res = await api.call('disk_usage');
  const bar = $('disk-bar'); const legend = $('disk-legend');
  clear(bar); clear(legend);
  if (!isOk(res)) { bar.hidden = true; $('disk').hidden = true; return; }
  const parts = [['models', 'Models'], ['cache', 'Compile cache'], ['runtime', 'Runtime'], ['logs', 'Logs']];
  const used = parts.reduce((n, [k]) => n + (Number(res[k]) || 0), 0);
  // Nothing stored yet: no bar of empty segments, only the free space.
  $('disk').hidden = used === 0 && !(Number(res.free) > 0);
  bar.hidden = used === 0;
  if (used === 0) {
    legend.append(el('li', {}, [el('span', { text: 'Nothing stored yet. Free on drive: ' }), el('b', { class: 'ui-num', text: fmtBytes(res.free) })]));
    return;
  }
  parts.forEach(([k, label], i) => {
    const v = Number(res[k]) || 0;
    const seg = el('i', { class: `seg seg-${i + 1}` });
    seg.style.width = used ? `${(v / used) * 100}%` : '0%';
    bar.append(seg);
    legend.append(el('li', {}, [el('span', { class: `swatch seg-${i + 1}`, 'aria-hidden': 'true' }), el('span', { text: `${label}: ` }), el('b', { class: 'ui-num', text: fmtBytes(v) })]));
  });
  legend.append(el('li', {}, [el('span', { class: 'swatch swatch-free', 'aria-hidden': 'true' }), el('span', { text: 'Free on drive: ' }), el('b', { class: 'ui-num', text: fmtBytes(res.free) })]));
  bar.setAttribute('aria-label', `ChatForge uses ${fmtBytes(used)}; ${fmtBytes(res.free)} free on the drive`);
}

// ------------------------------------------------------------------ runtime ----

async function refreshRuntime() {
  const res = await api.call('runtime_status');
  if (!isOk(res)) { $('rt-ovms').textContent = errText(res); return; }
  S.rtInfo = res;
  const installed = !!res.installed;
  $('rt-ovms').textContent = installed ? `Installed, version ${res.version || 'unknown'}${res.variant ? ` (${res.variant})` : ''}` : 'Not installed';
  $('rt-ovms').className = installed ? 'ok-text' : 'ui-text-secondary';
  // vcredist: true / false on Windows; null where it does not apply (hide the row, no warning).
  const vc = res.vcredist !== false;
  $('rt-vc').closest('div').hidden = res.vcredist == null;
  $('rt-vc').textContent = vc ? 'Installed' : 'Missing';
  $('rt-vc').className = vc ? 'ok-text' : 'danger-text';
  $('rt-vc-help').hidden = vc;
  const b = $('rt-install');
  b.hidden = installed;
  b.disabled = S.installing || !vc;
  b.title = !vc ? 'Install the Visual C++ runtime first' : '';
  renderModels();
}

/** The "Check again" button: re-read the runtime, rescan the models folder and disk usage,
 *  and say what was found (a re-check that changes nothing must still visibly finish). */
async function recheckAll() {
  const b = $('rt-recheck');
  const msg = $('rt-msg');
  b.disabled = true;
  b.textContent = 'Checking…';
  msg.className = 'ui-text-secondary ui-small';
  msg.textContent = '';
  try {
    await Promise.all([refreshRuntime(), loadModels(), loadDisk()]);
    const rt = S.rtInfo || {};
    const models = Array.isArray(S.models) ? S.models.length : null;
    const bits = [
      rt.installed ? `OVMS ${rt.version || ''} installed`.replace('  ', ' ') : 'OVMS not installed',
      rt.vcredist === false ? 'Visual C++ runtime missing' : (rt.vcredist == null ? null : 'Visual C++ runtime OK'),
    ].filter(Boolean);
    if (models != null) bits.push(`${models} model${models === 1 ? '' : 's'} found`);
    const at = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    msg.textContent = `Checked at ${at}: ${bits.join(' · ')}.`;
    msg.className = rt.installed && rt.vcredist !== false ? 'ok-text ui-small' : 'ui-text-secondary ui-small';
  } catch (err) {
    msg.textContent = `Check failed: ${(err && err.message) || err}`;
    msg.className = 'danger-text ui-small';
  } finally {
    b.disabled = false;
    b.textContent = 'Check again';
  }
}

function onRuntimeInstall(evt) {
  const wrap = $('rt-progress'); const bar = $('rt-bar'); const text = $('rt-progress-text');
  const status = evt.status || 'downloading';
  wrap.hidden = false;
  const total = Number(evt.total_bytes) || 0;
  const done = Number(evt.downloaded_bytes) || 0;
  const pct = total ? (done / total) * 100 : 0;
  bar.value = pct;
  if (status === 'error') {
    S.installing = false;
    S.installNote = `Install failed: ${evt.error || 'unknown error'}`;
    text.textContent = S.installNote;
    text.className = 'danger-text ui-small';
    $('rt-install').disabled = false;
    renderFirstRun();
    return;
  }
  text.className = 'ui-text-secondary ui-small ui-num';
  if (status === 'done') {
    S.installing = false;
    S.installNote = '';
    text.textContent = 'OVMS installed.';
    refreshRuntime().then(() => setTimeout(() => { if (!S.installing) $('rt-progress').hidden = true; }, 3000));
    loadDisk();
    return;
  }
  S.installing = true;
  $('rt-install').disabled = true;
  const label = status === 'downloading' ? 'Downloading' : status.charAt(0).toUpperCase() + status.slice(1);
  text.textContent = total
    ? `${label}: ${fmtBytes(done)} of ${fmtBytes(total)} (${Math.round(pct)}%), ${fmtSpeed(evt.speed_bps)}, ${fmtEta(evt.eta_s)} left`
    : `${label}…`;
  S.installNote = text.textContent;
  renderFirstRun();
}

async function installRuntime() {
  S.installing = true;
  $('rt-install').disabled = true;
  $('rt-progress').hidden = false;
  $('rt-progress-text').textContent = 'Starting…';
  const res = await api.call('runtime_install');
  if (!isOk(res)) {
    S.installing = false;
    $('rt-install').disabled = false;
    $('rt-progress-text').textContent = errText(res);
    $('rt-progress-text').className = 'danger-text ui-small';
    S.installNote = errText(res);
    renderFirstRun();
  }
}

// --------------------------------------------------------------- first run ----
// A checklist at the top of Models while the runtime or every model is missing:
// 1 install the runtime, 2 download the recommended model, 3 load it.

/** The catalog's recommended model: the search results carry it as badge "recommended". */
async function findRecommended() {
  if (S.recommended) return S.recommended;
  const local = S.models.find((m) => m.catalog && m.catalog.npu === 'recommended');
  if (local) { S.recommended = local.id; return S.recommended; }
  const res = await api.call('search_models', '', authorName());
  const hit = isOk(res) ? pick(res, 'results').find((r) => r.badge === 'recommended') : null;
  if (hit) S.recommended = hit.id;
  return S.recommended;
}

function activeDownload() {
  return [...dlRows.values()].find((r) => ACTIVE_DL.has(r.status)) || null;
}

function setStep(n, state, text) {
  const li = $(`fr-step-${n}`);
  li.dataset.state = state;
  if (state === 'current') li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
  li.querySelector('.step-no').textContent = state === 'done' ? '✓' : String(n);
  $(`fr-text-${n}`).textContent = text;
  $(`fr-text-${n}`).className = `${state === 'error' ? 'danger-text' : 'ui-text-secondary'} ui-small ui-num`;
}

function paintFirstRunLoad(line) {
  if (S.runtime && S.runtime.state !== 'ready' && ['starting', 'compiling'].includes(S.runtime.state)) {
    $('fr-text-3').textContent = line.text;
  }
}

function renderFirstRun() {
  const card = $('firstrun');
  if (!S.rtInfo || !S.modelsLoaded) { card.hidden = true; return; }
  const rt = S.rtInfo;
  const installed = rt.installed !== false;
  const vc = rt.vcredist !== false;
  const done = S.models.filter((m) => m.complete);
  const runState = (S.runtime && S.runtime.state) || 'unloaded';
  const ready = runState === 'ready';
  const loading = runState === 'starting' || runState === 'compiling';
  if (!installed || !done.length) S.firstRun = true;
  const show = !!S.firstRun && !ready;
  if (S.firstRun && ready) {
    S.firstRun = false;
    toast('The model is loaded. You can chat now.', 'ok');
  }
  card.hidden = !show;
  if (!show) return;

  const dl = activeDownload();
  const failed = [...dlRows.values()].find((r) => r.status === 'error' || r.status === 'failed');
  // Step 1
  if (installed) setStep(1, 'done', `The runtime${rt.version ? ` (version ${rt.version})` : ''} is installed.`);
  else if (!vc) setStep(1, 'current', 'The Visual C++ runtime is missing, so it cannot be installed yet. Install it first (see Local runtime, below).');
  else setStep(1, 'current', S.installing || S.installNote ? (S.installNote || 'Starting…') : 'A one-time download. Nothing is sent from your PC.');
  $('fr-install').hidden = installed;
  $('fr-install').disabled = S.installing || !vc;
  // Step 2
  const step2 = 'current';
  if (done.length) setStep(2, 'done', `${shortName(done[0].id)} is downloaded.`);
  else if (dl) {
    const d = dl.last || {};
    const pct = d.total_bytes ? Math.round((d.downloaded_bytes / d.total_bytes) * 100) : 0;
    setStep(2, step2, `Downloading ${shortName(dl.repo)}: ${pct}%${ACTIVE_DL.has(dl.status) && d.eta_s ? `, ${fmtEta(d.eta_s)} left` : ''}`);
  } else if (failed) setStep(2, 'error', `The last download stopped: ${friendlyDlError((failed.last || {}).error)} Press the button to try again.`);
  else setStep(2, step2, 'About 1 GB, downloaded once from Hugging Face. It runs on your NPU.');
  $('fr-download').hidden = done.length > 0;
  $('fr-download').disabled = !!dl;
  $('fr-download').textContent = dl ? 'Downloading…' : 'Download recommended model';
  // Step 3
  const ok3 = installed && done.length > 0;
  if (ready) setStep(3, 'done', 'The model is loaded.');
  else if (loading) setStep(3, 'current', loadLine().text);
  else if (ok3) setStep(3, 'current', 'Loads the model onto the NPU. The first time takes a few minutes.');
  else {
    const need = [!installed && 'install the runtime', !done.length && 'download a model'].filter(Boolean);
    setStep(3, 'todo', `Waiting: ${need.join(' and ')} first.`);
  }
  $('fr-load').disabled = !ok3 || loading;
  $('fr-load').hidden = ready;
  $('fr-load').textContent = loading ? 'Loading…' : 'Load the model';
  const doneCount = (installed ? 1 : 0) + (done.length ? 1 : 0) + (ready ? 1 : 0);
  $('fr-progress').textContent = `${doneCount} of 3 done`;
  // The first step that is not done is the current one.
  const first = [installed, done.length > 0, ready].findIndex((v) => !v);
  for (let n = 1; n <= 3; n++) {
    const li = $(`fr-step-${n}`);
    if (li.dataset.state !== 'done' && li.dataset.state !== 'error') {
      li.dataset.state = n - 1 === first ? 'current' : 'todo';
      if (n - 1 === first) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
    }
  }
}

async function downloadRecommended() {
  const msg = $('fr-msg');
  msg.className = 'ui-small ui-text-secondary';
  msg.textContent = 'Looking up the recommended model…';
  $('fr-download').disabled = true;
  const id = await findRecommended();
  if (!id) {
    msg.className = 'ui-small danger-text';
    msg.textContent = 'Could not find the recommended model. Check your internet connection and try again, or search below.';
    $('fr-download').disabled = false;
    return;
  }
  const r = await api.call('start_download', id);
  if (!isOk(r)) {
    msg.className = 'ui-small danger-text';
    msg.textContent = errText(r);
    $('fr-download').disabled = false;
    return;
  }
  msg.textContent = '';
  await refreshDownloads();
  renderFirstRun();
}

async function loadFirstModel() {
  const ready = S.models.filter((m) => m.complete);
  const pickM = ready.find((m) => m.id === S.selected.model) || ready.find((m) => m.catalog && m.catalog.npu === 'recommended') || ready[0];
  if (pickM) await doLoad(pickM.id);
}

// ---------------------------------------------------------------- downloads ----

const dlRows = new Map();   // id -> { li, refs, status, repo, last }
const FINISHED_DL = new Set(['done', 'error', 'failed']);

/** A plain-language reason for a failed download; the raw text goes in a title. */
function friendlyDlError(raw) {
  const e = String(raw || '');
  if (/space|disk|ENOSPC|write/i.test(e)) return 'There is not enough free disk space.';
  if (/401|403|gated|token|denied|forbidden/i.test(e)) return 'Hugging Face refused the download.';
  if (/time.?out|connect|network|resolve|getaddrinfo|ssl|reset|unreachable|offline/i.test(e)) return 'The connection to Hugging Face failed.';
  if (/sha|hash|checksum|verif|corrupt/i.test(e)) return 'A downloaded file did not match its checksum.';
  return 'The download stopped.';
}
function syncClearButton() {
  $('dl-clear').hidden = ![...dlRows.values()].some((r) => FINISHED_DL.has(r.status));
}

function dlId(d) { return d.group_id || d.download_id || d.id; }

function upsertDownload(d) {
  const id = dlId(d);
  if (!id) return;
  // A resumed download comes back under a new id: drop the stale row for the same repo.
  for (const [oid, row] of dlRows) {
    if (oid !== id && row.repo === d.repo_id && !ACTIVE_DL.has(row.status)) removeDlRow(oid);
  }
  let row = dlRows.get(id);
  const status = d.status || 'downloading';
  if (!row) {
    row = buildDlRow(id, d);
    dlRows.set(id, row);
    $('download-list').prepend(row.li);
  }
  row.repo = d.repo_id;
  row.last = d;
  if (row.status !== status) { row.status = status; rebuildDlActions(row, id, d); }
  updateDlRow(row, d);
  $('download-empty').hidden = dlRows.size > 0;
  syncClearButton();
  renderFirstRun();
}
function removeDlRow(id) {
  const row = dlRows.get(id);
  if (row) { row.li.remove(); dlRows.delete(id); }
  $('download-empty').hidden = dlRows.size > 0;
  syncClearButton();
}
function clearFinishedDownloads() {
  for (const [id, row] of [...dlRows]) if (FINISHED_DL.has(row.status)) removeDlRow(id);
  renderFirstRun();
}
function buildDlRow(id, d) {
  const bar = progressBar(`Download of ${d.repo_id}`);
  const refs = {
    status: el('span', { class: 'ui-badge' }),
    text: el('span', { class: 'ui-text-secondary ui-small ui-num' }),
    bar,
    actions: el('div', { class: 'dl-actions' }),
  };
  const li = el('li', { class: 'dl-row ui-well', 'data-id': id }, [
    el('div', { class: 'dl-top' }, [el('strong', { class: 'ui-mono ui-small', text: d.repo_id }), refs.status]),
    bar,
    el('div', { class: 'dl-bottom' }, [refs.text, refs.actions]),
  ]);
  return { li, refs, status: null, repo: d.repo_id };
}
function rebuildDlActions(row, id, d) {
  clear(row.refs.actions);
  const status = row.status;
  const name = shortName(d.repo_id);
  if (ACTIVE_DL.has(status)) {
    row.refs.actions.append(btn('Cancel', { 'aria-label': `Cancel download of ${name}`, onclick: async () => {
      const yes = await confirmAction({
        title: 'Cancel the download?', message: 'Cancel and delete the partial download?',
        confirmLabel: 'Cancel and delete', cancelLabel: 'Keep downloading', danger: true,
      });
      if (!yes || !ACTIVE_DL.has(row.status)) return;
      const r = await api.call('cancel_download', id);
      if (!isOk(r)) toast(errText(r), 'bad');
    } }));
  } else if (RESUMABLE_DL.has(status)) {
    row.refs.actions.append(btn('Resume', { cls: 'ui-btn--primary', 'aria-label': `Resume download of ${name}`, onclick: async () => {
      const r = await api.call('resume_download', id);
      if (!isOk(r)) toast(errText(r), 'bad'); else refreshDownloads();
    } }));
  }
  const kind = status === 'done' ? 'ok' : (status === 'error' || status === 'failed') ? 'bad' : ACTIVE_DL.has(status) ? 'info' : 'neutral';
  row.refs.status.className = badgeClass(kind);
  const labels = { downloading: 'Downloading', done: 'Done', cancelled: 'Cancelled', canceled: 'Cancelled', paused: 'Paused', error: 'Error', failed: 'Failed', queued: 'Queued' };
  row.refs.status.textContent = labels[status] || status;
}
function updateDlRow(row, d) {
  const total = Number(d.total_bytes) || 0;
  const done = Number(d.downloaded_bytes) || 0;
  const pct = total ? (done / total) * 100 : (row.status === 'done' ? 100 : 0);
  row.refs.bar.set(pct);
  let txt;
  if (row.status === 'done') txt = `${fmtBytes(total || done)} downloaded`;
  else if (d.error) txt = `${friendlyDlError(d.error)} Press Resume to try again.`;
  else {
    txt = `${fmtBytes(done)} of ${fmtBytes(total)} (${Math.round(pct)}%)`;
    if (ACTIVE_DL.has(row.status)) txt += `, ${fmtSpeed(d.speed_bps)}, ${fmtEta(d.eta_s)} left`;
    if (d.files_total) txt += `, file ${Math.min((d.files_done || 0) + 1, d.files_total)} of ${d.files_total}`;
  }
  row.refs.text.textContent = txt;
  if (d.error) row.refs.text.title = String(d.error); else row.refs.text.removeAttribute('title');
  row.refs.text.className = `${d.error ? 'danger-text' : 'ui-text-secondary'} ui-small ui-num`;
}
async function refreshDownloads() {
  const res = await api.call('list_downloads');
  if (!isOk(res)) return;
  for (const d of pick(res, 'downloads')) upsertDownload(d);
}
const doneSeen = new Set();
function onDownloadProgress(evt) {
  upsertDownload(evt);
  if (evt.status === 'done' && !doneSeen.has(dlId(evt))) {
    doneSeen.add(dlId(evt));
    toast(`${shortName(evt.repo_id)} downloaded.`);
    loadModels(); loadDisk();
    updateSearchInstalled();
  }
}

// ------------------------------------------------------------------- search ----

function authorName() { return (S.cfg && S.cfg.hf && S.cfg.hf.default_author) || 'OpenVINO'; }

async function doSearch() {
  const token = ++S.searchToken;
  const q = $('search-q').value.trim();
  const author = $('search-author').checked ? authorName() : null;
  $('search-status').textContent = 'Searching…';
  const res = await api.call('search_models', q, author);
  if (token !== S.searchToken) return;
  const list = $('search-results');
  clear(list);
  if (!isOk(res)) { $('search-status').textContent = errText(res); return; }
  const items = pick(res, 'results');
  const rec = items.find((r) => r.badge === 'recommended');
  if (rec && !S.recommended) S.recommended = rec.id;
  $('search-status').textContent = items.length
    ? `${items.length} result${items.length === 1 ? '' : 's'}${author ? ` from ${author}` : ''}`
    : 'No matching models. Try another search, or turn off the author filter.';
  for (const r of items) list.append(resultRow(r));
}

function resultRow(r) {
  const installed = S.installedIds.has(r.id);
  const badge = badgeChip(r.badge || 'untested');
  const meta = [`${fmtNum(r.downloads)} downloads`, `${fmtNum(r.likes)} likes`];
  const d = fmtDate(r.last_modified); if (d) meta.push(`updated ${d}`);
  const detailsBtn = btn('Details', { 'data-repo': r.id, 'aria-label': `Details for ${r.id}`, onclick: (e) => openDrawer(r, e.currentTarget) });
  return el('li', { class: 'result-row ui-well', 'data-id': r.id }, [
    el('div', { class: 'm-main' }, [
      el('div', { class: 'm-title' }, [el('strong', { class: 'ui-mono ui-small', text: r.id }), badge, installed ? chip('Installed', 'ok') : null]),
      el('div', { class: 'm-sub ui-text-secondary', text: meta.join(' · ') }),
      r.note ? el('div', { class: 'm-note ui-text-tertiary', text: r.note }) : null,
    ]),
    el('div', { class: 'm-actions' }, [detailsBtn]),
  ]);
}
function updateSearchInstalled() {
  for (const li of document.querySelectorAll('#search-results .result-row')) {
    const id = li.dataset.id; const title = li.querySelector('.m-title');
    const has = title.querySelector('.installed-badge');
    if (S.installedIds.has(id) && !has) { const c = chip('Installed', 'ok'); c.classList.add('installed-badge'); title.append(c); }
  }
}

let drawerTrigger = null;
async function openDrawer(item, trigger) {
  drawerTrigger = trigger || null;
  const token = ++S.drawerToken;
  const drawer = $('drawer'); const body = $('drawer-body');
  $('drawer-title').textContent = item.id;
  clear(body);
  body.append(el('p', { class: 'ui-text-secondary', text: 'Loading details…' }));
  drawer.hidden = false;
  drawer.focus();
  const res = await api.call('repo_details', item.id);
  if (token !== S.drawerToken) return;
  clear(body);
  if (!isOk(res)) { body.append(el('p', { class: 'danger-text', role: 'alert', text: errText(res) })); return; }
  const files = res.files || [];
  const fits = res.fits_disk !== false;
  const installed = S.installedIds.has(item.id);
  const activeDl = [...dlRows.values()].some((r) => r.repo === item.id && ACTIVE_DL.has(r.status));
  const avoid = item.badge === 'avoid';
  body.append(
    el('div', { class: 'm-title' }, [badgeChip(item.badge || 'untested'), installed ? chip('Installed', 'ok') : null]),
    avoid ? el('div', { class: 'ui-callout ui-callout--danger', role: 'alert' }, [
      el('strong', { class: 'ui-callout__title', text: 'Rated Avoid for this PC' }),
      el('span', { text: item.note || 'This model is known not to work well on the NPU.' }),
    ]) : null,
    !avoid && item.note ? el('p', { class: 'ui-text-secondary', text: item.note }) : null,
    el('dl', { class: 'kv' }, [
      el('div', {}, [el('dt', { text: 'Total download' }), el('dd', { class: 'ui-num', text: fmtBytes(res.total_bytes) })]),
      el('div', {}, [el('dt', { text: 'Free disk space' }), el('dd', { class: 'ui-num', text: fmtBytes(res.free_bytes) })]),
      el('div', {}, [el('dt', { text: 'Fits on disk' }), el('dd', { class: fits ? 'ok-text' : 'danger-text', text: fits ? 'Yes' : 'No, free up space first' })]),
      el('div', {}, [el('dt', { text: 'Files' }), el('dd', { class: 'ui-num', text: String(files.length) })]),
    ]),
  );
  if (files.length) {
    body.append(el('h3', { class: 'h3', text: 'Files' }),
      el('ul', { class: 'file-list' }, files.slice(0, 40).map((f) => el('li', {}, [el('span', { class: 'ui-mono', text: f.path }), el('span', { class: 'ui-text-tertiary ui-num', text: fmtBytes(f.size) })]))));
  }
  const dl = btn(installed ? 'Already installed' : activeDl ? 'Downloading' : avoid ? 'Download anyway' : 'Download', {
    cls: avoid ? 'ui-btn--danger' : 'ui-btn--primary', id: 'drawer-download', disabled: (!fits || installed || activeDl) ? '' : null,
    onclick: async () => {
      if (avoid) {
        const yes = await confirmAction({
          title: 'Download a model rated Avoid?',
          message: `${item.note || 'This model is known not to work well on the NPU.'} Download it anyway?`,
          confirmLabel: 'Download anyway', cancelLabel: 'Do not download', danger: true,
        });
        if (!yes) return;
      }
      $('drawer-download').disabled = true;
      const r = await api.call('start_download', item.id);
      if (!isOk(r)) { toast(errText(r), 'bad'); $('drawer-download').disabled = false; return; }
      toast(`Downloading ${shortName(item.id)}.`);
      closeDrawer();
      refreshDownloads();
    },
  });
  body.append(el('div', { class: 'ui-row drawer-actions' }, [dl]));
}
function closeDrawer() {
  S.drawerToken++;
  $('drawer').hidden = true;
  if (drawerTrigger && document.contains(drawerTrigger)) drawerTrigger.focus();
  drawerTrigger = null;
}

function initModels() {
  $('rt-install').addEventListener('click', installRuntime);
  $('rt-recheck').addEventListener('click', recheckAll);
  $('search-form').addEventListener('submit', (e) => { e.preventDefault(); doSearch(); });
  $('search-author').addEventListener('change', doSearch);
  let t = null;
  $('search-q').addEventListener('input', () => { clearTimeout(t); t = setTimeout(doSearch, 450); });
  $('drawer-close').addEventListener('click', closeDrawer);
  $('dl-clear').addEventListener('click', clearFinishedDownloads);
  $('fr-install').addEventListener('click', installRuntime);
  $('fr-download').addEventListener('click', downloadRecommended);
  $('fr-load').addEventListener('click', loadFirstModel);
  $('drawer').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); closeDrawer(); } });
  $('rt-vc-copy').addEventListener('click', async () => { toast((await copyText($('rt-vc-cmd').textContent)) ? 'Command copied.' : 'Could not copy. Select the command and press Ctrl+C.'); });
}

// ---------------------------------------------------------------- providers ----

async function loadSettings() {
  const res = await api.call('get_settings');
  if (isOk(res) && res.config) S.cfg = res.config;
  if (isOk(res) && res.quick_actions) QA.data = res.quick_actions;
  return res;
}

const remoteViews = () => S.views.filter((v) => v.kind !== 'ovms');

/** The cards are built once and then only refreshed in place, so a typed key and a test
 *  result survive a tab switch. `force` rebuilds them (a provider was added or removed). */
async function loadProviders(force = false) {
  const res = await api.call('list_providers');
  if (isOk(res)) S.views = pick(res, 'providers');
  const ids = remoteViews().map((v) => v.id);
  const same = !force && cards.size === ids.length && ids.every((id) => cards.has(id));
  if (same) {
    for (const v of remoteViews()) cards.get(v.id).setKey(v.key);
    refreshFallbackWarning();
    return;
  }
  await loadSettings();
  fillLocalCard();
  renderProviderCards();
  $('auto-refresh').checked = !(S.cfg && S.cfg.chat && S.cfg.chat.auto_refresh_models === false);
}

/** Pull the saved model lists into the cards that are already on screen. */
async function syncCards() {
  await loadSettings();
  const res = await api.call('list_providers');
  if (isOk(res)) S.views = pick(res, 'providers');
  const ids = remoteViews().map((v) => v.id);
  if (cards.size !== ids.length || !ids.every((id) => cards.has(id))) { await loadProviders(true); return; }
  for (const v of remoteViews()) cards.get(v.id).reload(((S.cfg && S.cfg.providers) || {})[v.id] || {}, v);
}

/** "Model list updated: 2 new (A, B), 1 removed (C)." for one refresh result. */
function refreshSummary(r) {
  if (!r.ok) return [r.error, r.hint].filter(Boolean).join(' ');
  const parts = [];
  const list = (a) => (a.length > 3 ? `${a.slice(0, 3).join(', ')} and ${a.length - 3} more` : a.join(', '));
  if (r.added && r.added.length) parts.push(`${r.added.length} new (${list(r.added)})`);
  if (r.removed && r.removed.length) parts.push(`${r.removed.length} removed (${list(r.removed)})`);
  const n = (r.models || []).length;
  return parts.length ? `Model list updated: ${parts.join(', ')}.` : `Up to date (${n} model${n === 1 ? '' : 's'}).`;
}

async function refreshAllModels() {
  const b = $('refresh-all'); const msg = $('refresh-msg');
  b.disabled = true; msg.className = 'ui-small ui-text-secondary'; msg.textContent = 'Reading the model lists…';
  const res = await api.call('refresh_models', null);
  b.disabled = false;
  if (!isOk(res)) { msg.className = 'ui-small danger-text'; msg.textContent = errText(res); return; }
  const results = res.results || {};
  const names = Object.fromEntries((S.views || []).map((v) => [v.id, v.display_name || v.id]));
  const lines = Object.entries(results).map(([id, r]) => `${names[id] || id}: ${refreshSummary(r)}`);
  msg.className = 'ui-small';
  msg.textContent = lines.length ? lines.join(' ') : 'No provider has a key yet, so there was nothing to refresh.';
  await syncCards();
}

function fillLocalCard() {
  const l = (S.cfg && S.cfg.local) || {};
  $('local-device').value = l.device || 'NPU';
  $('local-mpl').value = l.max_prompt_len != null ? l.max_prompt_len : '';
}

async function saveLocal() {
  clearErrors($('panel-providers'));
  const mpl = Number($('local-mpl').value);
  if (!Number.isInteger(mpl) || mpl < MPL_MIN || mpl > MPL_MAX) {
    setError('local.max_prompt_len', `Enter a whole number from ${MPL_MIN} to ${MPL_MAX}.`);
    $('local-mpl').closest('details').open = true;
    $('local-mpl').focus();
    return;
  }
  const res = await api.call('update_settings', { local: { device: $('local-device').value, max_prompt_len: mpl } });
  if (res && res.config) S.cfg = res.config;
  noteKeyRemoved(res);
  if (!isOk(res)) {
    const left = showFieldErrors(res.errors);
    $('local-msg').textContent = left.length ? left.join('; ') : errText(res);
    $('local-mpl').closest('details').open = true;
    return;
  }
  const restart = (res.restart_required || []).some((k) => k.startsWith('local.'));
  $('local-restart').hidden = !restart;
  $('local-msg').textContent = 'Saved.';
  fillLocalCard();
  loadModels();
}

function keyStatusText(key, envFallback) {
  if (!key || key.source === 'none') return key && key.required === false ? 'No key (optional for this server)' : 'No key';
  if (key.source === 'env') return `Using ${key.env_name || envFallback || 'the environment variable'} from the environment — it takes precedence over a saved key`;
  return 'Saved in Windows Credential Manager';
}

const cards = new Map();   // id -> { setKey }

function renderProviderCards() {
  const host = $('provider-cards');
  // Keys typed into a card that is about to be rebuilt are carried over.
  const typed = new Map([...host.querySelectorAll('.provider-card')].map((c) => [c.dataset.id, c.querySelector('input[data-key-input]').value]));
  clear(host); cards.clear();
  for (const v of S.views) {
    if (v.kind === 'ovms') continue;
    const spec = (S.cfg && S.cfg.providers && S.cfg.providers[v.id]) || {};
    host.append(providerCard(v, spec));
    const keep = typed.get(v.id);
    if (keep) host.querySelector(`.provider-card[data-id="${cssEsc(v.id)}"] input[data-key-input]`).value = keep;
  }
}

const clockNow = () => new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
/** Per-field error: the message in its own ui-error line, aria-invalid on the input. */
function fieldFail(input, errEl, message) {
  input.setAttribute('aria-invalid', 'true');
  errEl.textContent = message;
}
function fieldOk(input, errEl) {
  input.removeAttribute('aria-invalid');
  errEl.textContent = '';
}

const isLoopbackHost = (h) => ['localhost', '127.0.0.1', '::1', '[::1]'].includes(h) || h.endsWith('.localhost') || /^127\./.test(h);

function providerCard(v, spec) {
  const id = v.id;
  const uid = `pc-${id}`;
  const envName = spec.api_key_env || (v.key && v.key.env_name) || null;
  const region = spec.region || v.region || null;
  const models = [...(spec.models || v.models || [])];
  let currentModel = spec.default_model || v.default_model || models[0] || '';
  const builtin = !!(spec.builtin || v.builtin);
  const hasRegion = region != null;

  const msg = el('p', { class: 'ui-text-secondary ui-small', role: 'status', 'aria-live': 'polite', id: `${uid}-msg` });
  const keyLine = el('p', { class: 'key-status', id: `${uid}-keystatus`, role: 'status', 'aria-live': 'polite' });
  const testLine = el('p', { class: 'test-line ui-small', id: `${uid}-test`, role: 'status', 'aria-live': 'polite' });
  const fetched = el('div', { class: 'ui-row', hidden: '' });

  async function saveSpec(patch, okMsg = 'Saved.') {
    const next = { ...spec, ...patch, id, kind: spec.kind || 'openai' };
    delete next.builtin;
    if (!next.display_name) next.display_name = v.display_name || id;
    const res = await api.call('upsert_provider', next);
    if (!isOk(res)) { msg.textContent = errText(res); msg.className = 'danger-text ui-small'; return false; }
    noteKeyRemoved(res);
    Object.assign(spec, patch);
    if (S.cfg && S.cfg.providers) S.cfg.providers[id] = { ...(S.cfg.providers[id] || {}), ...patch };
    msg.textContent = okMsg; msg.className = 'ui-text-secondary ui-small';
    return true;
  }

  // Region -------------------------------------------------------------------
  let regionBlock = null;
  const urlInput = el('input', { id: `${uid}-url`, class: 'ui-input ui-mono', type: 'url', spellcheck: 'false', value: spec.base_url || v.base_url || '', 'aria-describedby': `${uid}-url-hint ${uid}-url-warn ${uid}-url-err` });
  const urlErr = el('p', { class: 'ui-error', id: `${uid}-url-err`, role: 'alert' });
  const urlWarn = el('p', { class: 'ui-help warn-text', id: `${uid}-url-warn`, hidden: '' });
  const urlField = el('div', { class: 'ui-field' }, [
    el('label', { class: 'ui-label', for: `${uid}-url`, text: 'Base URL' }),
    urlInput,
    el('p', { class: 'ui-help', id: `${uid}-url-hint`, text: 'An OpenAI-compatible endpoint, for example https://host/v1' }),
    urlWarn,
    urlErr,
  ]);
  /** Plain http to a host that is not this computer: the key and messages are not encrypted. */
  const paintUrlWarn = () => {
    let plain = false;
    try { const u = new URL(urlInput.value.trim()); plain = u.protocol === 'http:' && !isLoopbackHost(u.hostname.toLowerCase()); } catch { plain = false; }
    urlWarn.hidden = !plain;
    urlWarn.textContent = plain ? 'This address uses plain http, so your key and messages cross the network unencrypted. Use https unless the server is on your own network.' : '';
  };
  paintUrlWarn();
  const applyUrl = async () => {
    const val = urlInput.value.trim();
    let ok_ = false;
    try { const u = new URL(val); ok_ = u.protocol === 'https:' || u.protocol === 'http:'; } catch { ok_ = false; }
    if (!ok_) { fieldFail(urlInput, urlErr, 'Enter a full http(s) URL, for example https://host/v1.'); return; }
    fieldOk(urlInput, urlErr);
    if (val !== (spec.base_url || '')) await saveSpec({ base_url: val });
  };
  urlInput.addEventListener('input', () => { fieldOk(urlInput, urlErr); paintUrlWarn(); });
  urlInput.addEventListener('change', applyUrl);
  urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); applyUrl(); } });

  if (hasRegion) {
    const group = el('div', { class: 'seg-control', role: 'radiogroup', 'aria-labelledby': `${uid}-region-label` });
    for (const [val, label] of REGION_LABELS) {
      const input = el('input', { type: 'radio', name: `${uid}-region`, id: `${uid}-region-${val}`, value: val });
      if (val === region) input.checked = true;
      input.addEventListener('change', async () => {
        urlField.hidden = val !== 'custom';
        if (val !== 'custom') {
          const url = REGION_URLS[val];
          urlInput.value = url;
          await saveSpec({ region: val, base_url: url }, `Region set to ${label}.`);
        } else {
          await saveSpec({ region: 'custom' }, 'Enter the custom base URL below.');
          urlInput.focus();
        }
      });
      group.append(el('label', { class: 'seg-opt', for: input.id }, [input, el('span', { text: label })]));
    }
    regionBlock = el('div', { class: 'ui-field' }, [el('span', { class: 'ui-label', id: `${uid}-region-label`, text: 'Region' }), group]);
    urlField.hidden = region !== 'custom';
  }

  // Model --------------------------------------------------------------------
  const select = el('select', { id: `${uid}-model`, class: 'ui-select' });
  const CUSTOM = '__custom__';
  const custom = el('input', { id: `${uid}-model-custom`, class: 'ui-input ui-mono', type: 'text', spellcheck: 'false', autocomplete: 'off', placeholder: 'e.g. my-model-name', list: `${uid}-model-list` });
  const dlist = el('datalist', { id: `${uid}-model-list` });
  function fillModels() {
    clear(select); clear(dlist);
    for (const m of models) { select.append(el('option', { value: m, text: m })); dlist.append(el('option', { value: m })); }
    select.append(el('option', { value: CUSTOM, text: 'Custom (use the field below)' }));
    if (models.includes(currentModel)) { select.value = currentModel; custom.value = ''; }
    else if (currentModel) { select.value = CUSTOM; custom.value = currentModel; }
    else select.value = models[0] || CUSTOM;
  }
  fillModels();
  select.addEventListener('change', async () => {
    if (select.value === CUSTOM) { custom.focus(); return; }
    currentModel = select.value; custom.value = '';
    await saveSpec({ default_model: currentModel }, 'Model saved.');
  });
  const applyCustom = async () => {
    const val = custom.value.trim();
    if (!val) return;
    if (val.length > 200 || /\s/.test(val)) { msg.textContent = 'Model names cannot contain spaces.'; msg.className = 'danger-text ui-small'; return; }
    currentModel = val;
    const nextModels = models.includes(val) ? models : [...models, val];
    if (await saveSpec({ default_model: val, models: nextModels }, 'Model saved.')) { models.splice(0, models.length, ...nextModels); fillModels(); }
  };
  custom.addEventListener('change', applyCustom);
  custom.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); applyCustom(); } });

  // Context window and reply length --------------------------------------------
  const ctxInput = el('input', { id: `${uid}-ctx`, class: 'ui-input narrow', type: 'number', min: '2048', max: '10000000', step: '1024', inputmode: 'numeric', placeholder: 'Auto', value: spec.context_tokens != null ? String(spec.context_tokens) : '' });
  const ctxHint = el('p', { class: 'ui-help', id: `${uid}-ctx-hint` });
  const outInput = el('input', { id: `${uid}-out`, class: 'ui-input narrow ui-num', type: 'number', min: '256', max: '200000', step: '256', inputmode: 'numeric', value: String(spec.max_output_tokens || 2048), 'aria-describedby': `${uid}-out-err` });
  const ctxErr = el('p', { class: 'ui-error', id: `${uid}-ctx-err`, role: 'alert' });
  const outErr = el('p', { class: 'ui-error', id: `${uid}-out-err`, role: 'alert' });
  ctxInput.setAttribute('aria-describedby', `${uid}-ctx-hint ${uid}-ctx-err`);
  ctxInput.classList.add('ui-num');
  ctxInput.addEventListener('input', () => fieldOk(ctxInput, ctxErr));
  outInput.addEventListener('input', () => fieldOk(outInput, outErr));
  const fmtTokens = (n) => (n >= 1000000 ? `${(n / 1000000).toFixed(n % 1000000 ? 1 : 0)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));
  function paintCtxHint() {
    const reported = (spec.model_context || {})[currentModel];
    const set = spec.context_tokens;
    if (reported && set) ctxHint.textContent = `${shortName(currentModel)} reports ${fmtTokens(reported)} tokens; capped at ${fmtTokens(set)}.`;
    else if (reported) ctxHint.textContent = `${shortName(currentModel)} reports ${fmtTokens(reported)} tokens (from Refresh models).`;
    else if (set) ctxHint.textContent = `${fmtTokens(set)} tokens for every model of this provider.`;
    else ctxHint.textContent = 'Auto: 100k tokens unless the provider reports one (Refresh models).';
  }
  const shortName = (m) => String(m || 'This model').split('/').pop();
  paintCtxHint();
  ctxInput.addEventListener('change', async () => {
    const raw = ctxInput.value.trim();
    const n = raw === '' ? null : Number(raw);
    if (n !== null && (!Number.isInteger(n) || n < 2048 || n > 10000000)) { fieldFail(ctxInput, ctxErr, 'Enter a whole number of tokens from 2048 to 10000000, or leave it empty for Auto.'); return; }
    fieldOk(ctxInput, ctxErr);
    if (await saveSpec({ context_tokens: n }, n ? `Context window set to ${fmtTokens(n)} tokens.` : 'Context window set to Auto.')) paintCtxHint();
  });
  outInput.addEventListener('change', async () => {
    const n = Number(outInput.value);
    if (!Number.isInteger(n) || n < 256 || n > 200000) { fieldFail(outInput, outErr, 'Enter a whole number of tokens from 256 to 200000.'); return; }
    fieldOk(outInput, outErr);
    await saveSpec({ max_output_tokens: n }, `Replies can use up to ${fmtTokens(n)} tokens.`);
  });
  select.addEventListener('change', paintCtxHint);

  // Key ----------------------------------------------------------------------
  const keyInput = el('input', {
    id: `${uid}-key`, class: 'ui-input ui-mono', type: 'password', autocomplete: 'off', spellcheck: 'false',
    autocapitalize: 'none', 'data-key-input': '', placeholder: 'Paste your API key',
  });
  const eye = el('button', { type: 'button', class: 'ui-btn eye', 'data-eye': '' });
  setEye(eye, false);
  eye.addEventListener('click', () => {
    const show = keyInput.type === 'password';
    keyInput.type = show ? 'text' : 'password';
    setEye(eye, show);
  });
  const removeBtn = btn('Remove key', { cls: 'ui-btn--danger', 'aria-label': `Remove key for ${v.display_name || id}` });
  const setKey = (key) => {
    keyLine.textContent = keyStatusText(key, envName);
    keyLine.dataset.source = (key && key.source) || 'none';
    removeBtn.disabled = !(key && (key.source === 'keyring' || key.env_overrides_saved));
  };
  setKey(v.key);
  cards.set(id, {
    setKey,
    /** New saved spec (model lists) from outside the card, e.g. after "Refresh all models". */
    reload(ns, view) {
      Object.assign(spec, ns);
      models.splice(0, models.length, ...(ns.models || view.models || []));
      currentModel = ns.default_model || view.default_model || models[0] || '';
      fillModels();
      paintCtxHint();
      setKey(view.key);
    },
  });

  const takeKey = () => { const k = keyInput.value; keyInput.value = ''; keyInput.type = 'password'; setEye(eye, false); return k; };

  const showTest = (text, cls) => { testLine.textContent = text; testLine.className = `test-line ui-small ${cls}`; };
  /** Hand a typed key to the bridge. False (with the reason shown) when it was not saved. */
  async function saveTypedKey(k) {
    const res = await api.call('save_api_key', id, k);
    if (!isOk(res)) { showTest(errText(res), 'danger-text'); return false; }
    setKey(res.key);
    return true;
  }

  const saveBtn = btn('Save key', { 'aria-label': `Save key for ${v.display_name || id}`, title: 'Save the key without testing it' });
  saveBtn.addEventListener('click', async () => {
    const k = takeKey().trim();
    if (!k) { showTest('Type a key first.', 'danger-text'); keyInput.focus(); return; }
    saveBtn.disabled = true;
    const saved = await saveTypedKey(k);
    saveBtn.disabled = false;
    if (saved) showTest('Key saved.', 'ok-text');
  });
  // A key in the field is saved before the test (the inline key card's "Save & test"):
  // a key that was only tested was lost when Settings closed, after showing "Connected".
  // The test then checks the key the chat will use, saved or from the environment.
  const testBtn = btn('Save & test', { cls: 'ui-btn--primary', 'aria-label': `Save & test the connection to ${v.display_name || id}`, title: 'Saves the key you typed (if any), then tests the connection' });
  testBtn.addEventListener('click', async () => {
    const k = takeKey().trim();
    testBtn.disabled = true; saveBtn.disabled = true;
    const done = () => { testBtn.disabled = false; saveBtn.disabled = false; };
    if (k) {
      showTest('Saving the key…', 'ui-text-secondary');
      if (!(await saveTypedKey(k))) { done(); return; }
    }
    const savedNote = k ? 'Key saved. ' : '';
    showTest(`${savedNote}Testing…`, 'ui-text-secondary');
    const res = await api.call('test_provider', id, null, currentModel || null);
    done();
    fetched.hidden = true; clear(fetched);
    if (res && res.ok) {
      const list = res.models || [];
      const lat = res.latency_s != null ? ` in ${Number(res.latency_s).toFixed(2)} s` : '';
      showTest(`${savedNote}Connected${lat}. ${list.length ? `${list.length} model${list.length === 1 ? '' : 's'} available.` : ''} Checked at ${clockNow()}.`.replace(/\s+/g, ' ').trim(), 'ok-text');
      if (list.length) {
        for (const m of list) if (!dlist.querySelector(`option[value="${cssEsc(m)}"]`)) dlist.append(el('option', { value: m }));
        if (JSON.stringify(list) !== JSON.stringify(models)) {
          fetched.hidden = false;
          fetched.append(btn(`Use these ${list.length} models`, { onclick: async () => {
            const nextDefault = list.includes(currentModel) ? currentModel : list[0];
            if (await saveSpec({ models: list, default_model: nextDefault }, 'Model list updated.')) {
              models.splice(0, models.length, ...list); currentModel = nextDefault; fillModels();
              fetched.hidden = true; clear(fetched);
            }
          } }));
        }
      }
    } else {
      const err = (res && (res.error && typeof res.error === 'object' ? res.error.message : res.error)) || 'The test failed.';
      const hint = (res && (res.hint || (res.error && res.error.hint))) || '';
      showTest(`${k ? 'Key saved, but the test failed: ' : ''}${hint ? `${err} ${hint}` : err} Checked at ${clockNow()}.`, 'danger-text');
    }
  });
  const refreshBtn = btn('Refresh models', { 'aria-label': `Refresh models for ${v.display_name || id}` });
  refreshBtn.addEventListener('click', async () => {
    refreshBtn.disabled = true;
    testLine.textContent = 'Reading the model list…'; testLine.className = 'test-line ui-small ui-text-secondary';
    const res = await api.call('refresh_models', id);
    refreshBtn.disabled = false;
    const r = res && res.results && res.results[id];
    if (!isOk(res) || !r) { testLine.textContent = errText(res); testLine.className = 'test-line ui-small danger-text'; return; }
    testLine.textContent = refreshSummary(r);
    testLine.className = `test-line ui-small ${r.ok ? 'ok-text' : 'danger-text'}`;
    if (!r.ok) return;
    const view = (res.providers || []).find((p) => p.id === id) || {};
    models.splice(0, models.length, ...r.models);
    currentModel = view.default_model || (r.models.includes(currentModel) ? currentModel : r.models[0] || '');
    const modelContext = { ...(spec.model_context || {}), ...(r.contexts || {}) };
    // Which models see pictures, as the provider reported it (kept so a later save keeps it).
    const modelVision = { ...(spec.model_vision || {}), ...(r.vision || {}) };
    const refreshed = { models: [...r.models], default_model: currentModel, model_context: modelContext, model_vision: modelVision };
    Object.assign(spec, refreshed);
    if (S.cfg && S.cfg.providers) S.cfg.providers[id] = { ...(S.cfg.providers[id] || {}), ...refreshed };
    fillModels();
    paintCtxHint();
    fetched.hidden = true; clear(fetched);
  });
  removeBtn.addEventListener('click', async () => {
    takeKey();
    const res = await api.call('remove_api_key', id);
    if (!isOk(res)) { testLine.textContent = errText(res); testLine.className = 'test-line ui-small danger-text'; return; }
    setKey(res.key);
    testLine.textContent = 'Key removed.'; testLine.className = 'test-line ui-small ui-text-secondary';
  });
  // Enter saves the typed key and tests it; on an empty field it says to type a key first.
  keyInput.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (keyInput.value.trim()) testBtn.click(); else saveBtn.click();
  });

  // Remove provider (custom only) ----------------------------------------------
  let removeProvider = null;
  if (!builtin) {
    const holder = el('div', { class: 'ui-row remove-provider' });
    const askBtn = btn('Remove provider', { cls: 'ui-btn--danger' });
    const render = (confirm) => {
      clear(holder);
      if (!confirm) { holder.append(askBtn); return; }
      holder.append(
        el('span', { class: 'danger-text ui-small', text: `Remove ${v.display_name || id} and its saved key?` }),
        btn('Remove', { cls: 'ui-btn--danger', onclick: async () => {
          const res = await api.call('remove_provider', id);
          if (!isOk(res)) { msg.textContent = errText(res); msg.className = 'danger-text ui-small'; render(false); return; }
          toast(`${v.display_name || id} removed.`);
          await loadProviders(true);
          $('add-id').focus();
        } }),
        btn('Cancel', { onclick: () => { render(false); askBtn.focus(); } }),
      );
      holder.querySelector('button').focus();
    };
    askBtn.addEventListener('click', () => render(true));
    render(false);
    removeProvider = holder;
  }

  const headTitle = el('h3', { class: 'ui-card__title', id: `${uid}-title`, text: v.display_name || id });
  return el('section', { class: 'ui-card provider-card', 'data-id': id, 'aria-labelledby': `${uid}-title` }, [
    el('div', { class: 'ui-card__header' }, [
      headTitle,
      el('span', { class: 'ui-badge ui-mono', text: id }),
      chip(builtin ? 'Built in' : 'Custom', builtin ? 'neutral' : 'accent'),
    ]),
    regionBlock,
    urlField,
    el('div', { class: 'grid2' }, [
      el('div', { class: 'ui-field' }, [el('label', { class: 'ui-label', for: `${uid}-model`, text: 'Model' }), select]),
      el('div', { class: 'ui-field' }, [el('label', { class: 'ui-label', for: `${uid}-model-custom`, text: 'Custom model name' }), custom, dlist]),
    ]),
    el('div', { class: 'grid2' }, [
      el('div', { class: 'ui-field' }, [el('label', { class: 'ui-label', for: `${uid}-ctx`, text: 'Context window (tokens)' }), ctxInput, ctxHint, ctxErr]),
      el('div', { class: 'ui-field' }, [el('label', { class: 'ui-label', for: `${uid}-out`, text: 'Longest reply (tokens)' }), outInput, outErr]),
    ]),
    el('div', { class: 'ui-field' }, [
      el('label', { class: 'ui-label', for: `${uid}-key`, text: v.key && v.key.required === false ? 'API key (optional)' : 'API key' }),
      el('div', { class: 'ui-input-group' }, [keyInput, eye]),
      keyLine,
      v.docs_url ? el('p', { class: 'ui-small' }, [el('a', {
        href: v.docs_url, rel: 'noopener noreferrer', text: 'Where do I get a key?',
        onclick: (e) => { e.preventDefault(); api.call('open_external', v.docs_url); },
      })]) : null,
    ]),
    el('div', { class: 'ui-row' }, [saveBtn, testBtn, refreshBtn, removeBtn]),
    testLine,
    fetched,
    msg,
    removeProvider,
  ]);
}

async function refreshKeyStatuses() {
  const res = await api.call('list_providers');
  if (!isOk(res)) return;
  S.views = pick(res, 'providers');
  for (const v of S.views) { const c = cards.get(v.id); if (c) c.setKey(v.key); }
  refreshFallbackWarning();
}

function initProviders() {
  $('local-save').addEventListener('click', saveLocal);
  $('refresh-all').addEventListener('click', refreshAllModels);
  $('auto-refresh').addEventListener('change', async (e) => {
    const want = e.target.checked;
    const res = await api.call('update_settings', { chat: { auto_refresh_models: want } });
    if (!isOk(res)) { e.target.checked = !want; $('refresh-msg').className = 'ui-small danger-text'; $('refresh-msg').textContent = errText(res); return; }
    if (res.config) S.cfg = { ...S.cfg, ...res.config };
    noteKeyRemoved(res);
    $('refresh-msg').className = 'ui-small ui-text-secondary';
    $('refresh-msg').textContent = want ? 'Model lists will refresh once a day.' : 'Automatic refresh is off.';
  });
  for (const id of ['local-device', 'local-mpl']) $(id).addEventListener('input', () => { $('local-msg').textContent = ''; });

  const form = $('add-form');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    for (const k of ['id', 'name', 'url', 'env', 'models']) setError(`add-${k}`, '');
    $('add-msg').textContent = '';
    const id = $('add-id').value.trim();
    const name = $('add-name').value.trim();
    const url = $('add-url').value.trim();
    const env = $('add-env').value.trim();
    const models = [...new Set($('add-models').value.split(',').map((s) => s.trim()).filter(Boolean))];
    let bad = null;
    const fail = (k, m) => { setError(`add-${k}`, m); const inp = $(`add-${k}`); if (inp) inp.setAttribute('aria-invalid', 'true'); if (!bad) bad = inp; };
    if (!ID_RE.test(id)) fail('id', 'Use 2 to 32 lowercase letters, digits or dashes, starting with a letter.');
    else if (id === 'local-npu' || S.views.some((p) => p.id === id)) fail('id', 'A provider with this id already exists.');
    if (!name) fail('name', 'Enter a display name.');
    let urlOk = false;
    try { const u = new URL(url); urlOk = u.protocol === 'https:' || u.protocol === 'http:'; } catch { urlOk = false; }
    if (!urlOk) fail('url', 'Enter a full http(s) URL, for example https://host/v1.');
    if (env && !ENV_RE.test(env)) fail('env', 'Use capital letters, digits and underscores, starting with a letter (for example MY_API_KEY).');
    if (models.some((m) => /\s/.test(m) || m.length > 200)) fail('models', 'Model names cannot contain spaces.');
    if (bad) { bad.focus(); return; }
    const spec = { id, kind: 'openai', display_name: name, base_url: url, api_key_env: env || null, models, default_model: models[0] || null, quirks: [], supports_tools: true };
    const res = await api.call('upsert_provider', spec);
    if (!isOk(res)) { $('add-msg').textContent = errText(res); $('add-msg').className = 'danger-text ui-small'; return; }
    form.reset();
    $('add-msg').className = 'ui-text-secondary ui-small';
    $('add-msg').textContent = `Added ${name}. Enter its key in the new card above, then press Save & test to fetch its models.`;
    await loadProviders(true);
    const card = document.querySelector(`.provider-card[data-id="${cssEsc(id)}"]`);
    if (card) { card.scrollIntoView({ block: 'nearest' }); const k = card.querySelector('input[data-key-input]'); if (k) k.focus(); }
  });
}

// ------------------------------------------------------------------ general ----

function fillGeneral() {
  const c = S.cfg; if (!c) return;
  $('g-idle').value = c.local ? c.local.idle_unload_minutes : '';
  $('g-mpc').value = c.chat ? c.chat.max_prompt_chars : '';
  $('g-hotkey').value = (c.ui && c.ui.hotkey) || '';
  $('g-reasoning').value = (c.chat && c.chat.show_reasoning) || 'collapsed';
  $('g-sticky').checked = !(c.ui && c.ui.sticky === false);   // on unless turned off
  $('g-reply').checked = !(c.ui && c.ui.show_on_reply === false);   // on unless turned off
  const enabled = new Set((c.tools && c.tools.enabled) || []);
  for (const cb of document.querySelectorAll('[data-tool]')) cb.checked = enabled.has(cb.value);
  $('g-personality').value = (c.chat && c.chat.system_prompt) || '';
  $('g-instructions').value = (c.chat && c.chat.instructions) || '';
  $('g-location').value = (c.tools && c.tools.location) || '';
  $('g-units').value = (c.tools && c.tools.units) || 'metric';
  fillFallback();
  fillQuickActions();
  syncHotkeyReset();
  setDirty(false);
}

/** The ticked tools, plus any enabled tool this page has no box for. A save replaces
 *  tools.enabled as a whole, so a tool without a box would otherwise be switched off. */
function enabledTools() {
  const boxes = [...document.querySelectorAll('[data-tool]')];
  const listed = new Set(boxes.map((cb) => cb.value));
  const others = ((S.cfg && S.cfg.tools && S.cfg.tools.enabled) || []).filter((t) => !listed.has(t));
  return [...boxes.filter((cb) => cb.checked).map((cb) => cb.value), ...others];
}

/** Remote providers (not the local runtime) as [id, spec], in config order. */
function remoteProviders() {
  const all = (S.cfg && S.cfg.providers) || {};
  return Object.entries(all).filter(([, p]) => p && p.kind !== 'ovms');
}

function fillFallbackModels() {
  const list = $('g-fallback-models');
  clear(list);
  const spec = ((S.cfg && S.cfg.providers) || {})[$('g-fallback').value];
  for (const m of (spec && spec.models) || []) list.append(el('option', { value: m }));
  $('g-fallback-model').disabled = !$('g-fallback').value;
}

function fillFallback() {
  const c = S.cfg || {};
  const sel = $('g-fallback');
  clear(sel);
  sel.append(el('option', { value: '', text: 'Nothing (show the error)' }));
  for (const [id, p] of remoteProviders()) sel.append(el('option', { value: id, text: p.display_name || id }));
  const want = (c.chat && c.chat.fallback_provider) || '';
  sel.value = [...sel.options].some((o) => o.value === want) ? want : '';
  $('g-fallback-model').value = (c.chat && c.chat.fallback_model) || '';
  fillFallbackModels();
  refreshFallbackWarning();
}

/** "No key saved for X" under the fallback choice when that provider cannot answer yet. */
function refreshFallbackWarning() {
  const id = $('g-fallback').value;
  const view = id ? S.views.find((p) => p.id === id) : null;
  const key = view && view.key;
  const missing = !!(key && key.source === 'none' && key.required !== false);
  const warn = $('g-fallback-warn');
  warn.hidden = !missing;
  warn.textContent = missing ? `No key saved for ${view.display_name || id}. It cannot answer until you add one under Providers.` : '';
}

async function refreshAutostart() {
  const res = await api.call('get_autostart');
  if (!isOk(res)) { $('autostart-msg').textContent = errText(res); return; }
  $('g-autostart').checked = !!res.enabled;
}

function initGeneral() {
  const form = $('general-form');
  // The theme and "Start at login" apply as soon as they change; they are not part of Save.
  $('g-autostart').dataset.nodirty = '';
  const counts = (e) => !(e.target.closest && e.target.closest('[data-nodirty], #g-theme-grid, #g-theme'));
  form.addEventListener('input', (e) => { if (counts(e)) { setDirty(true); $('g-msg').textContent = ''; } });
  form.addEventListener('change', (e) => { if (counts(e)) setDirty(true); });
  $('g-fallback').addEventListener('change', () => { $('g-fallback-model').value = ''; fillFallbackModels(); refreshFallbackWarning(); });
  $('g-revert').addEventListener('click', () => {
    stopHotkeyRecording();
    clearErrors(form);
    fillGeneral();
    $('g-restart').hidden = true;
    $('g-msg').className = 'ui-text-secondary ui-small';
    $('g-msg').textContent = 'Changes reverted.';
  });
  initHotkey();
  initQuickActions();

  $('g-autostart').addEventListener('change', async (e) => {
    const want = e.target.checked;
    const res = await api.call('set_autostart', want);
    if (!isOk(res)) {
      e.target.checked = !want;
      $('autostart-msg').textContent = errText(res);
      return;
    }
    e.target.checked = res.enabled != null ? !!res.enabled : want;
    $('autostart-msg').textContent = e.target.checked ? 'ChatForge will start when you sign in.' : 'ChatForge will not start automatically.';
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    $('g-msg').textContent = ''; $('g-restart').hidden = true;
    const idle = Number($('g-idle').value);
    const mpc = Number($('g-mpc').value);
    let bad = null;
    const fail = (k, m, input) => { setError(k, m); if (!bad) bad = input; };
    if ($('g-idle').value === '' || !Number.isInteger(idle) || idle < 0 || idle > 1440) fail('local.idle_unload_minutes', 'Enter a whole number of minutes from 0 to 1440.', $('g-idle'));
    if ($('g-mpc').value === '' || !Number.isInteger(mpc) || mpc < 200 || mpc > 4000000) fail('chat.max_prompt_chars', 'Enter a whole number from 200 to 4000000.', $('g-mpc'));
    const hotkey = $('g-hotkey').value.trim();
    if (!hotkey) fail('ui.hotkey', 'Enter a hotkey, for example Ctrl+Alt+C.', $('g-hotkey'));
    const location = $('g-location').value.replace(/\s+/g, ' ').trim();
    if (location.length > 100) fail('tools.location', 'Keep the location under 100 characters.', $('g-location'));
    const fbModel = $('g-fallback-model').value.trim();
    if (/\s/.test(fbModel)) fail('chat.fallback_model', 'Model names cannot contain spaces.', $('g-fallback-model'));
    const quick = collectQuickActions(fail);
    if (bad) { bad.focus(); return; }

    const fallback = $('g-fallback').value;
    const patch = {
      local: { idle_unload_minutes: idle },
      chat: {
        max_prompt_chars: mpc,
        show_reasoning: $('g-reasoning').value,
        system_prompt: $('g-personality').value.trim(),
        instructions: $('g-instructions').value.trim(),
        fallback_provider: fallback,
        fallback_model: fallback ? fbModel : '',
        ...quick,
      },
      ui: { sticky: $('g-sticky').checked, show_on_reply: $('g-reply').checked },
      tools: {
        enabled: enabledTools(),
        location,
        units: $('g-units').value,
      },
    };
    $('g-save').disabled = true;
    const prevHotkey = (S.cfg && S.cfg.ui && S.cfg.ui.hotkey) || '';
    // The hotkey first: Windows may refuse it, and that must not be reported as if the
    // other settings had failed. The rest is saved either way.
    let hotkeyError = null;
    if (hotkey !== prevHotkey) {
      const hk = await api.call('set_hotkey', hotkey);
      if (isOk(hk)) {
        const applied = (hk && hk.hotkey) || hotkey;
        S.cfg = { ...S.cfg, ui: { ...((S.cfg && S.cfg.ui) || {}), hotkey: applied } };
        $('g-hotkey').value = applied;
      } else {
        hotkeyError = typeof hk.error === 'string' ? hk.error : errText(hk);
      }
    }
    const res = await api.call('update_settings', patch);
    const saved = isOk(res);
    if (res && res.config) S.cfg = { ...S.cfg, ...res.config };
    noteKeyRemoved(res);
    $('g-save').disabled = false;
    const msg = $('g-msg');
    if (!saved) {
      const left = showFieldErrors(res.errors);
      if (hotkeyError) setError('ui.hotkey', hotkeyError);
      msg.className = 'danger-text ui-small';
      msg.textContent = `Nothing was saved${hotkey !== prevHotkey && !hotkeyError ? ' except the hotkey' : ''}. ${left.length ? left.join('; ') : (res && res.errors && Object.keys(res.errors).length ? 'See the messages above.' : errText(res))}`;
      return;
    }
    // Saved: the editor shows the list as the app now has it (new custom ids, edited flags).
    if (res.quick_actions) { QA.data = res.quick_actions; fillQuickActions(); }
    if ((res.restart_required || []).length) {
      $('g-restart').hidden = false;
      $('g-restart').textContent = `Restart or reload required for: ${res.restart_required.join(', ')}.`;
    }
    if (hotkeyError) {
      // Everything else is saved; only the hotkey field still differs from the app.
      setError('ui.hotkey', hotkeyError);
      setDirty(true);
      msg.className = 'warn-text ui-small';
      msg.textContent = `Saved, except the hotkey: ${hotkeyError}`;
      toast(msg.textContent, 'warn');
      $('g-hotkey').focus();
      return;
    }
    setDirty(false);
    msg.className = 'ok-text ui-small';
    msg.textContent = 'Saved.';
    toast('Settings saved', 'ok');
  });
}

// ------------------------------------------------------------------- hotkey ----

const DEFAULT_HOTKEY = 'Ctrl+Alt+C';
const COPILOT_HOTKEY = 'Copilot';
const NAMED_KEYS = {
  Space: 'Space', Enter: 'Enter', Tab: 'Tab', Delete: 'Delete', Insert: 'Insert', Home: 'Home', End: 'End',
  PageUp: 'PageUp', PageDown: 'PageDown', ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right',
  Backquote: '`', Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']', Backslash: '\\',
  Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Slash: '/',
};
let recording = false;

/** The key of a keydown in the hotkey field's text format (Ctrl+Alt+C), or null. */
function keyFromEvent(e) {
  const c = String(e.code || '');
  if (/^Key[A-Z]$/.test(c)) return c.slice(3);
  if (/^Digit\d$/.test(c)) return c.slice(5);
  if (/^F\d{1,2}$/.test(c)) return c;
  return NAMED_KEYS[c] || null;
}
function modsFromEvent(e) {
  return [e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && 'Shift', e.metaKey && 'Win'].filter(Boolean);
}
function syncHotkeyReset() {
  $('g-hotkey-reset').hidden = $('g-hotkey').value.trim() === DEFAULT_HOTKEY;
}
function stopHotkeyRecording() {
  if (!recording) return;
  recording = false;
  document.removeEventListener('keydown', onRecordKey, true);
  const b = $('g-hotkey-record');
  b.textContent = 'Record';
  b.setAttribute('aria-pressed', 'false');
  $('g-hotkey').placeholder = DEFAULT_HOTKEY;
}
function onRecordKey(e) {
  e.preventDefault();
  e.stopPropagation();
  const input = $('g-hotkey');
  const mods = modsFromEvent(e);
  if (['Control', 'Alt', 'Shift', 'Meta', 'AltGraph'].includes(e.key)) {
    input.placeholder = mods.length ? `${mods.join('+')}+…` : DEFAULT_HOTKEY;   // modifiers alone: keep waiting
    return;
  }
  if (!mods.length && e.key === 'Escape') { stopHotkeyRecording(); $('g-hotkey-record').focus(); return; }
  if (!mods.length && e.key === 'Backspace') {
    input.value = '';
    stopHotkeyRecording();
    setDirty(true); syncHotkeyReset(); setError('ui.hotkey', '');
    $('g-hotkey-record').focus();
    return;
  }
  const key = keyFromEvent(e);
  // The Copilot key arrives as Meta+Shift+F23.
  if (key === 'F23' && e.metaKey && e.shiftKey && !e.ctrlKey && !e.altKey) {
    input.value = COPILOT_HOTKEY;
    setError('ui.hotkey', '');
    stopHotkeyRecording();
    setDirty(true); syncHotkeyReset();
    $('g-hotkey-record').focus();
    return;
  }
  if (!key) { setError('ui.hotkey', 'That key cannot be used in a hotkey. Try a letter, digit or F-key.'); return; }
  if (!mods.length) { setError('ui.hotkey', 'Hold Ctrl, Alt, Shift or Win together with the key.'); return; }
  input.value = [...mods, key].join('+');
  setError('ui.hotkey', '');
  stopHotkeyRecording();
  setDirty(true); syncHotkeyReset();
  $('g-hotkey-record').focus();
}
function initHotkey() {
  const rec = $('g-hotkey-record');
  rec.addEventListener('click', () => {
    if (recording) { stopHotkeyRecording(); return; }
    recording = true;
    setError('ui.hotkey', '');
    rec.textContent = 'Press the keys… (Esc cancels)';
    rec.setAttribute('aria-pressed', 'true');
    document.addEventListener('keydown', onRecordKey, true);
  });
  rec.addEventListener('blur', () => { if (recording) stopHotkeyRecording(); });
  $('g-hotkey').addEventListener('input', syncHotkeyReset);
  $('g-hotkey-copilot').addEventListener('click', () => {
    $('g-hotkey').value = COPILOT_HOTKEY;
    setError('ui.hotkey', '');
    setDirty(true); syncHotkeyReset();
    $('g-hotkey').focus();
  });
  $('g-hotkey-reset').addEventListener('click', () => {
    $('g-hotkey').value = DEFAULT_HOTKEY;
    setError('ui.hotkey', '');
    setDirty(true); syncHotkeyReset();
    $('g-hotkey').focus();
  });
}

// ------------------------------------------------------------ quick actions ----
// The quick actions (chat/actions.py) as an editable list in the General form: a name and
// instructions per action, two switches, Remove, Add action and Restore defaults; Save
// writes them with the rest of the form. Only what differs from the built-in actions is
// stored: chat.quick_actions holds edited built-in ones (by id; empty instructions keep
// the built-in text) and custom ones, chat.hidden_quick_actions the removed built-in ones.

const QA = { data: null };   // get_settings / update_settings quick_actions: {defaults, items}
const QA_MAX = 50;           // config.ChatCfg.quick_actions max_length
const QA_LABEL_MAX = 40;     // config.QuickActionCfg.label
const QA_INSTRUCTIONS_MAX = 4000;
let qaSeq = 0;

function qaDefaults() { return (QA.data && QA.data.defaults) || []; }

/** A "35/40" counter that appears once the field is within 20% of its limit. */
function attachCounter(input, max, out) {
  const update = () => {
    const n = input.value.length;
    out.hidden = n < max * 0.8;
    out.textContent = `${n}/${max}`;
    out.classList.toggle('danger-text', n >= max);
  };
  input.addEventListener('input', update);
  update();
}
const previewOf = (text) => (String(text).replace(/\s+/g, ' ').trim() || 'No instructions yet.');

/** One editable row. `a`: {id, label, hint, instructions, tools, match_style, builtin}. */
function quickActionRow(a, { open = false } = {}) {
  const n = ++qaSeq;
  const label = el('input', { id: `qa-label-${n}`, class: 'ui-input qa-label', type: 'text', maxlength: String(QA_LABEL_MAX),
    autocomplete: 'off', spellcheck: 'true', placeholder: 'Name, e.g. Make it friendlier', 'aria-describedby': `qa-label-count-${n}` });
  label.value = a.label || '';
  const labelCount = el('span', { id: `qa-label-count-${n}`, class: 'qa-count ui-small ui-num ui-text-tertiary', hidden: '' });
  const text = el('textarea', { id: `qa-text-${n}`, class: 'ui-textarea qa-instructions', rows: '4', maxlength: String(QA_INSTRUCTIONS_MAX),
    spellcheck: 'true', 'aria-describedby': `qa-text-count-${n}`,
    placeholder: 'What the model should do with the pasted text, and how to answer. For example: Rewrite the text in a warmer tone. Reply with only the new text in one ```text block.' });
  text.value = a.instructions || '';
  const textCount = el('span', { id: `qa-text-count-${n}`, class: 'qa-count ui-small ui-num ui-text-tertiary', hidden: '' });
  attachCounter(label, QA_LABEL_MAX, labelCount);
  attachCounter(text, QA_INSTRUCTIONS_MAX, textCount);
  const tools = el('input', { type: 'checkbox', class: 'ui-checkbox qa-tools', id: `qa-tools-${n}` });
  tools.checked = !!a.tools;
  const style = el('input', { type: 'checkbox', class: 'ui-checkbox qa-style', id: `qa-style-${n}` });
  style.checked = !!a.match_style;
  const up = el('button', { type: 'button', class: 'ui-btn ui-btn--sm ui-btn--ghost ui-btn--icon qa-up', text: '↑' });
  const down = el('button', { type: 'button', class: 'ui-btn ui-btn--sm ui-btn--ghost ui-btn--icon qa-down', text: '↓' });
  const remove = btn('Remove', { cls: 'ui-btn--ghost qa-remove' });
  const preview = el('p', { class: 'qa-preview ui-small ui-text-tertiary', text: previewOf(text.value) });
  const li = el('li', { class: 'qa-row ui-well', dataset: { id: a.id || '', builtin: a.builtin ? '1' : '0', hint: a.hint || '' } }, [
    el('div', { class: 'qa-head' }, [
      el('span', { class: 'qa-move', role: 'group', 'aria-label': 'Move' }, [up, down]),
      label,
      labelCount,
      chip(a.builtin ? 'Built in' : 'Custom', a.builtin ? 'neutral' : 'accent'),
      remove,
    ]),
    preview,
    el('details', { class: 'qa-more' }, [
      el('summary', { text: 'Instructions' }),
      text,
      textCount,
      el('div', { class: 'qa-flags' }, [
        el('label', { class: 'ui-check', for: tools.id }, [tools, el('span', { text: 'Can look things up on the web' })]),
        el('label', { class: 'ui-check', for: style.id }, [style, el('span', { text: 'Match the formatting of my text' })]),
      ]),
    ]),
  ]);
  if (open) li.querySelector('details').open = true;
  label.setAttribute('aria-label', 'Quick action name');
  const sync = () => {
    const name = label.value.trim() || 'this action';
    remove.setAttribute('aria-label', `Remove ${name}`);
    up.setAttribute('aria-label', `Move ${name} up`);
    down.setAttribute('aria-label', `Move ${name} down`);
    text.setAttribute('aria-label', `Instructions for ${name}`);
  };
  label.addEventListener('input', sync);
  text.addEventListener('input', () => { preview.textContent = previewOf(text.value); });
  sync();
  const move = (dir) => {
    const other = dir < 0 ? li.previousElementSibling : li.nextElementSibling;
    if (!other) return;
    if (dir < 0) other.before(li); else other.after(li);
    setDirty(true);
    syncQuickActionButtons();
    const target = dir < 0 ? up : down;
    (target.disabled ? (dir < 0 ? down : up) : target).focus();
  };
  up.addEventListener('click', () => move(-1));
  down.addEventListener('click', () => move(1));
  remove.addEventListener('click', () => {
    const next = li.nextElementSibling || li.previousElementSibling;
    li.remove();
    setDirty(true);
    syncQuickActionButtons();
    (next ? next.querySelector('.qa-remove') : $('qa-add')).focus();
  });
  return li;
}

function renderQuickActionRows(items) {
  $('qa-list').replaceChildren(...items.map((a) => quickActionRow(a)));
  syncQuickActionButtons();
}

function syncQuickActionButtons() {
  const rows = [...$('qa-list').children];
  $('qa-add').disabled = rows.length >= QA_MAX;
  rows.forEach((li, i) => {
    li.querySelector('.qa-up').disabled = i === 0;
    li.querySelector('.qa-down').disabled = i === rows.length - 1;
  });
}

function fillQuickActions() {
  if (!QA.data) return;
  renderQuickActionRows(QA.data.items || []);
}

/** The editor as a settings patch ({quick_actions, hidden_quick_actions}), or the first
 *  problem reported through `fail(key, message, input)`. */
function collectQuickActions(fail) {
  const defaults = new Map(qaDefaults().map((d) => [d.id, d]));
  const present = new Set();
  const quick_actions = [];
  let problem = false;
  const report = (message, input) => {
    if (problem) return;
    problem = true;
    input.setAttribute('aria-invalid', 'true');
    const more = input.closest('details');
    if (more) more.open = true;
    fail('chat.quick_actions', message, input);
  };
  // Built-in actions keep their built-in order unless the list was reordered; then every
  // built-in is written out, in the order shown (see the note in the report to Python).
  const shown = [...$('qa-list').querySelectorAll('.qa-row')].filter((li) => li.dataset.builtin === '1' && defaults.has(li.dataset.id)).map((li) => li.dataset.id);
  const wanted = [...defaults.keys()].filter((id) => shown.includes(id));
  const reordered = shown.some((id, i) => id !== wanted[i]);
  [...$('qa-list').querySelectorAll('.qa-row')].forEach((li, i) => {
    const labelIn = li.querySelector('.qa-label');
    const textIn = li.querySelector('.qa-instructions');
    const label = labelIn.value.replace(/\s+/g, ' ').trim();
    const instructions = textIn.value.trim();
    const tools = li.querySelector('.qa-tools').checked;
    const matchStyle = li.querySelector('.qa-style').checked;
    const hint = li.dataset.hint || '';
    const base = li.dataset.builtin === '1' ? defaults.get(li.dataset.id) : null;
    if (!label) { report(`Give quick action ${i + 1} a name.`, labelIn); return; }
    if (!base && !instructions) { report(`Write the instructions for “${label}”.`, textIn); return; }
    if (!base) {
      quick_actions.push({ id: li.dataset.id || '', label, instructions, hint, tools, match_style: matchStyle });
      return;
    }
    present.add(base.id);
    const text = instructions === base.instructions ? '' : instructions;   // '' keeps the built-in text
    const ownHint = hint === base.hint ? '' : hint;
    if (reordered || label !== base.label || text || ownHint || tools !== base.tools || matchStyle !== base.match_style) {
      quick_actions.push({ id: base.id, label, instructions: text, hint: ownHint, tools, match_style: matchStyle });
    }
  });
  const hidden_quick_actions = [...defaults.keys()].filter((id) => !present.has(id));
  return { quick_actions, hidden_quick_actions };
}

function initQuickActions() {
  $('qa-add').addEventListener('click', () => {
    if ($('qa-list').children.length >= QA_MAX) return;
    const li = quickActionRow({ id: '', label: '', instructions: '', tools: false, match_style: true, builtin: false }, { open: true });
    $('qa-list').append(li);
    setDirty(true);
    syncQuickActionButtons();
    li.querySelector('.qa-label').focus();
  });
  $('qa-restore').addEventListener('click', async () => {
    const yes = await confirmAction({
      title: 'Restore the built-in quick actions?',
      message: 'This replaces your edits, order and removed actions with the built-in ones, and drops your custom actions. Nothing is kept until you press Save.',
      confirmLabel: 'Restore defaults', cancelLabel: 'Keep my actions', danger: true,
    });
    if (!yes) return;
    renderQuickActionRows(qaDefaults());
    setDirty(true);
    $('g-msg').className = 'ui-text-secondary ui-small';
    $('g-msg').textContent = 'The built-in quick actions are back. Press Save to keep them.';
  });
  // A name or instructions being fixed clears its error.
  $('qa-list').addEventListener('input', (e) => {
    if (e.target.getAttribute('aria-invalid')) { e.target.removeAttribute('aria-invalid'); setError('chat.quick_actions', ''); }
  });
}

// -------------------------------------------------------------------- theme ----

let remoteTheme = false;

/** One radio card per theme, from UITheme.list() (each entry has slug, name, ground,
 *  themeColor and swatch). Picking one calls UITheme.set(slug), exactly as the select did;
 *  the hidden <select data-ui-theme-picker> stays for the runtime's own sync. */
function buildThemeGrid() {
  const UI = window.UITheme;
  const grid = $('g-theme-grid');
  clear(grid);
  const current = UI.current();
  for (const t of UI.list()) {
    const input = el('input', { type: 'radio', name: 'ui-theme', id: `theme-${t.slug}`, class: 'theme-radio', value: t.slug, 'data-nodirty': '' });
    input.checked = t.slug === current;
    input.addEventListener('change', () => { if (input.checked) UI.set(t.slug); });
    const swatch = el('span', { class: 'theme-swatch', 'aria-hidden': 'true' }, [el('i', { class: 'theme-dot' })]);
    swatch.style.setProperty('--sw-ground', t.themeColor || t.swatch);
    swatch.style.setProperty('--sw-accent', t.swatch || t.themeColor);
    grid.append(el('label', { class: 'theme-card', for: input.id }, [
      input,
      swatch,
      el('span', { class: 'theme-name', text: t.name }),
      el('span', { class: 'theme-ground ui-small ui-text-tertiary', text: t.ground === 'light' ? 'Light' : 'Dark' }),
    ]));
  }
}
function paintThemeGrid(slug) {
  for (const input of document.querySelectorAll('#g-theme-grid input[type="radio"]')) input.checked = input.value === slug;
}

function initTheme() {
  const UI = window.UITheme;
  if (!UI) return;
  try { UI.mountPicker($('g-theme')); } catch { /* the runtime already fills data-ui-theme-picker selects */ }
  buildThemeGrid();
  UI.onChange(async ({ slug }) => {
    paintThemeGrid(slug);
    if (remoteTheme) return;
    const res = await api.call('update_settings', { ui: { theme: slug } });
    if (!isOk(res)) toast(errText(res), 'bad');
    else { noteKeyRemoved(res); if (S.cfg) S.cfg.ui = { ...(S.cfg.ui || {}), theme: slug }; }
  });
}
function applyTheme(slug) {
  const UI = window.UITheme;
  if (!UI || !slug || UI.current() === slug) return;
  remoteTheme = true;
  try { UI.set(slug); } finally { remoteTheme = false; }
}

// --------------------------------------------------------------------- logs ----

let logTimer = null; let logBusy = false; let lastLogText = null; let lastLines = [];
const LOG_LINES = 500;

/** A log line's level class, from the level word near the start of the line. */
function logLevelClass(line) {
  const head = line.slice(0, 64);
  if (/\b(ERROR|CRITICAL|FATAL)\b/.test(head)) return 'll-error';
  if (/\bWARN(ING)?\b/.test(head)) return 'll-warn';
  return '';
}

/** Draw the lines that match the filter box; warnings and errors get their status colours. */
function renderLogLines() {
  const view = $('log-view');
  const f = $('log-filter').value.trim().toLowerCase();
  const shown = f ? lastLines.filter((l) => l.toLowerCase().includes(f)) : lastLines;
  const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 24;
  view.replaceChildren(...shown.map((l) => el('span', { class: `ll ${logLevelClass(l)}`.trim(), text: `${l}\n` })));
  if (atBottom) view.scrollTop = view.scrollHeight;
  const n = lastLines.length;
  $('log-count').textContent = f
    ? `${shown.length} of the last ${n} line${n === 1 ? '' : 's'} match`
    : `The last ${n} line${n === 1 ? '' : 's'} (at most ${LOG_LINES})`;
}

async function refreshLogs() {
  if (logBusy) return;
  logBusy = true;
  try {
    const res = await api.call('get_logs', LOG_LINES, $('log-level').value);
    if (!isOk(res)) { $('log-count').textContent = errText(res); return; }
    const raw = Array.isArray(res) ? res : (res.lines || res.logs || []);
    const lines = Array.isArray(raw) ? raw : String(raw).split('\n');
    const text = lines.join('\n');
    lastLines = lines;
    if (text === lastLogText) return;
    lastLogText = text;
    renderLogLines();
  } finally { logBusy = false; }
}
function syncLogTimer() {
  const want = S.tab === 'logs' && !document.hidden && $('log-auto').checked;
  if (want && !logTimer) logTimer = setInterval(refreshLogs, 2000);
  if (!want && logTimer) { clearInterval(logTimer); logTimer = null; }
}
function initLogs() {
  $('log-level').addEventListener('change', () => { lastLogText = null; refreshLogs(); });
  $('log-auto').addEventListener('change', syncLogTimer);
  $('log-refresh').addEventListener('click', () => { lastLogText = null; refreshLogs(); });
  $('log-filter').addEventListener('input', renderLogLines);
  $('log-open').addEventListener('click', async () => {
    const res = await api.call('open_logs_folder');
    if (!isOk(res)) toast(errText(res), 'bad');
  });
  $('log-open-config').addEventListener('click', async () => {
    const res = await api.call('open_config_folder');
    if (!isOk(res)) toast(errText(res), 'bad');
  });
  $('log-diagnostics').addEventListener('click', async () => {
    const res = await api.call('diagnostics');
    if (!isOk(res) || typeof res.text !== 'string') { toast(errText(res), 'bad'); return; }
    const ok_ = await copyText(res.text);
    toast(ok_ ? 'Diagnostics copied. Keys are left out.' : 'Could not copy. Try again from a window that has focus.', ok_ ? 'ok' : 'bad');
  });
  $('log-copy').addEventListener('click', async () => {
    const ok_ = await copyText(lastLines.join('\n'));
    toast(ok_ ? 'Logs copied to the clipboard.' : 'Could not copy. Select the text and press Ctrl+C.', ok_ ? 'ok' : 'bad');
  });
  document.addEventListener('visibilitychange', () => { syncLogTimer(); if (!document.hidden && S.tab === 'logs') refreshLogs(); });
}

// ------------------------------------------------------------------- events ----

function initEvents() {
  // bridge.js is still waiting for the app (it never uses the dev mock inside WebView2);
  // init() carries on if the bridge arrives.
  on('bridge.unavailable', () => {
    $('s-sub').textContent = 'Could not connect to ChatForge. Close this window and open Settings again from the tray; if that does not help, restart ChatForge.';
    $('s-sub').className = 's-sub danger-text';
  });
  on('runtime.status', (e) => {
    const changed = e.state !== S.runtime.state || e.model_id !== S.runtime.model_id;
    S.runtime = { ...S.runtime, ...e };
    if (changed) renderModels(); else paintLoad();
  });
  on('runtime.install', onRuntimeInstall);
  on('download.progress', onDownloadProgress);
  on('key.status', () => { refreshKeyStatuses(); });
  on('settings.changed', async (e) => {
    const c = e.config || {};
    if (c.ui && c.ui.theme) applyTheme(c.ui.theme);
    if (c.chat && c.chat.model) {
      S.selected = { provider: c.chat.provider || S.selected.provider, model: c.chat.model };
      renderModels();
    }
    if (!S.generalDirty) { await loadSettings(); fillGeneral(); }
    else if (c.ui && typeof c.ui.sticky === 'boolean' && S.cfg) {
      // The popup's pin changed it while other General edits wait: follow it, unless this
      // box was changed here too, so Save does not switch it back.
      const shown = !(S.cfg.ui && S.cfg.ui.sticky === false);
      if ($('g-sticky').checked === shown) $('g-sticky').checked = c.ui.sticky;
      S.cfg.ui = { ...(S.cfg.ui || {}), sticky: c.ui.sticky };
    }
  });
}

// --------------------------------------------------------------------- init ----

async function init() {
  initTabs(); initModels(); initProviders(); initGeneral(); initLogs(); initTheme(); initEvents();
  await ready();
  if (window.pywebview && window.pywebview.__mock) {
    $('s-sub').textContent = 'Development mock: no Python backend is connected.';
    try { await import('./settings-mock-ext.js'); } catch { /* the extension is optional */ }
  } else {
    $('s-sub').textContent = 'Changes are saved to config.toml.';
    $('s-sub').className = 's-sub ui-text-tertiary';
  }

  const [st] = await Promise.all([api.call('get_state'), loadSettings()]);
  if (isOk(st)) {
    if (st.selected) S.selected = st.selected;
    if (st.runtime) S.runtime = st.runtime;
    if (st.providers) S.views = st.providers;
    applyTheme(st.theme || (st.config && st.config.ui && st.config.ui.theme));
  }
  $('author-name').textContent = authorName();
  fillGeneral();
  fillLocalCard();
  await Promise.all([loadModels(), loadDisk(), refreshRuntime(), refreshAutostart(), refreshDownloads()]);
  doSearch();
  // Typed keys are wiped when the window goes away (not on a cancelled leave).
  window.addEventListener('pagehide', clearKeyInputs);
  // Unsaved General edits: ask before the window closes (best effort inside WebView2).
  window.addEventListener('beforeunload', (e) => {
    if (!S.generalDirty) return;
    e.preventDefault();
    e.returnValue = '';
  });
  // A file dropped on the window must not navigate it to file://.
  for (const type of ['dragover', 'drop']) document.addEventListener(type, (e) => e.preventDefault());
  document.documentElement.dataset.ready = 'true';
}

init();
