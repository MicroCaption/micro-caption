# MicroCaption — Development Log, Session 4
**Date:** 2026-06-15  
**Branch:** `feat/landing-page`  
**Engineer:** Peter Dews (info@peterdews.com)  
**System:** MicroCap-Proto, Ubuntu 26.04 "Resolute"  
**AI pair programmer:** Claude Sonnet 4.6 (Claude Code)

---

## Session Overview

This session built on top of the multi-stream control room (Session 3) and a set of intermediate commits that had added a public landing page, Google OAuth2 authentication, pricing/standards stub pages, and a README. The session focused on five areas:

1. **Watch viewer** — public caption viewer at `/watch/<code>` with bigger, centered captions and dwell timing
2. **Dashboard hardening** — QR code rendering, no-flicker in-place card updates, live caption sync to player position
3. **Replay mode** — historical caption playback synchronized to YouTube video time
4. **Mobile responsiveness** — all public pages usable on phones without zooming
5. **Dashboard card duplicate bug** — diagnosed and fixed the root cause (two rendering paths)

---

## Context: Commits Since Session 3

Between Session 3 and this session, an intermediate context added:

| Commit | Summary |
|--------|---------|
| `e8721d8` | Add landing page, Google OAuth2 auth, and queue stub |
| `9e371b4` | Expand landing page and add Pricing and Standards stub pages |
| `7365213` | docs: add README, update .gitignore, untrack `__pycache__` |
| `95f0d4d` | Make nav logo a home link on all pages; add auth setup guide |

Session 4 changes below are all uncommitted (working tree) as of this log.

---

## Phase 1 — Watch Viewer: Bigger Captions, Centered Layout

### Problem
The `/watch/<code>` public viewer was displaying captions at the bottom ~10% of the screen in small text. Not readable on any screen size.

### Changes (`microcaption/output/webvtt_server.py` — `_VIEWER_CSS`)

```css
/* Before */
#viewer-captions { ... justify-content: flex-end; }
.caption-line { font-size: clamp(1.5rem, 5vw, 2.6rem); }

/* After */
#viewer-captions {
  flex: 1; display: flex; flex-direction: column;
  justify-content: center; align-items: center;
  padding: 16px 20px 28px; gap: 10px;
}
.caption-line {
  font-size: clamp(2.4rem, 7vw, 4.2rem); line-height: 1.3;
  color: #fff; text-align: center;
  text-shadow: 2px 2px 6px #000, -1px -1px 4px #000;
}
```

Captions are now vertically and horizontally centered, with a `clamp()` font size that scales between 2.4rem (mobile) and 4.2rem (large screens). Text shadow improves readability on any background color.

### SSE Catch-up on Connect

**Problem:** A viewer opening the `/watch/<code>` page mid-session would see a blank screen until the next caption arrived (could be seconds).

**Fix:** In `_sse_stream()`, immediately after sending headers, fetch the most recent cue from `sess.writer.last_cue_data()` and emit it as a `cue` event before adding the client to the fan-out list:

```python
self.end_headers()
if sess.writer:
    last = sess.writer.last_cue_data()
    if last:
        catchup = json.dumps({
            'text':  last['text'],
            'lines': [l for l in last['text'].split('\n') if l.strip()],
            'start': f"{last['start']:.3f}",
            'end':   f"{last['end']:.3f}",
        })
        self.wfile.write(f'event: cue\ndata: {catchup}\n\n'.encode())
        self.wfile.flush()
with sess.sse_lock:
    sess.sse_clients.append(self)
```

**Required support change in `microcaption/caption/webvtt.py`:**
Added `_cue_data: List[dict]` (parallel to `_cues: List[str]`) and supporting methods:
- `add_cue()` now also appends `{'start': start, 'end': end, 'text': text}` to `_cue_data`
- `reset()` now also clears `_cue_data`
- New: `current_time` property, `last_cue_data()`, `all_cue_data()`

---

## Phase 2 — Dashboard: QR Code and No-Flicker Updates

### QR Code Not Showing

**Root cause:** The JS refresh loop was replacing the entire `innerHTML` of `sessions-grid-wrap` every 2 seconds:
```javascript
wrap.innerHTML = '<div class="sessions-grid">' + data.map(cardHtml).join('') + '</div>';
```
Every refresh destroyed and recreated all DOM elements including `<img class="watch-qr">`, triggering a fresh network fetch. The QR image would start loading, then be destroyed before it finished — the QR never displayed.

**Fix:** Rewrote the refresh loop to maintain a `_cards = {}` (session id → DOM element) registry. Cards are injected once on first appearance (`_injectCard`) and subsequently updated in-place (`_updateCard`). The `<img>` element is never destroyed.

```javascript
const _cards = {};

function _injectCard(grid, s) {
  const tmp = document.createElement('div');
  tmp.innerHTML = cardHtml(s);
  const card = tmp.firstElementChild;
  const qrImg = card.querySelector('.watch-qr');
  if (qrImg && s.code) qrImg.src = _qrSrc(s.code);
  grid.appendChild(card);
  _cards[s.id] = card;
}

function _updateCard(card, s) {
  // Updates: dot class, status label, uptime/cue stats, caption cue text
  // Does NOT touch: QR image, session code, URL, badge — stable across refreshes
}
```

---

## Phase 3 — Player Caption Sync (Live Mode)

### Problem
Opening `/player/<id>` mid-session: the YouTube iframe starts at `t=0` while the ASR pipeline is already at `t=300`. Captions were seconds or minutes behind.

### New API Endpoint

```
GET /api/session/<id>/position
→ { "current_time": 312.4, "cue_count": 156 }
```

### Player `onReady` Seek

When the YouTube IFrame API fires `onReady`, the player JS fetches the current ASR position and seeks:

```javascript
async function _mcOnPlayerReady(event) {
  try {
    const r = await fetch('/api/session/SESSION_ID/position');
    const d = await r.json();
    if (d.current_time > 2) {
      ytPlayer.seekTo(d.current_time, true);
    }
  } catch(e) {}
}
```

---

## Phase 4 — Replay Mode

### Feature
`/player/<id>?mode=replay` — starts the YouTube video at `t=0` and replays historical captions in sync with the video's current playback position.

### Implementation

**New API endpoint:**
```
GET /api/session/<id>/cues
→ [{"start": 0.0, "end": 2.1, "text": "..."}, ...]
```

**Player mode detection:**
```python
qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
mode = qs.get('mode', ['live'])[0]
html = _make_player_html(..., mode=mode)
```

**JS replay loop (250ms polling):**
```javascript
let allCues = [];    // loaded once from /api/session/<id>/cues
let lastIdx = -1;

function replayTick() {
  const t = ytPlayer.getCurrentTime();
  let idx = lastIdx;
  while (idx + 1 < allCues.length && allCues[idx + 1].start <= t) idx++;
  if (idx !== lastIdx) {
    lastIdx = idx;
    if (idx >= 0) onCue(allCues[idx].text);
  }
}
setInterval(replayTick, 250);
```

**Jump to live button:** In replay mode, a "⏭ Jump to live" button appears in the player header. Clicking it navigates to `/player/<id>` (live mode), which seeks to the current ASR position on load.

---

## Phase 5 — Mobile-Responsive Landing Page

### Root Cause
All public pages (`/`, `/pricing`, `/standards`) were missing `<meta name="viewport">`. Without it, mobile browsers render at ~980px virtual width and scale down — CSS `@media` queries never fire, and everything appears tiny.

### Viewport Fix
Added to all public page generators:
```html
<meta name="viewport" content="width=device-width, initial-scale=1">
```

### CSS Additions (`_LANDING_CSS`)

```css
.pub-nav { flex-wrap: wrap; }
.hero h1 { font-size: clamp(1.6rem, 5.5vw, 3rem); }

@media (max-width: 600px) {
  .pub-nav { padding: 12px 16px 8px; gap: 0 4px; }
  .pub-nav .nav-logo { flex-basis: 100%; margin-bottom: 8px; }
  .nav-link { font-size: 0.75em; padding: 4px 8px; }
  .hero { padding: 52px 20px 44px; }
  .hero-desc { font-size: 0.82em; margin-bottom: 28px; }
  .landing-section { padding: 0 16px 44px; }
  .section-label { margin-bottom: 16px; }
  .feature-card { padding: 16px 14px; }
  .audience-card { padding: 16px 14px; }
  .pricing-card { padding: 20px 16px; }
}
@media (max-width: 480px) {
  .hero-actions { flex-direction: column; align-items: stretch; gap: 10px; }
  .btn-cta, .btn-ghost { text-align: center; padding: 14px 24px; }
  .landing-footer { flex-direction: column; align-items: center; text-align: center; padding: 20px 16px; }
}
```

Nav wraps cleanly on small screens (logo on its own row, links wrap below). Hero text uses `clamp()` to stay readable without being huge. CTA buttons stack vertically at 480px. Section padding reduces to avoid wasted vertical space.

---

## Phase 6 — Dashboard Card Duplicate Bug (Two Rounds)

### First (incorrect) Fix
Symptom: two cards per stream — one styled/full (JS-injected), one simpler (server-rendered Python).

The initial fix removed server-side card rendering from `_page_control_room()`, leaving `sessions-grid-wrap` empty for JS to populate. This created the opposite problem: the server-rendered card (which the user considered the "good" one) was gone, and only the JS-injected card remained — which was visually plainer at that stage.

### Root Cause Analysis

**Before the in-place update (QR fix):** JS `refresh()` called `wrap.innerHTML = data.map(cardHtml).join('')` — this completely replaced server-rendered cards every 2 seconds. No duplicates.

**After the in-place update:** The new `refresh()` looked for an existing `.sessions-grid` div inside `sessions-grid-wrap`. When it found the server-rendered grid, it preserved it and called `_injectCard()` for each session — adding a second card alongside the server-rendered one. Two cards.

### Correct Fix

Three-part fix applied in this session:

**1. Restore server-side rendering in `_page_control_room()`:**
```python
if sessions:
    cards = ''.join(self._session_card_html(s) for s in sessions)
    grid_html = f'<div class="sessions-grid">{cards}</div>'
else:
    grid_html = '<div class="sessions-grid">...'
f'<div id="sessions-grid-wrap">{grid_html}</div>'
```

**2. Add `id="sess-{id}"` to server-rendered cards:**
```python
f'<div class="session-card {sess.status}" id="sess-{_esc(sess.id)}">'
```

**3. Build QR URL server-side (no empty `src=""`):**
```python
host = self.headers.get('Host', 'localhost:8765')
watch_url = urllib.parse.quote(f'http://{host}/watch/{code}', safe='')
qr_src = f'https://api.qrserver.com/v1/create-qr-code/?data={watch_url}&size=120x120&...'
```
Image loads immediately on page render — no JS needed to fill the `src`.

**4. JS adoption instead of injection (prevents duplicates):**
```javascript
// Before the inject/update loop, adopt existing server-rendered cards:
for (const s of data) {
  if (!_cards[s.id]) {
    const el = document.getElementById('sess-' + s.id);
    if (el) {
      _cards[s.id] = el;
      const qi = el.querySelector('.watch-qr');
      if (qi && !qi.src && s.code) qi.src = _qrSrc(s.code);
    }
  }
}
// Now the normal inject/update loop runs — adopted cards hit _updateCard(), not _injectCard()
```

**Result:** Page load shows server-rendered cards immediately (no JS wait). After 2s the JS adopts them, and subsequent refreshes update in-place. QR images are stable throughout. No duplicates under any timing.

---

## Files Modified This Session

| File | Changes |
|------|---------|
| `microcaption/caption/webvtt.py` | Added `_cue_data`, `last_cue_data()`, `all_cue_data()`, `current_time`, updated `reset()` |
| `microcaption/output/webvtt_server.py` | Major changes — see phases above; ~500 LOC net added |

---

## Architecture Notes

### Session Code → Watch URL Pattern
Each session gets a 6-digit `code` (via `Session.code`). This code is the public-facing identifier for the `/watch/<code>` viewer page — keeps the URL short and shareable (vs the internal 8-char hex session ID used in auth-gated routes like `/player/<id>`).

### Server-side vs JS card rendering — Design Decision
The dashboard uses a hybrid approach:
- **Server renders** the initial card set on page load (fast, visible immediately, no JS wait)
- **JS adopts** server-rendered cards on first poll, then updates in-place (preserves QR image DOM stability)
- **JS injects** for sessions that start after the page load
- **JS updates** (status, uptime, cue count, caption text) every 2 seconds without replacing the card element

This beats the pure-JS approach (slower first-paint, QR flicker on reconnect) and the pure-server approach (stale data, page would need to reload to update).

---

## Known Limitations / Next Steps

| Item | Notes |
|------|-------|
| `_updateCard()` doesn't update outer card class | If a session transitions starting → live after page load, `session-card.live` border-color class won't update. Minor visual issue. |
| Replay mode requires full cue history in memory | For very long streams (1000+ cues), `/api/session/<id>/cues` response grows unbounded. Add pagination or a binary search index. |
| No reconnect on SSE drop | If the SSE connection drops, the watch viewer goes blank. Add `EventSource` reconnect logic with the catch-up mechanism. |
| Auth not enabled by default | `auth.enabled: false` in `config/settings.yaml` — fine for local use, must be set `true` before exposing publicly with credentials configured. |
| Uncommitted changes | All session 4 work is in the working tree on `feat/landing-page`. Needs review and commit. |

---

*Log written end of session 2026-06-15.*
