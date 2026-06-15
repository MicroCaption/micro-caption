// Read session ID and mode from query params: /player.html?id=<id>&mode=live|replay
const params = new URLSearchParams(window.location.search);
const SESSION_ID = params.get('id') || '';
const MODE = params.get('mode') || 'live';

if (!SESSION_ID) {
  document.body.innerHTML = '<p style="color:#555;padding:40px;font-family:monospace">No session ID — redirecting…</p>';
  setTimeout(() => { window.location.href = '/dashboard.html'; }, 1500);
}

// ── Caption rendering ─────────────────────────────────────────────────────────
const bar    = document.getElementById('caption-bar');
const status = document.getElementById('status');

let pendingText   = '';
let displayedText = '';
let lastRenderTime = 0;
let renderTimer   = null;
let clearTimer    = null;
let videoPaused   = false;

const DWELL_MS      = 5000;
const MIN_STABLE_MS = 2000;
const MAX_CHARS     = 32;

function splitLines(text) {
  const words = text.trim().split(/\s+/);
  const lines = [];
  let line = '';
  for (const word of words) {
    const candidate = line ? line + ' ' + word : word;
    if (candidate.length <= MAX_CHARS) {
      line = candidate;
    } else {
      if (line) lines.push(line);
      line = word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function isContinuation(prev, next) {
  if (!prev) return false;
  const anchor = prev.trim().split(/\s+/).slice(-3).join(' ').toLowerCase();
  return anchor.length > 2 && next.toLowerCase().includes(anchor);
}

function doRender(text) {
  bar.innerHTML = '';
  splitLines(text).slice(-2).forEach(line => {
    if (!line.trim()) return;
    const s = document.createElement('span');
    s.className = 'caption-line';
    s.textContent = line;
    bar.appendChild(s);
  });
  displayedText  = text;
  lastRenderTime = Date.now();
}

function tryUpdate() {
  if (!pendingText || pendingText === displayedText) return;
  const wait = MIN_STABLE_MS - (Date.now() - lastRenderTime);
  if (wait <= 0) {
    doRender(pendingText);
  } else if (!renderTimer) {
    renderTimer = setTimeout(() => { renderTimer = null; tryUpdate(); }, wait);
  }
}

function onCue(newText) {
  if (!newText.trim()) return;
  if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }
  const isNew = !isContinuation(pendingText || displayedText, newText);
  pendingText = newText;
  if (isNew) {
    displayedText  = '';
    lastRenderTime = 0;
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
  }
  tryUpdate();
  clearTimer = setTimeout(() => {
    bar.innerHTML  = '';
    pendingText = displayedText = '';
    lastRenderTime = 0;
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    clearTimer = null;
  }, DWELL_MS);
}

// ── Stop button ───────────────────────────────────────────────────────────────
async function stopSession() {
  await fetch(API + '/api/stop/' + SESSION_ID, { method: 'POST' });
  window.location.href = '/dashboard.html';
}

// ── Replay mode ───────────────────────────────────────────────────────────────
const allCues = [];
let replayTimer = null;

async function initReplay() {
  try {
    const r = await fetch(API + '/api/session/' + SESSION_ID + '/cues');
    const cues = await r.json();
    allCues.push(...cues);
  } catch(e) {}
  replayTimer = setInterval(replayTick, 250);
}

function replayTick() {
  if (!window.ytPlayer || typeof ytPlayer.getCurrentTime !== 'function') return;
  const t = ytPlayer.getCurrentTime();
  let found = null;
  for (let i = allCues.length - 1; i >= 0; i--) {
    if (allCues[i].start <= t) { found = allCues[i]; break; }
  }
  if (found && found.text !== pendingText) {
    onCue(found.text);
  } else if (!found && pendingText) {
    bar.innerHTML = '';
    pendingText = displayedText = '';
  }
}

async function jumpToLive() {
  if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
  window.location.href = '/player.html?id=' + SESSION_ID;
}

// ── YouTube IFrame API ────────────────────────────────────────────────────────
let ytPlayer;

window.onYouTubeIframeAPIReady = function() {
  ytPlayer = new YT.Player('yt-iframe', {
    events: {
      onReady: _mcOnPlayerReady,
      onStateChange: onPlayerStateChange,
    },
  });
};

async function _mcOnPlayerReady(event) {
  if (MODE === 'replay') {
    initReplay();
  } else {
    try {
      const r = await fetch(API + '/api/session/' + SESSION_ID + '/position');
      const d = await r.json();
      if (d.current_time > 2) { event.target.seekTo(d.current_time, true); }
    } catch(e) {}
  }
}

function onPlayerStateChange(event) {
  if (event.data === YT.PlayerState.PAUSED || event.data === YT.PlayerState.BUFFERING) {
    videoPaused = true;
    if (clearTimer)  { clearTimeout(clearTimer);  clearTimer  = null; }
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    status.textContent = 'Paused';
    status.className   = 'waiting';
  } else if (event.data === YT.PlayerState.PLAYING) {
    videoPaused = false;
    status.textContent = 'Connected — waiting for speech…';
    status.className   = 'waiting';
  }
}

// ── SSE caption stream ────────────────────────────────────────────────────────
const es = new EventSource(API + '/events/' + SESSION_ID);

es.addEventListener('cue', e => {
  const d = JSON.parse(e.data);
  status.textContent = 'LIVE';
  status.className = 'live';
  const text = d.text || (d.lines || []).join(' ');
  if (MODE === 'replay') {
    // In replay mode SSE cues are appended to the replay buffer
    allCues.push({ start: parseFloat(d.start), end: parseFloat(d.end), text });
  } else {
    if (videoPaused) return;
    onCue(text);
  }
});

es.onopen = () => {
  status.textContent = 'Connected — waiting for speech…';
  status.className = 'waiting';
};

es.onerror = () => {
  status.textContent = 'Stream reconnecting…';
  status.className = 'error';
};
