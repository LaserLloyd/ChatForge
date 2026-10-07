#!/usr/bin/env node
// Render the README screenshots of the popup (and, later, the Settings page).
//
//   uv run python -m chatforge.desktop.webserver --port 8791 &
//   node scripts/screenshots.mjs            # popup and settings
//   node scripts/screenshots.mjs popup      # docs/images/popup-*.png only
//   node scripts/screenshots.mjs settings   # docs/images/settings-*.png only
//
// The pages run in a plain browser against static/js/dev-mock.js (canned replies, see its
// header for the magic words): the popup at 420x620, Settings at 980x760, both at 2x. Set SCREENSHOT_URL to use another server
// (default http://127.0.0.1:8791). Needs Playwright (global install is fine) and a
// Chromium browser (PLAYWRIGHT_BROWSERS_PATH). Each shot waits for the mock to finish
// streaming: no .streaming turn, live think block, running tool chip, cursor or typing
// bubble, no toast, and the Stop button hidden.

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(ROOT, 'docs/images');
const BASE = (process.env.SCREENSHOT_URL || 'http://127.0.0.1:8791').replace(/\/$/, '');
const VIEWPORT = { width: 420, height: 620 };            // the popup
const VIEWPORT_SETTINGS = { width: 980, height: 760 };   // the Settings window
const THEME_KEY = 'chatforge.theme';
const MAX_KB = 250;

async function loadChromium() {
  try {
    return (await import('playwright')).chromium;
  } catch {
    const globalRoot = execSync('npm root -g').toString().trim();
    return createRequire(path.join(globalRoot, 'x.js'))('playwright').chromium;
  }
}

/** A fresh browser context: its own localStorage, so the mock starts from a clean install. */
async function newPage(browser, { theme = null, viewport = VIEWPORT } = {}) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, reducedMotion: 'reduce' });
  if (theme) {
    // The app follows the theme in the settings (the mock's config.ui.theme, kept in
    // sessionStorage), so seed both that and ThemeForge's own storage key.
    await ctx.addInitScript(({ k, v }) => {
      try {
        localStorage.setItem(k, v);
        if (!sessionStorage.getItem('chatforge.mock.v1')) {
          const ui = { theme: v, hotkey: 'Ctrl+Alt+C', sticky: true, show_on_reply: true, width: 420, height: 620, margin: 12 };
          sessionStorage.setItem('chatforge.mock.v1', JSON.stringify({ config: { ui } }));
        }
      } catch { /* ignore */ }
    }, { k: THEME_KEY, v: theme });
  }
  const page = await ctx.newPage();
  page.on('console', (m) => { if (process.env.DEBUG_SHOTS) console.log('  console:', m.text()); });
  page.on('pageerror', (e) => console.warn('  page error:', e.message));
  return page;
}

/** True once nothing on the page is still moving or loading. */
const settled = () => {
  const q = (s) => document.querySelector(s);
  const chip = q('#chip-label')?.textContent || '';
  return !!chip && !/Loading/i.test(chip)
    && !q('.msg.streaming, .is-live, .stream-cursor, .typing-bubble, .tool-chip[data-state="running"]')
    && !q('#toast:not([hidden]), .ui-toast')
    && q('#stop')?.classList.contains('hidden') !== false;
};

async function waitSettled(page) {
  await page.waitForFunction(settled, null, { timeout: 60000 });
  await page.waitForTimeout(400);                       // two quiet frames, not one lucky one
  await page.waitForFunction(settled, null, { timeout: 5000 });
}

async function openPopup(page) {
  await page.goto(`${BASE}/index.html`);
  await page.waitForSelector('.empty-state');
  await waitSettled(page);
}

async function ask(page, text) {
  await page.fill('#input', text);
  await page.waitForFunction(() => !document.getElementById('send').disabled);
  await page.click('#send');
  await page.waitForSelector('.msg.assistant');
  await waitSettled(page);
  await page.evaluate(() => { const m = document.getElementById('messages'); m.scrollTop = m.scrollHeight; });
  await page.mouse.move(VIEWPORT.width - 1, VIEWPORT.height / 2);   // no hover state
  await page.evaluate(() => document.activeElement?.blur?.());
  await page.waitForTimeout(300);
}

async function shot(page, name) {
  const file = path.join(OUT, name);
  await page.screenshot({ path: file });
  const kb = fs.statSync(file).size / 1024;
  console.log(`${name}  ${kb.toFixed(0)} KB${kb > MAX_KB ? `  (over ${MAX_KB} KB)` : ''}`);
}

// What the mock answers to each message: no magic word gives the markdown showcase (heading,
// table, code block, callout, lists); "search" runs web_search (tool chip, short answer with
// a footnote); "document" runs create_document (a card with Open / Show in folder).
const REPLY_MESSAGE = 'Show me what you can render';   // no magic word: the markdown showcase
const SEARCH_MESSAGE = 'search openvino npu';
const FILES_MESSAGE = 'Write a document with my meeting summary';

async function popup(browser) {
  let page = await newPage(browser);
  await openPopup(page);
  await shot(page, 'popup-empty.png');

  await page.click('#model-chip');
  await page.waitForSelector('#model-menu:not([hidden])');
  await page.waitForTimeout(300);
  await shot(page, 'popup-model-menu.png');
  await page.context().close();

  page = await newPage(browser);
  await openPopup(page);
  await ask(page, REPLY_MESSAGE);
  await shot(page, 'popup-reply.png');
  // The reply is taller than the window: the same reply scrolled up to its table and code block.
  await page.evaluate(() => {
    const h = [...document.querySelectorAll('.msg.assistant h2')].find((e) => /table/i.test(e.textContent));
    const box = document.getElementById('messages');
    if (h) box.scrollTop += h.getBoundingClientRect().top - box.getBoundingClientRect().top - 12;
  });
  await page.waitForTimeout(300);
  await shot(page, 'popup-markdown.png');
  await page.context().close();

  page = await newPage(browser);
  await openPopup(page);
  await ask(page, SEARCH_MESSAGE);
  await shot(page, 'popup-search.png');
  await page.context().close();

  page = await newPage(browser);
  await openPopup(page);
  await ask(page, FILES_MESSAGE);
  await page.waitForSelector('.doc-card, [class*="doc-card"]');
  await shot(page, 'popup-files.png');
  await page.context().close();

  page = await newPage(browser, { theme: 'daylight' });
  await openPopup(page);
  await ask(page, REPLY_MESSAGE);
  const theme = await page.evaluate(() => document.documentElement.dataset.palette);
  if (theme !== 'daylight') console.warn(`  expected the daylight theme, got ${theme}`);
  await shot(page, 'popup-light.png');
  await page.context().close();
}

/** The Settings page has loaded its mock data (the subtitle leaves "Loading…") and is quiet. */
async function openSettings(page) {
  await page.goto(`${BASE}/settings.html`);
  await page.waitForFunction(() => {
    const sub = document.getElementById('s-sub')?.textContent || '';
    return sub && !/Loading/i.test(sub) && document.querySelector('#model-list .model-row');
  }, null, { timeout: 30000 });
  await page.waitForTimeout(600);
}

async function showTab(page, id) {
  await page.click(`#tab-${id}`);
  await page.waitForSelector(`#panel-${id}:not([hidden])`);
  await page.waitForTimeout(500);
}

/** Park the pointer and focus out of the way so no hover or focus ring is in the shot. */
async function calm(page) {
  await page.mouse.move(VIEWPORT_SETTINGS.width - 2, VIEWPORT_SETTINGS.height - 2);
  await page.evaluate(() => { document.activeElement?.blur?.(); window.scrollTo(0, 0); });
  await page.waitForTimeout(300);
}

async function settings(browser) {
  // Models: the first installed model is made active and loaded, then shown as Loaded.
  let page = await newPage(browser, { viewport: VIEWPORT_SETTINGS });
  await openSettings(page);
  await page.click('.model-row [data-fk$=":load"]');
  await page.waitForFunction(() => {
    const row = document.querySelector('.model-row');
    return row && /Loaded/.test(row.innerText) && /Unload/.test(row.innerText) && !/Loading/.test(row.innerText);
  }, null, { timeout: 30000 });
  // The "Loading the model" toast times out on its own; wait it out.
  await page.waitForFunction(() => !document.querySelector('.ui-toast, #toast:not([hidden])'), null, { timeout: 30000 });
  await page.waitForTimeout(500);
  await calm(page);
  await shot(page, 'settings-models.png');

  // Providers: scrolled to the cloud provider cards.
  await showTab(page, 'providers');
  await page.evaluate(() => {
    document.getElementById('h-cloud').scrollIntoView({ block: 'start' });
    window.scrollBy(0, -(document.querySelector('[role="tablist"]')?.getBoundingClientRect().bottom ?? 0) - 12);
  });
  await page.waitForTimeout(400);
  await page.mouse.move(VIEWPORT_SETTINGS.width - 2, VIEWPORT_SETTINGS.height - 2);
  await page.evaluate(() => document.activeElement?.blur?.());
  await shot(page, 'settings-providers.png');
  await page.context().close();

  // General: top of the tab (hotkey Record button, theme swatch grid).
  page = await newPage(browser, { viewport: VIEWPORT_SETTINGS });
  await openSettings(page);
  await showTab(page, 'general');
  await page.waitForFunction(() => document.querySelectorAll('#g-theme-grid > *').length > 1);
  await calm(page);
  await shot(page, 'settings-general.png');
  await page.context().close();
}

const which = process.argv[2] || 'all';
if (!['all', 'popup', 'settings'].includes(which)) {
  console.error('usage: node scripts/screenshots.mjs [popup|settings]');
  process.exit(2);
}
fs.mkdirSync(OUT, { recursive: true });
const browser = await (await loadChromium()).launch();
try {
  if (which !== 'settings') await popup(browser);
  if (which !== 'popup') await settings(browser);
} finally {
  await browser.close();
}
