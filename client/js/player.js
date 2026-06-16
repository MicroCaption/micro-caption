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

// SCHEDULED-mode pacer: blocks are laid out at a comfortable reading width and
// shown when the (delayed) video playhead reaches their stream-time onset, so
// captions stay aligned to the picture. Driven by tick() from the sync loop.
const pacer = new CaptionPacer(renderLines, { scheduled: true });

// ── Stop button ───────────────────────────────────────────────────────────────
async function stopSession() {
  await fetch(API + '/api/stop/' + SESSION_ID, { method: 'POST' });
  window.location.href = '/dashboard';
}

// ── Broadcast-style sync state (live mode) ─────────────────────────────────────
// streamTime(videoPos) = videoPos - K + nudge, where K is fixed at reveal so the
// video's live edge maps to the server's newest caption time, then the video is
// held `targetDelay` seconds behind it (see /api/session/<id>/sync).
let syncCfg   = { target_delay: 20, min_cache: 8, offset_nudge: 0 };
let K         = null;     // video↔stream clock offset, set at reveal
let nudge     = 0;        // live, tunable alignment fine-tune (seconds)
let revealed  = false;
let syncTimer = null;

async function fetchSync() {
  const r = await fetch(API + '/api/session/' + SESSION_ID + '/sync');
  return r.json();
}

// Map the current video position to stream-time (the caption timeline).
function videoStreamTime() {
  if (K === null || !ytPlayer || typeof ytPlayer.getCurrentTime !== 'function') return null;
  return ytPlayer.getCurrentTime() - K + nudge;
}

// Hold the video behind live until a caption cache covers the delayed start,
// then seek back `targetDelay` and start driving captions from the video clock.
async function beginSyncedPlayback() {
  try { syncCfg = await fetchSync(); } catch (e) {}
  nudge = syncCfg.offset_nudge || 0;
  status.textContent = 'Aligning captions…';
  status.className = 'waiting';
  if (ytPlayer && typeof ytPlayer.pauseVideo === 'function') ytPlayer.pauseVideo();

  // Cache-then-reveal: wait until the session has produced at least targetDelay
  // seconds of captions, so seeking the video targetDelay behind live lands on
  // a region we already have cues for.
  syncTimer = setInterval(async () => {
    let s;
    try { s = await fetchSync(); } catch (e) { return; }
    const ready = s.stream_clock_now >= Math.min(s.target_delay, s.target_delay);
    status.textContent = 'Aligning captions… ' +
      Math.min(99, Math.floor(100 * s.stream_clock_now / Math.max(1, s.target_delay))) + '%';
    if (ready && !revealed) reveal(s);
  }, 1000);
}

function reveal(s) {
  revealed = true;
  if (syncTimer) { clearInterval(syncTimer); syncTimer = null; }
  syncCfg = s;
  nudge = s.offset_nudge || 0;
  const delay = s.target_delay || 20;

  // Anchor: video live edge ↔ LIVE AUDIO time on the server, then drop back
  // `delay`. The live audio time is how long the adapter has been ingesting
  // (server_now - join_epoch) — NOT stream_clock_now, which is the newest
  // *committed caption* and lags the audio by the pipeline latency
  // (review_delay + LocalAgreement + pacing). Anchoring to the caption clock
  // would shift every caption late by that latency. K = liveEdge - liveAudio,
  // so streamTime = videoPos - K maps the picture to the audio/caption clock.
  const liveEdge = ytPlayer.getCurrentTime();
  const liveAudio = (s.join_epoch && s.server_now)
    ? (s.server_now - s.join_epoch)   // newest ingested audio position (true "live")
    : s.stream_clock_now;             // fallback if the adapter epoch isn't available
  K = liveEdge - liveAudio;
  try { ytPlayer.seekTo(Math.max(0, liveEdge - delay), true); } catch (e) {}
  if (typeof ytPlayer.playVideo === 'function') ytPlayer.playVideo();

  status.textContent = 'LIVE · −' + Math.round(delay) + 's';
  status.className = 'live';

  // Drive captions from the (delayed) video clock.
  setInterval(() => {
    if (pacer._paused) return;
    const st = videoStreamTime();
    if (st !== null) pacer.tick(st);
  }, 200);
}

// Runtime alignment fine-tune for the operator: [ = captions earlier, ] = later.
function mcNudge(delta) {
  nudge += delta;
  if (revealed) {
    status.textContent = 'LIVE · −' + Math.round(syncCfg.target_delay || 20) +
                         's · nudge ' + (nudge >= 0 ? '+' : '') + nudge.toFixed(1) + 's';
  }
}
document.addEventListener('keydown', e => {
  if (e.key === '[') mcNudge(-0.5);
  else if (e.key === ']') mcNudge(+0.5);
});

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
  // Stitch the trailing fragments up to the current playback time into ~2 lines.
  const parts = [];
  for (let i = allCues.length - 1; i >= 0; i--) {
    if (allCues[i].start > t) continue;
    parts.push(allCues[i].text);
    if (parts.join(' ').length > 80) break;
  }
  parts.reverse();
  const text = parts.join(' ');
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
    beginSyncedPlayback();
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
    if (revealed) {
      status.textContent = 'LIVE · −' + Math.round(syncCfg.target_delay || 20) + 's';
      status.className = 'live';
    }
  }
}

// ── SSE caption stream ────────────────────────────────────────────────────────
const es = new EventSource(API + '/events/' + SESSION_ID);

es.addEventListener('cue', e => {
  const d = JSON.parse(e.data);
  const text = d.text || (d.lines || []).join(' ');
  if (MODE === 'replay') {
    // In replay mode SSE cues are appended to the replay buffer.
    allCues.push({ start: parseFloat(d.start), end: parseFloat(d.end), text });
  } else {
    // Feed the scheduled pacer with stream timestamps; it's displayed by the
    // video clock once playback is revealed.
    pacer.push(text, parseFloat(d.start), parseFloat(d.end));
  }
});

es.onopen = () => {
  if (!revealed && MODE !== 'replay') {
    status.textContent = 'Connected — aligning…';
    status.className = 'waiting';
  }
};

es.onerror = () => {
  status.textContent = 'Stream reconnecting…';
  status.className = 'error';
};
