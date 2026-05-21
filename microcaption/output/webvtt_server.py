"""
HTTP server exposing two endpoints:

  GET /           — minimal HTML page with auto-updating caption display
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

_YOUTUBE_PLAYER_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>MicroCaption — YouTube Player</title>
  <style>
    body { margin: 0; padding: 20px; background: #111; color: #fff; font: 1.4em/1.6 monospace; min-height: 100vh; }
    #player-container { position: relative; width: 100%; max-width: 1280px; margin: 0 auto; }
    #video { width: 100%; aspect-ratio: 16/9; background: #000; border-radius: 8px; overflow: hidden; }
    #video iframe { width: 100%; height: 100%; border: none; }
    #captions { white-space: pre-wrap; position: absolute; bottom: 0; left: 0; right: 0; padding: 10px; background: linear-gradient(transparent, rgba(0,0,0,0.7)); font-size: 1.2em; color: #fff; text-shadow: 1px 1px 2px #000; pointer-events: none; }
    #status { position: fixed; top: 10px; right: 10px; color: #888; font-size: 0.8em; }
  </style>
</head>
<body>
  <div id="status">Loading...</div>
  <div id="player-container">
    <div id="video"></div>
    <div id="captions"></div>
  </div>
  <script>
    const videoId = "YOUR_VIDEO_ID";
    document.getElementById('status').textContent = 'YouTube stream ready';
    document.getElementById('video').innerHTML = '<iframe src="https://www.youtube.com/embed/' + videoId + '?autoplay=1" allowfullscreen allow="autoplay; encrypted-media"></iframe>';
    const captionEndpoint = window.location.origin + '/events';
    if (window.EventSource) {
      const es = new EventSource(captionEndpoint);
      es.addEventListener('cue', e => {
        const d = JSON.parse(e.data);
        const div = document.getElementById('captions');
        if (!d.lines || !d.lines.length) { div.innerHTML = ''; return; }
        d.lines.forEach((line, idx) => {
          if (idx === 0) { div.innerHTML += '<div class="ru">' + line + '</div>'; }
          else if (idx === 1) { div.innerHTML += '<div class="cr">' + line + '</div>'; }
        });
        es.onerror = () => console.warn('SSE disconnected');
      });
    }
  </script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    writer: WebVTTWriter = None         # injected by WebVTTServer
    _sse_clients: List = []             # list of response objects
    _sse_lock: threading.Lock = None

    def log_message(self, fmt, *args):
        pass  # suppress access log to keep terminal clean

    def do_GET(self):
        if self.path == '/':
            self._send(_HTML_PAGE.encode(), 'text/html')
        elif self.path == '/webvtt':
            self._send(self.writer.flush().encode(), 'text/vtt')
        elif self.path == '/events':
            self._sse_stream()
        elif self.path.startswith('/youtube/'):
            # Serve YouTube player - path is just video ID
            video_id = self.path.replace('/youtube/', '').strip()
            html = self._youtuber_html(video_id)
            self._send(html.encode(), 'text/html')
        else:
            self.send_error(404)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _youtuber_html(self, video_id: str) -> str:
        """Generate YouTube player HTML with video ID."""
        if not video_id:
            return _YOUTUBE_PLAYER_HTML

        return _YOUTUBE_PLAYER_HTML.replace('const videoId = "YOUR_VIDEO_ID"', f'const videoId = "{video_id}"')

    def _sse_stream(self) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        with self._sse_lock:
            self._sse_clients.append(self)
        try:
            # Keep connection alive — new cues are pushed via push_cue()
            while True:
                time.sleep(15)
                # Heartbeat comment to prevent proxy timeouts
                self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self._sse_lock:
                if self in self._sse_clients:
                    self._sse_clients.remove(self)

    def push_cue(self, cue_json: str) -> bool:
        """Send a single cue to this SSE client. Returns False if disconnected."""
        try:
            msg = f'event: cue\ndata: {cue_json}\n\n'.encode()
            self.wfile.write(msg)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False


class WebVTTServer:
    """
    Manages the HTTP server thread and fan-out to SSE clients.

    Call on_caption() from the ASR callback — it is thread-safe.
    """

    def __init__(self, config: dict, writer: WebVTTWriter) -> None:
        self._host: str = config.get('host', '0.0.0.0')
        self._port: int = config.get('port', 8765)
        self._writer = writer
        self._lock = threading.Lock()
        self._clients: List[_Handler] = []
        self._server: HTTPServer = None
        self._thread: threading.Thread = None

        # Inject shared state into handler class
        _Handler.writer = writer
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
        print(f'[WebVTT] Serving at http://{self._host}:{self._port}/')

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    def on_caption(self, text: str, start: float, end: float) -> None:
        self._writer.add_cue(text, start, end)
        cue_json = json.dumps({'text': text, 'start': f'{start:.3f}', 'end': f'{end:.3f}'})
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
