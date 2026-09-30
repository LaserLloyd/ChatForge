// Markdown -> sanitized HTML, plus code highlighting and image handling.
// Relies on globals provided by vendored scripts: marked and DOMPurify load
// with the document; hljs is fetched on demand (see ensureHighlighter).
import { loadScript, loadStyle, escapeHtml } from './util.js?v=13';
// markdown.js builds HTML as STRINGS rather than DOM nodes, so the two
// user-facing attributes below can't be reached by the data-i18n pass — they are
// translated inline instead. i18n.js imports nothing, so there is no cycle.
import { t } from './i18n.js?v=3';
//
// The rendering rules here are a port of the OpenClaw Control UI's markdown
// module (openclaw 2026.7.1, `dist/control-ui/assets/markdown-*.js`) so the same
// agent text looks the same in DisPatch as it does in the gateway's own chat:
// code blocks get a header + copy button, JSON collapses behind a <details>,
// raw HTML from the model is escaped rather than executed, block art keeps its
// alignment, and oversized text degrades to plain text instead of choking the
// parser. Verified against the live Control UI renderer: on a real 29-message
// session, 26/29 messages already matched tag-for-tag; these rules close the
// remaining three (file links, code-block chrome, raw-HTML escaping).
//
// Two deliberate divergences from upstream, both because DisPatch is not the
// Control UI:
//   - images: upstream only inlines data: URIs and drops the rest. DisPatch's
//     whole media pipeline ([[media:…]], /media/<uuid>) depends on real images,
//     so they stay.
//   - file links: upstream opens a side panel DisPatch doesn't have, so the
//     link markup is kept (for visual parity) but clicking copies the path.

// --- DOMPurify hooks: open links in a new tab safely, and keep data: URIs to
// real media. Registered LAZILY (from sanitize(), once) rather than at module
// eval: purify.min.js is a `defer` script, so a module that evaluates before it
// lands would silently skip registration and ship an unhooked sanitizer.
let _hooksReady = false;
function ensureHooks() {
  if (_hooksReady || !window.DOMPurify) return;
  _hooksReady = true;
  // data: is allowed on img/video/source via ADD_DATA_URI_TAGS so inline
  // pictures work. That switch is per-TAG, not per-TYPE: `data:text/html,…` in
  // an <img src> (or the data-full we copy it into) is still a document, and a
  // document is not a picture. Reject anything that isn't image/video/audio.
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    for (const attr of ['src', 'href', 'poster', 'data-full']) {
      const v = node.getAttribute && node.getAttribute(attr);
      if (!v) continue;
      if (/^\s*data:/i.test(v) && !/^\s*data:(?:image|video|audio)\//i.test(v)) {
        node.removeAttribute(attr);
      }
    }
  });
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      // A file link is not navigable — it carries a path, not an href.
      // Attribute order/value mirrors the Control UI so the two are diffable.
      if (!node.hasAttribute('data-file-path')) {
        node.setAttribute('rel', 'noreferrer noopener');
        node.setAttribute('target', '_blank');
      }
    }
    if (node.tagName === 'IMG') {
      node.setAttribute('loading', 'lazy');
    }
    // Task-list classes (upstream's markdown-it-task-lists convention): tag the
    // item and its list off the rendered checkbox, so a checklist matches the
    // Control UI without reimplementing marked's list rendering.
    if (node.tagName === 'INPUT' && node.classList.contains('task-list-item-checkbox')) {
      const li = node.closest('li');
      if (li) {
        li.classList.add('task-list-item');
        const list = li.parentElement;
        if (list && (list.tagName === 'UL' || list.tagName === 'OL')) {
          list.classList.add('contains-task-list');
        }
      }
    }
  });
}

// marked v18: configure GFM + line breaks — matches the Control UI's
// markdown-it options ({ html: true, breaks: true, linkify: true }); `html` is
// emulated by escaping raw HTML in the renderer overrides below, exactly as
// upstream does with its html_block/html_inline rules.
// `typeof window` rather than a bare `window.marked`: this module is otherwise
// pure at eval time, and the guard is what lets the test suite import it under
// plain node (no DOM shim) to exercise toPlainPreview() below.
if (typeof window !== 'undefined' && window.marked) {
  marked.setOptions({ gfm: true, breaks: true });
}

// Upstream limits (markdown-*.js): parse ceiling, plain-text fallback ceiling.
const MAX_RENDER_CHARS = 140000;   // hard truncate above this
const PLAIN_TEXT_CHARS = 40000;    // above this: don't parse markdown at all

// escapeHtml lives in util.js (one definition, imported above) — it used to be
// duplicated here with an identical body, which is one edit away from two
// different escapers. Re-exported so this module's public surface is unchanged.
export { escapeHtml };

/** Markdown -> a one-line plain-text summary, for the thread-list preview.
 *
 *  The preview is a TEXT node (thread rows never render markup), so pasting the
 *  raw source in showed the reader the syntax instead of the message: rows read
 *  "```python", "**Done** - [x] deploy", "![](/media/…)". This is a small
 *  reducer, not a parser — it strips the markers a preview would otherwise
 *  show and keeps the words, in the source order they appear.
 *
 *  Deliberately NOT `marked.parse()` + textContent: that pulls the whole parser
 *  (and a DOM) onto the thread-list paint path, which runs on every WS frame.
 */
export function toPlainPreview(md) {
  let s = String(md == null ? '' : md);
  // Fenced code: keep the code, drop the fence lines (and the language tag).
  s = s.replace(/^[ \t]*(?:```|~~~)[^\n]*$/gm, ' ');
  // Our own directives: a document shows its name, a picture its caption —
  // or a frame glyph when it has none. A picture that showed NOTHING left a
  // thread whose newest row is an image (every agent-fired image job) reading
  // "No messages yet" in the list, under a picture that plainly exists.
  s = s.replace(/\[\[(?:doc|media|image|view):[^\]|]*\|([^\]]*)\]\]/g, '$1');
  s = s.replace(/\[\[(?:media|image):[^\]]*\]\]/g, ' \u{1F5BC}\uFE0F ');
  s = s.replace(/\[\[doc:[^\]]*\]\]/g, ' ');
  // A viewer card with no label shows the path \u2014 the preview must never show
  // the reader `[[view:` itself.
  s = s.replace(/\[\[view:([^\]]*)\]\]/g, '$1');
  // Images before links — ![alt](url) would otherwise leave a stray "!".
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
  s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
  // Reference-style links and bare autolinks.
  s = s.replace(/\[([^\]]*)\]\[[^\]]*\]/g, '$1');
  s = s.replace(/<((?:https?|mailto):[^>\s]+)>/g, '$1');
  // Block markers at the start of a line: heading, quote, list bullet,
  // ordered-list number, task-list checkbox, horizontal rule.
  s = s.replace(/^[ \t]*#{1,6}[ \t]+/gm, '');
  // A callout marker is a LABEL, not words: `> [!WARNING]` on its own line
  // left the preview reading "[!WARNING] Deploying this…", which is the
  // syntax the bubble now renders as a panel. Drop the marker here too, or
  // the thread list keeps showing the reader the markup.
  s = s.replace(/^[ \t]*>[ \t]*\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*$/gim, ' ');
  s = s.replace(/^[ \t]*>[ \t]*\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*/gim, '');
  s = s.replace(/^[ \t]*>[ \t]?/gm, '');
  s = s.replace(/^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/gm, '');
  s = s.replace(/^[ \t]*(?:[-*_][ \t]*){3,}$/gm, ' ');
  // Table pipes and the |---|---| separator row.
  s = s.replace(/^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$/gm, ' ');
  s = s.replace(/[ \t]*\|[ \t]*/g, ' ');
  // Inline: code spans, bold/italic/strike. Emphasis runs are unwrapped rather
  // than deleted wholesale so "**Done**" reads "Done", not "".
  s = s.replace(/`+([^`]*)`+/g, '$1');
  s = s.replace(/\*\*\*(\S(?:[\s\S]*?\S)?)\*\*\*/g, '$1');
  s = s.replace(/\*\*(\S(?:[\s\S]*?\S)?)\*\*/g, '$1');
  s = s.replace(/\*(\S(?:[\s\S]*?\S)?)\*/g, '$1');
  // Underscore emphasis only at word boundaries — markdown itself does not
  // emphasise intraword underscores, and eating them turns snake_case_name
  // into snakecasename in the preview.
  s = s.replace(/(^|[^\w`])(___|__|_)(\S(?:[\s\S]*?\S)?)\2(?!\w)/g, '$1$3');
  s = s.replace(/~~(\S(?:[\s\S]*?\S)?)~~/g, '$1');
  // Escaped punctuation: \* was never meant to be seen.
  s = s.replace(/\\([\\`*_{}\[\]()#+\-.!>~|])/g, '$1');
  return s.replace(/\s+/g, ' ').trim();
}

// Escape plain text into HTML with bare http(s) URLs turned into real links.
// User/system bubbles render as escaped plain text (no markdown), which made a
// pasted URL dead — the #1 ask was "let me click the preview link an agent (or
// I) dropped in chat". Tokenize BEFORE escaping so entity-encoding inside the
// URL (&) can't split the match; the href is attribute-escaped separately.
const BARE_URL_RE = /https?:\/\/[^\s<>"']+/g;
export function linkifyPlain(text) {
  const s = String(text ?? '');
  let out = '', last = 0, m;
  BARE_URL_RE.lastIndex = 0;
  while ((m = BARE_URL_RE.exec(s))) {
    let url = m[0];
    // Trailing punctuation belongs to the sentence, not the URL. A closing
    // paren is only trimmed when the URL itself has no opening one, so
    // wiki-style /Foo_(bar) links survive while "(see http://x)" doesn't
    // swallow the paren.
    let trimmed = url.replace(/[.,;:!?…]+$/, '');
    while (trimmed.endsWith(')') && !trimmed.includes('(')) trimmed = trimmed.slice(0, -1);
    out += escapeHtml(s.slice(last, m.index));
    out += `<a href="${trimmed.replace(/&/g, '&amp;').replace(/"/g, '&quot;')}" target="_blank" rel="noopener noreferrer">${escapeHtml(trimmed)}</a>`;
    out += escapeHtml(url.slice(trimmed.length));
    last = m.index + url.length;
  }
  out += escapeHtml(s.slice(last));
  return out;
}

// --- text-level cleanup (ported) -------------------------------------------
// Search-result citation markers: private-use delimiters (U+E200 open, U+E202
// separator, U+E201 close) the models emit around a citation.
const CITATION_LINE_RE = /[ \t]*cite(?:[^]*)?(?=\r?\n|$)/g;
const CITATION_RE = /cite(?:[^]*)?/g;
function stripCitations(text) {
  return text.replace(CITATION_LINE_RE, '').replace(CITATION_RE, '');
}

// Internal runtime scaffolding — a defense-in-depth mirror of the backend's
// openclaw_text sanitizer. The backend already strips this before storing new
// content, so this only fires for content stored RAW before that existed (e.g.
// the pre-mirror history of a native thread) or any path that reaches the
// browser unsanitized. Matches what the OpenClaw Control UI shows: the gateway
// strips these server-side, so its chat never renders them.
const RC_BEGIN = '<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>';
const RC_END = '<<<END_OPENCLAW_INTERNAL_CONTEXT>>>';
const RC_NOTICE = 'This context is runtime-generated, not user-authored. Keep internal details private.';
const LEGACY_HEADER = `OpenClaw runtime context (internal):\n${RC_NOTICE}\n\n`;
const LEGACY_EVENT = '[Internal task completion event]';
const SCAFFOLD_BLOCK_RE = /<\s*(system-reminder|previous_response)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi;
const SCAFFOLD_TAG_RE = /<\s*\/?\s*(?:system-reminder|previous_response)\b[^>]*>/gi;

function standaloneLine(token) {
  return new RegExp(`(?:^|\\r?\\n)[ \\t]*${token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[ \\t]*(?=\\r?\\n|$)`, 'g');
}
function stripDelimitedBlock(text, begin, end) {
  const closed = new RegExp(`${standaloneLine(begin).source}[\\s\\S]*?${standaloneLine(end).source}`, 'g');
  const unmatched = new RegExp(`${standaloneLine(begin).source}[\\s\\S]*$`, 'g');
  return text.replace(closed, '').replace(unmatched, '').replace(standaloneLine(end), '');
}
function stripLegacyInternalContext(text) {
  let out = text;
  for (let guard = 0; guard < 50; guard++) {
    const h = out.indexOf(LEGACY_HEADER);
    if (h === -1) break;
    const evStart = h + LEGACY_HEADER.length;
    if (!out.startsWith(LEGACY_EVENT, evStart)) { out = out.slice(0, h) + out.slice(evStart); continue; }
    const para = out.indexOf('\n\n', evStart + LEGACY_EVENT.length);
    const end = para === -1 ? out.length : para;
    const before = out.slice(0, h).replace(/\s+$/, '');
    const after = out.slice(end).replace(/^\s+/, '');
    out = before && after ? `${before}\n\n${after}` : `${before}${after}`;
  }
  return out;
}
function stripInternalScaffolding(text) {
  if (!text) return text;
  let out = stripDelimitedBlock(text, RC_BEGIN, RC_END);
  out = stripLegacyInternalContext(out);
  out = out.replace(SCAFFOLD_BLOCK_RE, '').replace(SCAFFOLD_TAG_RE, '');
  return out;
}

// --- block art (ported) ----------------------------------------------------
// Half-block box drawing: proportional/markdown treatment would shred the
// alignment, so it's rendered verbatim in a <pre>.
const BLOCK_ART_LINE_RE = /^[\t  ▀▄█]+$/u;
const BLOCK_ART_CHAR_RE = /[▀▄█]/u;
export function isBlockArt(text) {
  const lines = text.replace(/\r\n?/g, '\n').split('\n').filter((l) => l.trim().length > 0);
  if (lines.length < 2) return false;
  let count = 0;
  for (const line of lines) {
    if (!BLOCK_ART_LINE_RE.test(line) || !BLOCK_ART_CHAR_RE.test(line)) return false;
    count += [...line].filter((ch) => BLOCK_ART_CHAR_RE.test(ch)).length;
  }
  return count >= 8;
}

// Normalize a media reference into a browser-loadable URL.
// Local absolute paths are served through the backend's validated /api/media.
export function normalizeMediaUrl(src) {
  if (!src) return src;
  if (/^(https?:|data:|blob:)/.test(src)) return src;
  if (src.startsWith('/media/') || src.startsWith('/static/') || src.startsWith('/api/media')) return src;
  if (src.startsWith('file://')) src = src.slice(7);
  if (src.startsWith('/')) return `/api/media?path=${encodeURIComponent(src)}`;
  return src;
}

// Video formats rendered as <video> (gifs stay <img> — they self-animate).
const VIDEO_RE = /\.(mp4|webm|mov|m4v|ogv|ogg|mkv|avi)(\?|#|$)/i;
export function isVideoUrl(url) {
  if (!url) return false;
  // /api/media?path=... — test the encoded path too.
  try {
    const u = String(url);
    if (VIDEO_RE.test(u)) return true;
    const m = u.match(/[?&]path=([^&]+)/);
    return m ? VIDEO_RE.test(decodeURIComponent(m[1])) : false;
  } catch { return false; }
}

// OpenClaw agents post media as [[media:/path|caption]]. Images become inline
// markdown images; videos become <video> elements (muted+loop = gif feel).
const attrEscape = (s) => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;');

// DisPatch generates its own HTML (video elements, doc cards) from directives.
// Because raw HTML in the SOURCE is now escaped (Control UI parity), that HTML
// can't be inlined before parsing — it is parked behind a placeholder token and
// substituted back into the parsed output. The token uses private-use
// codepoints so no model text can forge one, and any unclaimed token is scrubbed.
const PLACEHOLDER_OPEN = '';
const PLACEHOLDER_CLOSE = '';
const PLACEHOLDER_RE = /(\d+)/g;

// One pass over the parsed HTML, in order: every tag, and every placeholder
// that sits in the text stream between tags. Group 1 = the tag's leading '/',
// group 2 = its name, group 3 = a placeholder index. A token INSIDE a tag is
// swallowed by the tag branch, which is how restore() spots an attribute.
const TAG_OR_PLACEHOLDER_RE = /<(\/?)([a-zA-Z][^\s/>]*)[^>]*>|(\d+)/g;

function makeHtmlParker() {
  const parked = [];
  return {
    park(html) {
      parked.push(html);
      return `${PLACEHOLDER_OPEN}${parked.length - 1}${PLACEHOLDER_CLOSE}`;
    },
    // Restore after parse. A placeholder is parked at BLOCK level (wrapped in
    // blank lines), so it normally lands between <p>s and injecting raw HTML
    // there is exactly right.
    //
    // A blanket string replace is NOT safe on its own, though: if a token ever
    // lands inside a tag (an attribute value) or inside a <code>/<pre> body,
    // substituting unescaped HTML breaks out of that attribute and reparents
    // real elements — a <video> ended up as a child of the copy button, whose
    // autoplay then fetched the quoted path for real. DOMPurify runs after this
    // and strips scripts, so it was never stored XSS, but the DOM was corrupt.
    // The fence-regex fix removed the only known way to get there; this keeps
    // it structurally impossible rather than merely unreached. In those two
    // contexts the parked HTML is restored ESCAPED: visible, inert, and an
    // obvious symptom instead of silent corruption.
    restore(rendered) {
      if (!rendered) return rendered;
      const take = (i) => parked[Number(i)] ?? '';
      let out = '';
      let pos = 0;
      let codeDepth = 0;
      TAG_OR_PLACEHOLDER_RE.lastIndex = 0;
      for (let m; (m = TAG_OR_PLACEHOLDER_RE.exec(rendered)) !== null;) {
        out += rendered.slice(pos, m.index);
        pos = m.index + m[0].length;
        if (m[3] !== undefined) {              // a placeholder in the text stream
          out += codeDepth > 0 ? escapeHtml(take(m[3])) : take(m[3]);
          continue;
        }
        const tag = (m[2] || '').toLowerCase();
        if (tag === 'code' || tag === 'pre') codeDepth += m[1] ? -1 : 1;
        if (codeDepth < 0) codeDepth = 0;
        // A token inside the tag itself is inside an attribute value.
        out += m[0].replace(PLACEHOLDER_RE, (_, i) => escapeHtml(take(i)));
      }
      return out + rendered.slice(pos).replace(
        PLACEHOLDER_RE, (_, i) => (codeDepth > 0 ? escapeHtml(take(i)) : take(i)));
    },
  };
}

// Code spans and fenced blocks are QUOTED text: a `[[media:…]]` (or `[[doc:…]]`)
// inside one is being SHOWN, not sent — the same contract the backend keeps in
// openclaw_text.sub_outside_code() when it salvages media refs. Expanding here
// would paste an image into the middle of someone's quoted JSON/curl example
// (and, because the expansion inserts blank lines, it silently BREAKS an inline
// backtick span so the image actually renders — how an agent's inject-result echoes
// used to leak pictures into the gateway mirror thread). Skip code spans first.
// The fence alternation ends at its closing line OR at end of STRING. That
// second branch must be `(?![\s\S])` — a bare `$` under /m matches the end of
// ANY line, so the lazy body stops at the end of the FIRST content line and
// everything from line 2 down is treated as outside-code (quoted directives
// get expanded, and a parked placeholder lands inside the copy button's
// data-code attribute). The backend this mirrors uses `\Z` for exactly this
// reason; see openclaw_text.CODE_SPAN_RE.
const CODE_SPAN_RE = /(^[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ \t]*\2[ \t]*$|(?![\s\S])))|(`+)(?:(?!\3)[\s\S])*?\3/gm;

// Apply fn to the non-code segments of text; code spans/fences pass through
// verbatim. Mirrors backend/app/openclaw_text.py sub_outside_code().
function subOutsideCode(text, fn) {
  if (!text) return text;
  let out = '';
  let pos = 0;
  for (const m of text.matchAll(CODE_SPAN_RE)) {
    out += fn(text.slice(pos, m.index));
    out += m[0]; // quoted text stays untouched
    pos = m.index + m[0].length;
  }
  return out + fn(text.slice(pos));
}

// --- LaTeX, reduced to Unicode ---------------------------------------------
// Models write maths in LaTeX whether or not anyone asked: `$\alpha \approx
// 0.7$`, `\(a \ne b\)`, `$$E = mc^2$$`. DisPatch rendered every one of those as
// its own source code, dollar signs and backslashes included.
//
// The fix is deliberately NOT a maths typesetter. KaTeX is ~300 KB of vendored
// library to make a family chat render an integral sign, and the things these
// bots actually emit are a Greek letter, a comparison operator, an arrow and
// the occasional exponent — all of which Unicode already has. So: translate
// what translates, and leave anything else exactly as written rather than
// half-rendering it. `\iiint` stays `\iiint`; nobody is worse off than before.
const TEX_CHARS = {
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', varepsilon: 'ε',
  zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ',
  lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ',
  tau: 'τ', upsilon: 'υ', phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π',
  Sigma: 'Σ', Upsilon: 'Υ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
  times: '×', div: '÷', pm: '±', mp: '∓', cdot: '·', ast: '∗', star: '⋆',
  approx: '≈', sim: '∼', simeq: '≃', cong: '≅', equiv: '≡', propto: '∝',
  neq: '≠', ne: '≠', leq: '≤', le: '≤', geq: '≥', ge: '≥', ll: '≪', gg: '≫',
  infty: '∞', partial: '∂', nabla: '∇', sum: '∑', prod: '∏', int: '∫',
  sqrt: '√', angle: '∠', perp: '⊥', parallel: '∥', degree: '°', deg: '°',
  rightarrow: '→', to: '→', longrightarrow: '⟶', leftarrow: '←', gets: '←',
  leftrightarrow: '↔', Rightarrow: '⇒', implies: '⇒', Leftarrow: '⇐',
  Leftrightarrow: '⇔', iff: '⇔', uparrow: '↑', downarrow: '↓', mapsto: '↦',
  in: '∈', notin: '∉', ni: '∋', subset: '⊂', subseteq: '⊆', supset: '⊃',
  supseteq: '⊇', cup: '∪', cap: '∩', emptyset: '∅', varnothing: '∅',
  forall: '∀', exists: '∃', nexists: '∄', neg: '¬', land: '∧', lor: '∨',
  therefore: '∴', because: '∵', ldots: '…', dots: '…', cdots: '⋯',
  quad: ' ', qquad: '  ', ',': ' ', ';': ' ', '%': '%', '&': '&', '_': '_',
  '$': '$', '#': '#', '{': '{', '}': '}',
};
const TEX_SUP = { 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷',
  8: '⁸', 9: '⁹', '+': '⁺', '-': '⁻', '−': '⁻', '=': '⁼', '(': '⁽', ')': '⁾',
  n: 'ⁿ', i: 'ⁱ' };
const TEX_SUB = { 0: '₀', 1: '₁', 2: '₂', 3: '₃', 4: '₄', 5: '₅', 6: '₆', 7: '₇',
  8: '₈', 9: '₉', '+': '₊', '-': '₋', '−': '₋', '=': '₌', '(': '₍', ')': '₎',
  a: 'ₐ', e: 'ₑ', o: 'ₒ', x: 'ₓ', h: 'ₕ', k: 'ₖ', l: 'ₗ', m: 'ₘ', n: 'ₙ',
  p: 'ₚ', s: 'ₛ', t: 'ₜ' };

// Every character of `body` must map, or the whole script is left alone —
// a half-converted exponent (`x²ʸ?`) is worse than the source.
function scriptOf(body, table) {
  let out = '';
  for (const ch of body) {
    if (!(ch in table)) return null;
    out += table[ch];
  }
  return out;
}

function texToUnicode(body) {
  let out = body;
  // \frac{a}{b} -> a/b, and \text{...}/\mathrm{...} -> their contents.
  // A single atom needs no brackets: `\frac{a}{b}` is a/b, but `\frac{x+1}{2}`
  // has to keep them or the slash binds wrongly.
  const atom = (part) => (/^[\w.]+$/.test(part.trim()) ? part.trim() : `(${part.trim()})`);
  out = out.replace(/\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g,
    (_, a, b) => `${atom(a)}/${atom(b)}`);
  out = out.replace(/\\(?:text|mathrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}/g, '$1');
  // Named commands. The trailing `(?![a-zA-Z])` stops \in from eating \infty.
  out = out.replace(/\\([a-zA-Z]+)(?![a-zA-Z])|\\([,;%&_$#{}])/g, (whole, name, punct) => {
    const key = name || punct;
    return key in TEX_CHARS ? TEX_CHARS[key] : whole;
  });
  // Sub/superscripts, braced or single-character.
  out = out.replace(/\^\{([^{}]{1,12})\}|\^(\S)/g, (whole, braced, one) =>
    scriptOf(braced ?? one, TEX_SUP) ?? whole);
  out = out.replace(/_\{([^{}]{1,12})\}|_(\S)/g, (whole, braced, one) =>
    scriptOf(braced ?? one, TEX_SUB) ?? whole);
  // Left-over grouping braces carry no meaning once the commands are gone.
  out = out.replace(/[{}]/g, '');
  return out.replace(/[ \t]{2,}/g, ' ').trim();
}

// What makes a `$…$` span maths rather than money. This is the whole reason
// the delimiter rules below are so fussy: "it costs $5 and then $10 total"
// contains a perfectly good `$…$` match, and turning that into maths is a
// disfigurement of an ordinary sentence about money. So a span has to LOOK
// like maths — a backslash command, a script marker, or a relation — and it
// may not begin or end with a space, which "$5 and then $" does.
const MATH_ISH = /\\[a-zA-Z]+|[\^_]|[=<>≠≈]|\\\\/;
const INLINE_MATH_RE = /\$([^\s$][^$\n]{0,118}[^\s$]|[^\s$])\$/g;
const PAREN_MATH_RE = /\\\(([\s\S]{1,300}?)\\\)/g;
const BLOCK_MATH_RE = /\$\$([\s\S]{1,600}?)\$\$|\\\[([\s\S]{1,600}?)\\\]/g;

/** LaTeX -> Unicode across the non-code parts of a message. Code spans and
 *  fences are untouched: a fenced block of TeX is source, and a model showing
 *  you `$\alpha$` inside backticks means the characters, not the letter. */
function convertMath(text) {
  return subOutsideCode(text, (seg) => seg
    .replace(BLOCK_MATH_RE, (whole, a, b) => {
      const body = (a ?? b ?? '').trim();
      if (!body) return whole;
      const out = texToUnicode(body);
      return out === body && !MATH_ISH.test(body) ? whole : `\n\n${out}\n\n`;
    })
    // `\(x\)` has no money problem — the delimiter is unambiguous — so it is
    // converted whether or not it looks like maths. It also has to happen
    // HERE: markdown reads `\(` as an escaped parenthesis, which is why this
    // used to arrive as a mangled `(a \ne b)`.
    .replace(PAREN_MATH_RE, (whole, body) => texToUnicode(body) || whole)
    .replace(INLINE_MATH_RE, (whole, body) =>
      (MATH_ISH.test(body) ? texToUnicode(body) || whole : whole)));
}

// --- footnotes --------------------------------------------------------------
// `[^1]` with a `[^1]: …` line at the bottom is standard in anything that has
// read a paper, and marked does not implement it: the marker rendered as the
// literal text `[^1]` and the definition line rendered as a stray paragraph
// beginning `[^1]:` — the reader saw the plumbing twice and the footnote never.
//
// Rendered as a superscript number plus a list at the end of the message. NOT
// as a link to an anchor: several messages share one document, so the ids
// would collide across bubbles, and a footnote in a chat bubble is a few lines
// down rather than pages away. The note's text is also on the marker's own
// tooltip, which is the fastest way to read one.
const FOOTNOTE_DEF_RE = /^[ \t]{0,3}\[\^([^\]\s]{1,64})\]:[ \t]*([^\n]*)$/gm;
const FOOTNOTE_REF_RE = /\[\^([^\]\s]{1,64})\]/g;

/** Pull the definitions out of the source and park a superscript marker at
 *  each reference. Returns the rewritten source and the notes, in the order
 *  their markers appear. */
function extractFootnotes(text, parker) {
  const defs = new Map();
  let src = subOutsideCode(text, (seg) =>
    seg.replace(FOOTNOTE_DEF_RE, (whole, id, body) => {
      if (defs.has(id)) return whole;        // a duplicate id stays as prose
      defs.set(id, body.trim());
      return '';
    }));
  if (!defs.size) return { src: text, notes: [] };
  const notes = [];
  const numberOf = (id) => {
    const at = notes.findIndex((n) => n.id === id);
    if (at !== -1) return at + 1;
    notes.push({ id, text: defs.get(id) });
    return notes.length;
  };
  src = subOutsideCode(src, (seg) => seg.replace(FOOTNOTE_REF_RE, (whole, id) => {
    if (!defs.has(id)) return whole;         // a marker with no note is prose
    const n = numberOf(id);
    // toPlainPreview, not the raw note: a tooltip cannot render markdown, so
    // the source would show the reader `[linked](https://…)` verbatim.
    const tip = toPlainPreview(defs.get(id));
    return parker.park(`<sup class="md-fn-ref" title="${attrEscape(tip)}">${n}</sup>`);
  }));
  // A note nobody referenced still beats the stray `[^x]:` paragraph it would
  // otherwise have rendered as, so it is listed after the referenced ones.
  for (const [id, body] of defs) {
    if (!notes.some((n) => n.id === id)) notes.push({ id, text: body });
  }
  return { src, notes };
}

/** The footnote list, appended after the parsed message. `parseInline` so a
 *  note can carry a link or emphasis; it is sanitized with everything else. */
function footnotesHtml(notes) {
  if (!notes.length) return '';
  const items = notes.map((n) => {
    let body = escapeHtml(n.text);
    try { body = marked.parseInline(n.text); } catch { /* keep the escaped text */ }
    return `<li>${body}</li>`;
  }).join('');
  return `<div class="md-footnotes"><ol>${items}</ol></div>`;
}

function expandMediaDirectives(text, parker) {
  return subOutsideCode(text, (seg) => seg.replace(/\[\[media:([^\]|]+)(?:\|([^\]]*))?\]\]/g, (_, path, caption) => {
    const url = normalizeMediaUrl(path.trim());
    const cap = (caption || '').trim();
    if (isVideoUrl(url)) {
      return `\n\n${parker.park(`<video class="inline-video" src="${attrEscape(url)}" muted loop playsinline preload="metadata" title="${attrEscape(cap || 'video')}"></video>`)}\n\n`;
    }
    return `\n\n![${cap || 'image'}](${url})\n\n`;
  }));
}

// Doc icon map — kept in sync with main.js FILE_ICONS.
const _DOC_ICONS = [
  [/\.(zip|tar|gz|tgz|bz2|xz|7z|rar)$/i, '📦'],
  [/\.(pdf)$/i, '📕'],
  [/\.(txt|md|log|csv|json|ya?ml|xml)$/i, '📝'],
  [/\.(mp3|flac|wav|ogg|m4a|opus)$/i, '🎵'],
  [/\.(py|js|ts|sh|bash|zsh|c|cpp|h|hpp|rs|go|java|kt|swift|rb|php)$/i, '💻'],
  [/\.(html|css|scss|less)$/i, '🌐'],
  [/\.(toml|ini|cfg|conf)$/i, '⚙️'],
  [/\.(rst|tex|org|adoc)$/i, '📄'],
];
function _docIcon(name) {
  for (const [re, icon] of _DOC_ICONS) if (re.test(name)) return icon;
  return '📄';
}

function expandDocDirectives(text, parker) {
  return subOutsideCode(text, (seg) => seg.replace(/\[\[doc:([^\]|]+)(?:\|([^\]]*))?\]\]/g, (_, id, name) => {
    const fid = id.trim();
    const fname = (name || fid).trim();
    const icon = _docIcon(fname);
    // encodeURIComponent, not just attrEscape: attrEscape makes the id safe to
    // sit INSIDE an attribute, it does not stop the id from being read as PATH.
    // A crafted `[[doc:../../api/export?format=json]]` rendered a document card
    // whose download link walked out of /api/files/ and pulled a full chat
    // export. Percent-encoding pins the id to exactly one path segment.
    const eid = attrEscape(encodeURIComponent(fid));
    const dlUrl = `/api/files/${eid}/download`;
    const rawUrl = `/api/files/${eid}/raw`;
    return `\n\n${parker.park(`<div class="doc-card"><span class="doc-icon">${icon}</span><a href="${dlUrl}" download="${attrEscape(fname)}" target="_blank" class="doc-link">${attrEscape(fname)}</a><a href="${rawUrl}" target="_blank" class="doc-preview-link" title="${attrEscape(t('msg.view_raw'))}">👁</a></div>`)}\n\n`;
  }));
}

// Strip media from the markdown SOURCE — for Safe Mode. Removing media before
// parsing matters: assigning innerHTML containing an <img> starts the network
// fetch immediately, even on a detached node, so post-render removal would
// still hit the server / disk cache.
//
// Three spellings, not one. The inline form `![alt](url)` is the common case,
// but markdown also has REFERENCE-style images — `![cat][c]` (or the collapsed
// `![cat][]` / shortcut `![cat]`) paired with a `[c]: /media/cat.png`
// definition line somewhere else in the message. Missing those was not a fetch
// bug (DOMPurify still drops the <img> that marked builds from them), but
// isMediaOnly() reads the STRIPPED text to decide whether a row is nothing but
// picture: an image-only message written in reference style kept its leftover
// `![cat][c]` and its definition line, read as "has text", and rendered in NIM
// as an empty bubble instead of dropping the row entirely.
//
// The definition half is filtered by URL rather than stripped wholesale: a
// `[docs]: https://example.com` link definition is NOT media and removing it
// would break a working link.
const MEDIA_SRC_RE = /!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\](?:\[[^\]]*\])?|\[\[media:[^\]]*\]\]/g;
const MEDIA_EXT = 'png|jpe?g|gif|webp|avif|bmp|ico|svgz?|mp4|webm|mov|m4v|ogv|ogg';
const MEDIA_REF_DEF_RE = new RegExp(
  `^[ \\t]{0,3}\\[[^\\]\\n]+\\]:[ \\t]*<?(?:/media/[^\\s<>]*|[^\\s<>]*\\.(?:${MEDIA_EXT})(?:[?#][^\\s<>]*)?)>?[^\\n]*$`,
  'gim');
export function stripMediaSource(text) {
  return (text || '').replace(MEDIA_SRC_RE, '').replace(MEDIA_REF_DEF_RE, '');
}

// --- file links (ported) ---------------------------------------------------
// Inline code that is recognisably a file path becomes a file link, matching
// the Control UI. Upstream's path grammar, condensed to the same shape.
const FL_SEG = '[A-Za-z0-9_.@#+-]+';
const FL_EXT = '[A-Za-z0-9]{1,8}';
const FL_LINE = ':\\d{1,6}(?::\\d{1,6})?';
const FL_FILE = `${FL_SEG}\\.${FL_EXT}`;
const FL_ROOTED = `(?:~\\/|\\.\\.\\/|\\.\\/|\\/)(?:${FL_SEG}\\/)*${FL_FILE}`;
const FL_RELATIVE = `${FL_SEG}(?:\\/${FL_SEG})*\\/${FL_FILE}`;
const FL_PATH_RE = new RegExp(`^(?:${FL_ROOTED}|${FL_RELATIVE})(?:${FL_LINE})?$`);
const FL_BARE_RE = new RegExp(`^${FL_SEG}\\.(${FL_EXT})${`(?:${FL_LINE})?`}$`);
const FL_LINE_SUFFIX_RE = /:(\d{1,6})(?::\d{1,6})?$/;
const FL_BARE_EXTS = new Set(('astro.bash.c.cc.cfg.cjs.conf.cpp.cs.css.diff.fish.go.h.hpp.htm.html.ini.java.js.json.jsonc.jsx.kt.kts.less.lock.log.markdown.md.mdx.mjs.patch.plist.proto.py.rb.rs.scss.sh.sql.svelte.svg.swift.toml.ts.tsx.txt.vue.xml.yaml.yml.zsh').split('.'));

function splitFileLine(value) {
  const m = FL_LINE_SUFFIX_RE.exec(value);
  return m ? { path: value.slice(0, m.index), line: Number.parseInt(m[1], 10) }
           : { path: value, line: null };
}
function isBareFilename(value) {
  if (value.includes('/') || value.includes('\\')) return false;
  const m = FL_BARE_RE.exec(value);
  return !!(m && m[1] && FL_BARE_EXTS.has(m[1].toLowerCase()));
}
export function parseFilePath(value) {
  // Not `t` — that name is the i18n import at module scope. Harmless here (this
  // function doesn't call it), but keeping zero bare-`t` locals makes the
  // shadow invariant absolute and the static guard simple.
  const trimmed = (value || '').trim();
  if (!FL_PATH_RE.test(trimmed) && !isBareFilename(trimmed)) return null;
  return splitFileLine(trimmed);
}

// --- local paths (the Local Viewer affordance) -----------------------------
// A path on the DisPatch HOST that the operator can open in-app. Three
// spellings become a file link: a bare path in prose, `[[view:/p|label]]`, and
// a markdown link whose href is a local path.
//
// UNLOCKED ONLY. `noLocal` (renderMarkdown/enhanceContent option, set from
// state.decoy) is the client half of the gate; the server 403s every viewer
// route for a decoy session independently. Neither half is sufficient — and a
// local href left in place would navigate the APP's own origin, which is why
// the suppressed form is plain text/span rather than "a link that 404s".
const LOCAL_ROOTS = 'var|home|tmp|mnt|opt|srv|media|run\\/media|Users|root';
// Roots the design allows, as one token. The trailing character class is what
// bounds the path: whitespace, angle brackets, quotes, a backtick, a pipe, `]`
// or `)` all belong to the sentence (or to the markdown around it), not to the
// filename. Trailing sentence punctuation is trimmed afterwards.
const LOCAL_PATH_SRC =
  `(?:file:\\/\\/)?(?:~|\\/(?:${LOCAL_ROOTS}))\\/[^\\s<>"'\`|\\]\\)]*`;
// `/media` is a real Linux mount point AND the app's own media route; the app
// wins. `/api/…`, `/media/…` and `/static/…` are DisPatch URLs, never disk.
const APP_PATH_RE = /^(?:file:\/\/)?\/(?:api|media|static)\//;
const LOCAL_HREF_RE = new RegExp(`^(?:file:\\/\\/)?(?:~|\\/(?:${LOCAL_ROOTS}))\\/`);
// A rooted DIRECTORY: no file extension, and either a trailing slash or at
// least two segments (so `/var` and `~` alone are prose, `~/Projects` is not).
const LOCAL_DIR_RE = new RegExp(`^(?:~|\\/(?:${LOCAL_ROOTS}))(?:\\/${FL_SEG})*\\/?$`);

// --- loopback links, retargeted at the device that is reading ---------------
// An agent writes `http://127.0.0.1:8777/` because that is where the thing is
// FROM THE BOX. On the box that link works. On a phone reading the same chat
// over the tailnet, `127.0.0.1` is the phone — so the tab opens and connects to
// nothing. That is not an exotic case: half the links the bots post are a
// local service's address, and every one of them was dead on every device
// except the host.
//
// So a loopback host is rewritten to whatever host the READER reached the app
// on, keeping the scheme, port, path and query. Read the app at
// `http://homeserver:8765/` and `http://127.0.0.1:8777/x` becomes
// `http://homeserver:8777/x`; read it at `http://127.0.0.1:8765/` and nothing is
// rewritten, because there is nothing better to point at.
//
// Deliberately NOT rewritten: a private LAN address (`192.168.x`, `10.x`). It
// is a different machine, and this app has no idea what that machine is called
// on the tailnet — guessing would turn a link that is merely unreachable into
// one that points somewhere real and wrong. A rewrite also cannot make a
// loopback-BOUND service (one listening on 127.0.0.1 only) reachable; it can
// only stop the ones bound to 0.0.0.0 from being wrongly unreachable.
//
// The original address is kept in the title, so a dead link is still
// diagnosable rather than quietly a different address than the text says.
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]', '0.0.0.0']);

function isLoopbackHost(host) {
  const h = String(host || '').toLowerCase();
  return LOOPBACK_HOSTS.has(h) || h.endsWith('.localhost') || /^127\./.test(h);
}

/** The host to retarget loopback links at, or null when there is no better
 *  answer than the one already written (the reader IS on the box, or the page
 *  has no usable host — a `file://` open, say). */
export function retargetHost() {
  try {
    const here = location.hostname;
    if (!here || isLoopbackHost(here)) return null;
    return here;
  } catch { return null; }
}

/** Rewrite one href's loopback host, or return null when it is left alone.
 *
 *  Absolute URLs only — parsed with no base on purpose. A relative href is an
 *  app route (`/api/…`, `#x`), already on the right host by construction, and
 *  resolving it against the page would only invite rewriting our own links. */
export function retargetLoopbackHref(href, host) {
  if (!host || !href) return null;
  let u;
  try { u = new URL(String(href)); } catch { return null; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  if (!isLoopbackHost(u.hostname)) return null;
  u.hostname = host;
  return u.href;
}

/** Point every loopback link in a rendered container at the host this device
 *  reached the app on. Called from enhanceContent (agent messages) and from
 *  the plain-text user-bubble path — a pasted `http://127.0.0.1:…` is exactly
 *  as dead on a phone as an agent's, and it is a link either way.
 *
 *  Idempotent by construction: a rewritten anchor no longer has a loopback
 *  host, so a second pass finds nothing to do. Runs AFTER the local-path pass
 *  in enhanceContent, where a file link has already lost its href. */
export function retargetLinks(container) {
  if (!container) return;
  const host = retargetHost();
  if (!host) return;
  container.querySelectorAll('a[href]').forEach((a) => {
    const was = a.getAttribute('href');
    const now = retargetLoopbackHref(was, host);
    if (!now) return;
    a.setAttribute('href', now);
    a.dataset.originalHref = was;
    if (!a.getAttribute('title')) a.setAttribute('title', t('msg.link_retargeted', { url: was }));
    // A bare autolink shows its own URL as the text. Left at the written
    // address, that is a link visibly saying one host and going to another —
    // so when the text IS the address, it moves too. A link with words for
    // text keeps its words; only the title records where it was pointed.
    if (a.textContent.trim() === was) a.textContent = now;
  });
}

// --- quoted speech ----------------------------------------------------------
// Text between double quotes gets its own colour. Borrowed from the ClawChat
// pack, where it is the treatment that does the most work: in a long reply,
// the quoted bit is almost always the part being pointed AT — a line from a
// log, a phrase someone said, the exact wording of an error — and it used to
// sit in the paragraph looking like every other word.
//
// Done on the live DOM after sanitization, never by injecting markup, for the
// obvious reason: the text being wrapped came from a model, and the only safe
// way to put a span around somebody else's words is to build the span as a
// node around a Text node the parser already produced.
//
// Where it deliberately does nothing:
//   * inside `code`, `pre` and `kbd` — a JSON blob is full of quotes and none
//     of them are speech; this is the single most important exclusion.
//   * inside a link's text, and inside a callout's own label — those already
//     carry a colour that means something, and a second one on top reads as a
//     rendering bug.
//   * across a block boundary or a `<br>`. An unclosed quote at the end of a
//     paragraph is a stray quote character, not the start of a speech that
//     runs into the next paragraph.
//
// It DOES span inline markup: `"he said **run**"` is one quotation with a bold
// word in it, and colouring only the two plain-text halves would be worse than
// colouring nothing. That works because the treatment is colour alone — three
// adjacent spans of the same colour are indistinguishable from one, so the
// range can be wrapped piecewise instead of restructuring the tree.
const SPEECH_SKIP = 'code, pre, kbd, samp, a, .md-callout__label, .md-speech';
// Blocks that end a quotation. `div` covers our own generated cards; the
// container itself terminates the last one.
const SPEECH_BLOCK = 'p, li, td, th, blockquote, div, summary, h1, h2, h3, h4, h5, h6';
// One line, both quotes present, and bounded: past a few hundred characters a
// "quotation" is almost certainly two unrelated quote marks in a wall of prose,
// and colouring everything between them is worse than leaving it alone.
const SPEECH_RE = /["“]([^"“”\n]{1,500})["”]/g;

/** Collect the Text nodes of `root` in document order, grouped into runs that
 *  a quotation may not cross: a new run starts at every block element and at
 *  every <br>. Subtrees listed in SPEECH_SKIP are not descended into. */
function speechRuns(root, skip) {
  const runs = [];
  let run = [];
  const flush = () => { if (run.length) { runs.push(run); run = []; } };
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {                       // Text
        if (child.data) run.push(child);
        continue;
      }
      if (child.nodeType !== 1) continue;               // comments and friends
      if (child.matches(skip)) { flush(); continue; }
      const isBreak = child.tagName === 'BR';
      const isBlock = child.matches(SPEECH_BLOCK);
      if (isBreak || isBlock) flush();
      if (!isBreak) walk(child);
      if (isBlock) flush();
    }
  };
  walk(root);
  flush();
  return runs;
}

/** Wrap [from, to) of a run's concatenated text in a span, splitting Text
 *  nodes at the edges. Applied back to front by the caller, so earlier offsets
 *  in the same run stay valid. */
function wrapRange(run, offsets, from, to, cls) {
  for (let i = run.length - 1; i >= 0; i -= 1) {
    const node = run[i];
    const start = offsets[i];
    const end = start + node.data.length;
    if (end <= from || start >= to) continue;           // untouched by this quote
    let piece = node;
    if (end > to) piece.splitText(to - start);          // trim the tail off
    if (start < from) piece = piece.splitText(from - start);
    const span = node.ownerDocument.createElement('span');
    span.className = cls;
    piece.parentNode.insertBefore(span, piece);
    span.appendChild(piece);
  }
}

/** Wrap every match of `re` in `container` in a span of `cls`, skipping the
 *  subtrees in `skip` and never crossing a block or a <br>. `accept` gets the
 *  captured body and decides whether a match is really one. */
function markInline(container, { re, cls, skip, accept }) {
  if (!container) return;
  for (const run of speechRuns(container, skip)) {
    const offsets = [];
    let text = '';
    for (const node of run) { offsets.push(text.length); text += node.data; }
    const ranges = [];
    re.lastIndex = 0;
    let m = re.exec(text);
    while (m) {
      if (accept(m[1])) ranges.push([m.index, m.index + m[0].length]);
      m = re.exec(text);
    }
    // Back to front: an earlier match's offsets stay valid while a later one
    // splits the Text nodes after it.
    for (let i = ranges.length - 1; i >= 0; i -= 1) {
      wrapRange(run, offsets, ranges[i][0], ranges[i][1], cls);
    }
  }
}

/** Colour every quotation in a rendered container. Idempotent: a marked
 *  quotation lives inside `.md-speech`, which SPEECH_SKIP refuses to enter. */
export function markSpeech(container) {
  // A "quotation" of nothing but spaces is two quote marks with a gap.
  markInline(container, {
    re: SPEECH_RE, cls: 'md-speech', skip: SPEECH_SKIP, accept: (body) => !!body.trim(),
  });
}

// --- parentheses ------------------------------------------------------------
// An aside is quieter than the sentence it interrupts, and models write a LOT
// of them — "(about 12 minutes, mostly the judge)", "(this is the part that
// matters)". Dropping them to the secondary text colour is what a typesetter
// would do, and it makes the main line easier to follow at a glance.
//
// The whole difficulty is telling an aside from the many other things
// parentheses do, so the rule is narrow on purpose:
//   * at least three characters, so `(1)`, `(a)`, `(i)` — list markers people
//     write inline — are untouched;
//   * at least one letter, so `(42)`, `(3 + 4)` and `(2026-09-10)` stay plain:
//     a bare number in brackets is a value, not an aside;
//   * more than one word. An aside is prose; a single token in brackets is
//     almost always something else — `(x+1)` in a formula, `(container)` after
//     a function name, `(v2)` after a product. This costs the odd genuine
//     one-word aside like `(maybe)`, which is the right way round: a missed
//     aside reads as ordinary text, a wrongly muted formula reads as a bug;
//   * never across a line. Nested parentheses match the INNERMOST pair, which
//     is the one that is actually an aside — and, more importantly, is the
//     only reading that cannot produce a span straddling a mismatched pair;
//   * bounded, because a "(" and a ")" three paragraphs apart are not a pair.
// And, like speech, it never enters code, links, or callout labels — where a
// parenthesis is syntax rather than prose.
const PAREN_SKIP = `${SPEECH_SKIP}, .md-speech, .md-fn-ref, sub, sup`;
const PAREN_RE = /\(([^()\n]{3,300})\)/g;
const PAREN_HAS_LETTER = /\p{L}/u;

/** Mute every parenthetical aside in a rendered container. */
export function markParens(container) {
  markInline(container, {
    re: PAREN_RE,
    cls: 'md-paren',
    skip: PAREN_SKIP,
    accept: (body) => PAREN_HAS_LETTER.test(body) && /\S\s+\S/.test(body.trim()),
  });
}

// Ordering matters and is not obvious. This runs on a NON-code segment that
// has already had [[media:]]/[[doc:]]/[[view:]] expanded (each of those parks
// its HTML behind a placeholder, or leaves a `![](…)` image), so a path that
// already became a picture or a card cannot be linkified a second time. The
// alternation below then skips, in order: a markdown link/image (its
// destination is an href, not prose), an autolink or raw tag, and a
// reference-link definition line. Only what is left is prose.
const LOCAL_SCAN_RE = new RegExp(
  '(!?\\[(?:[^\\]\\\\]|\\\\.)*\\]\\([^)\\n]*\\))' +          // 1 md link/image
  '|(<[^\\s<>]*>)' +                                          // 2 autolink / raw tag
  '|(^[ \\t]{0,3}\\[[^\\]\\n]+\\]:[^\\n]*$)' +                // 3 reference definition
  `|(^|[\\s(\\[{"'])(${LOCAL_PATH_SRC})`,                     // 4 boundary, 5 the path
  'gm');
// Start-of-token is group 4: without it `https://example.com/var/home/x` would
// match from its `/var/` onwards and a website would render as a local file.

const LOCAL_TRAIL_RE = /[.,;:!?)'"]+$/;

function stripFileScheme(value) {
  return value.startsWith('file://') ? value.slice(7) : value;
}

// The label a `[[view:]]` card falls back to. `~` EXPANSION is the server's
// job (it knows the home directory); this only shortens for DISPLAY, and the
// data-file-path always carries the path verbatim.
function shortenHome(path) {
  return path.replace(/^(?:\/var)?\/home\/[^/]+(?=\/|$)/, '~')
             .replace(/^\/Users\/[^/]+(?=\/|$)/, '~');
}

/** A rooted directory reference, or null. Used for code spans, where
 *  parseFilePath (which requires a file extension) has already declined. */
export function parseLocalDir(value) {
  const trimmed = (value || '').trim();
  if (!trimmed || APP_PATH_RE.test(trimmed) || !LOCAL_DIR_RE.test(trimmed)) return null;
  const segs = trimmed.replace(/\/+$/, '').split('/').filter(Boolean);
  if (!segs.length) return null;
  if (/\.[A-Za-z0-9]{1,8}$/.test(segs[segs.length - 1])) return null;  // that's a file
  if (segs.length < 2 && !trimmed.endsWith('/')) return null;
  return { path: trimmed, line: null };
}

/** A bare local path, or null. `value` is one whitespace-delimited token. */
export function parseLocalPath(value) {
  const raw = stripFileScheme((value || '').trim());
  // Trailing punctuation belongs to the sentence. A directory's trailing
  // slash does not, so it is trimmed only when punctuation follows it.
  const trimmed = raw.replace(LOCAL_TRAIL_RE, '');
  if (!trimmed || !LOCAL_HREF_RE.test(trimmed) || APP_PATH_RE.test(trimmed)) return null;
  const { path, line } = splitFileLine(trimmed);
  if (!LOCAL_HREF_RE.test(path)) return null;
  return { path, line };
}

function fileLinkHtml(path, line, inner, extraClass = '') {
  const cls = extraClass ? `markdown-file-link ${extraClass}` : 'markdown-file-link';
  const ln = line === null || line === undefined ? '' : ` data-file-line="${escapeHtml(String(line))}"`;
  return `<a class="${cls}" data-file-path="${escapeHtml(path)}"${ln}>${inner}</a>`;
}

// `[[view:<path>|<label>]]` — the explicit directive agents write. Parked HTML
// exactly like expandDocDirectives: raw HTML in the SOURCE is escaped by the
// renderer, so generated HTML has to travel behind a placeholder instead.
function expandViewDirectives(text, parker, noLocal) {
  return subOutsideCode(text, (seg) => seg.replace(/\[\[view:([^\]|]+)(?:\|([^\]]*))?\]\]/g, (_, p, label) => {
    const path = stripFileScheme(p.trim());
    const shown = (label || '').trim() || shortenHome(path);
    // Safe Mode: the directive degrades to its label as plain text. Leaving
    // the raw `[[view:…]]` would show the reader syntax instead of words.
    if (noLocal) return shown;
    const card = `<div class="doc-card view-card"><span class="doc-icon">👁</span>` +
      `${fileLinkHtml(path, null, escapeHtml(shown), 'doc-link')}</div>`;
    return `\n\n${parker.park(card)}\n\n`;
  }));
}

// Bare paths in prose. Parked, not emitted as markdown: the anchor must not be
// re-parsed (a path with `_` or `*` in it would otherwise pick up emphasis).
function linkifyLocalPaths(text, parker, noLocal) {
  if (noLocal) return text;
  return subOutsideCode(text, (seg) => seg.replace(LOCAL_SCAN_RE,
    (whole, mdLink, tag, refDef, boundary, token) => {
      if (mdLink || tag || refDef) return whole;         // not prose — hands off
      const raw = token;
      const trimmed = raw.replace(LOCAL_TRAIL_RE, '');
      const parsed = parseLocalPath(raw);
      if (!parsed) return whole;
      const anchor = fileLinkHtml(parsed.path, parsed.line,
        `<code>${escapeHtml(trimmed)}</code>`);
      return `${boundary}${parker.park(anchor)}${raw.slice(trimmed.length)}`;
    }));
}

// --- code blocks (ported) --------------------------------------------------
// Upstream registers exactly these languages and auto-detects only among them,
// so an untagged fence can't be mis-detected as something exotic.
const HLJS_SUBSET = ['bash', 'cpp', 'css', 'diff', 'go', 'java', 'javascript',
  'json', 'markdown', 'python', 'rust', 'typescript', 'xml', 'yaml'];
const HLJS_ALIASES = { 'c++': 'cpp', cxx: 'cpp', js: 'javascript', jsx: 'javascript',
  md: 'markdown', sh: 'bash', shell: 'bash', ts: 'typescript', tsx: 'typescript' };

function highlightCode(code, lang) {
  const raw = (lang || '').trim().toLowerCase();
  const norm = HLJS_ALIASES[raw] || raw;
  try {
    if (norm && window.hljs && hljs.getLanguage(norm)) {
      return { html: hljs.highlight(code, { language: norm, ignoreIllegals: true }).value, lang: norm };
    }
    // No language tag: auto-detect within the subset, but only trust a
    // confident match (upstream's relevance >= 2 gate).
    if (!norm && code.trim() && window.hljs) {
      const subset = HLJS_SUBSET.filter((l) => hljs.getLanguage(l));
      const auto = hljs.highlightAuto(code, subset);
      // Report what it decided the language WAS, so the header can say so.
      // The `cls` the caller builds still keys off the author's tag only —
      // upstream parity — so this widens the badge and nothing else.
      if (auto.relevance >= 2) return { html: auto.value, lang: auto.language || norm };
    }
  } catch { /* fall through to escaped */ }
  return { html: escapeHtml(code), lang: norm };
}

let _hljsPromise = null;
/** Fetch highlight.js the first time a code block needs it. Resolves to the
 *  library, or null if it could not be loaded — callers must treat highlighting
 *  as optional, exactly as they already did when it was a possibly-absent
 *  global. */
export function ensureHighlighter() {
  if (window.hljs) return Promise.resolve(window.hljs);
  if (!_hljsPromise) {
    _hljsPromise = Promise.all([
      loadStyle('/static/vendor/github-dark.min.css'),
      loadScript('/static/vendor/highlight.min.js'),
    ]).then(() => window.hljs || null).catch(() => null);
  }
  return _hljsPromise;
}

/** Re-highlight already-rendered blocks once the library lands.
 *
 *  Highlighting normally happens during markdown parsing. With hljs arriving
 *  later, the first paint is plain — correct, just uncoloured — and this walks
 *  the DOM to colour it in place rather than re-parsing the message.
 *
 *  WHY THE innerHTML BELOW IS SAFE, since it is the one write in this file that
 *  does not go through DOMPurify. Its input is `codeEl.textContent` — text the
 *  sanitizer has ALREADY passed, read back as a string, so no markup survives
 *  into it. highlightCode() is the only thing that turns that string into HTML,
 *  and it escapes before wrapping (hljs's own `ignoreIllegals` path likewise
 *  emits escaped text plus its own <span class="hljs-*"> tags); its failure
 *  fallback is escapeHtml(). The `html.includes('hljs-')` guard means a result
 *  that carries no highlighter markup is not written back at all. So every
 *  branch that reaches innerHTML is escaped-text-plus-hljs-spans.
 *
 *  If highlightCode is ever changed to pass anything through unescaped, this
 *  write becomes an XSS sink — route it through the sanitizer at that point
 *  rather than trusting this comment. */
function rehighlight(container) {
  container.querySelectorAll('.code-block-wrapper pre > code').forEach((codeEl) => {
    if (codeEl.dataset.hl === '1' || codeEl.classList.contains('markdown-block-art')) return;
    codeEl.dataset.hl = '1';
    const lang = (codeEl.className.match(/language-([\w+#.-]+)/) || [])[1] || '';
    const { html } = highlightCode(codeEl.textContent || '', lang);
    if (html.includes('hljs-')) {
      codeEl.innerHTML = html;
      codeEl.classList.add('hljs');
    }
  });
}

// Block art is copied through a JSON-encoded attribute (upstream's
// `openclaw:block-art-code:` + data-code-encoding) because an HTML attribute
// normalises the runs of whitespace the art is made of.
const BLOCK_ART_CODE_PREFIX = 'openclaw:block-art-code:';
const BLOCK_ART_ENCODING = 'block-art-json';
export function decodeCopyPayload(value, encoding) {
  if (encoding !== BLOCK_ART_ENCODING || !value.startsWith(BLOCK_ART_CODE_PREFIX)) return value;
  try {
    const parsed = JSON.parse(value.slice(BLOCK_ART_CODE_PREFIX.length));
    return typeof parsed === 'string' ? parsed : value;
  } catch { return value; }
}

// Cheap shape check first so a large non-JSON block never reaches JSON.parse.
// Bounded because a message can carry a very big fence and this runs per block.
const JSON_SNIFF_MAX = 512 * 1024;
function isParseableJson(trimmed) {
  if (!trimmed || trimmed.length > JSON_SNIFF_MAX) return false;
  const first = trimmed[0];
  const last = trimmed[trimmed.length - 1];
  if (!((first === '{' && last === '}') || (first === '[' && last === ']'))) return false;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed !== null && typeof parsed === 'object';
  } catch { return false; }
}

// `code` carries the fence's trailing newline (markdown-it keeps it, marked
// strips it) so the line count and copy payload match upstream exactly.
function renderCodeBlock(code, lang, { copyText } = {}) {
  const art = isBlockArt(code);
  const copy = copyText ?? (code.endsWith('\n') ? code.slice(0, -1) : code);
  let detected = '';
  const codeHtml = art
    ? `<pre><code class="markdown-block-art">${escapeHtml(code)}</code></pre>`
    : (() => {
        const { html, lang: resolved } = highlightCode(code, lang);
        detected = resolved || '';
        const cls = [html.includes('hljs-') ? 'hljs' : '', lang ? `language-${lang}` : '']
          .filter(Boolean).join(' ');
        return `<pre><code${cls ? ` class="${escapeHtml(cls)}"` : ''}>${html}</code></pre>`;
      })();
  // The badge now also names a language marked did NOT tag but the highlighter
  // recognised (`detected` below), so an untagged fence that got highlighted
  // stops claiming to be prose. It is marked as detected rather than declared,
  // because auto-detection on a short block is a guess and labelling a guess
  // as fact is how a reader ends up trusting the wrong grammar.
  const shown = lang || (art ? '' : detected);
  const langLabel = shown
    ? `<span class="code-block-lang${lang ? '' : ' code-block-lang--detected'}"` +
      `${lang ? '' : ` title="${escapeHtml(t('msg.code_lang_detected'))}"`}>` +
      `${escapeHtml(shown)}</span>`
    : '';
  const dataCode = art ? BLOCK_ART_CODE_PREFIX + JSON.stringify(copy) : copy;
  const encAttr = art ? ` data-code-encoding="${BLOCK_ART_ENCODING}"` : '';
  // Wrap toggle. A long unbroken line — a stack trace, a URL, a base64 blob —
  // is the normal shape of what an agent posts, and on a phone the horizontal
  // scroll inside a bubble that is itself in a scrolling column is close to
  // unusable. Purely visual, per block, no state to persist: the class is
  // toggled on the wrapper by the same delegated handler that owns Copy.
  const wrapBtn = art ? '' :
    `<button type="button" class="code-block-wrap" aria-pressed="false" ` +
    `title="${escapeHtml(t('msg.code_wrap'))}" aria-label="${escapeHtml(t('msg.code_wrap'))}">` +
    `<span class="code-block-wrap__glyph" aria-hidden="true">↵</span></button>`;
  const header = `<div class="code-block-header">${langLabel}` +
    `<span class="code-block-actions">${wrapBtn}` +
    `<button type="button" class="code-block-copy" data-code="${escapeHtml(dataCode)}"${encAttr} aria-label="Copy code">` +
    `<span class="code-block-copy__idle">${escapeHtml(t('msg.copy'))}</span>` +
    `<span class="code-block-copy__done">${escapeHtml(t('msg.copied'))}</span></button></span></div>`;
  const body = `<div class="code-block-wrapper">${header}${codeHtml}</div>`;
  // JSON collapses behind a <details> — upstream's rule, and the thing the operator
  // noticed wasn't collapsing: a lang-tagged json fence OR an untagged fence
  // whose content is a bare object/array.
  // NOT `t` — that is the module-level i18n function used for the copy button
  // labels a few lines up. Shadowing it here put those earlier calls in the
  // const temporal dead zone, so renderCodeBlock threw ReferenceError on EVERY
  // fenced block: code degraded to plain text and block-art broke the whole
  // message. (Regression from the initial release; caught in review.)
  // An UNTAGGED fence has to actually parse as JSON before it is treated as
  // JSON. Brace-matching alone is a shape test, and plenty of non-JSON has that
  // shape: a bash block that opens with `{`, a C/Rust/JS function body pasted
  // without its signature, array-ish command output. Those were being hidden
  // behind a collapsed widget labelled "JSON", so the reader saw a folded
  // container over code that is not JSON and never opens by habit. A
  // lang-tagged ```json fence still collapses on the tag alone — that is the
  // author saying what it is, and it should fold even when it is truncated or
  // malformed.
  const trimmed = code.trim();
  const looksJson = lang === 'json' || (!lang && isParseableJson(trimmed));
  if (looksJson) {
    const lines = code.split('\n').length;
    const label = lines > 1 ? `JSON &middot; ${lines} lines` : 'JSON';
    return `<details class="json-collapse"><summary>${label}</summary>${body}</details>`;
  }
  return body;
}

// A ```checklist fence renders as an interactive table, NOT as a code block.
// The inner text is a normal GFM table (plus an optional leading caption line),
// so it is parsed with the same marked pipeline and parked behind a marker
// class for checklist.js. Everything still passes through DOMPurify: the
// widget is generated HTML, and the table inside it is exactly what a plain
// markdown table would have produced. The checkbox column itself is injected
// by checklist.js AFTER sanitization, so it never needs to trust model text.
function renderChecklist(md) {
  let inner;
  try {
    inner = marked.parse(String(md || ''));
  } catch (e) {
    console.warn('[markdown] checklist parse failed:', e && e.message);
    inner = escapeHtml(String(md || ''));
  }
  return `<div class="checklist-widget">${inner}</div>`;
}

// GitHub's five callout kinds, and only those five: an open-ended list would
// mean any bracketed first word silently changed how a quote renders.
// Matched against the PARSED html, because that is what the blockquote
// renderer receives — `[` is not entity-escaped, and marked turns the line
// break after the marker into either a <br> or a newline depending on the
// source, so both are optional here.
const CALLOUT_RE =
  /^\s*<p>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(?:<br\s*\/?>\s*)?/i;
const CALLOUT_ICONS = {
  note: 'ℹ️', tip: '💡', important: '❗', warning: '⚠️', caution: '🛑',
};

// --- marked renderer overrides (Control UI parity) -------------------------
// The renderer is registered ONCE per module (marked.use is global), so a
// per-call option cannot be passed through it. `_noLocal` is set around the
// synchronous marked.parse() call in renderMarkdown and restored in a finally;
// nothing yields in between, so there is no interleaving to worry about.
let _noLocal = false;
let _rendererReady = false;
function ensureRenderer() {
  if (_rendererReady || !window.marked) return;
  _rendererReady = true;
  const renderer = {
    // Raw HTML from a model is shown, never executed — upstream escapes both
    // html_block and html_inline. Our own generated HTML never travels through
    // here: it is parked behind a placeholder and restored after parsing.
    html(token) { return escapeHtml(token.text ?? token.raw ?? ''); },
    code(token) {
      const text = token.text ?? '';
      const lang = (token.lang || '').trim().split(/\s+/)[0] || '';
      // A ```checklist fence is a table the reader interacts with, not quoted
      // code: parse its inner markdown (a GFM table) and hand the widget to
      // checklist.js, which injects the checkbox column and persistence.
      if (lang === 'checklist') return renderChecklist(text);
      // Re-attach the newline marked strips, so line counts + copy payload
      // match the Control UI byte for byte.
      return renderCodeBlock(text.endsWith('\n') ? text : `${text}\n`,
                             lang,
                             { copyText: text });
    },
    // Upstream's task-list plugin tags the checkbox and adds a trailing space
    // before the label; the `contains-task-list` / `task-list-item` classes on
    // the surrounding <ul>/<li> are added by a DOMPurify hook (below) so
    // marked's own list machinery — ordered lists, nesting, loose items — is
    // left untouched.
    checkbox(token) {
      return `<input class="task-list-item-checkbox"${token.checked ? ' checked=""' : ''} disabled="" type="checkbox"> `;
    },
    codespan(token) {
      const raw = token.text ?? '';
      const html = `<code>${escapeHtml(raw)}</code>`;
      // marked has already entity-escaped the token text; decode before path
      // matching or `&amp;` in a filename would never match the grammar.
      const decoded = raw.replace(/&amp;/g, '&').replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
      // A rooted directory (`~/Projects/`, `/var/log`) has no extension, so the
      // file grammar declines it — but it is exactly what the viewer opens as a
      // listing, and it is what agents write when they name a folder.
      const f = parseFilePath(decoded) || parseLocalDir(decoded);
      // Safe Mode produces no local affordance at all: plain quoted code.
      if (!f || _noLocal) return html;
      return fileLinkHtml(f.path, f.line, html);
    },
    // A markdown link to a local path (`[report](~/x.md)`, `(/var/y)`,
    // `(file:///tmp/z)`) is rewritten HERE rather than after sanitization,
    // because `~/…` and `file://…` are not in ALLOWED_URI_REGEXP — DOMPurify
    // would have deleted the href and left an anchor with nothing to open.
    // Returning false falls back to marked's own link renderer.
    link(token) {
      const href = String(token.href || '');
      if (!LOCAL_HREF_RE.test(href) || APP_PATH_RE.test(href)) return false;
      const inner = this.parser.parseInline(token.tokens);
      if (_noLocal) return inner;
      const { path, line } = splitFileLine(stripFileScheme(href));
      return fileLinkHtml(path, line, inner);
    },
    // Upstream renders <s>; marked defaults to <del>.
    del(token) { return `<s>${this.parser.parseInline(token.tokens)}</s>`; },
    // GitHub-style callouts: a blockquote whose first line is `[!NOTE]` (or
    // TIP / IMPORTANT / WARNING / CAUTION) becomes a labelled panel.
    //
    // Worth having because it is what agents already write. This syntax comes
    // out of every README and every model that has read one, and before this
    // it rendered as a quote whose first line was a literal `[!WARNING]` —
    // i.e. the one marker meant to make a warning impossible to skim past was
    // the part that looked like a typo. A ship/hold verdict or a "this will
    // delete data" note now reads as one.
    blockquote(token) {
      const inner = this.parser.parse(token.tokens);
      const m = inner.match(CALLOUT_RE);
      if (!m) return `<blockquote>${inner}</blockquote>`;
      const kind = m[1].toLowerCase();
      // Drop the marker, keep the paragraph it opened, and drop that paragraph
      // entirely if the marker was the whole of it.
      const body = inner.replace(CALLOUT_RE, '<p>').replace(/^<p>\s*<\/p>/, '');
      // role="note", not "alert": these are authored asides in a message that
      // has already arrived. An alert role interrupts a screen reader
      // mid-sentence, which is wrong for text nobody is being warned about in
      // real time. The label carries the meaning.
      return `<div class="md-callout md-callout--${kind}" role="note">` +
        `<p class="md-callout__label"><span aria-hidden="true">${CALLOUT_ICONS[kind]}</span> ` +
        `${escapeHtml(t(`msg.callout_${kind}`))}</p>${body}</div>`;
    },
  };
  // `==highlight==` — the one inline mark markdown itself never got. Agents
  // reach for it to point at the answer inside a long reply ("the culprit is
  // ==line 412=="), and without it the reader saw the equals signs. Inline
  // level, so it composes with the rest: `==**this**==` is a highlighted bold.
  //
  // The `(?=\S)…\S` guard is what keeps `a == b` and `x ==` from being read
  // as an unterminated mark, and it must stay single-line: a `==` opening at
  // the end of one paragraph and closing three lines later is arithmetic, not
  // emphasis.
  const highlight = {
    name: 'highlight',
    level: 'inline',
    start(src) { return src.indexOf('=='); },
    tokenizer(src) {
      const m = /^==(?=[^\s=])([^\n]*?[^\s=])==/.exec(src);
      if (!m) return undefined;
      return { type: 'highlight', raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
    },
    renderer(token) { return `<mark>${this.parser.parseInline(token.tokens)}</mark>`; },
  };
  // `H~2~O` and `x^2^`. Not decoration — a CORRECTION: GFM reads a single
  // tilde as strikethrough, so `H~2~O` rendered as H, a struck-through 2, and
  // O. A chemical formula came out looking like a retracted one. Registering
  // these as extensions puts them ahead of marked's own `del` tokenizer; the
  // negative lookarounds hand `~~struck~~` back to it untouched.
  const subscript = {
    name: 'subscript',
    level: 'inline',
    start(src) { return src.indexOf('~'); },
    tokenizer(src) {
      const m = /^~(?!~)([^~\n]{1,32})~(?!~)/.exec(src);
      if (!m) return undefined;
      return { type: 'subscript', raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
    },
    renderer(token) { return `<sub>${this.parser.parseInline(token.tokens)}</sub>`; },
  };
  const superscript = {
    name: 'superscript',
    level: 'inline',
    start(src) { return src.indexOf('^'); },
    tokenizer(src) {
      const m = /^\^(?!\^)([^\^\n]{1,32})\^/.exec(src);
      if (!m) return undefined;
      return { type: 'superscript', raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
    },
    renderer(token) { return `<sup>${this.parser.parseInline(token.tokens)}</sup>`; },
  };
  marked.use({ renderer, extensions: [highlight, subscript, superscript] });
}

function plainTextFallback(text) {
  return `<div class="markdown-plain-text-fallback">${escapeHtml(text.replace(/\r\n?/g, '\n'))}</div>`;
}

// Upstream's truncation notice, same wording.
function truncate(text) {
  if (text.length <= MAX_RENDER_CHARS) return text;
  const cut = text.slice(0, MAX_RENDER_CHARS);
  return `${cut}\n\n… truncated (${text.length} chars, showing first ${cut.length}).`;
}

export function renderMarkdown(text, { noMedia = false, noLocal = false } = {}) {
  // Scrub placeholder codepoints from the SOURCE first: a model that emitted
  // them verbatim could otherwise forge a token and pull in parked HTML.
  const cleaned = stripInternalScaffolding(stripCitations(String(text || '')))
    .split(PLACEHOLDER_OPEN).join('').split(PLACEHOLDER_CLOSE).join('')
    .replace(/\r\n?/g, '\n');
  const src0 = noMedia ? stripMediaSource(cleaned) : cleaned;
  if (!src0.trim()) return '';
  if (isBlockArt(src0)) return sanitize(renderCodeBlock(src0, ''), noMedia);
  const src = truncate(src0);
  // Oversized text: skip the parser entirely rather than let it grind.
  if (src.length > PLAIN_TEXT_CHARS) return sanitize(plainTextFallback(src), noMedia);

  // Checked before anything is expanded: without the parser there is nothing to
  // restore the parked HTML, and a placeholder codepoint would be shown raw.
  if (!window.marked) return sanitize(plainTextFallback(src), noMedia);
  const parker = makeHtmlParker();
  // Maths first, on the rawest source there is: `\(x\)` has to be read before
  // markdown gets to call `\(` an escaped parenthesis.
  const { src: noted, notes } = extractFootnotes(convertMath(src), parker);
  // Order is load-bearing: every directive is expanded (media, doc, view)
  // BEFORE bare paths are linkified, so a path that has already become a
  // picture, a document card or a viewer card is behind a placeholder and
  // cannot be rewritten a second time.
  const expanded = linkifyLocalPaths(
    expandViewDirectives(
      expandDocDirectives(expandMediaDirectives(noted, parker), parker),
      parker, noLocal),
    parker, noLocal);
  ensureRenderer();
  let rawHtml;
  _noLocal = noLocal;
  try {
    rawHtml = marked.parse(expanded) + footnotesHtml(notes);
  } catch (e) {
    console.warn('[markdown] parse failed, falling back to plain text:', e && e.message);
    return sanitize(plainTextFallback(src), noMedia);
  } finally {
    _noLocal = false;
  }
  return sanitize(parker.restore(rawHtml), noMedia);
}

// Tag/attr allowlist mirrors the Control UI's DOMPurify config, plus the media
// tags DisPatch needs (upstream has no video/media pipeline) and the doc-card.
// h5/h6 were missing: DOMPurify kept the text but dropped the tag, so a deep
// heading rendered as a bare run of words glued to the next line.
const ALLOWED_TAGS = ('a b blockquote br button code del details div em h1 h2 h3 h4 h5 h6 hr i ' +
  'input li ol mark p pre s span strong sub sup summary table tbody td th thead tr ul img ' +
  'video source').split(' ');
const ALLOWED_ATTR = ['checked', 'class', 'disabled', 'href', 'rel', 'target', 'title',
  'start', 'src', 'alt', 'data-code', 'data-code-encoding', 'data-file-line',
  'data-file-path', 'type', 'aria-label', 'loading', 'controls', 'muted', 'loop',
  'playsinline', 'preload', 'poster', 'download', 'open',
  // The wrap toggle's state, and the callout panel's role/hidden-icon markup.
  // DOMPurify drops any attribute not named here, so a button whose pressed
  // state never survived sanitization would report "off" to a screen reader
  // forever while visibly being on.
  'aria-pressed', 'aria-hidden', 'role',
  // marked emits `<td align="center">` for a `|:-:|` column; without it here
  // every table rendered left-aligned no matter what the author asked for.
  'align'];

// Attributes that are NOT URIs. This list is load-bearing, not decoration:
// DOMPurify validates every attribute that isn't URI-safe, data-* or aria-*
// against ALLOWED_URI_REGEXP, so our (deliberately strict) scheme allowlist was
// silently deleting ordinary values — type="checkbox", download="notes.txt",
// loading="lazy", preload="metadata", controls. Declaring them URI-safe skips
// that check while href/src/poster stay fully URI-validated.
const URI_SAFE_ATTR = ['type', 'start', 'controls', 'muted', 'loop', 'playsinline',
  'preload', 'loading', 'download', 'open', 'checked', 'disabled', 'rel', 'target',
  'aria-label', 'data-code-encoding', 'align', 'aria-pressed', 'aria-hidden', 'role'];

function sanitize(html, noMedia) {
  if (window.DOMPurify) {
    ensureHooks();
    return DOMPurify.sanitize(html, {
      ALLOWED_TAGS: noMedia
        ? ALLOWED_TAGS.filter((t) => !['img', 'video', 'source'].includes(t))
        : ALLOWED_TAGS,
      ALLOWED_ATTR,
      ADD_URI_SAFE_ATTR: URI_SAFE_ATTR,
      // data: is confined to media tags via ADD_DATA_URI_TAGS below, so it is
      // NOT in the general scheme allowlist — this drops `<a href="data:…">`
      // (a data:text/html navigation vector) while inline data: images/video pass.
      // `[/.#?]` used to accept a bare leading `/` — which also matched a
      // PROTOCOL-RELATIVE `//evil.example/beacon.gif`, i.e. an off-box request
      // from a chat message. `\/(?!\/)` keeps root-relative paths and drops the
      // scheme-inheriting form.
      ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel):|[.#?]|\/(?!\/))/i,
      ADD_DATA_URI_TAGS: ['img', 'video', 'source'],
    });
  }
  // Fail CLOSED: if the sanitizer somehow failed to load, never inject raw
  // model/tool/web HTML (stored-XSS sink) — render it as escaped text instead.
  return `<div class="markdown-plain-text-fallback">${escapeHtml(html)}</div>`;
}

// --- tables: sort by header, resize by dragging the header edge --------------
// Deliberately tiny and dependency-free (~90 lines): markdown tables are the
// thing agents use for comparisons, and "which one is cheapest" means sorting.
// Runs AFTER sanitization on the live DOM — like the .table-scroll wrapper
// above — so nothing here travels through DOMPurify, and a re-render simply
// rebuilds from the original markdown (sort/width state is per-paint, on purpose).
//
// Sort: tap/click a header cycles ascending → descending → original order.
// Numbers (with thousands separators, currency signs, %, units like "42 ms")
// sort numerically, everything else by locale; blanks always sink to the end.
// Resize: a handle on each header's trailing edge; pointer events so one code
// path covers mouse, pen and touch (touch-action:none on the handle keeps the
// page from scrolling instead). The first drag locks every column to its
// current width and switches the table to fixed layout, so dragging one edge
// moves only that edge.
const NUMERIC_RE = /^[-+]?[$€£¥]?\s*(\d{1,3}(?:[,\s]\d{3})+|\d+)?(\.\d+)?\s*(%|[a-zA-Z]{1,4})?$/;
// Exported so checklist.js reuses the ONE comparator (numbers with currency /
// units / separators sort numerically, blanks last) instead of drifting its own.
export function cellSortValue(text) {
  const s = (text || '').trim();
  if (!s) return { num: null, str: '' };
  if (NUMERIC_RE.test(s)) {
    const n = Number.parseFloat(s.replace(/[$€£¥,\s]/g, ''));
    if (Number.isFinite(n)) return { num: n, str: s };
  }
  return { num: null, str: s.toLowerCase() };
}
export function compareCells(a, b) {
  if (!a.str && !b.str) return 0;
  if (!a.str) return 1;   // blanks last regardless of direction…
  if (!b.str) return -1;
  if (a.num !== null && b.num !== null) return a.num - b.num;
  if (a.num !== null) return -1;   // …numbers before words
  if (b.num !== null) return 1;
  return a.str.localeCompare(b.str, undefined, { numeric: true, sensitivity: 'base' });
}

/** Sort a table's body rows by column `col`. dir: 'ascending' | 'descending' |
 *  null (restore the authored order). Exported for tests. */
export function sortTable(table, col, dir) {
  const tbody = table.tBodies[0];
  if (!tbody) return;
  const rows = Array.from(tbody.rows);
  rows.forEach((r, i) => { if (r.dataset.origIndex === undefined) r.dataset.origIndex = String(i); });
  let sorted;
  if (!dir) {
    sorted = rows.slice().sort((a, b) => Number(a.dataset.origIndex) - Number(b.dataset.origIndex));
  } else {
    const keyed = rows.map((r, i) => ({ r, i, v: cellSortValue(r.cells[col]?.textContent) }));
    const sign = dir === 'descending' ? -1 : 1;
    keyed.sort((a, b) => {
      // Blanks stay last in BOTH directions — flipping the sign only applies
      // to real values, so a descending sort never leads with empty cells.
      const blank = (!a.v.str ? 1 : 0) - (!b.v.str ? 1 : 0);
      if (blank) return blank;
      return sign * compareCells(a.v, b.v) || a.i - b.i;   // stable
    });
    sorted = keyed.map((k) => k.r);
  }
  sorted.forEach((r) => tbody.appendChild(r));
  Array.from(table.tHead?.rows[0]?.cells || []).forEach((th, i) => {
    if (dir && i === col) th.setAttribute('aria-sort', dir);
    else th.removeAttribute('aria-sort');
  });
}

const MIN_COL_PX = 40;
function lockColumnWidths(table) {
  if (table.dataset.fixed === '1') return;
  const ths = Array.from(table.tHead?.rows[0]?.cells || []);
  // Measure every column FIRST, then write: a write in the loop would re-layout
  // and shift the widths still waiting to be read.
  const widths = ths.map((th) => Math.max(MIN_COL_PX, Math.round(th.getBoundingClientRect().width)));
  ths.forEach((th, i) => { th.style.width = `${widths[i]}px`; });
  table.style.width = `${widths.reduce((a, b) => a + b, 0)}px`;
  table.style.tableLayout = 'fixed';
  table.dataset.fixed = '1';
}

function enhanceTable(table) {
  const head = table.tHead?.rows[0];
  if (!head || table.dataset.enhanced === '1') return;
  table.dataset.enhanced = '1';
  table.classList.add('md-table');
  Array.from(head.cells).forEach((th, col) => {
    th.classList.add('md-th');
    th.tabIndex = 0;
    th.setAttribute('role', 'columnheader');
    const sortNext = () => {
      const cur = th.getAttribute('aria-sort');
      sortTable(table, col, cur === 'ascending' ? 'descending' : cur === 'descending' ? null : 'ascending');
    };
    th.addEventListener('click', (e) => { if (!e.target.closest('.md-th-resize')) sortNext(); });
    th.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortNext(); }
    });

    const grip = document.createElement('span');
    grip.className = 'md-th-resize';
    grip.setAttribute('aria-hidden', 'true');
    grip.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      e.preventDefault(); e.stopPropagation();
      lockColumnWidths(table);
      const startX = e.clientX;
      const startW = Number.parseFloat(th.style.width) || th.getBoundingClientRect().width;
      const startT = Number.parseFloat(table.style.width) || 0;
      const rtl = window.getComputedStyle(table).direction === 'rtl';
      table.classList.add('is-resizing');
      try { grip.setPointerCapture(e.pointerId); } catch { /* synthetic/unknown pointer id: document listeners below still track */ }
      const move = (ev) => {
        const dx = (ev.clientX - startX) * (rtl ? -1 : 1);
        const w = Math.max(MIN_COL_PX, Math.round(startW + dx));
        th.style.width = `${w}px`;
        table.style.width = `${startT - startW + w}px`;
      };
      const up = () => {
        table.classList.remove('is-resizing');
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        document.removeEventListener('pointercancel', up);
      };
      // Document-level, not grip-level: with pointer capture the events reach
      // the grip anyway, and without it (a finger that wanders off a 12px
      // handle) the drag still tracks instead of sticking half-done.
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
      document.addEventListener('pointercancel', up);
    });
    // A click that ends a drag must not also sort.
    grip.addEventListener('click', (e) => e.stopPropagation());
    th.appendChild(grip);
  });
}

// Highlight code blocks and add copy buttons inside a rendered container.
export function enhanceContent(container, { noLocal = false } = {}) {
  if (!container) return;
  // Local paths that arrived as a real <a href> — a markdown link rendered
  // elsewhere, or content re-enhanced after a paint. renderMarkdown's link
  // renderer already handles the common case; this is the backstop, and the
  // ONLY place that can catch an href the sanitizer left navigable.
  //
  // `/var/x` matches ALLOWED_URI_REGEXP, so without this a Safe-Mode session
  // would be offered a link that navigates the APP's origin to a path that
  // isn't a route. Under noLocal the anchor is replaced by a plain span: the
  // words survive, the affordance does not.
  container.querySelectorAll('a').forEach((a) => {
    const href = a.getAttribute('href') || '';
    const local = href && LOCAL_HREF_RE.test(href) && !APP_PATH_RE.test(href);
    if (!local && !a.hasAttribute('data-file-path')) return;
    if (noLocal) {
      const span = document.createElement('span');
      span.textContent = a.textContent || '';
      a.replaceWith(span);
      return;
    }
    if (!local) return;
    const { path, line } = splitFileLine(stripFileScheme(href));
    a.removeAttribute('href');
    a.removeAttribute('target');
    a.removeAttribute('rel');
    a.classList.add('markdown-file-link');
    a.dataset.filePath = path;
    if (line !== null) a.dataset.fileLine = String(line);
  });
  retargetLinks(container);
  // Wrap markdown tables in a scroll container so a wide table scrolls sideways
  // instead of collapsing its columns (see app.css), then make them sortable +
  // resizable. Idempotent: skips tables already inside a .table-scroll.
  container.querySelectorAll('table').forEach((table) => {
    if (table.parentElement && table.parentElement.classList.contains('table-scroll')) return;
    const wrap = document.createElement('div');
    wrap.className = 'table-scroll';
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
    // A checklist table is wired by checklist.js (checkbox column + a sort that
    // keeps completed rows pinned at the bottom). It still gets the shared
    // .md-table look; only the generic sort/resize wiring is skipped.
    if (table.closest('.checklist-widget')) {
      table.classList.add('md-table');
    } else {
      enhanceTable(table);
    }
  });
  // First code block on the page pulls highlight.js in; everything already
  // rendered gets coloured once it arrives.
  if (container.querySelector('.code-block-wrapper pre > code:not([data-hl])')) {
    ensureHighlighter().then((h) => { if (h) rehighlight(container); });
  }
  // Code blocks now arrive pre-highlighted with their own header/copy button
  // (Control UI parity). Any bare <pre> without our chrome — e.g. the plain-text
  // fallback path — still gets a copy affordance.
  container.querySelectorAll('pre').forEach((pre) => {
    if (pre.closest('.code-block-wrapper') || pre.querySelector('.copy-btn')) return;
    const block = pre.querySelector('code');
    if (!block || block.classList.contains('markdown-block-art')) return;
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(block.innerText);
        btn.textContent = 'Copied'; btn.classList.add('copied');
        setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1400);
      } catch { /* clipboard unavailable */ }
    });
    pre.appendChild(btn);
  });
  // Agents habitually write `http://…` in backticks; that renders as an inert
  // <code> span, which reads as a link and clicks as nothing. Wrap any inline
  // code whose entire text is one http(s) URL in a real anchor. Built via DOM
  // APIs after sanitization, scheme-checked, so nothing unvetted reaches href.
  container.querySelectorAll('code').forEach((code) => {
    if (code.closest('pre, a') || code.dataset.linked) return;
    const txt = (code.textContent || '').trim();
    if (!/^https?:\/\/\S+$/.test(txt)) return;
    code.dataset.linked = '1';
    const a = document.createElement('a');
    a.href = txt;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'code-url-link';
    code.replaceWith(a);
    a.appendChild(code);
  });
  // Rewrite any inline markdown image whose src is a bare local path.
  container.querySelectorAll('img').forEach((img) => {
    const raw = img.getAttribute('src') || '';
    const norm = normalizeMediaUrl(raw);
    if (norm !== raw) img.setAttribute('src', norm);
    // EVERY thumbnail opens its full-resolution original — including the ones
    // markdown builds. Attachments got this from mediaThumbEl(); an image the
    // AGENT sent arrives as markdown instead and was landing here with no
    // data-full, so the delegated listener ignored it and clicking a generated
    // picture did nothing at all. Chat media is stored once, so the original
    // is the same URL — the affordance is what was missing, not the file.
    //
    // Nothing to gate on here: when media is not allowed (Safe Mode, NIM) the
    // sanitizer has already stripped <img> from the allowed tags, so an image
    // that exists at this point is one the viewer is permitted to open.
    if (!img.dataset.full && norm) img.dataset.full = norm;
  });
  // Inline videos: normalize src and give them the gif treatment (muted
  // autoplay loop). Sound/controls live in the lightbox (click to open).
  container.querySelectorAll('video').forEach((v) => {
    const raw = v.getAttribute('src') || '';
    const norm = normalizeMediaUrl(raw);
    if (norm !== raw) v.setAttribute('src', norm);
    v.muted = true; v.loop = true; v.playsInline = true;
    v.play().catch(() => {});
  });
  // LAST, and deliberately so: these split Text nodes to wrap a run, and every
  // pass above reads whole ones. Speech before parentheses, so a parenthetical
  // inside a quotation keeps the quotation's colour rather than fighting it.
  markSpeech(container);
  markParens(container);
}

// Delegated handlers for the ported chrome: code-block copy + file-path copy.
// Registered once, on document, so streamed/re-rendered content is covered
// without re-binding per message.
let _delegatedReady = false;
export function installMarkdownHandlers(onToast, { onOpenFile } = {}) {
  if (_delegatedReady) return;
  _delegatedReady = true;
  // A long press is the touch equivalent of Shift-click: it keeps the old
  // copy-to-clipboard behaviour when the primary action became "open". Measured
  // on touchend rather than by a timer, so the copy fires with the same click
  // the finger produces instead of racing it.
  let touchStartedAt = 0;
  let longPress = false;
  const LONG_PRESS_MS = 500;
  document.addEventListener('touchstart', (e) => {
    if (!e.target.closest?.('a[data-file-path]')) return;
    touchStartedAt = Date.now();
    longPress = false;
  }, { passive: true });
  document.addEventListener('touchend', (e) => {
    if (!e.target.closest?.('a[data-file-path]')) return;
    longPress = touchStartedAt > 0 && (Date.now() - touchStartedAt) >= LONG_PRESS_MS;
    touchStartedAt = 0;
  }, { passive: true });
  document.addEventListener('click', async (e) => {
    const copyBtn = e.target.closest?.('.code-block-copy');
    if (copyBtn) {
      try {
        const raw = copyBtn.dataset.code || '';
        const text = decodeCopyPayload(raw, copyBtn.dataset.codeEncoding);
        await navigator.clipboard.writeText(text);
        copyBtn.classList.add('copied');
        setTimeout(() => copyBtn.classList.remove('copied'), 1500);
      } catch { /* clipboard unavailable */ }
      return;
    }
    const wrapBtn = e.target.closest?.('.code-block-wrap');
    if (wrapBtn) {
      const block = wrapBtn.closest('.code-block-wrapper');
      if (block) {
        const on = block.classList.toggle('wrapped');
        wrapBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
      return;
    }
    // Upstream opens a side panel here; DisPatch has the Local Viewer, so a
    // plain click OPENS the path and the modifier/long-press keeps the old
    // copy. Without an onOpenFile (Safe Mode, or a host that has no viewer)
    // copying stays the only behaviour.
    const fileLink = e.target.closest?.('a[data-file-path]');
    if (fileLink) {
      e.preventDefault();
      const path = fileLink.dataset.filePath || '';
      const line = fileLink.dataset.fileLine;
      const full = line ? `${path}:${line}` : path;
      const wantCopy = e.shiftKey || e.altKey || longPress;
      longPress = false;
      if (onOpenFile && !wantCopy) {
        onOpenFile(path, line ? Number.parseInt(line, 10) : null, e);
        return;
      }
      try {
        await navigator.clipboard.writeText(full);
        onToast?.(`Copied ${full}`);
      } catch { /* clipboard unavailable */ }
    }
  });
}
