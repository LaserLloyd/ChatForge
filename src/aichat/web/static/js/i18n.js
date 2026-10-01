// i18n shim for AI Chat (English only).
//
// markdown.js (verbatim from DisPatch) imports { t } from './i18n.js?v=3' and asks
// for `msg.*` keys. DisPatch nests those under "msg" in locales/en.json; this file
// flattens the ones markdown.js uses, so there is no dictionary loader, no locale
// files and no DOM pass. Adapted from DisPatch_Chat locales/en.json (MIT, LaserLloyd).
const DICT = {
  'msg.view_raw': 'View raw',
  'msg.link_retargeted': 'Opens on this device\u2019s network \u2014 written as {url}',
  'msg.code_lang_detected': 'Language detected automatically',
  'msg.code_wrap': 'Wrap long lines',
  'msg.copy': 'Copy',
  'msg.copied': 'Copied',
  'msg.regenerate': 'Regenerate',
  'menu.recent': 'Recently used',
  'msg.callout_note': 'Note',
  'msg.callout_tip': 'Tip',
  'msg.callout_important': 'Important',
  'msg.callout_warning': 'Warning',
  'msg.callout_caution': 'Caution',
};

export function t(key, vars) {
  let s = Object.prototype.hasOwnProperty.call(DICT, key) ? DICT[key] : String(key || '');
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
  return s;
}
export function applyDom() { /* English only: nothing to re-translate */ }
export function hasDictionary() { return true; }
