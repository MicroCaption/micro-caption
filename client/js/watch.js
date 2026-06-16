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

// Caption-only viewer: with no video to stay in lock-step with, favour
// readability over liveness. Stitch fragments into full ≤2×32 blocks and hold
// each on screen for at least 3s (the whole block appears at once — never
// word-by-word), with a longer idle hold to finish reading the last block.
const pacer = new CaptionPacer(renderLines, {
  minDwell: 3000,     // hold each block ≥3s for comfortable reading
  maxDwell: 6000,     // but cap so a long block doesn't stall the screen
  maxLagSec: 6.0,     // tolerate more lag rather than churn text quickly
  idleClearMs: 8000,
});

const es = new EventSource(API + '/events/watch/' + WATCH_CODE);

es.addEventListener('cue', e => {
  const d = JSON.parse(e.data);
  const text = d.text || (d.lines || []).join(' ');
  stat.textContent = 'LIVE'; stat.className = 'live';
  // Live best-effort CEA-608/708 speaker-change mark, stitched inline.
  const prefix = d.speaker_change ? '>> ' : '';
  pacer.push(prefix + text, parseFloat(d.start), parseFloat(d.end));
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
        const d = r.ok ? await r.json() : null;
        // The stream is over once it's gone (404) or the server marks it ended.
        if (r.status === 404 || (d && (d.status === 'ended' || d.status === 'gone'))) {
          clearInterval(pollTimer); pollTimer = null;
          stat.textContent = 'Stream ended'; stat.className = 'error';
          es.close();
        }
      } catch(e) {}
    }, 3000);
  }
};
