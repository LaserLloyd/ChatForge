// First-run cards for the popup's empty state.
//
//   createSetupCard({ api, onUseStudioForge, onUseKey })  "Set up local AI": installs the
//       runtime and downloads the starter model, one progress bar, Cancel, and two other ways
//       in (StudioForge, an API key). Fed with the bridge's setup snapshot (get_state.setup and
//       the `setup.progress` event); update() repaints in place so a download that reports
//       twenty times a second never rebuilds the DOM.
//   createAutostartCard({ api, onAnswered })              "Start ChatForge when you sign in?"
//       asked once, never answered for the user.
//
// Plain DOM through el(); no innerHTML.
import { el } from './util.js?v=13';

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v >= 9e8) return `${(v / 1e9).toFixed(2)} GB`;   // 0.94 GB, not 936 MB: the size everyone quotes
  if (v >= 1e6) return `${Math.round(v / 1e6)} MB`;
  return `${Math.max(0, Math.round(v / 1e3))} KB`;
}

function fmtEta(sec) {
  const s = Number(sec);
  if (!Number.isFinite(s) || s <= 0) return '';
  if (s < 90) return `about ${Math.ceil(s / 5) * 5} s left`;
  return `about ${Math.ceil(s / 60)} min left`;
}

/** The one line under the bar: "42% · 12 MB/s · about 1 min left". */
export function progressLine(snap) {
  const parts = [`${Math.round(Number(snap.percent) || 0)}%`];
  if (snap.speed_bps > 0) parts.push(`${fmtBytes(snap.speed_bps)}/s`);
  const eta = fmtEta(snap.eta_s);
  if (eta) parts.push(eta);
  return parts.join(' · ');
}

const RUNNING = new Set(['checking', 'runtime', 'model', 'verifying']);

export function createSetupCard({ api, onUseStudioForge, onUseKey }) {
  const uid = `sc-${Math.random().toString(36).slice(2, 8)}`;
  let snap = {};
  let busy = false;

  const title = el('h2', { class: 'sc-title', id: `${uid}-title`, text: 'Set up local AI' });
  const body = el('p', { class: 'sc-body' });
  const facts = el('p', { class: 'sc-facts' });
  const device = el('p', { class: 'sc-device' });
  const fill = el('div', { class: 'sc-fill' });
  const bar = el('div', {
    class: 'sc-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-labelledby': `${uid}-title`,
  }, [fill]);
  const line = el('p', { class: 'sc-line', role: 'status' });
  const problem = el('p', { class: 'sc-problem', role: 'alert' });
  const primary = el('button', { class: 'ui-btn ui-btn--primary', type: 'button', text: 'Set up local AI' });
  const cancel = el('button', { class: 'ui-btn', type: 'button', text: 'Cancel' });
  const sf = el('button', { class: 'ui-btn', type: 'button', text: 'Use StudioForge' });
  const key = el('button', { class: 'ui-btn', type: 'button', text: 'Use an API key' });
  const later = el('button', { class: 'ui-btn ui-btn--ghost sc-later', type: 'button', text: 'Not now' });
  const actions = el('div', { class: 'sc-actions' }, [primary, cancel]);
  const alts = el('div', { class: 'sc-alts' }, [
    el('span', { class: 'sc-or', text: 'Or use' }), sf, key, later,
  ]);

  const node = el('section', { class: 'setup-card', 'aria-labelledby': `${uid}-title` }, [
    title, body, facts, device, bar, line, problem, actions, alts,
  ]);

  async function call(name) {
    if (busy) return;
    busy = true;
    try {
      const r = await api.call(name);
      if (r && r.ok) update(r);
    } finally { busy = false; }
  }

  primary.addEventListener('click', () => call('setup_local_start'));
  cancel.addEventListener('click', () => call('setup_local_cancel'));
  later.addEventListener('click', async () => { await api.call('setup_local_dismiss'); update({ ...snap, needed: false, dismissed: true }); if (hooks.onDismiss) hooks.onDismiss(); });
  sf.addEventListener('click', () => onUseStudioForge && onUseStudioForge());
  key.addEventListener('click', () => onUseKey && onUseKey());

  const hooks = { onDismiss: null };

  function update(next) {
    snap = { ...snap, ...next };
    const phase = snap.phase || 'idle';
    const running = RUNNING.has(phase);
    const label = snap.model_label || 'the starter model';
    const total = fmtBytes((snap.runtime_installed ? 0 : snap.runtime_bytes) + (snap.model_installed ? 0 : snap.model_bytes));

    title.textContent = phase === 'error' ? 'Setup stopped' : phase === 'cancelled' ? 'Setup cancelled' : 'Set up local AI';
    body.textContent = 'Run a small AI model on this PC. No account, and your chats stay on this computer.';
    const bits = [];
    if (!snap.runtime_installed) bits.push(`the local runtime (${fmtBytes(snap.runtime_bytes)})`);
    if (!snap.model_installed) bits.push(`${label} (${fmtBytes(snap.model_bytes)}${snap.model_licence ? `, ${snap.model_licence}` : ''})`);
    facts.textContent = bits.length ? `Downloads ${bits.join(' and ')}: ${total} in all.` : '';
    facts.hidden = !bits.length || running;

    const dev = snap.device_note;
    device.textContent = dev && dev.label ? dev.label : '';
    device.hidden = !(dev && dev.label);
    device.dataset.slow = dev && dev.slower ? 'true' : 'false';

    bar.hidden = !running;
    fill.style.width = `${Math.min(100, Math.max(0, Number(snap.percent) || 0))}%`;
    bar.setAttribute('aria-valuenow', String(Math.round(Number(snap.percent) || 0)));
    line.hidden = !running;
    line.textContent = running ? `${snap.message || ''} — ${progressLine(snap)}` : '';

    const fail = snap.failure;
    problem.hidden = phase !== 'error' || !fail;
    problem.textContent = fail ? [fail.message, fail.hint].filter(Boolean).join(' ') : '';

    primary.hidden = running;
    primary.textContent = phase === 'error' || phase === 'cancelled' ? 'Try again' : 'Set up local AI';
    cancel.hidden = !running;
    cancel.disabled = phase === 'verifying';
    alts.hidden = running;
    node.setAttribute('aria-busy', running ? 'true' : 'false');
  }

  update({});
  return {
    node,
    update,
    get snapshot() { return snap; },
    set onDismiss(fn) { hooks.onDismiss = fn; },
    focus: () => primary.focus(),
    destroy() { node.remove(); },
  };
}

export function createAutostartCard({ api, onAnswered }) {
  const uid = `ac-${Math.random().toString(36).slice(2, 8)}`;
  const status = el('p', { class: 'sc-problem', role: 'alert' });
  status.hidden = true;
  const yes = el('button', { class: 'ui-btn ui-btn--primary', type: 'button', text: 'Yes, start at sign-in' });
  const no = el('button', { class: 'ui-btn', type: 'button', text: 'No thanks' });
  const node = el('section', { class: 'setup-card setup-card--small', 'aria-labelledby': `${uid}-title` }, [
    el('h2', { class: 'sc-title', id: `${uid}-title`, text: 'Start ChatForge when you sign in?' }),
    el('p', { class: 'sc-body', text: 'It waits in the notification area and opens with Ctrl+Alt+C. You can change this any time from the tray menu.' }),
    status,
    el('div', { class: 'sc-actions' }, [yes, no]),
  ]);
  async function answer(flag) {
    yes.disabled = no.disabled = true;
    const r = await api.call('answer_autostart', flag);
    if (r && r.ok) { if (onAnswered) onAnswered(flag); return; }
    yes.disabled = no.disabled = false;
    status.hidden = false;
    status.textContent = (r && r.error && r.error.message) || 'Could not change that.';
  }
  yes.addEventListener('click', () => answer(true));
  no.addEventListener('click', () => answer(false));
  return { node, destroy() { node.remove(); } };
}
