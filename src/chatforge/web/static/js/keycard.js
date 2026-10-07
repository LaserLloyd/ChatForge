// Inline "Add your <provider> API key" card, shown in the chat when a cloud provider
// has no key (chat.error code "no_key", action "add_key").
//
// Adapted from DisPatch_Chat frontend/static/js/llm.js (MIT, LaserLloyd): firstRunCard,
// runTest/save flow and the Enter-to-test wiring (lines 275-395). Built with el(); no i18n.
//
// Flow: "Save & test" -> api.save_api_key(pid, key) -> api.test_provider(pid, null, null)
// -> onSuccess() (chat.js re-sends the pending text).
//
// Key hygiene: the key lives only in the <input>. It is handed to save_api_key once, the
// field is cleared straight away, and it is never logged, stored in a variable that
// outlives the call, or written anywhere else.
import { el, railIcon } from './util.js?v=13';

const REGIONS = [
  { id: 'international', label: 'International', base: 'https://api.minimax.io/v1', site: 'https://platform.minimax.io' },
  { id: 'china', label: 'China', base: 'https://api.minimaxi.com/v1', site: 'https://platform.minimaxi.com' },
];

const EYE = ['M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z'];
const EYE_OFF = ['M3 3l18 18', 'M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4', 'M6.6 6.7A17 17 0 0 0 2 12s3.6 7 10 7a10 10 0 0 0 4.4-1', 'M9.9 9.9a3 3 0 0 0 4.2 4.2'];

/**
 * @param {object} opts
 * @param {{call: Function}} opts.api      the bridge api
 * @param {object} opts.provider           provider view: {id, display_name, region}
 * @param {Function} opts.onSuccess        called after the key was saved AND tested OK
 * @param {Function} opts.onCancel         called when the user dismisses the card
 * @param {Function} [opts.onOpenLink]     called with a URL to open (defaults to api open_external)
 * @returns {{node: HTMLElement, focus: Function, destroy: Function}}
 */
export function createKeyCard({ api, provider, onSuccess, onCancel, onOpenLink }) {
  const pid = provider.id;
  const name = provider.display_name || pid;
  const hasRegions = provider.region === 'international' || provider.region === 'china';
  let region = hasRegions ? provider.region : null;
  let busy = false;
  let destroyed = false;

  const uid = `kc-${Math.random().toString(36).slice(2, 8)}`;
  const status = el('p', { class: 'kc-status', id: `${uid}-status`, role: 'status' });
  status.hidden = true;

  const input = el('input', {
    class: 'kc-input', id: `${uid}-key`, type: 'password', autocomplete: 'off',
    spellcheck: 'false', autocapitalize: 'off', autocorrect: 'off',
    placeholder: 'Paste your key', 'aria-describedby': `${uid}-status`,
    'data-lpignore': 'true', 'data-1p-ignore': 'true',
  });
  const eyeIcon = railIcon(EYE);
  const eye = el('button', {
    class: 'kc-eye', type: 'button', 'aria-pressed': 'false', 'aria-label': 'Show key', title: 'Show key',
  }, [eyeIcon]);
  eye.addEventListener('click', () => {
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    eye.setAttribute('aria-pressed', show ? 'true' : 'false');
    eye.setAttribute('aria-label', show ? 'Hide key' : 'Show key');
    eye.title = show ? 'Hide key' : 'Show key';
    eye.replaceChildren(railIcon(show ? EYE_OFF : EYE));
    input.focus();
  });

  const save = el('button', { class: 'ui-btn ui-btn--primary', type: 'button', text: 'Save & test' });
  const cancel = el('button', { class: 'ui-btn', type: 'button', text: 'Cancel' });

  // Region toggle: a radiogroup of two buttons.
  let regionRow = null;
  const regionBtns = [];
  if (hasRegions) {
    regionRow = el('div', { class: 'kc-region', role: 'radiogroup', 'aria-label': 'Key region' });
    for (const r of REGIONS) {
      const b = el('button', {
        class: 'kc-seg', type: 'button', role: 'radio', 'aria-checked': String(r.id === region),
        dataset: { region: r.id }, text: r.label,
      });
      b.addEventListener('click', () => setRegion(r.id));
      regionBtns.push(b);
      regionRow.append(b);
    }
  }
  function setRegion(id) {
    region = id;
    for (const b of regionBtns) b.setAttribute('aria-checked', String(b.dataset.region === id));
    linkA.textContent = siteLabel();
  }
  const siteLabel = () => `Get a key at ${(REGIONS.find((r) => r.id === region) || REGIONS[0]).site.replace('https://', '')}`;
  const linkA = el('a', { class: 'kc-link', href: '#', text: siteLabel() });
  linkA.addEventListener('click', (e) => {
    e.preventDefault();
    const url = (REGIONS.find((r) => r.id === region) || REGIONS[0]).site;
    if (onOpenLink) onOpenLink(url); else api.call('open_external', url);
  });

  function show(msg, tone) {
    status.textContent = msg;
    status.hidden = !msg;
    status.dataset.tone = tone || '';
  }
  function setBusy(v) {
    busy = v;
    input.disabled = v; save.disabled = v; eye.disabled = v;
    for (const b of regionBtns) b.disabled = v;
    save.textContent = v ? 'Working…' : 'Save & test';
    node.setAttribute('aria-busy', v ? 'true' : 'false');
  }

  // The region lives on the provider spec, which the server validates as a whole, so send
  // the full spec back with the region and its matching base URL changed.
  async function applyRegion() {
    if (!hasRegions || region === provider.region) return null;
    const cur = await api.call('get_settings');
    const spec = cur && cur.ok && cur.config && cur.config.providers && cur.config.providers[pid];
    if (!spec) return { message: 'Could not read the provider settings.' };
    const base = (REGIONS.find((r) => r.id === region) || REGIONS[0]).base;
    const r = await api.call('upsert_provider', { id: pid, ...spec, region, base_url: base });
    if (!r || !r.ok) return { message: (r && r.error && r.error.message) || 'Could not change the region.' };
    provider.region = region;
    return null;
  }

  async function saveAndTest() {
    if (busy || destroyed) return;
    let key = input.value.trim();
    if (!key) { show('Paste your API key first.', 'bad'); input.focus(); return; }
    setBusy(true);
    show('Saving…', '');
    try {
      const regionErr = await applyRegion();
      if (regionErr) { show(regionErr.message, 'bad'); return; }
      const saving = api.call('save_api_key', pid, key);
      key = '';
      input.value = '';          // cleared as soon as it has been handed over
      const saved = await saving;
      if (!saved || !saved.ok) {
        show((saved && saved.error && saved.error.message) || 'Could not save the key.', 'bad');
        return;
      }
      show('Testing the key…', '');
      const t = await api.call('test_provider', pid, null, null);
      if (!t || !t.ok) {
        const msg = (t && (t.error && typeof t.error === 'object' ? t.error.message : t.error)) || 'The key did not work.';
        const hint = t && (t.hint || (t.error && t.error.hint));
        show(hint ? `${msg} ${hint}` : msg, 'bad');
        return;
      }
      show('Key works. Sending your message…', 'good');
      if (!destroyed) onSuccess();
    } finally {
      key = '';
      if (!destroyed) { input.value = ''; setBusy(false); }
    }
  }

  save.addEventListener('click', saveAndTest);
  cancel.addEventListener('click', () => { input.value = ''; if (onCancel) onCancel(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveAndTest(); }
  });

  const node = el('section', { class: 'key-card', 'aria-labelledby': `${uid}-title` }, [
    el('h2', { class: 'kc-title', id: `${uid}-title`, text: `Add your ${name} API key` }),
    el('p', { class: 'kc-body', text: 'Stored in Windows Credential Manager, never in a file. Your message is sent as soon as the key works.' }),
    regionRow,
    el('label', { class: 'kc-label', for: `${uid}-key`, text: 'API key' }),
    el('div', { class: 'kc-field' }, [input, eye]),
    status,
    el('div', { class: 'kc-actions' }, [save, cancel, hasRegions ? linkA : null]),
  ]);

  return {
    node,
    focus: () => input.focus(),
    isBusy: () => busy,
    destroy() { destroyed = true; input.value = ''; node.remove(); },
  };
}
