// Settings > General: one card per settings section that an installed add-on registered
// (chatforge.extensions, ExtensionAPI.register_settings_section). Generic: the add-on sends
// a declarative field list (bool, int, float, text, choice); this renders it, validates
// nothing itself (Python does) and saves through set_extension_settings.
//
// Plain DOM through el(); nothing from an add-on is ever put in through innerHTML.
import { el } from './util.js';

function fieldControl(ext, f, value, uid) {
  const id = `${uid}-${f.key}`;
  let input;
  if (f.type === 'bool') {
    input = el('input', { type: 'checkbox', id, class: 'ui-switch' });
    input.checked = !!value;
  } else if (f.type === 'choice') {
    input = el('select', { id, class: 'ui-select' }, (f.choices || []).map((c) => el('option', { value: c, text: c })));
    input.value = value == null ? (f.choices || [])[0] : String(value);
  } else if (f.type === 'int' || f.type === 'float') {
    input = el('input', { type: 'number', id, class: 'ui-input', step: f.type === 'int' ? '1' : 'any' });
    if (f.min != null) input.min = String(f.min);
    if (f.max != null) input.max = String(f.max);
    input.value = value == null ? '' : String(value);
  } else {
    input = el('input', { type: 'text', id, class: 'ui-input', spellcheck: 'false', autocomplete: 'off' });
    input.value = value == null ? '' : String(value);
  }
  input.dataset.key = f.key;
  input.dataset.type = f.type;
  return input;
}

function readValue(input) {
  switch (input.dataset.type) {
    case 'bool': return input.checked;
    case 'int': return input.value === '' ? null : parseInt(input.value, 10);
    case 'float': return input.value === '' ? null : parseFloat(input.value);
    default: return input.value;
  }
}

function sectionCard(api, ext, section, onSaved) {
  const uid = `ext-${ext.name}-${section.id}`;
  const msg = el('span', { class: 'ui-text-secondary ui-small', role: 'status', 'aria-live': 'polite' });
  const save = el('button', { type: 'button', class: 'ui-btn ui-btn--sm ui-btn--primary', text: 'Save' });
  const inputs = [];
  const rows = section.fields.map((f) => {
    const input = fieldControl(ext, f, ext.values[f.key] ?? f.default, uid);
    inputs.push(input);
    const label = el('label', { class: 'ui-label', for: input.id, text: f.label });
    const help = f.help ? el('p', { class: 'ui-help', text: f.help }) : null;
    return el('div', { class: f.type === 'bool' ? 'ui-field ui-field--inline' : 'ui-field' }, [label, input, help]);
  });
  save.addEventListener('click', async () => {
    const values = {};
    for (const input of inputs) values[input.dataset.key] = readValue(input);
    save.disabled = true;
    msg.textContent = 'Saving...';
    const res = await api.call('set_extension_settings', ext.name, values);
    save.disabled = false;
    if (res && res.ok) { msg.textContent = 'Saved.'; if (onSaved) onSaved(ext.name, res.values); }
    else msg.textContent = (res && res.error && res.error.message) || 'Could not save.';
  });
  return el('section', { class: 'ui-card ext-card', 'aria-labelledby': `${uid}-h`, 'data-extension': ext.name }, [
    el('h2', { id: `${uid}-h`, class: 'ui-card__title', text: section.title }),
    section.description ? el('p', { class: 'ui-help', text: section.description }) : null,
    ...rows,
    el('div', { class: 'savebar' }, [msg, el('span', { class: 'ui-spacer' }), save]),
  ]);
}

/** Fill `container` from get_extensions. The container stays hidden when there is nothing to show. */
export async function renderExtensions(api, container, onSaved) {
  const res = await api.call('get_extensions');
  container.replaceChildren();
  const exts = res && res.ok && Array.isArray(res.extensions) ? res.extensions : [];
  for (const ext of exts) {
    if (!ext.ok) {
      container.append(el('p', { class: 'ui-callout ui-callout--warning', role: 'status',
        text: `The add-on "${ext.name}" could not be loaded: ${ext.error || 'unknown error'}` }));
      continue;
    }
    for (const section of ext.sections || []) container.append(sectionCard(api, ext, section, onSaved));
  }
  container.hidden = container.childElementCount === 0;
  return exts;
}
