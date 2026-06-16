// Read watch code from query param: /watch-viewer?code=<code>
// CaptionPacer comes from captions.js (loaded first).
const params = new URLSearchParams(window.location.search);
const WATCH_CODE = params.get('code') || '';

if (!WATCH_CODE) {
  window.location.href = '/watch';
}

document.getElementById('code-display').textContent = 'Code: ' + WATCH_CODE;

const cap  = document.getElementById('viewer-captions');
const stat = document.getElementById('viewer-status');

let pollTimer = null;

function renderLines(lines) {
  cap.innerHTML = '';
  (lines || []).forEach(l => {
    if (!l || !l.trim()) return;
    const s = document.createElement('span');
    s.className = 'caption-line';
    s.textContent = l;
    cap.appendChild(s);
  });
}

// Caption-only viewer: pace fragments for comfortable reading. A slightly
// longer idle hold suits the no-video viewer (more time to finish reading).
const pacer = new CaptionPacer(renderLines, { idleClearMs: 8000 });

const es = new EventSource(API + '/events/watch/' + WATCH_CODE);

es.addEventListener('cue', e => {
  const d = JSON.parse(e.data);
  const text = d.text || (d.lines || []).join(' ');
  stat.textContent = 'LIVE'; stat.className = 'live';
  pacer.push(text, parseFloat(d.start), parseFloat(d.end));
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
