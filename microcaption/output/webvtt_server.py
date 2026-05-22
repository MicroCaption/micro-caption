"""
HTTP server — endpoints:

  GET  /          — URL submission form (landing page)
  POST /start     — accept YouTube URL, kick off new session, redirect to /player
  GET  /player    — YouTube video + live caption overlay
  GET  /events    — Server-Sent Events stream (pushed per caption)
  GET  /webvtt    — full accumulated WebVTT document
  GET  /status    — JSON: {active, video_id, cue_count}
"""

import json
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Callable, Deque, Dict, List, Optional
from ..caption.webvtt import WebVTTWriter


def _video_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname in ('youtu.be',):
        return parsed.path.lstrip('/').split('/')[0]
    if parsed.hostname and 'youtube' in parsed.hostname:
        # standard ?v=ID query param
        vid = urllib.parse.parse_qs(parsed.query).get('v', [''])[0]
        if vid:
            return vid
        # path-based formats: /shorts/ID, /live/ID, /embed/ID
        parts = [p for p in parsed.path.split('/') if p]
        if len(parts) >= 2 and parts[0] in ('shorts', 'live', 'embed'):
            return parts[1]
    return ''


# ── Shared UI helpers ─────────────────────────────────────────────────────────

_SHARED_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d0d0d; color: #ddd; font-family: monospace; min-height: 100vh; }
nav {
  display: flex; align-items: center; gap: 2px;
  padding: 11px 20px; border-bottom: 1px solid #181818;
}
.nav-logo {
  color: #3a3a3a; letter-spacing: 0.14em; text-transform: uppercase;
  font-size: 0.8em; margin-right: auto;
}
.nav-link {
  color: #555; text-decoration: none; padding: 5px 11px;
  border-radius: 4px; font-size: 0.8em; transition: color .15s;
}
.nav-link:hover { color: #aaa; }
.nav-link.active { color: #ddd; background: #181818; }
.nav-live {
  color: #3a9a4a; text-decoration: none; padding: 5px 11px;
  font-size: 0.8em; margin-left: 6px;
  animation: blink 1.4s ease-in-out infinite;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.45} }
main { max-width: 1080px; margin: 0 auto; padding: 26px 20px; }
h2 {
  color: #444; font-size: 0.68em; letter-spacing: 0.12em;
  text-transform: uppercase; margin-bottom: 10px;
}
.card {
  background: #0f0f0f; border: 1px solid #1c1c1c;
  border-radius: 5px; padding: 18px 22px; margin-bottom: 14px;
}
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
.stat-label { color: #383838; font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.08em; }
.stat-val { color: #ccc; font-size: 1.05em; margin-top: 5px; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
.dot-live { background: #3a9a4a; animation: blink 1.4s infinite; }
.dot-idle { background: #2a2a2a; }
.live { color: #3a9a4a; }
.idle { color: #444; }
.stub { color: #2a2a2a; font-style: italic; font-size: 0.85em; }
table { width: 100%; border-collapse: collapse; font-size: 0.83em; }
td, th { padding: 7px 10px; border-bottom: 1px solid #161616; }
th { color: #383838; font-weight: normal; text-transform: uppercase; font-size: 0.7em; letter-spacing: 0.07em; }
td:first-child { color: #666; }
pre {
  background: #080808; border: 1px solid #1c1c1c; border-radius: 4px;
  padding: 16px; overflow-x: auto; font-size: 0.8em; color: #7a9a7a; line-height: 1.65;
}
form.row { display: flex; gap: 10px; }
input[type=url] {
  flex: 1; padding: 10px 14px; background: #141414; border: 1px solid #242424;
  border-radius: 5px; color: #eee; font-family: monospace; font-size: 0.9em; outline: none;
}
input[type=url]:focus { border-color: #3a6a3a; }
input[type=url]::placeholder { color: #2c2c2c; }
.btn-primary {
  padding: 10px 22px; background: #2a7a3a; border: none; border-radius: 5px;
  color: #fff; font-family: monospace; font-size: 0.9em; cursor: pointer; white-space: nowrap;
}
.btn-primary:hover { background: #3a9a4a; }
.btn-danger {
  background: none; border: 1px solid #4a1a1a; color: #744;
  font-family: monospace; font-size: 0.82em; padding: 6px 14px;
  border-radius: 4px; cursor: pointer;
}
.btn-danger:hover { border-color: #944; color: #b66; }
.ts { color: #2a2a2a; font-size: 0.72em; }
"""


def _nav(active: str, session_active: bool) -> str:
    tabs = [('Dashboard', '/dashboard'), ('Monitor', '/monitor'),
            ('Config', '/config'), ('Logs', '/logs')]
    links = ''.join(
        f'<a href="{h}" class="nav-link{"  active" if lbl.lower() == active else ""}">{lbl}</a>'
        for lbl, h in tabs
    )
    live = ('<a href="/player" class="nav-live">&#9654;&nbsp;Live</a>'
            if session_active else '')
    return (f'<nav><span class="nav-logo">MicroCaption</span>'
            f'{links}{live}</nav>')


def _wrap(title: str, active: str, body: str,
          session_active: bool = False, script: str = '') -> str:
    st = f'<script>{script}</script>' if script else ''
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<title>MicroCaption — {title}</title>'
            f'<style>{_SHARED_CSS}</style></head><body>'
            f'{_nav(active, session_active)}'
            f'<main>{body}</main>{st}</body></html>')


# ── Landing page ──────────────────────────────────────────────────────────────

_LANDING_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>MicroCaption — Live ASR Demo</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d0d0d; color: #ddd; font-family: monospace;
      min-height: 100vh; display: flex; flex-direction: column;
      align-items: center; justify-content: center; padding: 2em; gap: 0;
    }}
    h1 {{ font-size: 2em; letter-spacing: 0.18em; text-transform: uppercase; color: #fff; }}
    .tagline {{ color: #444; font-size: 0.8em; letter-spacing: 0.06em; margin: 0.6em 0 2.4em; }}
    form {{ display: flex; gap: 10px; width: 100%; max-width: 720px; }}
    input[type=url] {{
      flex: 1; padding: 13px 16px; background: #181818; border: 1px solid #2a2a2a;
      border-radius: 5px; color: #eee; font-family: monospace; font-size: 0.95em; outline: none;
      transition: border-color .2s;
    }}
    input[type=url]:focus {{ border-color: #3a3; }}
    input[type=url]::placeholder {{ color: #383838; }}
    button {{
      padding: 13px 28px; background: #2a7a3a; border: none; border-radius: 5px;
      color: #fff; font-family: monospace; font-size: 1em; font-weight: bold;
      cursor: pointer; white-space: nowrap; transition: background .2s;
    }}
    button:hover {{ background: #3a9a4a; }}
    .hint {{ margin-top: 1.2em; color: #383838; font-size: 0.72em; }}
    .now-playing {{
      margin-top: 2.4em; padding: 14px 22px; background: #131313;
      border: 1px solid #2a3a2a; border-radius: 5px; font-size: 0.82em; color: #666;
      display: flex; align-items: center; gap: 14px; width: 100%; max-width: 720px;
    }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #3a9a4a;
             animation: pulse 1.4s ease-in-out infinite; flex-shrink: 0; }}
    @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.3 }} }}
    .now-playing a {{ color: #4c4; text-decoration: none; font-weight: bold; }}
    .now-playing a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>MicroCaption</h1>
  <p class="tagline">Real-time ASR captions &bull; CEA-608 / CEA-708 &bull; Whisper large-v3-turbo on GPU</p>
  <form method="POST" action="/start">
    <input type="url" name="url"
      placeholder="YouTube, Twitch, peg.tv, or any yt-dlp supported URL&hellip;"
      required autofocus>
    <button type="submit">&#9654;&nbsp; Caption</button>
  </form>
  <p class="hint">Audio is extracted server-side via yt-dlp &mdash; no third-party transcription APIs.</p>
  {NOW_PLAYING}
</body>
</html>
"""

_NOW_PLAYING_BLOCK = """
  <div class="now-playing">
    <div class="dot"></div>
    <span>Session active &mdash; <a href="/player">watch with live captions &rarr;</a></span>
    <form method="POST" action="/stop" style="margin:0;margin-left:auto">
      <button type="submit" style="background:none;border:1px solid #5a2020;color:#844;font-family:monospace;font-size:0.9em;padding:4px 12px;border-radius:4px;cursor:pointer;">&#9632; Stop</button>
    </form>
  </div>
"""

# ── Player page ───────────────────────────────────────────────────────────────

def _make_player_html(video_id: str, source_url: str = '',
                      source_type: str = 'youtube') -> str:
    is_yt = (source_type == 'youtube')

    # ── Parts that vary between YouTube and generic embeds ────────────────────
    # These are plain Python strings substituted into the f-string below.
    # Their { } are literal JS braces — they are NOT re-processed by the
    # f-string escaping rules, so no {{ }} doubling is needed inside them.

    yt_api_tag = (
        '  <script src="https://www.youtube.com/iframe_api"></script>\n'
        if is_yt else ''
    )

    if is_yt:
        player_iframe = (
            f'    <iframe id="yt-iframe"\n'
            f'      src="https://www.youtube.com/embed/{video_id}'
            f'?autoplay=1&mute=0&enablejsapi=1"\n'
            f'      allow="autoplay; encrypted-media; picture-in-picture"\n'
            f'      allowfullscreen>\n'
            f'    </iframe>'
        )
    else:
        safe_src = source_url.replace('"', '%22')
        player_iframe = (
            f'    <iframe id="source-iframe"\n'
            f'      src="{safe_src}"\n'
            f'      allow="autoplay; encrypted-media; picture-in-picture"\n'
            f'      allowfullscreen>\n'
            f'    </iframe>'
        )

    # YouTube IFrame API callbacks (omitted for non-YouTube — videoPaused stays
    # false so the cue guard is a no-op).
    yt_pause_js = (
        '\n'
        '    let ytPlayer;\n'
        '    function onYouTubeIframeAPIReady() {\n'
        "      ytPlayer = new YT.Player('yt-iframe', {\n"
        '        events: { onStateChange: onPlayerStateChange }\n'
        '      });\n'
        '    }\n'
        '    function onPlayerStateChange(event) {\n'
        '      if (event.data === YT.PlayerState.PAUSED ||\n'
        '          event.data === YT.PlayerState.BUFFERING) {\n'
        '        videoPaused = true;\n'
        "        if (clearTimer)  { clearTimeout(clearTimer);  clearTimer  = null; }\n"
        "        if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }\n"
        "        status.textContent = 'Paused';\n"
        "        status.className   = 'waiting';\n"
        '      } else if (event.data === YT.PlayerState.PLAYING) {\n'
        '        videoPaused = false;\n'
        "        status.textContent = 'Connected — waiting for speech…';\n"
        "        status.className   = 'waiting';\n"
        '      }\n'
        '    }'
    ) if is_yt else ''

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>MicroCaption — Live</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d0d0d; color: #ddd; font-family: monospace; min-height: 100vh; padding: 14px; }}
    #topbar {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 12px; font-size: 0.78em;
    }}
    #topbar h1 {{ color: #555; letter-spacing: 0.12em; text-transform: uppercase; font-size: 1em; }}
    #topbar a {{ color: #555; text-decoration: none; border: 1px solid #2a2a2a; padding: 5px 12px; border-radius: 4px; }}
    #topbar a:hover {{ color: #aaa; border-color: #444; }}
    #stop-btn {{ background: none; border: 1px solid #5a2020; color: #844; font-family: monospace; font-size: 1em; padding: 5px 12px; border-radius: 4px; cursor: pointer; }}
    #stop-btn:hover {{ border-color: #a44; color: #c66; }}
    #player-wrap {{
      position: relative; width: 100%; max-width: 1280px; margin: 0 auto;
      background: #000; border-radius: 6px; overflow: hidden;
      box-shadow: 0 4px 32px rgba(0,0,0,.7);
    }}
    #player-wrap iframe {{ display: block; width: 100%; aspect-ratio: 16/9; border: none; }}
    #caption-bar {{
      position: absolute; bottom: 0; left: 0; right: 0;
      padding: 14px 18px 20px;
      background: linear-gradient(transparent, rgba(0,0,0,.85));
      text-align: center; pointer-events: none; min-height: 6em;
      display: flex; flex-direction: column; justify-content: flex-end;
      align-items: center; gap: 6px;
    }}
    .caption-line {{
      display: inline-block; font-size: 1.45em; line-height: 1.35; color: #fff;
      text-shadow: 1px 1px 3px #000, -1px -1px 3px #000;
      background: rgba(0,0,0,.38); padding: 2px 10px; border-radius: 3px;
      transition: opacity 0.15s ease;
    }}
    .caption-line.prev {{ opacity: 0.62; }}
    #footer {{
      max-width: 1280px; margin: 10px auto 0; display: flex;
      justify-content: space-between; align-items: center;
      font-size: 0.72em; color: #333;
    }}
    #status {{ display: inline-block; padding: 2px 8px; border-radius: 3px; }}
    #status.live {{ color: #4c4; }}
    #status.waiting {{ color: #555; }}
    #status.error {{ color: #c44; }}
    #footer a {{ color: #333; text-decoration: none; }}
    #footer a:hover {{ color: #666; }}
  </style>
{yt_api_tag}</head>
<body>
  <div id="topbar">
    <h1>MicroCaption &mdash; Live ASR</h1>
    <div style="display:flex;gap:8px">
      <a href="/">&#8592; New video</a>
      <form method="POST" action="/stop" style="margin:0">
        <button id="stop-btn" type="submit">&#9632; Stop</button>
      </form>
    </div>
  </div>
  <div id="player-wrap">
{player_iframe}
    <div id="caption-bar"></div>
  </div>
  <div id="footer">
    <span id="status" class="waiting">Connecting&hellip;</span>
    <span><a href="/webvtt">Download WebVTT</a></span>
  </div>

  <script>
    const bar = document.getElementById('caption-bar');
    const status = document.getElementById('status');

    // Rate-limited two-line display.
    //
    // The ASR backend fires every ~0.5 s (sliding-window step). Rendering every
    // update looks like flickering. Broadcast standards (BBC, Netflix) require a
    // minimum of ~2 s per screen so viewers can finish reading before the text
    // changes. We enforce that here:
    //
    //   - First cue of a new utterance shows immediately.
    //   - Subsequent cues of the same utterance are buffered; the screen only
    //     updates once MIN_STABLE_MS has elapsed since the last render.
    //   - After DWELL_MS of silence the screen clears.
    //   - splitLines word-wraps to MAX_CHARS and we show the last 2 lines
    //     (most recently spoken words when text is longer than two lines).

    let pendingText   = '';   // latest text from ASR (may not be shown yet)
    let displayedText = '';   // text currently on screen
    let lastRenderTime = 0;   // Date.now() of the last screen update
    let renderTimer   = null; // handle for the deferred-update setTimeout
    let clearTimer    = null; // handle for the silence-dwell setTimeout
    let videoPaused   = false;
{yt_pause_js}

    const DWELL_MS      = 5000;  // clear after 5 s of silence
    const MIN_STABLE_MS = 2000;  // minimum hold per screen (BBC/Netflix standard)
    const MAX_CHARS     = 32;    // CEA-608 / CEA-708 line width

    function splitLines(text) {{
      const words = text.trim().split(/\\s+/);
      const lines = [];
      let line = '';
      for (const word of words) {{
        const candidate = line ? line + ' ' + word : word;
        if (candidate.length <= MAX_CHARS) {{
          line = candidate;
        }} else {{
          if (line) lines.push(line);
          line = word;
        }}
      }}
      if (line) lines.push(line);
      return lines;
    }}

    function isContinuation(prev, next) {{
      if (!prev) return false;
      const anchor = prev.trim().split(/\\s+/).slice(-3).join(' ').toLowerCase();
      return anchor.length > 2 && next.toLowerCase().includes(anchor);
    }}

    function doRender(text) {{
      bar.innerHTML = '';
      splitLines(text).slice(-2).forEach(line => {{
        if (!line.trim()) return;
        const s = document.createElement('span');
        s.className = 'caption-line';
        s.textContent = line;
        bar.appendChild(s);
      }});
      displayedText  = text;
      lastRenderTime = Date.now();
    }}

    function tryUpdate() {{
      if (!pendingText || pendingText === displayedText) return;
      const wait = MIN_STABLE_MS - (Date.now() - lastRenderTime);
      if (wait <= 0) {{
        doRender(pendingText);
      }} else if (!renderTimer) {{
        renderTimer = setTimeout(() => {{ renderTimer = null; tryUpdate(); }}, wait);
      }}
    }}

    function onCue(newText) {{
      if (!newText.trim()) return;

      if (clearTimer) {{ clearTimeout(clearTimer); clearTimer = null; }}

      const isNew = !isContinuation(pendingText || displayedText, newText);
      pendingText = newText;

      if (isNew) {{
        // New utterance — clear and render immediately
        displayedText  = '';
        lastRenderTime = 0;
        if (renderTimer) {{ clearTimeout(renderTimer); renderTimer = null; }}
      }}

      tryUpdate();

      clearTimer = setTimeout(() => {{
        bar.innerHTML  = '';
        pendingText = displayedText = '';
        lastRenderTime = 0;
        if (renderTimer) {{ clearTimeout(renderTimer); renderTimer = null; }}
        clearTimer = null;
      }}, DWELL_MS);
    }}

    const es = new EventSource('/events');

    es.addEventListener('cue', e => {{
      if (videoPaused) return;   // freeze: drop cues while video is paused
      const d = JSON.parse(e.data);
      status.textContent = 'LIVE';
      status.className = 'live';
      const text = d.text || (d.lines || []).join(' ');
      onCue(text);
    }});

    es.onopen = () => {{
      status.textContent = 'Connected — waiting for speech…';
      status.className = 'waiting';
    }};

    es.onerror = () => {{
      status.textContent = 'Stream reconnecting…';
      status.className = 'error';
    }};
  </script>
</body>
</html>
"""

_NO_SESSION_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>MicroCaption</title>
<meta http-equiv="refresh" content="2;url=/">
<style>body{{background:#0d0d0d;color:#555;font-family:monospace;
display:flex;align-items:center;justify-content:center;height:100vh;}}</style>
</head>
<body>No active session &mdash; redirecting&hellip;</body>
</html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    writer: WebVTTWriter = None
    video_id: str = ''
    session_active: bool = False   # set True when any session starts; cleared on stop
    start_callback: Optional[Callable[[str], None]] = None
    stop_callback: Optional[Callable[[], None]] = None
    _sse_clients: List = []
    _sse_lock: threading.Lock = None
    # source info
    source_url: str = ''
    source_type: str = 'youtube'   # 'youtube' | 'stream'
    # dashboard / monitoring
    metrics_provider: Optional[Callable[[], Dict]] = None
    config_snapshot: Dict = {}
    _recent_cues: Deque = deque(maxlen=50)
    _session_start: Optional[float] = None   # monotonic timestamp

    def log_message(self, fmt, *args):
        pass

    # ── GET ───────────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == '/':
            now_playing = _NOW_PLAYING_BLOCK if self.session_active else ''
            html = _LANDING_HTML.format(NOW_PLAYING=now_playing)
            self._send(html.encode(), 'text/html')

        elif self.path == '/player':
            if not self.session_active:
                self._send(_NO_SESSION_HTML.encode(), 'text/html')
            else:
                html = _make_player_html(
                    self.video_id, self.source_url, self.source_type)
                self._send(html.encode(), 'text/html')

        elif self.path == '/events':
            self._sse_stream()

        elif self.path == '/webvtt':
            self._send(self.writer.flush().encode(), 'text/vtt')

        elif self.path == '/status':
            payload = json.dumps({
                'active': self.session_active,
                'video_id': self.video_id,
                'cue_count': self.writer.cue_count,
            })
            self._send(payload.encode(), 'application/json')

        elif self.path == '/dashboard':
            self._send(self._page_dashboard().encode(), 'text/html')
        elif self.path == '/monitor':
            self._send(self._page_monitor().encode(), 'text/html')
        elif self.path == '/config':
            self._send(self._page_config().encode(), 'text/html')
        elif self.path == '/logs':
            self._send(self._page_logs().encode(), 'text/html')
        elif self.path == '/api/metrics':
            self._send(self._build_metrics_json().encode(), 'application/json')
        elif self.path == '/api/config':
            self._send(json.dumps(self.config_snapshot, indent=2).encode(),
                       'application/json')

        else:
            self.send_error(404)

    # ── POST ──────────────────────────────────────────────────────────────────

    def do_POST(self):
        if self.path == '/start':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode(errors='replace')
            params = urllib.parse.parse_qs(body)
            url = params.get('url', [''])[0].strip()

            if url:
                # Set source info and session_active NOW so /player renders
                # correctly before the background thread finishes yt-dlp.
                _Handler.video_id    = _video_id_from_url(url)
                _Handler.source_url  = url
                _Handler.source_type = 'youtube' if _Handler.video_id else 'stream'
                _Handler.session_active = True
                _Handler._session_start = time.monotonic()

                # Access via class, not self — prevents Python binding the
                # function as an instance method and adding a spurious argument.
                cb = _Handler.start_callback
                if cb:
                    threading.Thread(
                        target=cb,
                        args=(url,),
                        daemon=True,
                        name='session-start',
                    ).start()

            self.send_response(303)
            self.send_header('Location', '/player')
            self.send_header('Content-Length', '0')
            self.end_headers()
        elif self.path == '/stop':
            cb = _Handler.stop_callback
            if cb:
                threading.Thread(target=cb, daemon=True, name='session-stop').start()
            _Handler.video_id = ''
            _Handler.session_active = False
            _Handler._session_start = None
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.send_error(405)

    # ── dashboard pages ───────────────────────────────────────────────────────

    def _page_dashboard(self) -> str:
        if self.session_active:
            vid_url = self.source_url or '—'
            session_block = (
                f'<div class="card">'
                f'<span class="dot dot-live"></span>'
                f'<span class="live">Session active</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#555;font-size:.85em">{vid_url}</span>'
                f'&nbsp;&nbsp;'
                f'<a href="/player" style="color:#4c4;font-size:.85em;text-decoration:none">'
                f'&#9654;&nbsp;Watch live &rarr;</a>'
                f'&nbsp;&nbsp;'
                f'<form method="POST" action="/stop" style="display:inline;margin:0">'
                f'<button class="btn-danger" type="submit">&#9632; Stop</button>'
                f'</form>'
                f'</div>'
            )
        else:
            session_block = (
                '<div class="card">'
                '<span class="dot dot-idle"></span>'
                '<span class="idle">No active session</span>'
                '</div>'
            )

        cue_count = self.writer.cue_count if self.writer else 0
        backend = self.config_snapshot.get('asr', {}).get('primary', '—')

        body = (
            f'<h2>Session</h2>{session_block}'
            f'<h2>New session</h2>'
            f'<div class="card">'
            f'<form class="row" method="POST" action="/start">'
            f'<input type="url" name="url" placeholder="https://www.youtube.com/watch?v=…" required autofocus>'
            f'<button class="btn-primary" type="submit">&#9654;&nbsp;Caption</button>'
            f'</form>'
            f'</div>'
            f'<h2>Quick stats</h2>'
            f'<div class="card grid3">'
            f'<div><div class="stat-label">Cues generated</div>'
            f'<div class="stat-val" id="qs-cues">{cue_count}</div></div>'
            f'<div><div class="stat-label">ASR backend</div>'
            f'<div class="stat-val">{backend}</div></div>'
            f'<div><div class="stat-label">Session uptime</div>'
            f'<div class="stat-val" id="qs-uptime">—</div></div>'
            f'</div>'
        )
        script = (
            'async function poll(){'
            'try{'
            'const d=await(await fetch("/api/metrics")).json();'
            'const g=id=>document.getElementById(id);'
            'if(g("qs-cues"))g("qs-cues").textContent=d.captions?.cue_count??"—";'
            'const s=d.session?.uptime_s;'
            'if(g("qs-uptime")&&s!=null){'
            'const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=Math.floor(s%60);'
            'g("qs-uptime").textContent='
            'String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(sc).padStart(2,"0");'
            '}}catch(e){}}'
            'poll();setInterval(poll,3000);'
        )
        return _wrap('Dashboard', 'dashboard', body, self.session_active, script)

    def _page_monitor(self) -> str:
        body = (
            '<div class="grid2">'
            '<div><h2>ASR Latency</h2><div class="card">'
            '<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>'
            '<tr><td>Mean</td><td id="lat-mean">—</td></tr>'
            '<tr><td>p95</td><td id="lat-p95">—</td></tr>'
            '<tr><td>Max</td><td id="lat-max">—</td></tr>'
            '<tr><td>Rate</td><td id="lat-rate">—</td></tr>'
            '<tr><td>Total inferences</td><td id="lat-total">—</td></tr>'
            '</tbody></table></div></div>'
            '<div><h2>Session</h2><div class="card">'
            '<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>'
            '<tr><td>Status</td><td id="mon-status">—</td></tr>'
            '<tr><td>Uptime</td><td id="mon-uptime">—</td></tr>'
            '<tr><td>Cues generated</td><td id="mon-cues">—</td></tr>'
            '<tr><td>ASR backend</td><td id="mon-backend">—</td></tr>'
            '</tbody></table></div></div>'
            '</div>'
            '<h2>System</h2>'
            '<div class="card grid3">'
            '<div><div class="stat-label">GPU utilisation</div>'
            '<div class="stat-val stub">Phase 2</div></div>'
            '<div><div class="stat-label">CPU utilisation</div>'
            '<div class="stat-val stub">Phase 2</div></div>'
            '<div><div class="stat-label">VRAM used</div>'
            '<div class="stat-val stub">Phase 2</div></div>'
            '</div>'
            '<p class="ts" id="refresh-ts" style="margin-top:8px">'
            'Refreshing every 2 s…</p>'
        )
        script = (
            'function fms(v){return v==null?"—":v.toFixed(0)+" ms";}'
            'function fup(s){'
            'if(s==null)return "—";'
            'const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=Math.floor(s%60);'
            'return String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(sc).padStart(2,"0");'
            '}'
            'async function refresh(){'
            'try{'
            'const d=await(await fetch("/api/metrics")).json();'
            'const id=s=>document.getElementById(s);'
            'id("lat-mean").textContent=fms(d.asr?.mean_ms);'
            'id("lat-p95").textContent=fms(d.asr?.p95_ms);'
            'id("lat-max").textContent=fms(d.asr?.max_ms);'
            'id("lat-rate").textContent=d.asr?.rate!=null?d.asr.rate.toFixed(2)+"/s":"—";'
            'id("lat-total").textContent=d.asr?.total??"—";'
            'id("mon-status").textContent=d.session?.active?"Active":"Idle";'
            'id("mon-status").style.color=d.session?.active?"#3a9a4a":"#444";'
            'id("mon-uptime").textContent=fup(d.session?.uptime_s);'
            'id("mon-cues").textContent=d.captions?.cue_count??"—";'
            'id("mon-backend").textContent=d.asr?.backend??"—";'
            'id("refresh-ts").textContent="Last update: "+new Date().toLocaleTimeString();'
            '}catch(e){}}'
            'refresh();setInterval(refresh,2000);'
        )
        return _wrap('Monitor', 'monitor', body, self.session_active, script)

    def _page_config(self) -> str:
        cfg_json = json.dumps(self.config_snapshot, indent=2)
        body = (
            '<h2>Active configuration</h2>'
            f'<div class="card"><pre>{cfg_json}</pre></div>'
            '<p class="stub" style="margin-top:6px">'
            'Per-field editing and hot-reload — Phase 2</p>'
        )
        return _wrap('Config', 'config', body, self.session_active)

    def _page_logs(self) -> str:
        rows = ''.join(
            f'<tr><td class="ts">{c["ts"]}</td><td>{c["text"]}</td></tr>'
            for c in reversed(list(self._recent_cues))
        ) or ('<tr><td colspan="2" class="stub">'
               'No captions yet this session.</td></tr>')
        body = (
            '<h2>Recent captions</h2>'
            '<div class="card">'
            '<table><thead><tr><th>Time</th><th>Caption</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            '<p style="margin-top:8px;font-size:.75em">'
            '<a href="/webvtt" style="color:#444">Download WebVTT</a>'
            '&nbsp;&nbsp;<span class="stub">SRT / PDF export — Phase 2</span></p>'
            '<h2 style="margin-top:20px">Packet logs</h2>'
            '<div class="card"><p class="stub">'
            'Live packet log viewer — Phase 2</p></div>'
        )
        return _wrap('Logs', 'logs', body, self.session_active)

    # ── API endpoints ─────────────────────────────────────────────────────────

    def _build_metrics_json(self) -> str:
        asr: Dict = {}
        provider = _Handler.metrics_provider  # access via class to avoid method binding
        if provider:
            try:
                asr = provider()
            except Exception:
                pass

        uptime = None
        if self._session_start is not None:
            uptime = time.monotonic() - self._session_start

        payload = {
            'session': {
                'active': self.session_active,
                'video_id': self.video_id,
                'uptime_s': round(uptime, 1) if uptime is not None else None,
            },
            'asr': asr,
            'captions': {
                'cue_count': self.writer.cue_count if self.writer else 0,
                'recent': list(self._recent_cues)[-10:],
            },
        }
        return json.dumps(payload)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _sse_stream(self) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        with self._sse_lock:
            self._sse_clients.append(self)
        try:
            while True:
                time.sleep(15)
                self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self._sse_lock:
                if self in self._sse_clients:
                    self._sse_clients.remove(self)

    def push_cue(self, cue_json: str) -> bool:
        try:
            self.wfile.write(f'event: cue\ndata: {cue_json}\n\n'.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Public API ────────────────────────────────────────────────────────────────

class WebVTTServer:
    """
    HTTP server managing the caption web UI and SSE fan-out.

    Pass start_callback(url: str) to handle YouTube URL submissions from the UI.
    Call on_caption() from the ASR callback — thread-safe.
    Call set_video_id() when a new session starts.
    """

    def __init__(self, config: dict, writer: WebVTTWriter,
                 video_id: str = '',
                 start_callback: Optional[Callable[[str], None]] = None,
                 stop_callback: Optional[Callable[[], None]] = None,
                 metrics_provider: Optional[Callable[[], Dict]] = None,
                 config_snapshot: Optional[Dict] = None) -> None:
        self._host: str = config.get('host', '0.0.0.0')
        self._port: int = config.get('port', 8765)
        self._writer = writer
        self._lock = threading.Lock()
        self._clients: List[_Handler] = []
        self._server: HTTPServer = None
        self._thread: threading.Thread = None

        _Handler.writer = writer
        _Handler.video_id = video_id
        _Handler.start_callback = start_callback
        _Handler.stop_callback = stop_callback
        _Handler._sse_clients = self._clients
        _Handler._sse_lock = self._lock
        _Handler.metrics_provider = metrics_provider
        _Handler.config_snapshot = config_snapshot or {}
        _Handler._recent_cues = deque(maxlen=50)

    def set_video_id(self, video_id: str) -> None:
        _Handler.video_id = video_id

    def set_source(self, url: str, video_id: str, source_type: str) -> None:
        _Handler.source_url  = url
        _Handler.video_id    = video_id
        _Handler.source_type = source_type

    def start(self) -> None:
        self._server = _ThreadedHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name='webvtt-http',
        )
        self._thread.start()
        print(f'[WebVTT] Serving at http://localhost:{self._port}/')

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    def on_caption(self, text: str, start: float, end: float) -> None:
        self._writer.add_cue(text, start, end)
        _Handler._recent_cues.append({
            'ts': time.strftime('%H:%M:%S'),
            'text': text,
        })
        cue_json = json.dumps({
            'text': text,
            'lines': [l for l in text.split('\n') if l.strip()],
            'start': f'{start:.3f}',
            'end': f'{end:.3f}',
        })
        dead = []
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            if not client.push_cue(cue_json):
                dead.append(client)
        if dead:
            with self._lock:
                for c in dead:
                    if c in self._clients:
                        self._clients.remove(c)
