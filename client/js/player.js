// SESSION_ID and MODE are declared by the inline script in player.html before this file loads.
// CaptionPacer and wrapLines come from captions.js (loaded first).

// ── Caption rendering ─────────────────────────────────────────────────────────
const bar    = document.getElementById('caption-bar');
const status = document.getElementById('status');

function renderLines(lines) {
  bar.innerHTML = '';
  (lines || []).forEach(line => {
    if (!line || !line.trim()) return;
    const s = document.createElement('span');
    s.className = 'caption-line';
    s.textContent = line;
    bar.appendChild(s);
  });
}

// Reading-paced display for the live SSE stream.
const pacer = new CaptionPacer(renderLines);

// CEA-608/708 speaker prefix: a known label wins ('>> SPEAKER 1: '), else a bare
// turn mark ('>> '), else nothing. `speaker` (stable, from the behind-live pass)
// upgrades the live `speakerChange` mark once it arrives.
function speakerPrefix(speaker, speakerChange) {
  if (speaker) return '>> ' + speaker + ': ';
  if (speakerChange) return '>> ';
  return '';
}

// ── Stop button ───────────────────────────────────────────────────────────────
async function stopSession() {
  await fetch(API + '/api/stop/' + SESSION_ID, { method: 'POST' });
  window.location.href = '/dashboard';
}

// ── Replay mode ───────────────────────────────────────────────────────────────
// Historical fragment cues, replayed in sync with the video's current time.
const allCues = [];
let replayTimer = null;
let _lastReplayText = null;

async function initReplay() {
  try {
    const r = await fetch(API + '/api/session/' + SESSION_ID + '/cues');
    const cues = await r.json();
    allCues.push(...cues);
  } catch(e) {}
  replayTimer = setInterval(replayTick, 250);
}

function replayTick() {
  // `ytPlayer` is a top-level `let`, so it is NOT a property of `window`
  // (only `var`/globals are) — guard on the binding itself.
  if (!ytPlayer || typeof ytPlayer.getCurrentTime !== 'function') return;
  const t = ytPlayer.getCurrentTime();
  // Stitch the trailing fragments up to current playback time into ~2 lines,
  // prefixing each cue with its (now-resolved) speaker label / turn mark.
  const cues = [];
  for (let i = allCues.length - 1; i >= 0; i--) {
    if (allCues[i].start > t) continue;
    const c = allCues[i];
    cues.push(speakerPrefix(c.speaker, c.speaker_change) + c.text);
    if (cues.join(' ').length > 80) break;
  }
  cues.reverse();
  const text = cues.join(' ');
  if (text === _lastReplayText) return;
  _lastReplayText = text;
  renderLines(wrapLines(text));
}

async function jumpToLive() {
  if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
  window.location.href = '/player?id=' + SESSION_ID;
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
  // Only a real user pause freezes captions — transient BUFFERING must not,
  // or captions would stick on every network stall.
  if (event.data === YT.PlayerState.PAUSED) {
    pacer.pause();
    status.textContent = 'Paused';
    status.className   = 'waiting';
  } else if (event.data === YT.PlayerState.PLAYING) {
    pacer.resume();
    status.textContent = 'LIVE';
    status.className   = 'live';
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
    // In replay mode SSE cues are appended to the replay buffer; prefix is
    // applied at render time (and may be upgraded by a later 'speaker' event).
    allCues.push({
      start: parseFloat(d.start), end: parseFloat(d.end), text,
      speaker: d.speaker || null, speaker_change: !!d.speaker_change,
    });
  } else {
    // Live: only the best-effort '>>' turn mark is available; stitch it inline.
    pacer.push(speakerPrefix(null, d.speaker_change) + text,
               parseFloat(d.start), parseFloat(d.end));
  }
});

// Behind-live diarization resolved stable SPEAKER N labels for earlier cues.
// Relabel the replay buffer; the live roll-up is not retroactively edited.
es.addEventListener('speaker', e => {
  if (MODE !== 'replay') return;
  let d;
  try { d = JSON.parse(e.data); } catch (_) { return; }
  for (const u of (d.updates || [])) {
    const start = parseFloat(u.start);
    for (let i = allCues.length - 1; i >= 0; i--) {
      if (Math.abs(allCues[i].start - start) <= 0.05) {
        allCues[i].speaker = u.speaker;
        if (u.speaker_change) allCues[i].speaker_change = true;
        break;
      }
    }
  }
  _lastReplayText = null;   // force a re-render with the new label
});

es.onopen = () => {
  status.textContent = 'Connected — waiting for speech…';
  status.className = 'waiting';
};

es.onerror = () => {
  status.textContent = 'Stream reconnecting…';
  status.className = 'error';
};
