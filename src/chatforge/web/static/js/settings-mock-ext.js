// Settings-window extension of dev-mock.js. Loaded by settings.js ONLY when the dev mock is
// active (window.pywebview.__mock), so it never runs under the real Python bridge.
//
// dev-mock.js always reports OVMS installed and the VC++ runtime present. To exercise those
// branches of the Models tab, add query parameters to settings.html:
//   ?runtime=missing   OVMS reports "not installed" until runtime_install() reaches "done"
//   ?vcredist=missing  the VC++ runtime reports missing (Install is disabled, winget help shown)
//   ?vcredist=na       vcredist is null and platform "linux" (not applicable: the row is hidden)
//   ?firstrun=1        a fresh install: no runtime and no models (the Models tab's setup checklist);
//                      "Download recommended model" then really downloads the mock model

import { on } from './bridge.js';

const params = new URLSearchParams(location.search);
const api = window.pywebview && window.pywebview.api;

if (api && api.runtime_status && (params.get('runtime') === 'missing' || ['missing', 'na'].includes(params.get('vcredist')))) {
  const original = api.runtime_status.bind(api);
  let installed = params.get('runtime') !== 'missing';
  on('runtime.install', (e) => { if (e.status === 'done') installed = true; });
  api.runtime_status = async () => {
    const res = await original();
    const out = { ...res, installed };
    if (!installed) out.version = null;
    if (params.get('vcredist') === 'missing') out.vcredist = false;
    if (params.get('vcredist') === 'na') { out.vcredist = null; out.platform = 'linux'; }
    return out;
  };
}

// A fresh install: nothing installed. runtime_install() and the mock downloads fill it in.
if (params.get('firstrun') === '1' && window.__mock) {
  const st = window.__mock.state();
  st.models = [];
  st.runtimeInstalled = false;
  st.config.chat.model = '';
}
