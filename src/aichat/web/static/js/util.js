// Small DOM helpers.
//
// The date/time/size formatters that used to live here are gone: they hard-coded
// 'en-US' and English words ("Yesterday", "5m", "6.2 MB"), which no amount of
// locale data could reach. Their replacements are in js/i18n.js, where Intl owns
// the formatting and the unit labels are translatable keys. What is left here is
// deliberately language-free — DOM construction and lazy asset loading.

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    // No `html:` option on purpose: this helper is the one place the whole app
    // builds DOM, and an innerHTML escape hatch here is how untrusted text
    // reaches the parser. Rendered markup goes through markdown.js's sanitizer.
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

// --- Lazy asset loading ----------------------------------------------------
// The heavy vendor bundle (highlight.js, 127KB) used to be a plain <script>
// tag in the document, so every cold start — including a phone that only ever
// reads a message — paid for it before first paint. It is now fetched the
// first time something actually needs it. Both helpers memoise per URL, so
// concurrent callers share one network request.
const _assetCache = new Map();

export function loadScript(src) {
  if (_assetCache.has(src)) return _assetCache.get(src);
  const p = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = false;          // preserve order between dependent bundles
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`Failed to load ${src}`));
    document.head.append(s);
  });
  _assetCache.set(src, p);
  return p;
}

export function loadStyle(href) {
  if (_assetCache.has(href)) return _assetCache.get(href);
  const p = new Promise((resolve) => {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = href;
    // A missing stylesheet is cosmetic — never block the feature on it.
    l.onload = l.onerror = () => resolve();
    document.head.append(l);
  });
  _assetCache.set(href, p);
  return p;
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// --- Rail icons -------------------------------------------------------------
// Thin-stroke line icons in currentColor for the sidebar rail and anywhere
// else an emoji would clash with monochrome chrome: emoji draw in the
// platform's colour set, so a rail mixing them with line art reads as two
// different apps. One language, one place. 24px grid, 1.8px rounded strokes;
// colour comes from the element, so themes and accent states apply for free.
//
// Built with createElementNS from constant path data. No innerHTML: el()
// above bans it, and a hand-rolled exception for "trusted" markup is how that
// ban erodes.

const SVG_NS = 'http://www.w3.org/2000/svg';

/** One icon: an array of path `d` strings on a 24×24 grid → an <svg>. */
export function railIcon(paths) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('rail-icon');
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

/** The shared set, named by meaning. */
export const RAIL_ICONS = {
  // Picture frame, broken by the slash: "no images".
  nim: [
    'M2 2 22 22',
    'M10.41 10.41a2 2 0 1 1-2.83-2.83',
    'M13.5 13.5 6 21',
    'M18 12l3 3',
    'M3.59 3.59A1.99 1.99 0 0 0 3 5v14a2 2 0 0 0 2 2h14c.55 0 1.052-.22 1.41-.59',
    'M21 15V5a2 2 0 0 0-2-2H9',
  ],
  // Eye, struck through: "this device sees nothing".
  privacy: [
    'M2 2 22 22',
    'M9.88 9.88a3 3 0 1 0 4.24 4.24',
    'M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68',
    'M6.61 6.61A13.53 13.53 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61',
  ],
  // The silhouette Minimal avatars switches to.
  avatars: [
    'M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2',
    'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  ],
  // External-link arrow: a custom link button with no chosen emoji.
  link: [
    'M15 3h6v6',
    'M10 14 21 3',
    'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6',
  ],
  moon: ['M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z'],
  sun: [
    'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8',
    'M12 2v2', 'M12 20v2', 'M4.93 4.93l1.41 1.41', 'M17.66 17.66l1.41 1.41',
    'M2 12h2', 'M20 12h2', 'M6.34 17.66l-1.41 1.41', 'M19.07 4.93l-1.41 1.41',
  ],
  lock: [
    'M5 11h14a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2Z',
    'M7 11V7a5 5 0 0 1 10 0v4',
  ],
  unlock: [
    'M5 11h14a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2Z',
    'M7 11V7a5 5 0 0 1 9.9-1',
  ],
  folder: [
    'M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z',
  ],
  upload: [
    'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4',
    'M17 8l-5-5-5 5',
    'M12 3v12',
  ],
  // --- Local viewer chrome -------------------------------------------------
  // Chevron pointing back. The viewer's own history (listing → file), not the
  // browser's — hidden at depth 0.
  'viewer-back': ['M15 5 8 12l7 7'],
  // The same external-link arrow as `link`, kept separate so the rail's
  // meaning ("a link button") and the viewer's ("open this in a tab") can
  // drift apart later without one of them silently changing.
  'viewer-open': [
    'M15 3h6v6',
    'M10 14 21 3',
    'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6',
  ],
  // Tray with an arrow into it: save this file.
  'viewer-download': [
    'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4',
    'M7 10l5 5 5-5',
    'M12 15V3',
  ],
  // Circular arrow: re-fetch the pane.
  'viewer-reload': [
    'M21 12a9 9 0 1 1-2.64-6.36',
    'M21 3v6h-6',
  ],
  // Folder, in the listing and as a directory entry's icon.
  'viewer-folder': [
    'M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z',
  ],
  // Page with a folded corner: every non-directory entry.
  'viewer-file': [
    'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z',
    'M14 3v5h5',
  ],
  gear: [
    'M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z',
    'M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  ],
};
