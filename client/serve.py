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

os.chdir(os.path.dirname(os.path.abspath(__file__)))


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
