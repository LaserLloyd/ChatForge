#!/usr/bin/env node
// Render the ThemeForge showcase images for the README.
//
//   node scripts/theme_showcase.mjs
//
// Writes docs/images/themes/<slug>.png (one 560x360 card per theme, 2x scale) and
// docs/images/themes/gallery.png (all ten cards, 2-column grid), straight from the
// bundle in src/chatforge/web/static/ui-theme/ (themes.json for names and one-liners).
// Re-run it after a ThemeForge update. Needs Playwright (global install is fine) and a
// Chromium browser (PLAYWRIGHT_BROWSERS_PATH). No colour literals: every colour on a card
// comes from the bundle's tokens, so the images are honest renders of each theme.
//
// Each card is its own HTML document (the runtime applies one theme per <html>), written to
// a temp dir and opened over file://; the gallery embeds those same files in iframes.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BUNDLE = path.join(ROOT, 'src/chatforge/web/static/ui-theme');
const OUT = path.join(ROOT, 'docs/images/themes');
const CARD_W = 560;
const CARD_H = 360;
const GAP = 24;

async function loadChromium() {
  try {
    return (await import('playwright')).chromium;
  } catch {
    const globalRoot = execSync('npm root -g').toString().trim();
    return createRequire(path.join(globalRoot, 'x.js'))('playwright').chromium;
  }
}

const themes = JSON.parse(fs.readFileSync(path.join(BUNDLE, 'themes.json'), 'utf8')).themes;
const SLUGS = themes.map((t) => t.slug);
const url = (f) => pathToFileURL(path.join(BUNDLE, f)).href;
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// ---- Compositions: hand-assigned, a different set of real components per theme ----------

const ava = (t, cls = '') => `<span class="ui-avatar ${cls}" aria-hidden="true">${t}</span>`;
const page = (n, cur) =>
  `<a class="ui-btn ui-btn--sm" href="#"${cur ? ' aria-current="page"' : ''}>${n}</a>`;

const COMPOSITIONS = {
  // tabs + primary button + switches
  purple: `
    <div class="ui-tabs" role="tablist">
      <button class="ui-tab" role="tab" aria-selected="true">Overview</button>
      <button class="ui-tab" role="tab" aria-selected="false">Models</button>
      <button class="ui-tab" role="tab" aria-selected="false">History</button>
    </div>
    <div class="row">
      <button class="ui-btn ui-btn--primary">Start a chat</button>
      <label class="ui-check"><input type="checkbox" role="switch" class="ui-switch" checked> Streaming <span class="ui-switch-state" aria-hidden="true"></span></label>
      <label class="ui-check"><input type="checkbox" role="switch" class="ui-switch"> Tools <span class="ui-switch-state" aria-hidden="true"></span></label>
    </div>`,

  // chat bubbles + chips
  'midnight-gold': `
    <div class="ui-stack" style="gap: var(--space-2)">
      <div class="ui-bubble ui-bubble--user">Summarise today's tasks.</div>
      <div class="ui-bubble">Three open: review the draft, ship the fix, and reply to Sam.</div>
    </div>
    <div class="row">
      <span class="ui-chip">tasks</span>
      <span class="ui-chip" aria-pressed="true">today</span>
      <span class="ui-chip">high priority <button class="ui-chip__remove" aria-label="Remove">&times;</button></span>
    </div>`,

  // code block + badge row
  glacier: `
    <figure class="ui-codeblock">
      <figcaption class="ui-codeblock__bar"><span>python</span><button class="ui-codeblock__button" type="button">Copy</button></figcaption>
      <pre><code><span class="hljs-keyword">def</span> <span class="hljs-title function_">greet</span>(name: <span class="hljs-built_in">str</span>) -&gt; <span class="hljs-built_in">str</span>:
    <span class="hljs-comment"># say hello</span>
    <span class="hljs-keyword">return</span> <span class="hljs-string">f"Hello, {name}!"</span>
<span class="hljs-built_in">print</span>(greet(<span class="hljs-string">"world"</span>), <span class="hljs-number">42</span>)</code></pre>
    </figure>
    <div class="row">
      <span class="ui-badge ui-badge--accent">Accent</span>
      <span class="ui-badge ui-badge--success">Done</span>
      <span class="ui-badge ui-badge--info">Info</span>
      <span class="ui-badge ui-badge--warning">Warn</span>
      <span class="ui-badge ui-badge--live">Live</span>
    </div>`,

  // stats + progress + success callout
  forest: `
    <div class="row" style="gap: var(--space-8)">
      <div class="ui-stat"><span class="ui-stat__label">Chats</span><span class="ui-stat__value">1,284</span></div>
      <div class="ui-stat"><span class="ui-stat__label">Tokens</span><span class="ui-stat__value">3.2M</span></div>
    </div>
    <progress class="ui-progress" value="72" max="100" aria-label="Sync"></progress>
    <div class="ui-callout ui-callout--success"><div class="ui-callout__title">Synced</div>All conversations are up to date.</div>`,

  // input + radio group + outline button, serif prose
  paper: `
    <p class="prose">Ink on parchment, a quiet place to read and write.</p>
    <div class="ui-field"><label class="ui-label" for="p-name">Notebook name</label><input class="ui-input" id="p-name" value="Field notes"></div>
    <div class="row">
      <label class="ui-check"><input type="radio" name="p" class="ui-radio" checked> Draft</label>
      <label class="ui-check"><input type="radio" name="p" class="ui-radio"> Final</label>
      <label class="ui-check"><input type="radio" name="p" class="ui-radio"> Archive</label>
      <span class="spacer"></span>
      <button class="ui-btn ui-btn--outline">Save copy</button>
    </div>`,

  // table (two rows) + pagination
  daylight: `
    <div class="ui-table-wrap"><table class="ui-table">
      <thead><tr><th>Model</th><th>Provider</th><th>Status</th></tr></thead>
      <tbody>
        <tr><td>claude-sonnet</td><td>Anthropic</td><td><span class="ui-badge ui-badge--success">Ready</span></td></tr>
        <tr><td>llama-3.3</td><td>Ollama</td><td><span class="ui-badge ui-badge--idle">Idle</span></td></tr>
      </tbody>
    </table></div>
    <nav class="ui-pagination" aria-label="Pages">${page('Previous')}${page(1)}${page(2, true)}${page(3)}${page('Next')}</nav>`,

  // segmented + danger callout
  'electric-yellow': `
    <div class="ui-segmented" role="group" aria-label="Range">
      <button aria-pressed="false">Hour</button><button aria-pressed="true">Day</button><button aria-pressed="false">Week</button><button aria-pressed="false">Month</button>
    </div>
    <div class="ui-callout ui-callout--danger"><div class="ui-callout__title">Provider unreachable</div>Check the API key and try again.</div>`,

  // menu + cta button
  laserlloyd: `
    <div class="row" style="align-items: flex-start; gap: var(--space-6)">
      <div class="ui-menu" role="menu">
        <button class="ui-menu__item" role="menuitem">Rename</button>
        <button class="ui-menu__item" role="menuitem">Duplicate</button>
        <hr class="ui-menu__separator">
        <button class="ui-menu__item ui-menu__item--danger" role="menuitem">Delete</button>
      </div>
      <button class="ui-btn ui-btn--cta">Launch ChatForge</button>
    </div>`,

  // checkboxes + chips
  'laserlloyd-light': `
    <div class="ui-stack" style="gap: var(--space-2)">
      <label class="ui-check"><input type="checkbox" class="ui-checkbox" checked> Remember me</label>
      <label class="ui-check"><input type="checkbox" class="ui-checkbox" checked> Email me a summary</label>
      <label class="ui-check"><input type="checkbox" class="ui-checkbox"> Share usage data</label>
    </div>
    <div class="row">
      <span class="ui-chip" aria-pressed="true">design</span>
      <span class="ui-chip">research</span>
      <span class="ui-chip">code <button class="ui-chip__remove" aria-label="Remove">&times;</button></span>
      <span class="ui-chip">ops</span>
    </div>`,

  // toast + avatar + ghost buttons
  'night-red': `
    <div class="ui-toast ui-toast--danger">Connection lost. Retrying in 5 seconds.</div>
    <div class="row">
      ${ava('JL', 'ui-avatar--lg')}${ava('AK')}${ava('SM')}
      <span class="spacer"></span>
      <button class="ui-btn ui-btn--ghost">Dismiss</button>
      <button class="ui-btn ui-btn--ghost">Retry</button>
    </div>`,
};

// ---- Card document ---------------------------------------------------------------------------

function cardHtml(t) {
  const body = COMPOSITIONS[t.slug];
  if (!body) throw new Error(`no composition for ${t.slug}`);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<script src="${url('ui-theme.js')}"
        data-themes="${SLUGS.join(',')}"
        data-default="${t.slug}"
        data-storage-key="themeshowcase.${t.slug}"></script>
<link rel="stylesheet" href="${url('ui-theme-base.css')}">
<link rel="stylesheet" href="${url('ui-components.css')}">
<style>
  html, body { width: ${CARD_W}px; height: ${CARD_H}px; margin: 0; overflow: hidden; }
  body { padding: var(--space-6) var(--space-7); box-sizing: border-box; display: flex; flex-direction: column; }
  .name {
    margin: 0;
    font-family: var(--font-display);
    font-size: var(--fs-3xl);
    font-weight: var(--fw-bold);
    letter-spacing: var(--ls-snug);
    line-height: var(--lh-tight);
    color: var(--text-primary);
  }
  .desc { margin: var(--space-2) 0 0; color: var(--text-secondary); font-size: var(--fs-sm); line-height: 1.45; }
  .demo { margin-top: auto; display: flex; flex-direction: column; gap: var(--space-4); }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); }
  .spacer { flex: 1; }
  .prose { margin: 0; font-family: var(--font-serif); font-size: var(--fs-md); line-height: 1.5; color: var(--text-primary); }
  .ui-menu { flex: none; }
</style>
<link rel="stylesheet" href="${url('ui-theme.css')}">
</head>
<body>
  <h1 class="name">${esc(t.name)}</h1>
  <p class="desc">${esc(t.description)}</p>
  <div class="demo">${body}</div>
</body>
</html>`;
}

// ---- Gallery: ten iframes, themed with the bundle's base theme for its own ground ------------

function galleryHtml(cardUrls) {
  const frames = cardUrls
    .map((u, i) => `<iframe src="${u}" title="${esc(themes[i].name)}" width="${CARD_W}" height="${CARD_H}" loading="eager"></iframe>`)
    .join('\n');
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<script src="${url('ui-theme.js')}" data-themes="purple" data-default="purple" data-storage-key="themeshowcase.gallery"></script>
<link rel="stylesheet" href="${url('ui-theme-base.css')}">
<style>
  html, body { margin: 0; }
  body { padding: ${GAP}px; width: ${CARD_W * 2 + GAP * 3}px; box-sizing: border-box; }
  .grid { display: grid; grid-template-columns: repeat(2, ${CARD_W}px); gap: ${GAP}px; }
  iframe { display: block; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface-0); }
</style>
<link rel="stylesheet" href="${url('ui-theme.css')}">
</head>
<body><div class="grid">
${frames}
</div></body>
</html>`;
}

// ---- Render ----------------------------------------------------------------------------------

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'theme-showcase-'));
fs.mkdirSync(OUT, { recursive: true });

const chromium = await loadChromium();
const browser = await chromium.launch();
try {
  const ctx = await browser.newContext({
    viewport: { width: CARD_W, height: CARD_H },
    deviceScaleFactor: 2,
    reducedMotion: 'reduce',
  });
  const cardUrls = [];
  const settle = async (p) => {
    await p.evaluate(() => document.fonts.ready);
    await p.waitForTimeout(250);
  };

  for (const t of themes) {
    const file = path.join(tmp, `${t.slug}.html`);
    fs.writeFileSync(file, cardHtml(t));
    cardUrls.push(pathToFileURL(file).href);
    const p = await ctx.newPage();
    await p.goto(cardUrls.at(-1));
    await settle(p);
    const got = await p.evaluate(() => document.documentElement.getAttribute('data-palette') || 'purple');
    if (got !== t.slug) throw new Error(`${t.slug}: runtime applied "${got}"`);
    const clipped = await p.evaluate(() => document.body.scrollHeight > document.body.clientHeight + 1);
    if (clipped) console.warn(`warning: ${t.slug} content overflows the card`);
    await p.screenshot({ path: path.join(OUT, `${t.slug}.png`) });
    await p.close();
    console.log('wrote', `${t.slug}.png`);
  }

  const gFile = path.join(tmp, 'gallery.html');
  fs.writeFileSync(gFile, galleryHtml(cardUrls));
  const gp = await ctx.newPage();
  await gp.setViewportSize({ width: CARD_W * 2 + GAP * 3, height: Math.ceil(themes.length / 2) * (CARD_H + GAP) + GAP });
  await gp.goto(pathToFileURL(gFile).href);
  await settle(gp);
  // every iframe must have loaded its document and fonts before the shot
  for (const f of gp.frames().slice(1)) {
    await f.waitForLoadState('load');
    await f.evaluate(() => document.fonts.ready);
  }
  await gp.waitForTimeout(500);
  await gp.screenshot({ path: path.join(OUT, 'gallery.png'), fullPage: true });
  console.log('wrote gallery.png');
} finally {
  await browser.close();
  fs.rmSync(tmp, { recursive: true, force: true });
}
