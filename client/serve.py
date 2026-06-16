#!/usr/bin/env python3
"""
MicroCaption client dev server.
Serves the static HTML/CSS/JS files in this directory on http://localhost:3000.
The caption API runs separately on http://localhost:8765 (see server/run.sh).
"""

import http.server
import os
import sys

PORT = int(os.environ.get('PORT', 3000))

_CLIENT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_CLIENT_DIR)


class _Tee:
    """Mirror output to the console and to server/logs/client.log so the Logs
    page (served by the API on :8765) can show client-server request logs."""
    def __init__(self, stream, path):
        self._stream = stream
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._f = open(path, 'a', buffering=1)

    def write(self, s):
        self._stream.write(s)
        try:
            self._f.write(s)
        except Exception:
            pass

    def flush(self):
        self._stream.flush()


_LOG_PATH = os.path.join(_CLIENT_DIR, '..', 'server', 'logs', 'client.log')
sys.stdout = _Tee(sys.stdout, _LOG_PATH)
sys.stderr = _Tee(sys.stderr, _LOG_PATH)


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        '': 'application/octet-stream',
        '.html': 'text/html; charset=utf-8',
        '.css': 'text/css',
        '.js': 'application/javascript',
        '.json': 'application/json',
        '.ico': 'image/x-icon',
    }

    def translate_path(self, path):
        # Clean URLs: serve /dashboard from dashboard.html. If the request has
        # no extension and no matching file/dir exists, fall back to <path>.html.
        fs = super().translate_path(path)
        _, ext = os.path.splitext(fs)
        if not ext and not os.path.isdir(fs) and not os.path.exists(fs):
            if os.path.exists(fs + '.html'):
                return fs + '.html'
        return fs


print(f'Client dev server: http://localhost:{PORT}/')
print(f'API expected at:   http://localhost:8765/ (start with server/run.sh)')

with http.server.HTTPServer(('', PORT), Handler) as srv:
    srv.serve_forever()
