"""
HTTP server exposing:

  GET /           — text-only caption scroll page (no YouTube URL) or
                    YouTube video+caption overlay (when video_id is set)
  GET /webvtt     — full WebVTT document (accumulated cues)
  GET /events     — Server-Sent Events stream (new cues pushed as they arrive)

Uses only stdlib (http.server + threading) — no aiohttp dependency needed.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List
from ..caption.webvtt import WebVTTWriter


_HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>MicroCaption Live</title>
  <style>
    body { background: #111; color: #fff; font: 1.4em/1.6 monospace; padding: 2em; }
    #captions { white-space: pre-wrap; }
    .ts { color: #888; font-size: 0.75em; }
  </style>
</head>
<body>
  <h2>MicroCaption — Live Captions</h2>
  <div id="captions"></div>
  <script>
    const div = document.getElementById('captions');
    const es = new EventSource('/events');
    es.addEventListener('cue', e => {
      const d = JSON.parse(e.data);
      div.insertAdjacentHTML('beforeend',
        '<p><span class="ts">' + d.start + '</span> ' + d.text + '</p>');
      window.scrollTo(0, document.body.scrollHeight);
    });
    es.onerror = () => console.warn('SSE connection lost; reconnecting…');
  </script>
</body>
</html>
"""


def _make_youtube_player_html(video_id: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>MicroCaption — Live ASR Captions</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #111; color: #fff; font-family: monospace; min-height: 100vh; padding: 16px; }}
    h1 {{ font-size: 0.85em; color: #555; letter-spacing: 0.12em; text-transform: uppercase; margin-bottom: 12px; }}
    #player-wrap {{ position: relative; width: 100%; max-width: 1280px; margin: 0 auto; background: #000; border-radius: 6px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.6); }}
    #player-wrap iframe {{ display: block; width: 100%; aspect-ratio: 16/9; border: none; }}
    #caption-bar {{
      position: absolute; bottom: 0; left: 0; right: 0;
      padding: 12px 16px 18px;
      background: linear-gradient(transparent, rgba(0,0,0,0.82));
      text-align: center;
      pointer-events: none;
      min-height: 3.5em;
      display: flex; flex-direction: column; justify-content: flex-end; align-items: center; gap: 4px;
    }}
    .caption-line {{
      display: inline-block;
      font-size: 1.45em;
      line-height: 1.35;
      color: #fff;
      text-shadow: 1px 1px 3px #000, -1px -1px 3px #000, 0 2px 6px rgba(0,0,0,0.8);
      background: rgba(0,0,0,0.35);
      padding: 2px 8px;
      border-radius: 3px;
    }}
    #footer {{ margin-top: 10px; font-size: 0.72em; color: #444; text-align: center; }}
    #status {{ display: inline-block; padding: 2px 8px; border-radius: 3px; }}
    #status.live {{ color: #4c4; }}
    #status.waiting {{ color: #888; }}
    #status.error {{ color: #c44; }}
  </style>
</head>
<body>
  <h1>MicroCaption &mdash; YouTube Live ASR Demo</h1>
  <div id="player-wrap">
    <iframe
      src="https://www.youtube.com/embed/{video_id}?autoplay=1&mute=0"
      allow="autoplay; encrypted-media; picture-in-picture"
      allowfullscreen>
    </iframe>
    <div id="caption-bar"></div>
  </div>
  <div id="footer">
    <span id="status" class="waiting">Connecting to caption stream&hellip;</span>
    &nbsp;&bull;&nbsp; CEA-608/708 packets logged to <code>logs/</code>
    &nbsp;&bull;&nbsp; <a href="/webvtt" style="color:#666">Download WebVTT</a>
  </div>

  <script>
    const bar = document.getElementById('caption-bar');
    const status = document.getElementById('status');

    function showLines(lines) {{
      bar.innerHTML = '';
      lines.forEach(line => {{
        if (!line.trim()) return;
        const span = document.createElement('span');
        span.className = 'caption-line';
        span.textContent = line;
        bar.appendChild(span);
      }});
    }}

    const es = new EventSource('/events');

    es.addEventListener('cue', e => {{
      const d = JSON.parse(e.data);
      status.textContent = 'LIVE';
      status.className = 'live';
      if (d.lines && d.lines.length) {{
        showLines(d.lines);
      }} else if (d.text) {{
        showLines(d.text.split('\\n'));
      }}
    }});

    es.onopen = () => {{
      status.textContent = 'Connected — waiting for speech…';
      status.className = 'waiting';
    }};

    es.onerror = () => {{
      status.textContent = 'Stream disconnected — retrying…';
      status.className = 'error';
    }};
  </script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    writer: WebVTTWriter = None
    video_id: str = ''
    _sse_clients: List = []
    _sse_lock: threading.Lock = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/':
            if self.video_id:
                html = _make_youtube_player_html(self.video_id)
                self._send(html.encode(), 'text/html')
            else:
                self._send(_HTML_PAGE.encode(), 'text/html')
        elif self.path == '/webvtt':
            self._send(self.writer.flush().encode(), 'text/vtt')
        elif self.path == '/events':
            self._sse_stream()
        else:
            self.send_error(404)

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
            msg = f'event: cue\ndata: {cue_json}\n\n'.encode()
            self.wfile.write(msg)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False


class WebVTTServer:
    """
    Manages the HTTP server thread and SSE fan-out.

    Pass video_id to enable the YouTube overlay player at '/'.
    Call on_caption() from the ASR callback — it is thread-safe.
    """

    def __init__(self, config: dict, writer: WebVTTWriter, video_id: str = '') -> None:
        self._host: str = config.get('host', '0.0.0.0')
        self._port: int = config.get('port', 8765)
        self._writer = writer
        self._lock = threading.Lock()
        self._clients: List[_Handler] = []
        self._server: HTTPServer = None
        self._thread: threading.Thread = None

        _Handler.writer = writer
        _Handler.video_id = video_id
        _Handler._sse_clients = self._clients
        _Handler._sse_lock = self._lock

    def start(self) -> None:
        self._server = HTTPServer((self._host, self._port), _Handler)
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
