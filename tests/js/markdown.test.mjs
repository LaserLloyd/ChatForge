// Behavioural tests for the popup's markdown pipeline (PLAN section 5).
//
// Harness adapted from DisPatch_Chat frontend/tests/markdown-behaviour.test.js (MIT, LaserLloyd):
// one memoised jsdom window per file, vendored marked + DOMPurify evaluated into it, then
// markdown.js imported AFTER the globals exist (so marked.setOptions({breaks:true}) applies,
// exactly as in the page where the deferred vendor scripts run before the module).
//
//   node --test tests/js
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'src', 'chatforge', 'web', 'static');
const fixture = (name) => readFileSync(join(HERE, 'fixtures', name), 'utf8');

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');

// runScripts:'outside-only' makes window.eval real. Without it the vendored bundles evaluate
// into nothing and every render silently takes the fail-closed plain-text path, which would
// look like a passing XSS test.
const win = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://127.0.0.1:8765/',
  runScripts: 'outside-only',
}).window;
for (const file of ['purify.min.js', 'marked.min.js']) {
  win.eval(readFileSync(join(STATIC, 'vendor', file), 'utf8'));
}
globalThis.window = win;
globalThis.document = win.document;
globalThis.DOMPurify = win.DOMPurify;
globalThis.marked = win.marked;
assert.ok(win.DOMPurify, 'purify.min.js did not define DOMPurify');
assert.ok(win.marked, 'marked.min.js did not define marked');

const md = await import(pathToFileURL(join(STATIC, 'js', 'markdown.js')).href);
const { renderMarkdown, enhanceContent } = md;
const OPTS = { noMedia: true, noLocal: true };   // what chat.js passes

/** Render into a live container in the jsdom document. */
function mount(html) {
  const div = win.document.createElement('div');
  div.className = 'ui-markdown';
  div.innerHTML = html;
  win.document.body.appendChild(div);
  return div;
}

// ---------------------------------------------------------------- PLAN 5 ----

test('a GFM table becomes a <table>', () => {
  const div = mount(renderMarkdown(fixture('showcase.md'), OPTS));
  const table = div.querySelector('table');
  assert.ok(table, 'no <table> rendered');
  assert.equal(table.querySelectorAll('thead th').length, 4);
  assert.equal(table.querySelectorAll('tbody tr').length, 2);
});

test('fenced code has the code-block header and the copy button', () => {
  const div = mount(renderMarkdown(fixture('showcase.md'), OPTS));
  const block = div.querySelector('.code-block-wrapper');
  assert.ok(block, 'no .code-block-wrapper');
  assert.ok(block.querySelector('.code-block-header'), 'no header');
  assert.equal(block.querySelector('.code-block-lang').textContent.trim().toLowerCase(), 'python');
  const copy = block.querySelector('button.code-block-copy');
  assert.ok(copy, 'no copy button');
  assert.match(copy.textContent, /Copy/);           // from the i18n shim (msg.copy)
  assert.match(copy.textContent, /Copied/);         // and msg.copied
  assert.ok(block.querySelector('pre > code'), 'code is not inside <pre>');
  assert.ok(block.querySelector('button.code-block-wrap'), 'no wrap toggle');
  assert.match(block.querySelector('button.code-block-wrap').getAttribute('title'), /Wrap long lines/);
});

test('footnotes render as a marker plus a list', () => {
  const div = mount(renderMarkdown(fixture('showcase.md'), OPTS));
  const ref = div.querySelector('sup.md-fn-ref');
  assert.ok(ref, 'no footnote marker');
  assert.equal(ref.textContent, '1');
  const list = div.querySelector('.md-footnotes ol li');
  assert.ok(list, 'no footnote list');
  assert.match(list.textContent, /The footnote text\./);
  assert.doesNotMatch(div.textContent, /\[\^1\]/, 'raw footnote syntax leaked');
});

test('<script> and onerror are removed', () => {
  const out = renderMarkdown(fixture('hostile.md'), OPTS);
  assert.doesNotMatch(out, /<script/i);
  assert.doesNotMatch(out, /<img/i);
  const div = mount(out);
  assert.equal(div.querySelectorAll('script, img').length, 0);
  assert.equal(div.querySelectorAll('[onerror], [onclick], [onload]').length, 0);
  // Raw HTML is escaped to text by the renderer, not parsed.
  assert.match(out, /&lt;script/);
  assert.equal(win.__pwned, undefined);
});

test('a javascript: href is removed', () => {
  const out = renderMarkdown('[click me](javascript:alert(1)) and <a href="javascript:alert(2)">x</a>', OPTS);
  const div = mount(out);
  assert.equal(div.querySelectorAll('a[href]').length, 0, out);   // no anchor keeps a javascript: target
  assert.equal(div.querySelectorAll('[href^="javascript" i]').length, 0, out);
  assert.match(div.textContent, /click me/);                        // the link text survives as text
  const direct = win.DOMPurify.sanitize('<a href="javascript:alert(1)">x</a>');
  assert.doesNotMatch(direct, /javascript:/i);
});

test('external links open in a new tab with rel=noopener', () => {
  const div = mount(renderMarkdown('[docs](https://example.com/docs)', OPTS));
  const a = div.querySelector('a[href^="https://example.com/"]');
  assert.ok(a, 'link missing');
  assert.equal(a.getAttribute('target'), '_blank');
  assert.match(a.getAttribute('rel'), /\bnoopener\b/);
  assert.match(a.getAttribute('rel'), /\bnoreferrer\b/);
});

test('[[media:...]] with noMedia does not produce <img>', () => {
  for (const src of ['[[media:/media/a.png|pic]]', '![inline](https://example.com/a.png)',
    '![data](data:image/png;base64,iVBORw0KGgo=)', 'text [[media:/tmp/x.mp4]] more']) {
    const out = renderMarkdown(src, OPTS);
    assert.doesNotMatch(out, /<img|<video|<source/i, `${src} -> ${out}`);
    assert.doesNotMatch(out, /api\/media/, `${src} -> ${out}`);
  }
});

test("renderMarkdown('') does not throw", () => {
  assert.equal(renderMarkdown('', OPTS), '');
  assert.equal(renderMarkdown(undefined, OPTS), '');
  assert.equal(renderMarkdown(null, OPTS), '');
  assert.equal(renderMarkdown('   \n  ', OPTS), '');
  assert.doesNotThrow(() => renderMarkdown(''));
});

// ------------------------------------------------------------ hardening ----

test('protocol-relative and data: text/html URLs never survive', () => {
  for (const src of ['![x](//evil.example/b.gif)', '[x](//evil.example/p)', '[x](data:text/html;base64,PHNjcmlwdD4=)']) {
    const out = renderMarkdown(src, OPTS);
    assert.doesNotMatch(out, /\/\/evil\.example/, `${src} -> ${out}`);
    assert.doesNotMatch(out, /data:text\/html/i, `${src} -> ${out}`);
  }
});

test('noLocal: a bare local path is not turned into a file link', () => {
  const out = renderMarkdown('see C:\\Users\\me\\secret.txt and /etc/passwd or `~/notes.md`', OPTS);
  assert.doesNotMatch(out, /data-file-path/);
  assert.doesNotMatch(out, /markdown-file-link/);
});

test('noLocal: a [[doc:...]] directive is plain text, never a card pointing at /api/files', () => {
  for (const src of ['see [[doc:report.pdf]] here', '[[doc:abc123|Quarterly report.pdf]]', '[[doc:../../api/export?format=json]]']) {
    const out = renderMarkdown(src, OPTS);
    assert.doesNotMatch(out, /doc-card|api\/files|<a /, `${src} -> ${out}`);
    assert.doesNotMatch(out, /\[\[doc:/, `${src} -> ${out}`);
  }
  assert.match(mount(renderMarkdown('see [[doc:report.pdf]] here', OPTS)).textContent, /see report\.pdf here/);
  assert.match(mount(renderMarkdown('[[doc:abc123|Quarterly report.pdf]]', OPTS)).textContent, /Quarterly report\.pdf/);
  // Without noLocal (DisPatch) the card is unchanged.
  assert.match(renderMarkdown('[[doc:report.pdf]]'), /class="doc-card"/);
});

test('enhanceContent with noLocal strips a local-path anchor', () => {
  const div = mount('<p><a href="/home/me/file.txt">the file</a> and <a href="https://example.com/">web</a></p>');
  enhanceContent(div, { noLocal: true });
  assert.equal(div.querySelector('a[href="/home/me/file.txt"]'), null);
  assert.match(div.textContent, /the file/);
  assert.ok(div.querySelector('a[href="https://example.com/"]'), 'web link was removed');
});

test('a [!WARNING] blockquote renders as a labelled callout', () => {
  const div = mount(renderMarkdown('> [!WARNING]\n> Careful here.', OPTS));
  const callout = div.querySelector('.md-callout.md-callout--warning');
  assert.ok(callout, 'not a callout');
  assert.match(callout.querySelector('.md-callout__label').textContent, /Warning/);  // msg.callout_warning
  assert.match(callout.textContent, /Careful here\./);
});

test('every callout label comes from the i18n shim', () => {
  for (const kind of ['note', 'tip', 'important', 'warning', 'caution']) {
    const div = mount(renderMarkdown(`> [!${kind.toUpperCase()}]\n> body`, OPTS));
    const label = div.querySelector('.md-callout__label').textContent.trim().toLowerCase();
    assert.ok(label.endsWith(kind), `${kind}: label was ${JSON.stringify(label)} (missing msg.callout_${kind}?)`);
  }
});

test('enhanceContent makes tables sortable and wraps them for scrolling', () => {
  const div = mount(renderMarkdown('| name | price |\n|---|---|\n| pear | $1,200 |\n| apple | 95 |\n| fig |  |', OPTS));
  enhanceContent(div, { noLocal: true });
  const table = div.querySelector('table');
  assert.equal(table.parentElement.className, 'table-scroll');
  const th = table.tHead.rows[0].cells[1];
  assert.ok(th.querySelector('.md-th-resize'), 'no resize grip');
  const col = () => Array.from(table.tBodies[0].rows).map((r) => r.cells[1].textContent.trim());
  th.click();
  assert.deepEqual(col(), ['95', '$1,200', ''], 'ascending, blanks last');
  th.click();
  assert.deepEqual(col(), ['$1,200', '95', ''], 'descending, blanks last');
});

test('a stream cut mid-table or mid-fence still renders (no throw, no raw script)', () => {
  const partial = fixture('partial.md');
  let out;
  assert.doesNotThrow(() => { out = renderMarkdown(partial, OPTS); });
  const div = mount(out);
  assert.ok(div.querySelector('pre'), 'the unfinished fence should still render as code');
  assert.match(div.textContent, /const x = \[1, 2,/);
  // Every prefix of a document renders: chat.js paints these as they stream in.
  const doc = fixture('showcase.md');
  for (let i = 1; i < doc.length; i += 23) {
    assert.doesNotThrow(() => renderMarkdown(doc.slice(0, i), OPTS), `prefix ${i}`);
  }
});

test('reasoning-style text with angle brackets is shown as text, never parsed', () => {
  const out = renderMarkdown('use a <b onclick="x()">bold</b> tag and <iframe src="//e"></iframe>', OPTS);
  const div = mount(out);
  assert.equal(div.querySelectorAll('iframe, b, [onclick]').length, 0);
});

test('the i18n shim serves every msg.* key markdown.js asks for', async () => {
  const { t, hasDictionary, applyDom } = await import(pathToFileURL(join(STATIC, 'js', 'i18n.js')).href);
  assert.equal(hasDictionary(), true);
  assert.doesNotThrow(() => applyDom());
  for (const [k, v] of Object.entries({
    'msg.view_raw': 'View raw', 'msg.code_wrap': 'Wrap long lines', 'msg.copy': 'Copy', 'msg.copied': 'Copied',
    'msg.callout_note': 'Note', 'msg.callout_tip': 'Tip', 'msg.callout_important': 'Important',
    'msg.callout_warning': 'Warning', 'msg.callout_caution': 'Caution',
    'msg.code_lang_detected': 'Language detected automatically',
  })) assert.equal(t(k), v, k);
  assert.match(t('msg.link_retargeted', { url: 'http://x:1/' }), /http:\/\/x:1\//);
  assert.equal(t('nope.missing'), 'nope.missing');
});

test('prose blocks (text, markdown, untagged) start wrapped; code and JSON do not; Copy copies the exact text', () => {
  const para = 'A corrected paragraph that is long enough to need wrapping in a 420 pixel popup, with “quotes” and  two spaces.';
  const fence = (lang, body) => `\`\`\`${lang}\n${body}\n\`\`\``;
  for (const lang of ['text', 'markdown', 'md', 'TXT', '']) {
    const block = mount(renderMarkdown(fence(lang, para), OPTS)).querySelector('.code-block-wrapper');
    assert.ok(block.classList.contains('wrapped'), `${lang || 'untagged'} should start wrapped`);
    assert.equal(block.querySelector('.code-block-wrap').getAttribute('aria-pressed'), 'true');
    assert.equal(block.querySelector('.code-block-copy').dataset.code, para);
  }
  const code = mount(renderMarkdown(fence('python', 'print("a very long line of code that should scroll, not wrap")'), OPTS))
    .querySelector('.code-block-wrapper');
  assert.equal(code.classList.contains('wrapped'), false);
  assert.equal(code.querySelector('.code-block-wrap').getAttribute('aria-pressed'), 'false');
  const json = mount(renderMarkdown(fence('', '{"a": [1, 2, 3]}'), OPTS)).querySelector('.code-block-wrapper');
  assert.equal(json.classList.contains('wrapped'), false, 'untagged JSON is data, not prose');
  // A multi-line block keeps its lines exactly for Copy.
  const lines = 'Line one\n\n  - indented item\nLast line';
  const multi = mount(renderMarkdown(fence('text', lines), OPTS)).querySelector('.code-block-copy');
  assert.equal(multi.dataset.code, lines);
});

// ------------------------------------------------- streaming render caches ----

test('repeat renders are stable, and the options are part of the cache key', () => {
  const src = 'Look: ![pic](/media/a.png) and `~/notes/todo.md`';
  const full = renderMarkdown(src, {});
  assert.equal(renderMarkdown(src, {}), full);
  assert.match(full, /<img/);
  assert.doesNotMatch(renderMarkdown(src, { noMedia: true }), /<img/);
  assert.doesNotMatch(renderMarkdown(src, { noLocal: true }), /data-file-path/);
  assert.match(renderMarkdown(src, {}), /data-file-path/);
  // Same text again after the others: still the first answer, not the last one stored.
  assert.equal(renderMarkdown(src), full);
});

test('highlighting is not served stale when highlight.js arrives after a render', () => {
  const code = 'def f(x):\n    return x + 1\n';
  const before = renderMarkdown('```python\n' + code + '```', OPTS);
  assert.doesNotMatch(before, /hljs-/);
  win.eval(readFileSync(join(STATIC, 'vendor', 'highlight.min.js'), 'utf8'));
  globalThis.hljs = win.hljs;
  try {
    const after = renderMarkdown('```python\n' + code + '```', OPTS);
    assert.match(after, /hljs-/, 'the cached plain render was reused');
    // A second pass (memoised highlight) is byte-identical to the first.
    assert.equal(renderMarkdown('```python\n' + code + '```\n\nmore', OPTS).includes(after.slice(0, 200)), true);
    // Streaming a block one chunk at a time ends on the same HTML as one render.
    const whole = renderMarkdown('```js\nconst a = 1;\nconst b = 2;\n```', OPTS);
    for (let n = 1; n < 30; n += 3) renderMarkdown('```js\nconst a = 1;\nconst b = 2;\n```'.slice(0, n), OPTS);
    assert.equal(renderMarkdown('```js\nconst a = 1;\nconst b = 2;\n```', OPTS), whole);
  } finally {
    delete win.hljs;
    delete globalThis.hljs;
  }
  assert.doesNotMatch(renderMarkdown('```python\n' + code + '```', OPTS), /hljs-/);
});

test('the fast paths agree with the full scrub on scaffolding, citations and placeholders', () => {
  const U = String.fromCharCode;
  assert.equal(renderMarkdown('before <system-reminder>secret</system-reminder> after', OPTS), '<p>before  after</p>\n');
  assert.equal(renderMarkdown('a\n<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\nhidden\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>\nb', OPTS),
    renderMarkdown('a\nb', OPTS));
  assert.equal(renderMarkdown(`x${U(0xe200)}cite${U(0xe202)}turn0${U(0xe201)}y`, OPTS), '<p>xy</p>\n');
  // A forged placeholder in the source must not pull parked HTML in.
  const forged = renderMarkdown(`a ${U(0xe300)}0${U(0xe301)} b`, OPTS);
  assert.doesNotMatch(forged, /[]/);
});
