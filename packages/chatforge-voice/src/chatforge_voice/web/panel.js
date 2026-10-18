'use strict';
// The Voice panel. The token comes in the URL fragment and is sent as a header; text from the server is
// only ever put in with textContent.
const token = new URLSearchParams(location.hash.slice(1)).get('t') || '';
if (location.hash) history.replaceState(null, '', location.pathname);

const $ = (id) => document.getElementById(id);
const STATES = {
  off: 'Voice is off. Turn on Voice (CPU) in ChatForge Settings.',
  idle: 'Ready.',
  recording: 'Listening...',
  transcribing: 'Transcribing...',
  thinking: 'Waiting for the answer...',
  speaking: 'Speaking...',
};

async function call(path, body) {
  const opts = { headers: { 'X-Voice-Token': token } };
  if (body !== undefined) {
    opts.method = 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  try {
    const res = await fetch(path, opts);
    return await res.json();
  } catch (e) {
    return { ok: false, message: 'Cannot reach ChatForge. Is it running?' };
  }
}

function fill(list, items) {
  list.replaceChildren(...items.map((t) => { const li = document.createElement('li'); li.textContent = t; return li; }));
}

function render(data) {
  if (!data.voice) { $('state').textContent = data.message || 'Not connected.'; return; }
  const v = data.voice, m = data.models;
  $('state').textContent = v.message || STATES[v.state] || v.state;
  $('heard').textContent = v.heard ? `Heard: ${v.heard}` : '';
  const notices = [...(v.notices || []), ...(m.notices || [])];
  $('notices').hidden = notices.length === 0;
  $('notices').textContent = notices.join(' ');
  $('mic').disabled = !v.enabled || !m.ready;
  $('models-line').textContent = m.ready ? 'The voice models are installed.' : 'The voice models are not installed yet.';
  fill($('missing'), m.ready ? [] : m.missing);
  const job = m.job, running = job.state === 'running';
  $('bar').hidden = !running; $('bar').value = job.percent || 0;
  $('cancel').hidden = !running;
  $('download').disabled = running || m.ready;
  $('job').textContent = running ? `${job.percent}% ${job.file || ''}`
    : job.state === 'failed' ? `The download failed: ${job.error}`
    : job.state === 'cancelled' ? 'The download was cancelled; press Download to resume.'
    : job.state === 'done' ? 'Download finished.' : '';
  $('size').textContent = `About ${(m.download_mb / 1000).toFixed(1)} GB to download; about ${m.ram_gb} GB of memory while voice is loaded.`;
  fill($('programs'), m.programs.map((p) => `${p.id}: ${p.found ? 'found' : 'not found'} - ${p.what} (not needed for push-to-talk)`));
  fill($('licences'), m.licences.map((l) => `${l.name}: ${l.licence} (${l.by})`));
  $('attribution').textContent = m.attribution;
}

async function refresh() { render(await call('/api/status')); }

const mic = $('mic');
let holding = false;
async function down() { if (holding || mic.disabled) return; holding = true; mic.classList.add('live'); render(await call('/api/talk/start', {})); }
async function up() { if (!holding) return; holding = false; mic.classList.remove('live'); render(await call('/api/talk/stop', {})); }
mic.addEventListener('pointerdown', (e) => { mic.setPointerCapture(e.pointerId); down(); });
mic.addEventListener('pointerup', up);
mic.addEventListener('pointercancel', up);
mic.addEventListener('keydown', (e) => { if (e.key === ' ' && !e.repeat) { e.preventDefault(); down(); } });
mic.addEventListener('keyup', (e) => { if (e.key === ' ') { e.preventDefault(); up(); } });
$('stop').addEventListener('click', async () => render(await call('/api/stop', {})));
$('download').addEventListener('click', async () => {
  const mb = $('size').textContent;
  if (!window.confirm(`${mb}\n\n${$('attribution').textContent}\n\nDownload now?`)) return;
  const res = await call('/api/models/download', { confirm: true });
  if (!res.ok) $('job').textContent = res.message || 'Could not start the download.';
  refresh();
});
$('cancel').addEventListener('click', async () => { await call('/api/models/cancel', {}); refresh(); });

refresh();
setInterval(refresh, 700);
