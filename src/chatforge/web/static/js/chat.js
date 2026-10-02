// ChatForge popup UI.
//
// Adapted from DisPatch_Chat frontend/static/js/main.js (MIT, LaserLloyd), lifted by
// function (line numbers from ca186b1):
//   messageEl (1281-1482), appendMessageToView (1574), typingEl (1605), appendErrorBubble (1758),
//   isNearBottom/scrollToBottom/showScrollButton/updateScrollBadge (1785-1940),
//   updateSendEnabled/autosize (1941-1956), sendMessage (1957),
//   streamBuffers/scheduleStreamRender/beginStream/renderStreamMarkdown (4760-4870).
// Stripped: auth/Safe Mode, websockets, bots/threads, avatars, reactions, DisPatch's upload
// pipeline, i18n.
// New: model chip + status dot + compile/idle status line, think block, tool chips, error
// actions, inline key card, bridge events (chat.*, runtime.status, key.status, ...),
// attachments (paperclip -> attach_files, drag-and-drop and paste -> attach_data, chips that
// go with the message), document cards for files create_document saved, and the context
// divider (chat.context / "notice" items: where the model's view of a long conversation
// starts, plus a note on a message that was cut to fit).
import { api, on, ready } from './bridge.js';
import { el, railIcon, RAIL_ICONS } from './util.js?v=13';
import { renderMarkdown, enhanceContent, installMarkdownHandlers } from './markdown.js';
import { t } from './i18n.js?v=3';
import { createKeyCard } from './keycard.js';

const $ = (id) => document.getElementById(id);

// ---- Streaming paint cadence (DisPatch main.js:4778-4779) ----
const STREAM_PAINT_MS = 100;      // ~10 repaints/sec: reads as smooth, costs 6x less
const STREAM_PAINT_CHARS = 160;   // ...but a burst that big repaints immediately
const MD_OPTS = { noMedia: true, noLocal: true };

// attachments.py MAX_FILES / MAX_FILE_BYTES. Checked here as well, so a dropped file that
// cannot be attached is refused before its bytes cross the bridge.
const MAX_FILES = 10;
const MAX_FILE_BYTES = 20 * 1024 * 1024;
const TOO_MANY_FILES = `Only ${MAX_FILES} files can be attached to one message.`;
const EMPTY_REPLY = '(The model returned an empty reply.)';
// Said on the composer (and on each picture chip) while the chosen model cannot see pictures:
// it then gets a note, and any text Windows can read in the picture, instead.
const NO_VISION = 'This model can’t see images — pick a vision model in the menu.';

const ICON = {
  wrench: ['M14.7 6.3a4 4 0 0 0 5 5L13 18a2.1 2.1 0 0 1-3-3z', 'M14.7 6.3l3-3 3 3'],
  brain: ['M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 3 4 3 3 0 0 0 5 1V5a2 2 0 0 0-3-1z', 'M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-3 4 3 3 0 0 1-5 1'],
  warn: ['M12 4l10 17H2z', 'M12 10v4', 'M12 17.5v.01'],
  copy: ['M8 8h11v11H8z', 'M5 16V5h11'],
  regen: ['M20 11a8 8 0 0 0-14.3-4.9L4 8', 'M4 3.5V8h4.5', 'M4 13a8 8 0 0 0 14.3 4.9L20 16', 'M20 20.5V16h-4.5'],
  close: ['M6 6l12 12', 'M18 6L6 18'],
  file: RAIL_ICONS['viewer-file'],
  folder: RAIL_ICONS.folder,
  download: RAIL_ICONS['viewer-download'],
  image: ['M4 5h16v14H4z', 'M4 16l5-5 4 4 2.5-2.5L20 17', 'M15 9.5h.01'],
};

const S = {
  providers: [],
  selected: { provider: null, model: null },
  runtime: { state: 'unloaded' },
  config: { chat: {}, ui: {}, local: {} },
  limits: { max_prompt_chars: 4000 },
  pinned: false,
  busy: false,
  reqId: null,
  sendSeq: 0,             // bumped by every send(): tells a late send_message reply it was abandoned
  ignore: new Set(),      // request ids we no longer care about (stopped / new chat)
  cur: null,              // the assistant turn being streamed
  lastText: '',           // last text sent (for Retry)
  lastFiles: [],          // ...and the files sent with it: their ids stay valid after an error
  lastUserEl: null,
  retryRegen: false,      // the last request was a Regenerate: Retry regenerates again
  regen: null,            // a running Regenerate: {old}, the reply it replaces (hidden meanwhile)
  ctxBefore: null,        // where the context divider was when this request started (for errors)
  files: [],              // composer attachments: the bridge's view + {key, state: loading|ready}
  picking: false,         // the native file dialog is open (attach_files in flight)
  filesBlind: null,       // the chips were drawn for a model that cannot see pictures
  keyCard: null,
  menuOpen: false,
  unseen: 0,
  settingsOpeningUntil: 0,
  stopping: false,
  bootFailed: false,
  actions: [],            // quick actions in use (get_state.config.quick_actions): {id, label, hint, tools}
  action: null,           // the quick action picked in the composer for the next message
  lastAction: null,       // ...and the one the last message was sent with (for Retry)
};

// =================================================================== helpers ====

function providerById(id) { return S.providers.find((p) => p.id === id) || null; }
function providerName(id) { const p = providerById(id); return p ? p.display_name : id; }
function selectedProvider() { return providerById(S.selected.provider); }
function isLocalProvider(p) { return !!p && p.kind === 'ovms'; }
/** A provider that can answer right now: the local runtime, a server that needs no key
 *  (StudioForge), or a provider whose key is saved or set in the environment. */
function isConfigured(p) {
  if (!p) return false;
  if (isLocalProvider(p)) return true;
  const key = p.key || {};
  return key.required === false || (!!key.source && key.source !== 'none');
}
function shortModel(id) { return String(id || '').split('/').pop(); }
function maxChars() { return Number(S.limits.max_prompt_chars) || 4000; }
function showReasoning() { return (S.config.chat && S.config.chat.show_reasoning) !== 'hidden'; }

function clockTime(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtClock(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function announce(text) {
  const a = $('announcer');
  if (!a) return;
  a.textContent = '';
  // A tick later, so repeating the same sentence is still announced.
  setTimeout(() => { a.textContent = text; }, 30);
}

let toastTimer = 0;
function toast(text) {
  const n = $('toast');
  n.textContent = text;
  n.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { n.hidden = true; }, 2200);
}

/** The first string value of a tool call's arguments, for the chip label. */
function shortArg(args) {
  if (!args) return '';
  let v = '';
  try {
    const o = JSON.parse(args);
    if (o && typeof o === 'object') {
      const first = Object.values(o).find((x) => typeof x === 'string');
      v = first !== undefined ? first : '';
    } else if (typeof o === 'string') v = o;
  } catch {
    const m = /"[^"]*"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(args);
    v = m ? m[1] : '';
  }
  v = String(v).replace(/\s+/g, ' ').trim();
  return v.length > 42 ? `${v.slice(0, 41)}…` : v;
}

/** "812 B", "14 KB", "2.4 MB". */
function fmtSize(bytes) {
  const n = Math.max(0, Number(bytes) || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  const mb = n / (1024 * 1024);
  return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
}

/** "830 chars", "12.5k chars", "200k chars": short enough for a chip. */
function fmtChars(chars) {
  const n = Math.max(0, Math.round(Number(chars) || 0));
  if (n < 1000) return `${n} chars`;
  const k = n / 1000;
  return `${k < 100 ? Number(k.toFixed(1)) : Math.round(k)}k chars`;
}

function isImage(f) { return !!f && f.kind === 'image'; }
/** A picture's thumbnail from the bridge: only a data: URL of a picture is ever shown. */
function thumbUrl(f) {
  const url = String((f && f.thumb) || '');
  return /^data:image\/(?:jpeg|png|gif|webp);base64,[A-Za-z0-9+/=]+$/.test(url) ? url : '';
}
/** "1568 × 1176", or '' when the size is not known. */
function fmtPixels(f) { return f && f.width && f.height ? `${f.width} × ${f.height}` : ''; }
/** The selected model sees pictures: the bridge's provider view says so per model. */
function canSeeImages() {
  const p = selectedProvider();
  return !!(p && p.vision && p.vision[S.selected.model]);
}

/** "DOCX" for "report.docx"; '' when the name has no extension. */
function fileExt(name) {
  const m = /\.([A-Za-z0-9]{1,8})$/.exec(String(name || ''));
  return m ? m[1].toUpperCase() : '';
}

// ===================================================================== scroll ====
// Lifted from DisPatch main.js:1785-1940.

function messagesBox() { return $('messages'); }

function isNearBottom(px = 140) {
  const b = messagesBox();
  return b.scrollHeight - b.scrollTop - b.clientHeight < px;
}

function scrollToBottom(instant = false) {
  const b = messagesBox();
  if (instant) {
    b.style.scrollBehavior = 'auto';
    b.scrollTop = b.scrollHeight;
    requestAnimationFrame(() => {
      b.scrollTop = b.scrollHeight;
      requestAnimationFrame(() => { b.scrollTop = b.scrollHeight; b.style.scrollBehavior = ''; });
    });
  } else {
    b.scrollTo({ top: b.scrollHeight, behavior: 'smooth' });
    requestAnimationFrame(() => b.scrollTo({ top: b.scrollHeight, behavior: 'smooth' }));
  }
  showScrollButton(false);
}

function updateScrollBadge() {
  const b = $('sb-count');
  if (!b) return;
  if (S.unseen > 0) { b.textContent = S.unseen > 99 ? '99+' : String(S.unseen); b.classList.remove('hidden'); }
  else b.classList.add('hidden');
}
function bumpUnseen() { S.unseen += 1; updateScrollBadge(); }
function showScrollButton(show) {
  $('scroll-bottom').classList.toggle('hidden', !show);
  if (!show) { S.unseen = 0; updateScrollBadge(); }
}

/** Keep the view pinned if the reader is at the bottom; otherwise offer the badge. */
function stickOrBadge(newRow = false) {
  if (isNearBottom(newRow ? 140 : 220)) scrollToBottom(true);
  else { showScrollButton(true); if (newRow) bumpUnseen(); }
}

// ================================================================ message rows ====

function clearEmpty() {
  const e = messagesBox().querySelector('.empty-state');
  if (e) e.remove();
}

function addRow(node, { assistant = false } = {}) {
  clearEmpty();
  messagesBox().append(node);
  stickOrBadge(assistant);
  syncRegenerate();
}

/** A user message: its attached files as chips over the bubble. A files-only message
 *  has no bubble. `ts` (the engine's _ts, also set later from chat.start) lets chat.context
 *  find the row; `cut` marks a message that was cut to fit the context window. */
function userRow(text, ts, files = [], { cut = false, action = null } = {}) {
  const kids = [];
  if (action) kids.push(actionTag(action));   // the quick action it was sent with
  if (files.length) {
    kids.push(el('div', { class: 'msg-files', role: 'list', 'aria-label': 'Attached files' }, files.map((f) => fileChip(f))));
  }
  if (text || !files.length) kids.push(el('div', { class: 'bubble', dir: 'auto', text }));
  kids.push(el('div', { class: 'msg-time', text: clockTime(ts) }));
  const row = el('div', { class: 'msg user', dataset: ts != null ? { ts: String(ts) } : null }, [el('div', { class: 'msg-col' }, kids)]);
  if (cut) setCutNote(row, true);
  return row;
}

// ============================================================ context window ====
// When a conversation no longer fits the model's context window, the engine leaves its
// oldest turns out of the prompt (chat.context live, a "notice" item in a snapshot). A thin
// divider marks where the model's view starts; a message cut to fit says so in its meta line.

const CONTEXT_CUT_TEXT = 'Older messages are past the context window (too long for context).';
const CUT_NOTE = '(Too long for context)';

function contextDivider(text = CONTEXT_CUT_TEXT) {
  return el('div', { class: 'ctx-divider', role: 'separator', 'aria-label': text }, [
    el('span', { class: 'ctx-divider-text', text }),
  ]);
}

function setCutNote(row, cut) {
  const note = row.querySelector('.ctx-cut');
  if (!cut) { if (note) note.remove(); return; }
  const meta = row.querySelector('.msg-time');
  if (note || !meta) return;
  meta.append(el('span', { class: 'ctx-cut', title: 'Too long for the model’s context window: only part of this message was sent.', text: ` ${CUT_NOTE}` }));
}

/** Put the divider before the first user message sent at or after `firstTs` (the engine's
 *  first_kept_ts), or remove it when nothing was dropped. A row this popup has no time for
 *  falls back to the message being answered: then the divider claims less than the model sees,
 *  never more. */
function placeContextDivider(firstTs, dropped) {
  const box = messagesBox();
  const current = box.querySelector('.ctx-divider');
  let row = null;
  if (dropped) {
    const start = Number(firstTs);
    if (firstTs != null && Number.isFinite(start)) {
      row = [...box.querySelectorAll('.msg.user')].find((r) => r.dataset.ts != null && Number(r.dataset.ts) >= start) || null;
    }
    if (!row && S.lastUserEl && S.lastUserEl.isConnected) row = S.lastUserEl;
  }
  // Nothing before the row means nothing on screen is out of view.
  const before = row && row.previousElementSibling;
  if (!before || (before === current && !current.previousElementSibling)) {
    if (current) current.remove();
    return;
  }
  if (before !== current) row.before(current || contextDivider());
}

/** The divider's place (the row after it), to put it back if a request fails. */
function dividerAnchor() {
  const d = messagesBox().querySelector('.ctx-divider');
  return d ? { next: d.nextElementSibling } : null;
}

function restoreDivider(anchor) {
  const d = messagesBox().querySelector('.ctx-divider');
  if (anchor && anchor.next && anchor.next.isConnected) {
    if (anchor.next.previousElementSibling !== d) anchor.next.before(d || contextDivider());
  } else if (d) d.remove();
}

const PICTURE_NAME = /\.(?:png|jpe?g|jpe|jfif|gif|bmp|dib|webp|tiff?|avif|heic|heif)$/i;

/** One attached file: name, size · characters, a "partial" badge when only part of its
 *  text is used, and (in the composer) a remove button. `f` is the bridge's view
 *  {id, name, kind, chars, size, truncated, warning}, or a saved message's {name, kind,
 *  chars, truncated}. A picture (kind "image", plus {width, height, thumb}) shows its
 *  thumbnail (alt text: its name) and its size in pixels; `blind` (the chosen model cannot
 *  see pictures) adds a warning to it. */
function fileChip(f, { onRemove = null, blind = false } = {}) {
  const loading = f.state === 'loading';
  const name = String(f.name || 'file');
  const image = isImage(f) || (loading && PICTURE_NAME.test(name));
  const meta = loading ? 'Reading…'
    : (image ? [fmtPixels(f) || null, f.size != null ? fmtSize(f.size) : null]
      : [f.size != null ? fmtSize(f.size) : null, f.chars != null ? fmtChars(f.chars) : null]).filter(Boolean).join(' · ');
  const note = [image && blind && !loading ? NO_VISION : null,
    f.warning || (f.truncated ? 'Only part of this file is used.' : null)].filter(Boolean).join(' ');
  const title = [
    name,
    image && !loading ? fmtPixels(f) || null : null,
    f.size != null ? fmtSize(f.size) : null,
    !loading && !image && f.chars != null ? `${Number(f.chars).toLocaleString()} characters of text` : null,
  ].filter(Boolean).join(', ');
  const thumb = image ? thumbUrl(f) : '';
  const parts = [
    el('span', { class: 'att-name', dir: 'auto', text: name }),
    meta ? el('span', { class: 'att-meta', text: meta }) : null,
    note ? el('span', { class: 'att-warn', role: 'img', 'aria-label': note }, [
      railIcon(ICON.warn),
      f.truncated ? el('span', { 'aria-hidden': 'true', text: 'partial' }) : null,
    ]) : null,
  ];
  const chip = el('span', { class: image ? 'att-chip att-image' : 'att-chip', role: 'listitem',
    title: note ? `${title}. ${note}` : title, dataset: { state: loading ? 'loading' : 'ready' } }, [
    thumb ? el('img', { class: 'att-thumb', src: thumb, alt: name, draggable: 'false' }) : railIcon(image ? ICON.image : ICON.file),
    ...(image ? [el('span', { class: 'att-cap' }, parts)] : parts),
  ]);
  if (onRemove && !loading) {
    const b = el('button', { class: 'att-remove', type: 'button', 'aria-label': `Remove ${name}`, title: 'Remove' }, [railIcon(ICON.close)]);
    b.addEventListener('click', onRemove);
    chip.append(b);
  }
  return chip;
}

/** A file create_document saved: name, type · size, Open, Download and Show in folder. */
function docCard(doc) {
  const name = String(doc.name || 'document');
  const meta = [fileExt(name) || null, doc.size != null ? fmtSize(doc.size) : null].filter(Boolean).join(' · ');
  const open = el('button', { class: 'btn btn-sm doc-open', type: 'button', 'aria-label': `Open ${name}`, text: 'Open' });
  const save = el('button', { class: 'btn btn-sm doc-save', type: 'button', 'aria-label': `Download ${name}` }, [
    railIcon(ICON.download), el('span', { text: 'Download' }),
  ]);
  const reveal = el('button', { class: 'btn btn-sm doc-reveal', type: 'button', 'aria-label': `Show in folder: ${name}` }, [
    railIcon(ICON.folder), el('span', { text: 'Show in folder' }),
  ]);
  open.addEventListener('click', () => documentAction('open_document', doc.path, open));
  save.addEventListener('click', () => documentAction('save_document', doc.path, save));
  reveal.addEventListener('click', () => documentAction('reveal_document', doc.path, reveal));
  return el('div', { class: 'doc-card', role: 'group', 'aria-label': `Document ${name}`, title: doc.path || name }, [
    el('span', { class: 'doc-icon' }, [railIcon(ICON.file)]),
    el('div', { class: 'doc-info' }, [
      el('div', { class: 'doc-name', dir: 'auto', text: name }),
      meta ? el('div', { class: 'doc-meta', text: meta }) : null,
    ]),
    el('div', { class: 'doc-actions' }, [open, save, reveal]),
  ]);
}

/** "Downloads\Report.docx" for a full path: the folder and the file, short enough for a toast. */
function shortPath(path) {
  const parts = String(path || '').split(/[\\/]/).filter(Boolean);
  return parts.length > 2 ? parts.slice(-2).join(path.includes('\\') ? '\\' : '/') : String(path || '');
}

/** open_document / save_document / reveal_document. The bridge only accepts files in the
 *  documents folder; a moved or deleted file comes back as not_found, said in a toast.
 *  save_document shows the Save As dialog: "Saved to …" when a copy was made, nothing when
 *  it was cancelled. */
async function documentAction(method, path, button) {
  if (button.disabled) return;
  const focused = document.activeElement === button;
  button.disabled = true;
  const r = await api.call(method, path);
  button.disabled = false;
  // Disabling the focused button dropped the focus; give it back so the keyboard keeps its place.
  if (focused && (!document.activeElement || document.activeElement === document.body)) button.focus();
  const saving = method === 'save_document';
  if (r && r.ok) {
    if (saving && !r.cancelled && r.path) toast(`Saved to ${shortPath(r.path)}`);
    return;
  }
  toast((r && r.error && r.error.message) || (saving ? 'The document could not be saved.' : 'The document could not be opened.'));
}

function addDocCard(turn, doc) {
  if (!doc || !doc.path) return;
  turn.docs.hidden = false;
  turn.docs.append(docCard(doc));
}

function toolChip(call) {
  const arg = shortArg(call.arguments);
  // ok is null in a snapshot for a call that has no result yet; only false means failed.
  const state = call.ok == null ? 'running' : (call.ok ? 'ok' : 'fail');
  const chip = el('span', { class: 'tool-chip', dataset: { state, callId: call.call_id || '' } }, [
    railIcon(ICON.wrench),
    el('span', { class: 'tool-name', text: call.name }),
    arg ? el('span', { class: 'tool-arg', text: `“${arg}”` }) : null,
    el('span', { class: 'tool-state' }),
    el('span', { class: 'tool-sum' }),
  ]);
  setChipState(chip, state, call.summary);
  return chip;
}

function setChipState(chip, state, summary) {
  chip.dataset.state = state;
  const mark = chip.querySelector('.tool-state');
  const sum = chip.querySelector('.tool-sum');
  const label = { running: 'running', ok: 'done', fail: 'failed' }[state];
  mark.textContent = { running: '…', ok: '✓', fail: '✗' }[state];
  mark.setAttribute('role', 'img');
  mark.setAttribute('aria-label', label);
  sum.textContent = summary || '';
  chip.title = summary ? `${chip.querySelector('.tool-name').textContent}: ${summary}` : chip.querySelector('.tool-name').textContent;
}

/** The assistant turn: think block, tool chips, bubble (+ typing placeholder while waiting),
 *  then the cards of documents it saved. The bubble goes in before the cards, so a card
 *  that arrives with its tool result still ends up under the text that mentions it. */
function newTurn({ streaming }) {
  const col = el('div', { class: 'msg-col' });
  const node = el('div', { class: `msg assistant${streaming ? ' streaming' : ''}` }, [col]);
  const turn = {
    node, col, content: '', reasoning: '', think: null, thinkBody: null, thinkLabel: null, thinkText: null,
    tools: el('div', { class: 'tools' }), docs: el('div', { class: 'docs' }), bubble: null, md: null,
    cursor: null, typing: null, phaseEl: null, chips: new Map(), paint: { at: 0, len: 0 }, timer: 0, queued: false,
  };
  turn.tools.hidden = true;
  turn.docs.hidden = true;
  col.append(turn.tools, turn.docs);
  return turn;
}

function ensureThink(turn) {
  if (turn.think || !showReasoning()) return;
  const label = el('span', { class: 'think-label', text: 'Thinking…' });
  const body = el('div', { class: 'think-body' });
  const text = document.createTextNode('');
  body.append(text);
  const details = el('details', { class: 'think' }, [el('summary', {}, [railIcon(ICON.brain), label]), body]);
  turn.col.insertBefore(details, turn.col.firstChild);
  Object.assign(turn, { think: details, thinkBody: body, thinkLabel: label, thinkText: text });
}

function ensureBubble(turn) {
  if (turn.bubble) return;
  if (turn.typing) { turn.typing.remove(); turn.typing = null; }
  turn.md = el('div', { class: 'ui-markdown' });
  turn.bubble = el('div', { class: 'bubble', dir: 'auto' }, [turn.md]);
  turn.col.insertBefore(turn.bubble, turn.docs);
}

function addTypingPlaceholder(turn, text) {
  turn.phaseEl = el('span', { class: 'typing-hint', text });
  turn.typing = el('div', { class: 'bubble typing-bubble', role: 'status', 'aria-label': 'Waiting for a reply' }, [
    el('span', { class: 'dots', 'aria-hidden': 'true' }, [el('span'), el('span'), el('span')]),
    turn.phaseEl,
  ]);
  turn.col.insertBefore(turn.typing, turn.docs);
}

function paintThinkLabel(turn, streaming) {
  if (!turn.think) return;
  turn.thinkLabel.textContent = streaming ? 'Thinking…' : 'Reasoning';
  turn.think.classList.toggle('is-live', streaming);
}

function finishMeta(turn, { ts, model, tokPerS, note } = {}) {
  const bits = [clockTime(ts)];
  if (model) bits.push(shortModel(model));
  if (tokPerS) bits.push(`${Number(tokPerS).toFixed(1)} tok/s`);
  if (note) bits.push(note);
  turn.col.append(el('div', { class: 'msg-time', text: bits.join(' · ') }));
  turn.col.append(el('div', { class: 'msg-actions' }, [
    turn.content ? copyButton(() => turn.content) : null,
    regenerateButton(),
  ]));
  syncRegenerate();
}

function copyButton(getText) {
  const b = el('button', { class: 'msg-act-btn', type: 'button', title: 'Copy the reply' }, [
    railIcon(ICON.copy), el('span', { text: t('msg.copy') }),
  ]);
  b.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(getText() || '');
      const label = b.querySelector('span');
      label.textContent = t('msg.copied'); b.classList.add('ok');
      setTimeout(() => { label.textContent = t('msg.copy'); b.classList.remove('ok'); }, 1200);
    } catch { toast('Copy is not available here.'); }
  });
  return b;
}

function regenerateButton() {
  const b = el('button', { class: 'msg-act-btn act-regen', type: 'button', title: 'Ask again for a new reply' }, [
    railIcon(ICON.regen), el('span', { text: t('msg.regenerate') }),
  ]);
  b.addEventListener('click', regenerate);
  return b;
}

/** Regenerate is offered on the latest reply only (the last row, so not under an error or a
 *  key card), and not while a reply is running. */
function syncRegenerate() {
  const box = messagesBox();
  const rows = [...box.querySelectorAll('.msg')].filter((r) => !r.hidden);
  const last = rows[rows.length - 1] || null;
  for (const b of box.querySelectorAll('.act-regen')) b.hidden = S.busy || !last || !last.contains(b);
}

/** A finished assistant message from the conversation snapshot. */
function assistantRowFromData(m) {
  const turn = newTurn({ streaming: false });
  if (m.reasoning) {
    ensureThink(turn);
    if (turn.think) { turn.thinkText.data = m.reasoning; paintThinkLabel(turn, false); }
  }
  for (const c of (m.tools || [])) {
    turn.tools.hidden = false;
    turn.tools.append(toolChip(c));
  }
  if (m.content) {
    ensureBubble(turn);
    turn.content = m.content;
    turn.md.innerHTML = renderMarkdown(m.content, MD_OPTS);
    enhanceContent(turn.md, { noLocal: true });
  } else if (!m.reasoning && !(m.tools || []).length && !(m.documents || []).length && !m.stopped) {
    ensureBubble(turn);
    turn.md.append(el('span', { class: 'muted', text: EMPTY_REPLY }));
  }
  for (const d of (m.documents || [])) addDocCard(turn, d);
  finishMeta(turn, { ts: m.ts, model: m.model, note: m.stopped ? 'stopped' : undefined });
  return turn.node;
}

/** The meta note of a stopped reply: "stopped", or the engine's reason when it gave one
 *  ("Stopped: the local model was unloaded." -> "stopped: the local model was unloaded"). */
function stoppedNote(message) {
  const m = String(message || '').trim().replace(/\.$/, '');
  if (!m || m.toLowerCase() === 'stopped') return 'stopped';
  return m.charAt(0).toLowerCase() + m.slice(1);
}

// ===================================================================== streaming ====

function scheduleStreamRender(turn) {
  if (turn.queued) return;
  const now = performance.now();
  const wait = Math.max(0, STREAM_PAINT_MS - (now - turn.paint.at));
  const burst = turn.content.length - turn.paint.len >= STREAM_PAINT_CHARS;
  turn.queued = true;
  const run = () => requestAnimationFrame(() => { turn.queued = false; paintStream(turn); });
  if (burst || wait === 0) run(); else turn.timer = setTimeout(run, wait);
}

function paintStream(turn) {
  // A frame queued before the turn ended must not repaint it: chat.done usually lands in
  // the same event batch as the last delta, and this raw repaint would replace the
  // finalized, enhanced reply (code highlighting, table wrappers, link chrome). The same
  // goes for a turn dropped by New chat, chat.reset or chat.fallback.
  if (!turn.md || !turn.node.isConnected || !turn.node.classList.contains('streaming')) return;
  turn.paint = { at: performance.now(), len: turn.content.length };
  turn.md.innerHTML = renderMarkdown(turn.content, MD_OPTS);
  stickOrBadge(false);
}

function startTurn() {
  const turn = newTurn({ streaming: true });
  addTypingPlaceholder(turn, 'Waiting…');
  S.cur = turn;
  addRow(turn.node, { assistant: true });
  return turn;
}

function curTurn() { return S.cur || startTurn(); }

function accept(evt) {
  if (!evt.request_id) return false;
  if (S.ignore.has(evt.request_id)) return false;
  if (!S.busy) return false;
  if (!S.reqId) S.reqId = evt.request_id;
  return evt.request_id === S.reqId;
}

const PHASE_TEXT = {
  loading_model: 'Loading the model…',
  thinking: 'Thinking…',
  calling_tool: 'Using a tool…',
  generating: 'Writing…',
  reading_image: 'Reading the text in the picture…',
};

function endRequest() {
  S.busy = false;
  S.reqId = null;
  S.stopping = false;
  S.cur = null;
  reflectComposer();
}

function turnIsEmpty(turn) {
  return !turn.content && !turn.reasoning && !turn.chips.size;
}

function finalizeTurn(turn, meta) {
  clearTimeout(turn.timer);
  turn.queued = false;
  if (turn.typing) { turn.typing.remove(); turn.typing = null; }
  if (turn.md) {
    turn.md.innerHTML = renderMarkdown(turn.content, MD_OPTS);
    enhanceContent(turn.md, { noLocal: true });   // code highlight, sortable tables, link chrome
  }
  if (turn.cursor) { turn.cursor.remove(); turn.cursor = null; }
  turn.node.classList.remove('streaming');
  paintThinkLabel(turn, false);
  finishMeta(turn, meta);   // also under an empty reply: Regenerate is most useful there
}

// ===================================================================== errors ====

function errorRow(err) {
  const message = err.message || 'Something went wrong.';
  const bubble = el('div', { class: 'bubble error-bubble', dir: 'auto', role: 'alert' }, [
    el('div', { class: 'err-line' }, [railIcon(ICON.warn), el('span', { class: 'err-msg', text: message })]),
    err.hint ? el('div', { class: 'err-hint', text: err.hint }) : null,
  ]);
  const actions = el('div', { class: 'err-actions' });
  const btn = (label, fn) => {
    const b = el('button', { class: 'btn btn-sm', type: 'button', text: label });
    b.addEventListener('click', fn);
    actions.append(b);
  };
  if (err.action === 'add_key') {
    btn('Add key', () => { row.remove(); showKeyCard(S.selected.provider, S.lastText, S.lastFiles, S.lastAction); });
  } else if (err.action === 'open_settings') {
    btn('Open settings', openSettings);
    if (S.lastText || S.lastFiles.length || S.retryRegen) btn('Retry', () => { row.remove(); retry(); });
  } else if (err.action === 'retry') {
    btn('Retry', () => { row.remove(); retry(); });
  }
  if (actions.childElementCount) bubble.append(actions);
  const row = el('div', { class: 'msg error' }, [el('div', { class: 'msg-col' }, [bubble])]);
  return row;
}

function showError(err) {
  addRow(errorRow(err), { assistant: true });
  announce(err.message || 'Error');
}

// =============================================================== key card flow ====

function showKeyCard(providerId, pendingText, pendingFiles = [], pendingAction = null) {
  closeKeyCard();
  const provider = providerById(providerId) || { id: providerId, display_name: providerId, region: null };
  // The key works (saved by the card, or in Settings while the card was open): close the
  // card and send the pending message, taking it (text and files) back out of the composer.
  // The files are read from the card's state when it finishes: a chip removed meanwhile
  // (removeFile) is gone from the backend, and sending its id would fail the whole message.
  const finish = async () => {
    const files = kc.pendingFiles;
    closeKeyCard();
    await refreshProviders();
    const text = pendingText;
    const input = $('input');
    if (text && input.value.trim() === text) { input.value = ''; autosize(); }
    if (files.length && sameFiles(readyFiles(), files)) setComposerFiles([]);
    // The quick action it was sent with goes with it again (and out of the composer).
    const action = pendingAction;
    if (action && S.action && S.action.id === action.id) setComposerAction(null, { focus: false });
    if (text || files.length) send(text, { files, action });
    input.focus();
  };
  const card = createKeyCard({
    api,
    provider,
    onSuccess: finish,
    onCancel: () => { closeKeyCard(); $('input').focus(); },
  });
  const kc = { card, providerId, pendingText, pendingFiles: [...pendingFiles], finish };
  S.keyCard = kc;
  const row = el('div', { class: 'msg card' }, [card.node]);
  S.keyCard.row = row;
  addRow(row, { assistant: true });
  card.focus();
}

function closeKeyCard() {
  if (!S.keyCard) return;
  S.keyCard.card.destroy();
  S.keyCard.row.remove();
  S.keyCard = null;
  renderEmpty();
  syncRegenerate();
}

async function refreshProviders() {
  const r = await api.call('list_providers');
  if (r && r.ok && Array.isArray(r.providers)) { S.providers = r.providers; renderChip(); renderEmpty(); syncVision(); }
}

// ================================================================ attachments ====
// Files attached in the composer (S.files). The ids are the backend's: send_message takes
// them with the message, remove_attachment forgets one. A sent file leaves the composer
// WITHOUT remove_attachment: the backend keeps it until the reply finishes or is stopped,
// so Retry and the key card can send the same ids again.

let fileKey = 0;

function readyFiles() { return S.files.filter((f) => f.state === 'ready'); }
function filesLoading() { return S.files.some((f) => f.state === 'loading'); }
/** The bridge's view of an attachment, without the composer's own fields. */
function fileView({ key, state, ...view }) { return view; }
function sameFiles(a, b) { return a.length === b.length && a.every((f, i) => f.id === b[i].id); }

function setComposerFiles(views) {
  S.files = views.map((v) => ({ ...fileView(v), key: ++fileKey, state: 'ready' }));
  renderFiles();
}

function renderFiles() {
  const keepBottom = isNearBottom();   // the chips shrink the conversation: stay at the end
  const list = $('attach-list');
  const blind = !canSeeImages();
  S.filesBlind = blind;
  list.replaceChildren(...S.files.map((f) => fileChip(f, { onRemove: () => removeFile(f.key), blind })));
  list.hidden = !S.files.length;
  // A picture the chosen model cannot see: said once over the chips (each also warns).
  const note = $('attach-note');
  const warn = blind && S.files.some((f) => isImage(f) && f.state === 'ready') ? NO_VISION : '';
  if (note && note.textContent !== warn) note.textContent = warn;
  if (note) note.hidden = !warn;
  $('attach').setAttribute('aria-busy', S.picking ? 'true' : 'false');
  $('attach').disabled = S.picking;
  if (keepBottom && messagesBox().querySelector('.msg')) scrollToBottom(true);
  updateSendEnabled();
}

/** The chosen model (or what it can do) changed: redraw the picture chips when whether it
 *  sees pictures did. */
function syncVision() {
  if (S.filesBlind !== !canSeeImages() && S.files.some(isImage)) renderFiles();
}

function removeFile(key) {
  const i = S.files.findIndex((f) => f.key === key);
  if (i < 0) return;
  const [f] = S.files.splice(i, 1);
  if (f.id) {
    api.call('remove_attachment', f.id);
    // A chip put back after no_key may still be pending for the key card or Retry: the
    // backend no longer has it, so it is not sent again.
    const keep = (p) => p.id !== f.id;
    if (S.keyCard) S.keyCard.pendingFiles = S.keyCard.pendingFiles.filter(keep);
    S.lastFiles = S.lastFiles.filter(keep);
  }
  renderFiles();
  // Keep the keyboard in the list: the chip that took its place, else the message box.
  const buttons = $('attach-list').querySelectorAll('.att-remove');
  (buttons[Math.min(i, buttons.length - 1)] || $('input')).focus();
  announce(`Removed ${f.name}`);
}

function clearAttachErrors() {
  const box = $('attach-errors');
  box.replaceChildren();
  box.hidden = true;
}

/** List files that could not be attached ({name, message}) above the composer. They stay
 *  until dismissed, or until the next attach or send. */
function addAttachErrors(errors) {
  if (!errors || !errors.length) return;
  const box = $('attach-errors');
  let list = box.querySelector('.att-err-list');
  if (!list) {
    list = el('div', { class: 'att-err-list' });
    const close = el('button', { class: 'att-err-close', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss' }, [railIcon(ICON.close)]);
    close.addEventListener('click', () => { clearAttachErrors(); $('input').focus(); });
    box.replaceChildren(list, close);
  }
  for (const e of errors) {
    list.append(el('div', { class: 'att-err' }, [
      e.name ? el('span', { class: 'att-err-name', dir: 'auto', text: e.name }) : null,
      el('span', { class: 'att-err-msg', text: e.message || 'The file could not be attached.' }),
    ]));
  }
  box.hidden = false;
}

function announceAttached(added) {
  if (added.length === 1) announce(`Attached ${added[0].name}`);
  else if (added.length > 1) announce(`Attached ${added.length} files`);
}

/** The paperclip: the app's native file dialog (attach_files). */
async function pickFiles() {
  if (S.picking) return;
  clearAttachErrors();
  if (S.files.length >= MAX_FILES) {
    addAttachErrors([{ name: '', message: `${TOO_MANY_FILES} Remove one to add another.` }]);
    return;
  }
  S.picking = true;   // the dialog takes the focus: the blur handler must not hide the popup
  renderFiles();
  const r = await api.call('attach_files');
  S.picking = false;
  const added = [];
  const errors = [];
  if (!r || !r.ok) {
    errors.push({ name: '', message: (r && r.error && r.error.message) || 'The files could not be attached.' });
  } else {
    errors.push(...(r.errors || []));
    for (const a of r.attachments || []) {
      // The dialog does not know how many files the composer already holds.
      if (S.files.length >= MAX_FILES) {
        api.call('remove_attachment', a.id);
        errors.push({ name: a.name, message: TOO_MANY_FILES });
        continue;
      }
      const f = { ...a, key: ++fileKey, state: 'ready' };
      S.files.push(f);
      added.push(f);
    }
  }
  renderFiles();
  addAttachErrors(errors);
  announceAttached(added);
  $('input').focus();
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('read failed'));
    reader.readAsDataURL(file);
  });
}

const PASTED_EXT = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp', 'image/bmp': 'bmp' };

/** The name a pasted file is attached under. A pasted screenshot has no name of its own
 *  (WebView2 calls each one "image.png"), so it is "Pasted image 14.05.33.png", plus
 *  " (2)" and so on when several come in one paste. */
function pastedName(file, n) {
  const name = String(file.name || '');
  const type = String(file.type || '').toLowerCase();
  if (!type.startsWith('image/') || (name && !/^image\.\w+$/i.test(name))) return name || 'file';
  const d = new Date();
  const hms = [d.getHours(), d.getMinutes(), d.getSeconds()].map((x) => String(x).padStart(2, '0')).join('.');
  return `Pasted image ${hms}${n ? ` (${n + 1})` : ''}.${PASTED_EXT[type] || 'png'}`;
}

/** Dropped or pasted files: each is checked here (count, 20 MB), shown as a "Reading…"
 *  chip, read as a data: URL and handed to attach_data, one at a time. `pasted`: name
 *  pasted screenshots (pastedName). */
async function attachLocalFiles(fileList, { pasted = false } = {}) {
  const files = Array.from(fileList || []).filter(Boolean);
  if (!files.length) return;
  clearAttachErrors();
  const errors = [];
  const jobs = [];
  for (const [n, file] of files.entries()) {
    const name = pasted ? pastedName(file, n) : String(file.name || 'file');
    if (S.files.length >= MAX_FILES) { errors.push({ name, message: TOO_MANY_FILES }); continue; }
    if (Number(file.size) > MAX_FILE_BYTES) { errors.push({ name, message: 'The file is larger than 20 MB.' }); continue; }
    const entry = { key: ++fileKey, name, size: Number(file.size) || 0, state: 'loading' };
    S.files.push(entry);
    jobs.push([entry, file]);
  }
  renderFiles();
  addAttachErrors(errors);
  const added = [];
  for (const [entry, file] of jobs) {
    let r;
    try {
      r = await api.call('attach_data', entry.name, await readAsDataUrl(file));
    } catch {
      r = { ok: true, attachments: [], errors: [{ name: entry.name, message: 'The file could not be read.' }] };
    }
    const got = r && r.ok ? (r.attachments || [])[0] : null;
    const i = S.files.indexOf(entry);
    if (i < 0) {   // the composer was emptied meanwhile
      if (got) api.call('remove_attachment', got.id);
      continue;
    }
    if (got) {
      S.files[i] = { ...got, key: entry.key, state: 'ready' };
      added.push(S.files[i]);
    } else {
      S.files.splice(i, 1);
    }
    renderFiles();
    addAttachErrors(r && r.ok ? (r.errors || [])
      : [{ name: entry.name, message: (r && r.error && r.error.message) || 'The file could not be attached.' }]);
  }
  announceAttached(added);
}

/** Drag-and-drop anywhere on the popup, and pasting files. */
function wireAttachments() {
  $('attach').addEventListener('click', pickFiles);
  const zone = $('drop-zone');
  const carriesFiles = (e) => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  // dragenter/dragleave fire for every element crossed: count them to know when the drag
  // has left the window.
  let depth = 0;
  document.addEventListener('dragenter', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    depth += 1;
    zone.hidden = false;
  });
  // Without preventDefault on dragover the drop never comes, and WebView2 navigates to the file.
  document.addEventListener('dragover', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  document.addEventListener('dragleave', (e) => {
    if (!carriesFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) zone.hidden = true;
  });
  document.addEventListener('drop', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    depth = 0;
    zone.hidden = true;
    attachLocalFiles(e.dataTransfer.files);
  });
  document.addEventListener('paste', (e) => {
    const cd = e.clipboardData;
    if (!cd) return;
    let files = Array.from(cd.files || []);
    if (!files.length) {
      files = Array.from(cd.items || []).filter((it) => it.kind === 'file').map((it) => it.getAsFile()).filter(Boolean);
    }
    if (!files.length) return;
    // Copying from Word or Excel puts a picture of the selection on the clipboard next to
    // its text: the text is what was meant, so it pastes as usual. A picture alone (a
    // screenshot) is attached.
    if ((cd.getData('text/plain') || '').trim()) return;
    e.preventDefault();
    attachLocalFiles(files, { pasted: true });
  });
  // The toast sits just above the composer, which grows with the chips.
  if (typeof ResizeObserver === 'function') {
    const composer = $('composer');
    new ResizeObserver(() => {
      document.documentElement.style.setProperty('--composer-h', `${composer.offsetHeight}px`);
    }).observe(composer);
  }
}

// ==================================================================== sending ====

function updateSendEnabled() {
  const text = $('input').value;
  // A message can be files only. Not while a dropped file is still being read.
  const has = text.trim().length > 0 || readyFiles().length > 0;
  $('send').disabled = !has || text.length > maxChars() || S.busy || filesLoading();
}

function reflectComposer() {
  syncRegenerate();
  $('send').classList.toggle('hidden', S.busy);
  $('stop').classList.toggle('hidden', !S.busy);
  const stop = $('stop');
  stop.disabled = S.stopping;
  const label = S.stopping ? 'Stopping…' : 'Stop';
  stop.setAttribute('aria-label', label);
  stop.title = S.stopping ? 'Stopping…' : 'Stop generating';
  messagesBox().setAttribute('aria-busy', S.busy ? 'true' : 'false');
  updateSendEnabled();
}

function autosize() {
  const ta = $('input');
  ta.style.height = 'auto';
  ta.style.height = `${Math.min(ta.scrollHeight, 150)}px`;
  const n = ta.value.length;
  const limit = maxChars();
  const counter = $('char-count');
  // Visible once the text passes 80% of the limit.
  counter.hidden = n <= limit * 0.8;
  if (!counter.hidden) {
    counter.textContent = `${n.toLocaleString()} / ${limit.toLocaleString()}`;
    counter.dataset.over = n > limit ? 'true' : 'false';
  }
  updateSendEnabled();
}

/** Send `text` with the attached `files` (bridge views with their ids) and the quick
 *  `action` ({id, label, hint}) it runs, if any. `showUser` false when re-sending (Retry,
 *  key card) and the bubble is already there. */
async function send(text, { showUser = true, files = [], action = null } = {}) {
  text = String(text || '').trim();
  files = files.map(fileView);
  if ((!text && !files.length) || S.busy) return;
  if (text.length > maxChars()) { toast(`Message is longer than ${maxChars().toLocaleString()} characters.`); return; }
  S.busy = true; S.reqId = null; S.stopping = false; S.lastText = text; S.lastFiles = files; S.retryRegen = false;
  S.lastAction = action;
  S.ctxBefore = dividerAnchor();
  const mine = ++S.sendSeq;
  if (showUser) { S.lastUserEl = userRow(text, undefined, files, { action }); addRow(S.lastUserEl); scrollToBottom(true); }   // your own message always follows you down
  startTurn();
  reflectComposer();
  // Without files (or a quick action) send_message is called exactly as before they existed.
  const ids = files.map((f) => f.id);
  const r = action ? await api.call('send_message', text, ids, action.id)
    : ids.length ? await api.call('send_message', text, ids) : await api.call('send_message', text);
  adoptRequest(r, mine, 'Could not send the message.');
}

/** Regenerate: ask again for a new reply to the latest message. The old reply is hidden
 *  meanwhile; it is removed when the new one is done, and shown again if the new one fails
 *  or is stopped before it says anything (the engine then keeps it: chat.error restored). */
async function regenerate() {
  if (S.busy) return;
  const rows = [...messagesBox().querySelectorAll('.msg')].filter((r) => !r.hidden);
  const old = rows[rows.length - 1];
  if (!old || !old.classList.contains('assistant')) return;
  const users = rows.filter((r) => r.classList.contains('user'));
  S.busy = true; S.reqId = null; S.stopping = false;
  S.lastText = ''; S.lastFiles = []; S.retryRegen = true; S.lastAction = null;
  S.lastUserEl = users[users.length - 1] || null;
  S.ctxBefore = dividerAnchor();
  S.regen = { old };
  old.hidden = true;
  const mine = ++S.sendSeq;
  startTurn();
  reflectComposer();
  const r = await api.call('regenerate');
  adoptRequest(r, mine, 'Could not regenerate the reply.');
}

/** The bridge answered send_message / regenerate with `r`: take its request id. */
function adoptRequest(r, mine, failMessage) {
  // Abandoned while the call was in flight (New chat, the Stop safety net, or a reply
  // that already finished and a newer send): the engine may still be generating for this
  // id, so stop it and ignore its events, or the next send would adopt them. (Not S.cur:
  // chat.reset / chat.fallback can replace the turn before this reply arrives.)
  if (S.sendSeq !== mine || !S.busy) {
    if (r && r.ok && r.request_id) { S.ignore.add(r.request_id); api.call('stop_generation', r.request_id); }
    return;
  }
  if (!r || !r.ok) {
    dropTurn();
    keepOldReply();
    endRequest();
    showError((r && r.error) || { message: failMessage, action: 'retry' });
    return;
  }
  // This reply's id is authoritative: an id accept() picked up from a stray event of an
  // abandoned request is ignored from now on.
  if (S.reqId && S.reqId !== r.request_id) S.ignore.add(S.reqId);
  S.reqId = r.request_id;
  // Stop was pressed before the id was known: send it now.
  if (S.stopping) api.call('stop_generation', S.reqId);
}

/** A Regenerate that failed or said nothing: the reply it was replacing comes back. */
function keepOldReply() {
  if (S.regen) { S.regen.old.hidden = false; S.regen = null; }
}

/** A Regenerate that produced a reply: the one it replaces goes. */
function dropOldReply() {
  if (S.regen) { S.regen.old.remove(); S.regen = null; }
}

function retry() {
  if (S.retryRegen) { regenerate(); return; }
  // The files' ids are still valid: the backend forgets them only when a reply finishes
  // or is stopped.
  if (S.lastText || S.lastFiles.length) send(S.lastText, { showUser: false, files: S.lastFiles, action: S.lastAction });
}

function dropTurn() {
  if (S.cur) { clearTimeout(S.cur.timer); S.cur.node.remove(); }
  S.cur = null;
}

async function stopGeneration() {
  if (!S.busy || S.stopping) return;
  S.stopping = true;
  reflectComposer();
  const mine = S.sendSeq;
  // Without an id yet (send_message has not answered), send() stops it once the id arrives.
  if (S.reqId) await api.call('stop_generation', S.reqId);
  // Safety net: if the cancelled event never comes, release the button. Keyed to this
  // request, not to its id: the id may arrive after Stop was pressed.
  setTimeout(() => {
    if (S.stopping && S.sendSeq === mine) {
      if (S.reqId) S.ignore.add(S.reqId);
      finishCancelled();
    }
  }, 8000);
}

/** End a stopped request. `message` is the engine's reason (chat.error{cancelled});
 *  `restored`: a Regenerate stopped before it said anything, so the old reply stays. */
function finishCancelled(message, restored = false) {
  const turn = S.cur;
  const note = stoppedNote(message);
  const empty = !turn || turnIsEmpty(turn) || restored;
  if (turn) {
    if (empty) turn.node.remove();
    else finalizeTurn(turn, { model: turn.fallbackModel || S.selected.model, note });
  }
  if (empty) keepOldReply(); else dropOldReply();
  endRequest();
  // Nothing on screen says why when the engine stopped an empty reply on its own.
  if (empty && note !== 'stopped') toast(String(message));
}

async function newChat() {
  if (S.busy) {
    if (S.reqId) { S.ignore.add(S.reqId); api.call('stop_generation', S.reqId); }
    dropTurn();
    endRequest();
  }
  closeKeyCard();
  await api.call('new_chat');
  messagesBox().replaceChildren();
  S.lastText = ''; S.lastFiles = []; S.lastUserEl = null; S.retryRegen = false; S.regen = null;
  showScrollButton(false);
  renderEmpty();
  $('input').focus();
}

function openSettings() {
  S.settingsOpeningUntil = Date.now() + 1500;   // do not hide-on-blur while settings opens
  api.call('open_settings');
}

// ============================================================== bridge events ====

// user_ts is the time the engine stores this message with: chat.context finds rows by it.
on('chat.start', (e) => {
  if (accept(e) && S.lastUserEl && e.user_ts != null) S.lastUserEl.dataset.ts = String(e.user_ts);
});

// The conversation does not fit the model's context window (or fits again).
on('chat.context', (e) => {
  if (!accept(e)) return;
  placeContextDivider(e.first_kept_ts, e.dropped_messages);
  if (S.lastUserEl) setCutNote(S.lastUserEl, !!e.message_cut);
  if (e.message_cut) announce('Too long for context: only part of this message was sent.');
  else if (e.dropped_messages) announce('Too long for context: older messages are past the context window.');
});

on('chat.phase', (e) => {
  if (!accept(e) || !S.cur) return;
  if (S.cur.phaseEl) S.cur.phaseEl.textContent = PHASE_TEXT[e.phase] || 'Working…';
});

on('chat.delta', (e) => {
  if (!accept(e)) return;
  const turn = curTurn();
  if (e.reasoning) {
    turn.reasoning += e.reasoning;
    ensureThink(turn);
    if (turn.think) { turn.thinkText.data += e.reasoning; paintThinkLabel(turn, true); }
    else if (turn.phaseEl) turn.phaseEl.textContent = 'Thinking…';
  }
  if (e.content) {
    ensureBubble(turn);
    if (!turn.cursor) { turn.cursor = el('span', { class: 'stream-cursor', 'aria-hidden': 'true' }); turn.bubble.append(turn.cursor); }
    paintThinkLabel(turn, false);
    turn.content += e.content;
    scheduleStreamRender(turn);
  }
});

on('chat.tool_call', (e) => {
  if (!accept(e)) return;
  const turn = curTurn();
  const chip = toolChip({ call_id: e.call_id, name: e.name, arguments: e.arguments });
  turn.chips.set(e.call_id, chip);
  turn.tools.hidden = false;
  turn.tools.append(chip);
  stickOrBadge(false);
});

on('chat.tool_result', (e) => {
  if (!accept(e)) return;
  const turn = curTurn();
  let chip = turn.chips.get(e.call_id);
  if (!chip) {   // a result with no call event: still show it
    chip = toolChip({ call_id: e.call_id, name: e.name });
    turn.chips.set(e.call_id, chip); turn.tools.hidden = false; turn.tools.append(chip);
  }
  setChipState(chip, e.ok ? 'ok' : 'fail', e.summary);
  // create_document saved a file: its card goes under the reply.
  if (e.document) { addDocCard(turn, e.document); stickOrBadge(false); }
});

// The engine dropped the text streamed so far (a "no real-time data" refusal it is now
// answering with a tool); the next deltas are the real answer.
on('chat.reset', (e) => {
  if (!accept(e) || !S.cur) return;
  // A fresh turn: the dropped round had no tool chips (recovery only runs before any
  // tool call), and its text and reasoning are gone from the conversation too. A
  // fallback that already happened still holds, so it carries over.
  const { fallbackModel, fallbackProvider } = S.cur;
  dropTurn();
  const turn = startTurn();
  Object.assign(turn, { fallbackModel, fallbackProvider });
  if (turn.phaseEl) turn.phaseEl.textContent = 'Looking it up…';
});

// The local model failed; the same message runs again on the fallback provider.
on('chat.fallback', (e) => {
  if (!accept(e)) return;
  dropTurn();
  const turn = startTurn();
  turn.fallbackModel = e.model || null;
  turn.fallbackProvider = e.provider || null;
  if (turn.phaseEl) turn.phaseEl.textContent = `Local model failed. Asking ${providerName(e.provider) || 'the fallback provider'}…`;
});

on('chat.done', (e) => {
  if (!accept(e)) return;
  const turn = curTurn();
  // The engine's final content is authoritative: it can differ from the streamed text
  // (e.g. a reply with only reasoning is shown as its reasoning, and text the stream showed
  // can turn out to be hidden markup, leaving nothing).
  if (typeof e.content === 'string' && e.content !== turn.content) {
    turn.content = e.content;
    ensureBubble(turn);
  }
  if (!turn.content.trim()) turn.content = '';
  const empty = !turn.content && !turn.chips.size && !turn.reasoning.trim();
  if (empty) ensureBubble(turn);
  // chat.done names the provider and model that actually answered; a provider other than
  // the selected one means the fallback answered (even if its chat.fallback was missed).
  const provider = e.provider || turn.fallbackProvider || S.selected.provider;
  const fellBack = !!turn.fallbackProvider || provider !== S.selected.provider;
  finalizeTurn(turn, {
    model: e.model || turn.fallbackModel || S.selected.model,
    tokPerS: e.tok_per_s,
    note: fellBack ? `via ${providerName(provider) || 'the fallback provider'}` : undefined,
  });
  if (empty) turn.md.replaceChildren(el('span', { class: 'muted', text: EMPTY_REPLY }));
  dropOldReply();
  announce((turn.content || 'Reply finished').slice(0, 300));
  endRequest();
});

on('chat.error', (e) => {
  if (!accept(e)) return;
  if (e.code === 'cancelled') { finishCancelled(e.message, !!e.restored); return; }
  const turn = S.cur;
  if (S.regen) {
    // A failed Regenerate: the engine put the old reply back, and so does the popup.
    dropTurn();
    keepOldReply();
    restoreDivider(S.ctxBefore);
    endRequest();
    const action = e.action === 'add_key' ? 'open_settings' : e.action;
    showError({ message: e.message, hint: e.hint, action, code: e.code });
    return;
  }
  const hadOutput = turn && !turnIsEmpty(turn);
  if (turn) {
    if (hadOutput) finalizeTurn(turn, { model: turn.fallbackModel || S.selected.model });
    else dropTurn();
  }
  // The engine rolled the turn back: what it said about the context window no longer holds.
  restoreDivider(S.ctxBefore);
  if (S.lastUserEl) setCutNote(S.lastUserEl, false);
  endRequest();
  if (e.code === 'no_key' || e.action === 'add_key') {
    // The server has not recorded this message: take it back out and keep the text and
    // the files (their ids are still valid) in the composer.
    const text = S.lastText;
    const files = S.lastFiles;
    if (S.lastUserEl) { S.lastUserEl.remove(); S.lastUserEl = null; }
    const input = $('input');
    if (!input.value.trim()) { input.value = text; autosize(); }
    if (files.length && !S.files.length) setComposerFiles(files);
    if (S.lastAction && !S.action) setComposerAction(S.lastAction, { focus: false });
    showKeyCard(e.provider || S.selected.provider, text, files, S.lastAction);
    return;
  }
  showError({ message: e.message, hint: e.hint, action: e.action, code: e.code });
});

on('runtime.status', (e) => {
  const before = S.runtime.state;
  S.runtime = { ...S.runtime, ...e };
  renderChip();
  renderStatus();
  if (before !== e.state) {
    if (e.state === 'ready') announce('Model ready');
    else if (e.state === 'starting' || e.state === 'compiling') announce(e.first_compile ? 'Compiling the model for the first time' : 'Loading the model');
    else if (e.state === 'error') announce('Model failed to load');
  }
});

on('key.status', (e) => {
  const p = providerById(e.provider_id);
  if (p) {
    p.key = { ...(p.key || {}), source: e.source, env_name: e.env_name };
    renderChip(); renderEmpty();
  }
  // A key saved in Settings while the card is open finishes the card.
  // (Not while the card is itself saving: it finishes through its own onSuccess.)
  if (S.keyCard && !S.keyCard.card.isBusy() && S.keyCard.providerId === e.provider_id && e.source && e.source !== 'none') {
    S.keyCard.finish();
  }
});

on('settings.changed', (e) => {
  applyConfig(e.config || {});
  renderChip(); renderStatus(); renderEmpty(); syncVision(); autosize();
  // Keys, providers and model lists may have changed (Settings, or the daily refresh):
  // the model menu reads S.providers, so reload it.
  refreshProviders();
});

on('popup.shown', () => {
  refreshProviders();
  $('input').focus();
  if (isNearBottom(400)) scrollToBottom(true);
});

// ================================================================== rendering ====

function applyConfig(cfg) {
  if (cfg.chat) {
    S.config.chat = { ...S.config.chat, ...cfg.chat };
    if (cfg.chat.provider) S.selected = { provider: cfg.chat.provider, model: cfg.chat.model || S.selected.model };
    if (cfg.chat.max_prompt_chars) S.limits.max_prompt_chars = cfg.chat.max_prompt_chars;
  }
  if (cfg.ui) {
    S.config.ui = { ...S.config.ui, ...cfg.ui };
    if (cfg.ui.theme && window.UITheme && window.UITheme.current() !== cfg.ui.theme) window.UITheme.set(cfg.ui.theme);
  }
  if (cfg.local) S.config.local = { ...S.config.local, ...cfg.local };
  if (Array.isArray(cfg.quick_actions)) syncActions(cfg.quick_actions);
}

function dotState() {
  const p = selectedProvider();
  if (!p) return { state: 'offline', text: 'unknown' };
  if (isLocalProvider(p)) {
    switch (S.runtime.state) {
      case 'ready': return { state: 'live', text: 'ready' };
      case 'starting': case 'compiling': return { state: 'idle', text: 'loading' };
      case 'unloading': return { state: 'idle', text: 'unloading' };
      case 'error': return { state: 'danger', text: 'error' };
      case 'not_installed': return { state: 'offline', text: 'not installed' };
      default: return { state: 'offline', text: 'not loaded' };
    }
  }
  return isConfigured(p) ? { state: 'live', text: 'ready' } : { state: 'warning', text: 'needs an API key' };
}

function renderChip() {
  const p = selectedProvider();
  const name = p ? p.display_name : (S.selected.provider || 'No provider');
  const model = shortModel(S.selected.model);
  const label = $('chip-label');
  label.replaceChildren(
    el('span', { class: 'chip-provider', text: name }),
    model ? el('span', { class: 'chip-sep', 'aria-hidden': 'true', text: '·' }) : null,
    model ? el('span', { class: 'chip-model', text: model }) : null,
  );
  const d = dotState();
  $('status-dot').dataset.state = d.state;
  $('model-chip').title = `${name}${model ? ` · ${model}` : ''} (${d.text}). Choose model`;
  $('model-chip').setAttribute('aria-label', `Model: ${name}${model ? `, ${model}` : ''}, ${d.text}. Choose model`);
}

function statusInfo() {
  const p = selectedProvider();
  if (!isLocalProvider(p)) return null;
  const r = S.runtime;
  // A model the NPU cannot run correctly runs on the GPU (or CPU) instead.
  const fb = r.device_fallback;
  const onOther = fb && fb.to ? ` · on ${fb.to}` : '';
  const fbTitle = fb && fb.reason ? `Runs on ${fb.to} instead of ${fb.from || 'the NPU'}: ${fb.reason}` : '';
  switch (r.state) {
    case 'starting':
    case 'compiling': {
      const exp = Number(r.expected_s) || 0;
      const pct = exp ? Math.min(95, ((Number(r.elapsed_s) || 0) / exp) * 100) : 5;
      if (r.background) {
        return { text: `Preparing ${shortModel(r.model_id)} for ${r.device || 'NPU'} in the background ${fmtClock(r.elapsed_s)} / ~${fmtClock(exp)}`,
          title: 'A one-time compile, done while the app is idle so this model loads in seconds later.', pct };
      }
      if (r.first_compile) {
        return { text: `Compiling for ${r.device || 'NPU'} ${fmtClock(r.elapsed_s)} / ~${fmtClock(exp)}`,
          title: 'First-time compile for this model and device. It is cached afterwards.', pct };
      }
      return { text: `Loading… ~${Math.round(exp) || 10} s`, title: 'Loading the model from the cache.', pct };
    }
    case 'unloading': return { text: 'Unloading the model…' };
    case 'ready': {
      if (r.unload_at && r.idle_timeout_s > 0) {
        const left = r.unload_at - Date.now() / 1000;
        if (left <= 0) return { text: `Unloading soon${onOther}`, title: fbTitle };
        return { text: (left >= 90 ? `Unloads in ${Math.ceil(left / 60)} min` : `Unloads in ${Math.ceil(left)} s`) + onOther, title: fbTitle };
      }
      return onOther ? { text: `Running${onOther}`, title: fbTitle } : null;
    }
    case 'error':
      return { text: `Model failed to load${r.error ? `: ${r.error}` : ''}`, tone: 'bad',
        action: { label: 'Retry', fn: () => api.call('load_model') } };
    case 'not_installed':
      return { text: 'The local runtime is not installed.', tone: 'bad', action: { label: 'Open settings', fn: openSettings } };
    default: return null;
  }
}

function renderStatus() {
  const info = statusInfo();
  const line = $('status-line');
  if (!info) { line.hidden = true; return; }
  line.hidden = false;
  line.dataset.tone = info.tone || '';
  $('status-text').textContent = info.text;
  line.title = info.title || '';
  const act = $('status-action');
  act.hidden = !info.action;
  if (info.action) { act.textContent = info.action.label; act.onclick = info.action.fn; }
  const bar = $('status-bar');
  bar.hidden = info.pct == null;
  if (info.pct != null) $('status-fill').style.width = `${info.pct.toFixed(1)}%`;
}
setInterval(() => { if (S.runtime.state === 'ready') renderStatus(); }, 1000);

/** The empty chat (start, and after Clear chat): who answers, then the quick actions. */
function renderEmpty() {
  const box = messagesBox();
  const existing = box.querySelector('.empty-state');
  if (box.querySelector('.msg')) { if (existing) existing.remove(); return; }
  // A re-render (settings.changed, a key saved) keeps the keyboard on the same chip.
  const focused = existing && existing.contains(document.activeElement) ? document.activeElement.dataset.action : null;
  const p = selectedProvider();
  const needsKey = p && !isConfigured(p);
  const kids = [
    railIcon(['M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-7l-5 4v-4H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z']),
    el('h2', { class: 'empty-title', text: 'Ask anything' }),
    el('p', { class: 'empty-sub', text: p ? `Chatting with ${p.display_name}${S.selected.model ? ` · ${shortModel(S.selected.model)}` : ''}.` : 'Loading…' }),
  ];
  if (needsKey) {
    const b = el('button', { class: 'btn btn-primary', type: 'button', text: `Add your ${p.display_name} key` });
    b.addEventListener('click', () => showKeyCard(p.id, ''));
    kids.push(b);
  } else {
    kids.push(...actionGrid());
  }
  const node = el('div', { class: 'empty-state' }, kids);
  if (existing) existing.replaceWith(node); else box.prepend(node);
  if (focused) [...node.querySelectorAll('.qa-chip')].find((b) => b.dataset.action === focused)?.focus();
}

function renderConversation(items) {
  const box = messagesBox();
  box.replaceChildren();
  for (const m of items || []) {
    if (m.role === 'user') box.append(userRow(m.content || '', m.ts, m.attachments || [], { cut: !!m.cut, action: m.action || null }));
    else if (m.role === 'assistant') box.append(assistantRowFromData(m));
    else if (m.role === 'notice' && m.kind === 'context_cut') box.append(contextDivider(m.content || CONTEXT_CUT_TEXT));
  }
  renderEmpty();
  syncRegenerate();
  scrollToBottom(true);
}

// ================================================================ quick actions ====
// One-tap prompt templates (chat/actions.py; the list is get_state.config.quick_actions and
// changes with settings.changed). They show as chips in an empty chat and in the composer's
// quick-actions menu. Picking one puts a pill in the composer and its hint in the
// placeholder, keeps what is typed, and focuses the box for a paste; the next message is
// sent with the action's id and shows the action over its bubble.

const DEFAULT_PLACEHOLDER = 'Message ChatForge';   // index.html
const ICON_BOLT = ['M13 2.5L4.5 13.5h6.5l-1 8 8.5-11h-6.5z'];

/** The list changed (boot, Settings): a picked action follows its new name and hint, or
 *  leaves the composer when it was removed. */
function syncActions(list) {
  S.actions = list.filter((a) => a && a.id && a.label);
  if (!S.action) return;
  const now = S.actions.find((a) => a.id === S.action.id);
  if (!now) setComposerAction(null, { focus: false });
  else if (now.label !== S.action.label || now.hint !== S.action.hint) setComposerAction(now, { focus: false });
}

/** The pill over a sent message's bubble. */
function actionTag(action) {
  const label = String(action.label || action.id || '');
  return el('div', { class: 'msg-action', title: `Sent with the quick action “${label}”` }, [
    railIcon(ICON_BOLT), el('span', { text: label }),
  ]);
}

/** Put `action` ({id, label, hint}) in the composer, or take it out (null). The typed text stays. */
function setComposerAction(action, { focus = true } = {}) {
  S.action = action ? { id: action.id, label: String(action.label || action.id), hint: String(action.hint || '') } : null;
  const bar = $('action-bar');
  if (!S.action) {
    bar.replaceChildren();
    bar.hidden = true;
  } else {
    const remove = el('button', { class: 'action-pill-remove', type: 'button', 'aria-label': `Remove the quick action ${S.action.label}`,
      title: 'Remove (or Backspace in an empty box)' }, [railIcon(ICON.close)]);
    remove.addEventListener('click', () => { setComposerAction(null); announce('Quick action removed'); });
    bar.replaceChildren(el('span', { class: 'action-pill', role: 'group', 'aria-label': `Quick action: ${S.action.label}` }, [
      railIcon(ICON_BOLT), el('span', { class: 'action-pill-label', text: S.action.label }), remove,
    ]));
    bar.hidden = false;
  }
  $('input').placeholder = S.action ? (S.action.hint || `${S.action.label}…`) : DEFAULT_PLACEHOLDER;
  if (focus) $('input').focus();
}

/** A chip or menu item was picked. */
function chooseAction(action) {
  closeQuickMenu({ focusButton: false });
  setComposerAction(action);
  announce(`${action.label}. ${action.hint || 'Type or paste the text, then press Enter.'}`);
}

/** The empty chat's quick actions: a line saying what they do, then a grid of chips. */
function actionGrid() {
  if (!S.actions.length) return [];
  const grid = el('div', { class: 'qa-grid', role: 'group', 'aria-label': 'Quick actions' });
  for (const a of S.actions) {
    const b = el('button', { class: 'qa-chip', type: 'button', title: a.hint || a.label, dataset: { action: a.id } }, [
      railIcon(ICON_BOLT), el('span', { class: 'qa-label', text: a.label }),
    ]);
    b.addEventListener('click', () => chooseAction(a));
    grid.append(b);
  }
  return [el('p', { class: 'qa-intro', text: 'Pick a quick action, then paste your text.' }), grid];
}

function quickMenu() { return $('quick-menu'); }
function quickMenuOpen() { return !quickMenu().hidden; }
function quickMenuItems() { return [...quickMenu().querySelectorAll('[role="menuitem"]')]; }

function buildQuickMenu() {
  const items = S.actions.map((a) => {
    const current = !!S.action && S.action.id === a.id;
    const b = el('button', { class: `qm-item${current ? ' is-current' : ''}`, type: 'button', role: 'menuitem', tabindex: '-1',
      title: a.hint || a.label, dataset: { action: a.id } }, [
      el('span', { class: 'qm-check', 'aria-hidden': 'true', text: current ? '✓' : '' }),
      el('span', { class: 'qm-label', text: a.label }),
      a.tools ? el('span', { class: 'qm-tag', text: 'web' }) : null,
    ]);
    b.addEventListener('click', () => chooseAction(a));
    return b;
  });
  const edit = el('button', { class: 'qm-item qm-edit', type: 'button', role: 'menuitem', tabindex: '-1', text: 'Edit quick actions…' });
  edit.addEventListener('click', () => { closeQuickMenu({ focusButton: false }); openSettings(); });
  quickMenu().replaceChildren(
    el('div', { class: 'qm-head', 'aria-hidden': 'true', text: 'Quick actions' }),
    ...(items.length ? items : [el('div', { class: 'qm-empty', text: 'None yet: add some in Settings.' })]),
    el('div', { class: 'qm-sep', role: 'separator' }),
    edit,
  );
}

function openQuickMenu() {
  if (S.menuOpen) closeMenu({ focusChip: false });
  buildQuickMenu();
  quickMenu().hidden = false;
  $('quick').setAttribute('aria-expanded', 'true');
  const items = quickMenuItems();
  (items.find((b) => b.classList.contains('is-current')) || items[0])?.focus();
}

function closeQuickMenu({ focusButton = true } = {}) {
  if (!quickMenuOpen()) return;
  quickMenu().hidden = true;
  $('quick').setAttribute('aria-expanded', 'false');
  if (focusButton) $('quick').focus();
}

function onQuickMenuKey(e) {
  const items = quickMenuItems();
  const i = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
  else if (e.key === 'Home') { e.preventDefault(); items[0]?.focus(); }
  else if (e.key === 'End') { e.preventDefault(); items[items.length - 1]?.focus(); }
  else if (e.key === 'Escape') {
    // Closes the menu only (the document's Escape would hide the popup).
    e.preventDefault(); e.stopPropagation(); closeQuickMenu();
  } else if (e.key === 'Tab') closeQuickMenu({ focusButton: false });
}

function wireQuickActions() {
  const button = $('quick');
  button.addEventListener('click', () => (quickMenuOpen() ? closeQuickMenu() : openQuickMenu()));
  button.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); openQuickMenu(); }
  });
  quickMenu().addEventListener('keydown', onQuickMenuKey);
  document.addEventListener('click', (e) => {
    if (quickMenuOpen() && !e.target.closest('#quick-menu, #quick')) closeQuickMenu({ focusButton: false });
  });
  // Backspace in an empty message box takes the pill out, like a chip in a search box.
  $('input').addEventListener('keydown', (e) => {
    if (e.key === 'Backspace' && S.action && !e.target.value && !e.isComposing) {
      e.preventDefault();
      setComposerAction(null);
      announce('Quick action removed');
    }
  });
}

// =================================================================== model menu ====

function menuItems() {
  return [...$('model-menu').querySelectorAll('[role="option"], [role="menuitem"]')];
}

function offeredInMenu(p) { return isConfigured(p) || p.id === S.selected.provider; }

function menuModels(p) {
  return p.models && p.models.length ? p.models : (p.default_model ? [p.default_model] : []);
}

/** The "Recently used" entries (config chat.recent_models, newest first) that can still be
 *  chosen: the provider is offered and, when it lists models, still lists this one (a local
 *  model must still be installed). */
function recentModels() {
  const out = [];
  for (const r of (S.config.chat && S.config.chat.recent_models) || []) {
    const p = providerById(r.provider);
    if (!p || !r.model || !offeredInMenu(p)) continue;
    const listed = menuModels(p);
    const current = S.selected.provider === p.id && S.selected.model === r.model;
    if ((isLocalProvider(p) || listed.length) && !listed.includes(r.model) && !current) continue;
    out.push({ p, m: r.model });
  }
  return out;
}

function menuItem(p, m, { withProvider = false } = {}) {
  const sel = S.selected.provider === p.id && S.selected.model === m;
  const b = el('button', { class: 'mm-item', type: 'button', role: 'option', 'aria-selected': String(sel), tabindex: '-1',
    dataset: { provider: p.id, model: m } }, [
    el('span', { class: 'mm-check', 'aria-hidden': 'true', text: sel ? '✓' : '' }),
    el('span', { class: 'mm-name', text: shortModel(m) }),
    // A model that sees pictures says so (the composer's warning points here).
    p.vision && p.vision[m] ? el('span', { class: 'mm-vision', role: 'img', 'aria-label': 'sees pictures', title: 'Sees pictures' }, [railIcon(ICON.image)]) : null,
    withProvider ? el('span', { class: 'mm-sub', text: p.display_name }) : null,
  ]);
  b.addEventListener('click', () => chooseModel(p.id, m));
  return b;
}

function buildMenu() {
  const menu = $('model-menu');
  menu.replaceChildren();
  const recent = recentModels();
  if (recent.length) {
    const group = el('div', { class: 'mm-group mm-recent', role: 'group', 'aria-label': t('menu.recent') }, [
      el('div', { class: 'mm-head', text: t('menu.recent') }),
    ]);
    for (const { p, m } of recent) group.append(menuItem(p, m, { withProvider: true }));
    menu.append(group, el('div', { class: 'mm-sep', role: 'separator' }));
  }
  // Only configured providers are offered; the selected one stays visible even without a
  // key, so the menu never hides what is in use. Keys are added in Settings → Providers.
  for (const p of S.providers.filter(offeredInMenu)) {
    const models = menuModels(p);
    const group = el('div', { class: 'mm-group', role: 'group', 'aria-label': p.display_name }, [
      el('div', { class: 'mm-head', text: p.display_name }),
    ]);
    if (!models.length) group.append(el('div', { class: 'mm-empty', text: isLocalProvider(p) ? 'No installed models' : 'No models listed' }));
    for (const m of models) group.append(menuItem(p, m));
    menu.append(group);
  }
  const p = selectedProvider();
  if (isLocalProvider(p)) {
    const loaded = S.runtime.state === 'ready';
    const busy = ['starting', 'compiling', 'unloading'].includes(S.runtime.state);
    const act = el('button', { class: 'mm-item mm-action', type: 'button', role: 'menuitem', tabindex: '-1',
      text: loaded ? 'Unload model' : 'Load model', disabled: busy ? '' : null });
    act.addEventListener('click', () => { closeMenu(); api.call(loaded ? 'unload_model' : 'load_model'); });
    menu.append(el('div', { class: 'mm-sep', role: 'separator' }), act);
  }
  const more = el('button', { class: 'mm-item mm-action', type: 'button', role: 'menuitem', tabindex: '-1', text: 'Manage models…' });
  more.addEventListener('click', () => { closeMenu(); openSettings(); });
  menu.append(more);
}

function openMenu() {
  buildMenu();
  const menu = $('model-menu');
  menu.hidden = false;
  S.menuOpen = true;
  $('model-chip').setAttribute('aria-expanded', 'true');
  const items = menuItems();
  const cur = items.find((b) => b.getAttribute('aria-selected') === 'true') || items[0];
  if (cur) cur.focus();
}

function closeMenu({ focusChip = true } = {}) {
  if (!S.menuOpen) return;
  $('model-menu').hidden = true;
  S.menuOpen = false;
  $('model-chip').setAttribute('aria-expanded', 'false');
  if (focusChip) $('model-chip').focus();
}

async function chooseModel(providerId, modelId) {
  closeMenu();
  if (S.busy) { toast('Wait for the current reply, or press Stop.'); return; }
  const r = await api.call('select_model', providerId, modelId);
  if (!r || !r.ok) { toast((r && r.error && r.error.message) || 'Could not switch model.'); return; }
  S.selected = { provider: providerId, model: modelId };
  // Whether the new model sees pictures (the picture chips warn when it does not).
  const p = providerById(providerId);
  if (p && typeof r.vision === 'boolean') p.vision = { ...(p.vision || {}), [modelId]: r.vision };
  if (S.keyCard && S.keyCard.providerId !== providerId) closeKeyCard();
  renderChip(); renderStatus(); syncVision();
  const box = messagesBox();
  if (!box.querySelector('.msg')) renderEmpty();
  $('input').focus();
}

function onMenuKey(e) {
  if (!S.menuOpen) return;
  const items = menuItems().filter((b) => !b.disabled);
  const i = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
  else if (e.key === 'Home') { e.preventDefault(); items[0]?.focus(); }
  else if (e.key === 'End') { e.preventDefault(); items[items.length - 1]?.focus(); }
  else if (e.key === 'Tab') closeMenu({ focusChip: false });
}

// ======================================================================== boot ====

function wire() {
  const input = $('input');
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (e) => {
    // Enter sends; Shift+Enter adds a newline; never send mid-IME-composition.
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!$('send').disabled) doSend();
    }
  });
  $('send').addEventListener('click', doSend);
  $('stop').addEventListener('click', stopGeneration);
  wireAttachments();
  wireQuickActions();
  $('btn-new').addEventListener('click', newChat);
  $('btn-settings').addEventListener('click', openSettings);
  $('btn-close').addEventListener('click', () => api.call('hide_popup'));
  $('btn-pin').addEventListener('click', async () => {
    const next = !S.pinned;
    const r = await api.call('set_pinned', next);
    if (r && r.ok === false) return;
    S.pinned = next;
    $('btn-pin').setAttribute('aria-pressed', String(next));
    $('btn-pin').title = next ? 'Pinned: stays open when you click away' : 'Keep window open';
  });
  $('scroll-bottom').addEventListener('click', () => { scrollToBottom(); $('input').focus(); });
  messagesBox().addEventListener('scroll', () => {
    if (isNearBottom()) showScrollButton(false);
    else if ($('scroll-bottom').classList.contains('hidden') && messagesBox().querySelector('.msg')) $('scroll-bottom').classList.remove('hidden');
  }, { passive: true });

  $('model-chip').addEventListener('click', () => (S.menuOpen ? closeMenu() : openMenu()));
  $('model-menu').addEventListener('keydown', onMenuKey);
  document.addEventListener('click', (e) => {
    if (S.menuOpen && !e.target.closest('.model-wrap')) closeMenu({ focusChip: false });
  });

  // Links in replies: never navigate the popup; http(s) goes to the default browser.
  messagesBox().addEventListener('click', (e) => {
    const a = e.target.closest('a[href]');
    if (!a) return;
    e.preventDefault();
    const href = a.href;
    if (/^https?:\/\//i.test(href)) api.call('open_external', href);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (S.menuOpen) { e.preventDefault(); closeMenu(); return; }
    e.preventDefault();
    api.call('hide_popup');
  });

  // Hide on blur, debounced. The host ignores it while pinned or while settings is opening.
  // Not while the file dialog is open either: it takes the focus, and hide_popup is not
  // held back by the host's suspend_blur.
  let blurTimer = 0;
  window.addEventListener('blur', () => {
    clearTimeout(blurTimer);
    blurTimer = setTimeout(() => {
      if (document.hasFocus()) return;
      if (S.config.ui.hide_on_blur === false || S.pinned || S.picking || Date.now() < S.settingsOpeningUntil) return;
      api.call('hide_popup');
    }, 150);
  });
  window.addEventListener('focus', () => clearTimeout(blurTimer));
}

function doSend() {
  const input = $('input');
  const text = input.value.trim();
  const files = readyFiles().map(fileView);
  if ((!text && !files.length) || S.busy || filesLoading()) return;
  input.value = '';
  S.files = [];   // they go with the message (no remove_attachment)
  clearAttachErrors();
  renderFiles();
  const action = S.action;   // the quick action goes with the message too
  if (action) setComposerAction(null, { focus: false });
  autosize();
  send(text, { files, action });
}

function showNotConnected(title, message) {
  $('chip-label').textContent = 'Not connected';
  const retryBtn = el('button', { class: 'btn btn-primary', type: 'button', text: 'Try again' });
  retryBtn.addEventListener('click', () => location.reload());
  messagesBox().replaceChildren(el('div', { class: 'empty-state' }, [
    el('h2', { class: 'empty-title', text: title }),
    el('p', { class: 'empty-sub', text: message }),
    retryBtn,
  ]));
}

// bridge.js has waited a long time for the app's bridge (inside WebView2 it never falls
// back to the dev mock). It keeps waiting, so boot() still finishes if the bridge arrives.
on('bridge.unavailable', () => {
  showNotConnected('Could not connect to ChatForge',
    'The app has not answered yet. This window connects as soon as it does; if it does not, quit ChatForge from the tray and start it again.');
});

async function boot() {
  wire();
  installMarkdownHandlers(toast);
  reflectComposer();
  await ready();
  const r = await api.call('get_state');
  if (!r || !r.ok) {
    S.bootFailed = true;
    showNotConnected('Could not reach the app', (r && r.error && r.error.message) || 'The backend did not answer.');
    return;
  }
  S.providers = r.providers || [];
  S.runtime = r.runtime || { state: 'unloaded' };
  if (r.limits) S.limits = { ...S.limits, ...r.limits };
  S.selected = r.selected || S.selected;
  applyConfig(r.config || {});
  if (r.theme && window.UITheme && window.UITheme.current() !== r.theme) window.UITheme.set(r.theme);
  renderChip();
  renderStatus();
  renderConversation(r.conversation);
  autosize();
  $('input').focus();
}

boot();

// Debug hook for the browser console when running against dev-mock.js.
window.__chat = { S, send, showKeyCard, renderConversation };
