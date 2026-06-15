// Read watch code from query param: /watch-viewer.html?code=<code>
const params = new URLSearchParams(window.location.search);
const WATCH_CODE = params.get('code') || '';

if (!WATCH_CODE) {
  window.location.href = '/watch.html';
}

document.getElementById('code-display').textContent = 'Code: ' + WATCH_CODE;

const cap  = document.getElementById('viewer-captions');
const stat = document.getElementById('viewer-status');

const MAX_CHARS    = 32;
const DWELL_MS     = 6000;
const MIN_STABLE_MS = 1500;

let pendingText = '', displayedText = '', lastRenderTime = 0;
let renderTimer = null, clearTimer = null, pollTimer = null;

function splitLines(text) {
  const words = text.trim().split(/\s+/);
  const lines = []; let line = '';
  for (const w of words) {
    const c = line ? line + ' ' + w : w;
    if (c.length <= MAX_CHARS) { line = c; } else { if (line) lines.push(line); line = w; }
  }
  if (line) lines.push(line);
  return lines;
}

function doRender(text) {
  cap.innerHTML = '';
  splitLines(text).slice(-2).forEach(l => {
    if (!l.trim()) return;
    const s = document.createElement('span');
    s.className = 'caption-line'; s.textContent = l; cap.appendChild(s);
  });
  displayedText = text; lastRenderTime = Date.now();
}

function tryUpdate() {
  if (!pendingText || pendingText === displayedText) return;
  const wait = MIN_STABLE_MS - (Date.now() - lastRenderTime);
  if (wait <= 0) { doRender(pendingText); } else if (!renderTimer) {
    renderTimer = setTimeout(() => { renderTimer = null; tryUpdate(); }, wait);
  }
}

function onCue(text) {
  if (!text.trim()) return;
  if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }
  pendingText = text;
  tryUpdate();
  clearTimer = setTimeout(() => {
    cap.innerHTML = ''; pendingText = displayedText = ''; lastRenderTime = 0;
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    clearTimer = null;
  }, DWELL_MS);
}

const es = new EventSource(API + '/events/watch/' + WATCH_CODE);

es.addEventListener('cue', e => {
  const d = JSON.parse(e.data);
  const text = d.text || (d.lines || []).join(' ');
  stat.textContent = 'LIVE'; stat.className = 'live';
  onCue(text);
});

es.onopen = () => {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
};

es.onerror = () => {
  stat.textContent = 'Reconnecting…'; stat.className = 'error';
  if (!pollTimer) {
    pollTimer = setInterval(async () => {
      try {
        const r = await fetch(API + '/api/watch/' + WATCH_CODE);
        if (r.status === 404) {
          clearInterval(pollTimer); pollTimer = null;
          stat.textContent = 'Session ended'; stat.className = 'error';
          es.close();
        }
      } catch(e) {}
    }, 3000);
  }
};
