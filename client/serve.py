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

handler = http.server.SimpleHTTPRequestHandler

handler.extensions_map = {
    '': 'application/octet-stream',
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.json': 'application/json',
    '.ico': 'image/x-icon',
}

print(f'Client dev server: http://localhost:{PORT}/')
print(f'API expected at:   http://localhost:8765/ (start with server/run.sh)')

with http.server.HTTPServer(('', PORT), handler) as srv:
    srv.serve_forever()
