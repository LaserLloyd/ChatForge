// Resizing the popup (desktop/popup.py begin_resize).
//
// The popup sits in the lower-right corner of the screen, so it grows up and to the left:
// the grip in its top-left corner resizes both ways, the strips along its top and left
// edges one way each, and the bottom-right corner stays put. Python moves the window. With a
// mouse it follows the cursor itself until the button comes up, so the window never waits
// on the bridge; a pen or a finger sends its moves from here. The size is kept when the drag
// ends and the popup opens at it from then on; a double-click on the grip (or Clear chat)
// brings the default size back.
//
// pywebview runs every bridge call on a thread of its own, so all of these go through one
// promise chain: end_resize never overtakes the start_resize before it.
import { api } from './bridge.js';

let chain = Promise.resolve();
/** The drag in progress: {id, edge, follow, sx, sy, pending: [dx, dy] | null, sending}. */
let drag = null;

function queue(name, ...args) {
  chain = chain.then(() => api.call(name, ...args));
  return chain;
}

function onDown(e) {
  if (e.button !== 0 || !e.isPrimary) return;
  if (drag) finish();   // a release that never arrived
  e.preventDefault();   // no text selection, and the focus stays where it is
  const handle = e.currentTarget;
  const edge = handle.dataset.resize;
  const dpr = window.devicePixelRatio || 1;
  drag = {
    id: e.pointerId, edge, follow: e.pointerType === 'mouse',
    sx: e.screenX, sy: e.screenY, pending: null, sending: false,
  };
  try { handle.setPointerCapture(e.pointerId); } catch { /* the window listeners still see it */ }
  document.documentElement.dataset.resizing = edge;
  // Where the pointer went down, in physical px from the window's top-left corner.
  queue('start_resize', edge, Math.round(e.clientX * dpr), Math.round(e.clientY * dpr), drag.follow);
}

function onMove(e) {
  if (!drag || drag.follow || e.pointerId !== drag.id) return;
  // screenX/Y do not move with the window, unlike clientX/Y. The ratio is read each time:
  // it changes when the window reaches a monitor with another scale.
  const dpr = window.devicePixelRatio || 1;
  drag.pending = [Math.round((e.screenX - drag.sx) * dpr), Math.round((e.screenY - drag.sy) * dpr)];
  send(drag);
}

/** One drag_resize in flight at a time, always with the latest position. */
function send(d) {
  if (d.sending || !d.pending || drag !== d) return;
  const [dx, dy] = d.pending;
  d.pending = null;
  d.sending = true;
  queue('drag_resize', dx, dy).then(() => { d.sending = false; send(d); });
}

function onUp(e) {
  if (drag && e.pointerId === drag.id) finish();
}

/** The popup was put away or came back during a drag whose release the page never saw (the
 *  app already stopped its side): leave the resizing state. */
export function cancelResize() {
  if (drag) finish();
}

function finish() {
  const d = drag;
  drag = null;
  delete document.documentElement.dataset.resizing;
  if (d.pending) queue('drag_resize', ...d.pending);
  queue('end_resize');
}

export function wireResize() {
  for (const handle of document.querySelectorAll('[data-resize]')) {
    handle.addEventListener('pointerdown', onDown);
    handle.addEventListener('lostpointercapture', onUp);
  }
  // Captured pointer events still bubble here, and these also catch an engine without capture.
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);
  document.addEventListener('visibilitychange', () => { if (document.hidden) cancelResize(); });
  const grip = document.getElementById('resize-grip');
  if (grip) grip.addEventListener('dblclick', (e) => { e.preventDefault(); queue('reset_popup_size'); });
}
