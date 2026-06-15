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
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Callable, List, Optional
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
      placeholder="https://www.youtube.com/watch?v=..."
      required autofocus>
    <button type="submit">&#9654;&nbsp; Caption</button>
  </form>
  <p class="hint">Audio is pulled server-side and transcribed locally &mdash; no third-party APIs.</p>
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

def _make_player_html(video_id: str) -> str:
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
</head>
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
    <iframe
      src="https://www.youtube.com/embed/{video_id}?autoplay=1&mute=0"
      allow="autoplay; encrypted-media; picture-in-picture"
      allowfullscreen>
    </iframe>
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
                self._send(_make_player_html(self.video_id).encode(), 'text/html')

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
                # Set video_id and session_active NOW so /player renders
                # correctly before the background thread finishes yt-dlp.
                _Handler.video_id = _video_id_from_url(url)
                _Handler.session_active = True

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
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.send_error(405)

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
                 stop_callback: Optional[Callable[[], None]] = None) -> None:
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

    def set_video_id(self, video_id: str) -> None:
        _Handler.video_id = video_id

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
