// AI Chat popup UI.
//
// Adapted from DisPatch_Chat frontend/static/js/main.js (MIT, LaserLloyd), lifted by
// function (line numbers from ca186b1):
//   messageEl (1281-1482), appendMessageToView (1574), typingEl (1605), appendErrorBubble (1758),
//   isNearBottom/scrollToBottom/showScrollButton/updateScrollBadge (1785-1940),
//   updateSendEnabled/autosize (1941-1956), sendMessage (1957),
//   streamBuffers/scheduleStreamRender/beginStream/renderStreamMarkdown (4760-4870).
// Stripped: auth/Safe Mode, websockets, bots/threads, avatars, reactions, attachments, i18n.
// New: model chip + status dot + compile/idle status line, think block, tool chips, error
// actions, inline key card, bridge events (chat.*, runtime.status, key.status, ...).
import { api, on, ready } from './bridge.js';
import { el, railIcon } from './util.js?v=13';
import { renderMarkdown, enhanceContent, installMarkdownHandlers } from './markdown.js';
import { t } from './i18n.js?v=3';
import { createKeyCard } from './keycard.js';

const $ = (id) => document.getElementById(id);

// ---- Streaming paint cadence (DisPatch main.js:4778-4779) ----
const STREAM_PAINT_MS = 100;      // ~10 repaints/sec: reads as smooth, costs 6x less
const STREAM_PAINT_CHARS = 160;   // ...but a burst that big repaints immediately
const MD_OPTS = { noMedia: true, noLocal: true };

const ICON = {
  wrench: ['M14.7 6.3a4 4 0 0 0 5 5L13 18a2.1 2.1 0 0 1-3-3z', 'M14.7 6.3l3-3 3 3'],
  brain: ['M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 3 4 3 3 0 0 0 5 1V5a2 2 0 0 0-3-1z', 'M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-3 4 3 3 0 0 1-5 1'],
  warn: ['M12 4l10 17H2z', 'M12 10v4', 'M12 17.5v.01'],
  copy: ['M8 8h11v11H8z', 'M5 16V5h11'],
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
  ignore: new Set(),      // request ids we no longer care about (stopped / new chat)
  cur: null,              // the assistant turn being streamed
  lastText: '',           // last text sent (for Retry)
  lastUserEl: null,
  keyCard: null,
  menuOpen: false,
  unseen: 0,
  settingsOpeningUntil: 0,
  stopping: false,
  bootFailed: false,
};

// =================================================================== helpers ====

function providerById(id) { return S.providers.find((p) => p.id === id) || null; }
function selectedProvider() { return providerById(S.selected.provider); }
function isLocalProvider(p) { return !!p && p.kind === 'ovms'; }
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
}

function userRow(text, ts) {
  const bubble = el('div', { class: 'bubble', dir: 'auto', text });
  const col = el('div', { class: 'msg-col' }, [bubble, el('div', { class: 'msg-time', text: clockTime(ts) })]);
  return el('div', { class: 'msg user' }, [col]);
}

function toolChip(call) {
  const arg = shortArg(call.arguments);
  const state = call.ok === undefined ? 'running' : (call.ok ? 'ok' : 'fail');
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

/** The assistant turn: think block, tool chips, bubble (+ typing placeholder while waiting). */
function newTurn({ streaming }) {
  const col = el('div', { class: 'msg-col' });
  const node = el('div', { class: `msg assistant${streaming ? ' streaming' : ''}` }, [col]);
  const turn = {
    node, col, content: '', reasoning: '', think: null, thinkBody: null, thinkLabel: null, thinkText: null,
    tools: el('div', { class: 'tools' }), bubble: null, md: null, cursor: null, typing: null, phaseEl: null,
    chips: new Map(), paint: { at: 0, len: 0 }, timer: 0, queued: false,
  };
  turn.tools.hidden = true;
  col.append(turn.tools);
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
  turn.col.append(turn.bubble);
}

function addTypingPlaceholder(turn, text) {
  turn.phaseEl = el('span', { class: 'typing-hint', text });
  turn.typing = el('div', { class: 'bubble typing-bubble', role: 'status', 'aria-label': 'Waiting for a reply' }, [
    el('span', { class: 'dots', 'aria-hidden': 'true' }, [el('span'), el('span'), el('span')]),
    turn.phaseEl,
  ]);
  turn.col.append(turn.typing);
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
  if (turn.content) turn.col.append(copyAction(() => turn.content));
}

function copyAction(getText) {
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
  return el('div', { class: 'msg-actions' }, [b]);
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
  }
  finishMeta(turn, { ts: m.ts, model: m.model });
  return turn.node;
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
  if (!turn.md) return;
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
  if (turn.content || turn.chips.size || turn.reasoning) finishMeta(turn, meta);
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
    btn('Add key', () => { row.remove(); showKeyCard(S.selected.provider, S.lastText); });
  } else if (err.action === 'open_settings') {
    btn('Open settings', openSettings);
    if (S.lastText) btn('Retry', () => { row.remove(); retry(); });
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

function showKeyCard(providerId, pendingText) {
  closeKeyCard();
  const provider = providerById(providerId) || { id: providerId, display_name: providerId, region: null };
  const card = createKeyCard({
    api,
    provider,
    onSuccess: async () => {
      closeKeyCard();
      await refreshProviders();
      const text = pendingText;
      const input = $('input');
      if (text && input.value.trim() === text) { input.value = ''; autosize(); }
      if (text) send(text);
      input.focus();
    },
    onCancel: () => { closeKeyCard(); $('input').focus(); },
  });
  S.keyCard = { card, providerId, pendingText };
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
}

async function refreshProviders() {
  const r = await api.call('list_providers');
  if (r && r.ok && Array.isArray(r.providers)) { S.providers = r.providers; renderChip(); renderEmpty(); }
}

// ==================================================================== sending ====

function updateSendEnabled() {
  const text = $('input').value;
  const has = text.trim().length > 0;
  $('send').disabled = !has || text.length > maxChars() || S.busy;
}

function reflectComposer() {
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

/** Send `text`. `showUser` false when re-sending (Retry, key card) and the bubble is already there. */
async function send(text, { showUser = true } = {}) {
  text = String(text || '').trim();
  if (!text || S.busy) return;
  if (text.length > maxChars()) { toast(`Message is longer than ${maxChars().toLocaleString()} characters.`); return; }
  S.busy = true; S.reqId = null; S.stopping = false; S.lastText = text;
  if (showUser) { S.lastUserEl = userRow(text); addRow(S.lastUserEl); scrollToBottom(true); }   // your own message always follows you down
  startTurn();
  reflectComposer();
  const r = await api.call('send_message', text);
  if (!r || !r.ok) {
    dropTurn();
    endRequest();
    showError((r && r.error) || { message: 'Could not send the message.', action: 'retry' });
    return;
  }
  if (!S.reqId && !S.ignore.has(r.request_id)) S.reqId = r.request_id;
}

function retry() {
  if (S.lastText) send(S.lastText, { showUser: false });
}

function dropTurn() {
  if (S.cur) { clearTimeout(S.cur.timer); S.cur.node.remove(); }
  S.cur = null;
}

async function stopGeneration() {
  if (!S.busy || S.stopping) return;
  S.stopping = true;
  reflectComposer();
  const id = S.reqId;
  if (id) await api.call('stop_generation', id);
  // Safety net: if the cancelled event never comes, release the button.
  setTimeout(() => {
    if (S.stopping && S.reqId === id) {
      if (S.reqId) S.ignore.add(S.reqId);
      finishCancelled();
    }
  }, 8000);
}

function finishCancelled() {
  const turn = S.cur;
  if (turn) {
    if (turnIsEmpty(turn)) turn.node.remove();
    else finalizeTurn(turn, { model: S.selected.model, note: 'stopped' });
  }
  endRequest();
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
  S.lastText = ''; S.lastUserEl = null;
  showScrollButton(false);
  renderEmpty();
  $('input').focus();
}

function openSettings() {
  S.settingsOpeningUntil = Date.now() + 1500;   // do not hide-on-blur while settings opens
  api.call('open_settings');
}

// ============================================================== bridge events ====

on('chat.start', (e) => { accept(e); });

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
});

on('chat.done', (e) => {
  if (!accept(e)) return;
  const turn = curTurn();
  // The engine's final content is authoritative: it can differ from the streamed text
  // (e.g. a reply with only reasoning is shown as its reasoning).
  if (typeof e.content === 'string' && e.content && e.content !== turn.content) {
    turn.content = e.content;
    ensureBubble(turn);
  }
  if (!turn.content && !turn.chips.size && !turn.reasoning) {
    ensureBubble(turn);
    turn.md.append(el('span', { class: 'muted', text: '(The model returned an empty reply.)' }));
  }
  finalizeTurn(turn, { model: S.selected.model, tokPerS: e.tok_per_s });
  announce((turn.content || 'Reply finished').slice(0, 300));
  endRequest();
});

on('chat.error', (e) => {
  if (!accept(e)) return;
  if (e.code === 'cancelled') { finishCancelled(); return; }
  const turn = S.cur;
  const hadOutput = turn && !turnIsEmpty(turn);
  if (turn) {
    if (hadOutput) finalizeTurn(turn, { model: S.selected.model });
    else dropTurn();
  }
  endRequest();
  if (e.code === 'no_key' || e.action === 'add_key') {
    // The server has not recorded this message: take it back out and keep the text.
    const text = S.lastText;
    if (S.lastUserEl) { S.lastUserEl.remove(); S.lastUserEl = null; }
    const input = $('input');
    if (!input.value.trim()) { input.value = text; autosize(); }
    showKeyCard(S.selected.provider, text);
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
    const { pendingText } = S.keyCard;
    closeKeyCard();
    if (pendingText) send(pendingText);
  }
});

on('settings.changed', (e) => {
  applyConfig(e.config || {});
  renderChip(); renderStatus(); renderEmpty(); autosize();
});

on('popup.shown', () => {
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
  const hasKey = p.key && p.key.source && p.key.source !== 'none';
  return hasKey ? { state: 'live', text: 'ready' } : { state: 'warning', text: 'needs an API key' };
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
  switch (r.state) {
    case 'starting':
    case 'compiling': {
      const exp = Number(r.expected_s) || 0;
      const pct = exp ? Math.min(95, ((Number(r.elapsed_s) || 0) / exp) * 100) : 5;
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
        if (left <= 0) return { text: 'Unloading soon' };
        return { text: left >= 90 ? `Unloads in ${Math.ceil(left / 60)} min` : `Unloads in ${Math.ceil(left)} s` };
      }
      return null;
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

const SUGGESTIONS = [
  'What is today’s date?',
  'Search the web for the latest OpenVINO release',
  'What is sqrt(2) * 10?',
];

function renderEmpty() {
  const box = messagesBox();
  const existing = box.querySelector('.empty-state');
  if (box.querySelector('.msg')) { if (existing) existing.remove(); return; }
  const p = selectedProvider();
  const needsKey = p && !isLocalProvider(p) && !(p.key && p.key.source && p.key.source !== 'none');
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
    const list = el('div', { class: 'suggestions' });
    for (const s of SUGGESTIONS) {
      const b = el('button', { class: 'suggestion', type: 'button', text: s });
      b.addEventListener('click', () => { const i = $('input'); i.value = s; autosize(); i.focus(); });
      list.append(b);
    }
    kids.push(list);
  }
  const node = el('div', { class: 'empty-state' }, kids);
  if (existing) existing.replaceWith(node); else box.prepend(node);
}

function renderConversation(items) {
  const box = messagesBox();
  box.replaceChildren();
  for (const m of items || []) {
    if (m.role === 'user') box.append(userRow(m.content || '', m.ts));
    else if (m.role === 'assistant') box.append(assistantRowFromData(m));
  }
  renderEmpty();
  scrollToBottom(true);
}

// =================================================================== model menu ====

function menuItems() {
  return [...$('model-menu').querySelectorAll('[role="option"], [role="menuitem"]')];
}

function buildMenu() {
  const menu = $('model-menu');
  menu.replaceChildren();
  for (const p of S.providers) {
    const models = p.models && p.models.length ? p.models : (p.default_model ? [p.default_model] : []);
    const group = el('div', { class: 'mm-group', role: 'group', 'aria-label': p.display_name }, [
      el('div', { class: 'mm-head', text: p.display_name }),
    ]);
    if (!models.length) group.append(el('div', { class: 'mm-empty', text: isLocalProvider(p) ? 'No installed models' : 'No models listed' }));
    for (const m of models) {
      const sel = S.selected.provider === p.id && S.selected.model === m;
      const b = el('button', { class: 'mm-item', type: 'button', role: 'option', 'aria-selected': String(sel), tabindex: '-1',
        dataset: { provider: p.id, model: m } }, [
        el('span', { class: 'mm-check', 'aria-hidden': 'true', text: sel ? '✓' : '' }),
        el('span', { class: 'mm-name', text: shortModel(m) }),
      ]);
      b.addEventListener('click', () => chooseModel(p.id, m));
      group.append(b);
    }
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
  if (S.keyCard && S.keyCard.providerId !== providerId) closeKeyCard();
  renderChip(); renderStatus();
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
  let blurTimer = 0;
  window.addEventListener('blur', () => {
    clearTimeout(blurTimer);
    blurTimer = setTimeout(() => {
      if (document.hasFocus()) return;
      if (S.config.ui.hide_on_blur === false || S.pinned || Date.now() < S.settingsOpeningUntil) return;
      api.call('hide_popup');
    }, 150);
  });
  window.addEventListener('focus', () => clearTimeout(blurTimer));
}

function doSend() {
  const input = $('input');
  const text = input.value.trim();
  if (!text || S.busy) return;
  input.value = '';
  autosize();
  send(text);
}

async function boot() {
  wire();
  installMarkdownHandlers(toast);
  reflectComposer();
  await ready();
  const r = await api.call('get_state');
  if (!r || !r.ok) {
    S.bootFailed = true;
    $('chip-label').textContent = 'Not connected';
    const box = messagesBox();
    const retryBtn = el('button', { class: 'btn btn-primary', type: 'button', text: 'Try again' });
    retryBtn.addEventListener('click', () => location.reload());
    box.replaceChildren(el('div', { class: 'empty-state' }, [
      el('h2', { class: 'empty-title', text: 'Could not reach the app' }),
      el('p', { class: 'empty-sub', text: (r && r.error && r.error.message) || 'The backend did not answer.' }),
      retryBtn,
    ]));
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
window.__chat = { S, send, showKeyCard };
