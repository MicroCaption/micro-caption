"""
HTTP server — API and SSE endpoints only (UI served by client/).

  GET  /api/sessions                 — all active sessions as JSON
  GET  /api/metrics                  — aggregate ASR latency JSON
  GET  /api/config                   — active config JSON
  GET  /api/queue                    — queue JSON
  GET  /api/session/<id>/position    — current playback position + cue count
  GET  /api/session/<id>/cues        — all cues for replay mode
  GET  /api/watch/<code>             — public: session lookup by code
  POST /api/start                    — start session → {session_id, player_url}
  POST /api/stop/<id>                — stop session → 200 JSON
  POST /api/queue                    — add to queue → {id, position}
  GET  /events/<id>                  — per-session SSE caption stream (auth required)
  GET  /events/watch/<code>          — public SSE caption stream
  GET  /webvtt/<id>                  — WebVTT file download (auth required)
  GET  /login                        — OAuth2 redirect (if auth enabled)
  GET  /auth/callback                — OAuth2 callback
  GET  /logout                       — clear session cookie
  GET  /status                       — backward-compat status JSON (no auth)
"""

import base64
import datetime
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Callable, Dict, List, Optional

from ..caption.webvtt import WebVTTWriter


def _esc(s: str) -> str:
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


# ── Cookie helpers ────────────────────────────────────────────────────────────

def _parse_cookie(header: str, name: str) -> 'str | None':
    for part in header.split(';'):
        part = part.strip()
        if part.startswith(name + '='):
            return part[len(name) + 1:]
    return None


# ── Auth manager (Google OAuth2 + signed session cookies) ────────────────────

class _AuthManager:
    GOOGLE_AUTH_URL  = 'https://accounts.google.com/o/oauth2/v2/auth'
    GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
    GOOGLE_CERTS_URL = 'https://www.googleapis.com/oauth2/v3/certs'

    def __init__(self, cfg: dict) -> None:
        self.client_id       = cfg.get('google_client_id', '')
        self.client_secret   = cfg.get('google_client_secret', '')
        self.redirect_uri    = cfg.get('redirect_uri', '')
        self.allowed_emails  = [e.lower() for e in cfg.get('allowed_emails', [])]
        self.session_max_age = int(cfg.get('session_max_age', 86400))
        self.session_secret  = cfg.get('session_secret', '')
        self.cookie_name     = cfg.get('cookie_name', 'mc_session')
        self.cookie_secure   = bool(cfg.get('cookie_secure', False))
        self._jwks_client    = None
        self._jwks_lock      = threading.Lock()

    def login_url(self, state: str) -> str:
        params = urllib.parse.urlencode({
            'client_id':     self.client_id,
            'redirect_uri':  self.redirect_uri,
            'response_type': 'code',
            'scope':         'openid email',
            'state':         state,
            'access_type':   'online',
        })
        return f'{self.GOOGLE_AUTH_URL}?{params}'

    def exchange_code(self, code: str) -> dict:
        data = urllib.parse.urlencode({
            'code':          code,
            'client_id':     self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri':  self.redirect_uri,
            'grant_type':    'authorization_code',
        }).encode()
        req = urllib.request.Request(self.GOOGLE_TOKEN_URL, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def verify_id_token(self, id_token: str) -> dict:
        try:
            import jwt
            from jwt import PyJWKClient
        except ImportError as exc:
            raise RuntimeError(
                'PyJWT not installed — run: pip install "PyJWT[crypto]>=2.8,<3"'
            ) from exc
        with self._jwks_lock:
            if self._jwks_client is None:
                self._jwks_client = PyJWKClient(self.GOOGLE_CERTS_URL)
            client = self._jwks_client
        signing_key = client.get_signing_key_from_jwt(id_token)
        return jwt.decode(
            id_token,
            signing_key.key,
            algorithms=['RS256'],
            audience=self.client_id,
        )

    def make_cookie(self, email: str) -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps({
                'e': email,
                'x': int(time.time()) + self.session_max_age,
            }).encode()
        ).rstrip(b'=').decode()
        sig = hmac.new(
            self.session_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f'{payload}.{sig}'

    def verify_cookie(self, raw: str) -> 'str | None':
        try:
            payload, sig = raw.rsplit('.', 1)
        except ValueError:
            return None
        expected = hmac.new(
            self.session_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + '=='))
        except Exception:
            return None
        if data.get('x', 0) < time.time():
            return None
        return data.get('e')

    def is_allowed(self, email: str) -> bool:
        if not self.allowed_emails:
            return True
        return email.lower() in self.allowed_emails


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    _session_registry: Dict = {}
    _registry_lock: threading.Lock = None

    _auth: Optional['_AuthManager'] = None

    _queue: List[Dict] = []
    _queue_lock: threading.Lock = None

    _code_registry: Dict[str, str] = {}
    _code_lock: threading.Lock = None

    start_callback: Optional[Callable[[str], str]] = None
    stop_callback: Optional[Callable[[str], None]] = None
    metrics_provider: Optional[Callable[[], Dict]] = None
    config_snapshot: Dict = {}

    def log_message(self, fmt, *args):
        pass

    # ── routing ───────────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        parts = [p for p in path.split('/') if p]

        # ── Public / no-auth ──────────────────────────────────────────────
        if path == '/status':
            sessions = list(self._session_registry.values())
            self._send_json({
                'active': len(sessions) > 0,
                'session_count': len(sessions),
                'sessions': [{'id': s.id, 'status': s.status} for s in sessions],
            })

        elif path == '/login':
            self._get_login()

        elif path == '/auth/callback':
            self._get_auth_callback()

        elif path == '/logout':
            self._get_logout()

        # ── Public watch (no auth) ────────────────────────────────────────
        elif parts[:2] == ['events', 'watch'] and len(parts) == 3:
            self._sse_watch_stream(parts[2])

        elif parts[:2] == ['api', 'watch'] and len(parts) == 3:
            code = parts[2]
            with _Handler._code_lock:
                sid = _Handler._code_registry.get(code)
            if not sid:
                self.send_error(404)
                return
            sess = _Handler._session_registry.get(sid)
            self._send_json({
                'session_id': sid,
                'status': sess.status if sess else 'gone',
            })

        # ── Protected API ─────────────────────────────────────────────────
        elif path == '/api/sessions':
            if self._require_auth() is None: return
            self._send_json_raw(self._build_sessions_json())

        elif path == '/api/metrics':
            if self._require_auth() is None: return
            self._send_json_raw(self._build_metrics_json())

        elif path == '/api/config':
            if self._require_auth() is None: return
            self._send_json_raw(json.dumps(_Handler.config_snapshot, indent=2))

        elif path == '/api/queue':
            if self._require_auth() is None: return
            with _Handler._queue_lock:
                self._send_json_raw(json.dumps(_Handler._queue))

        elif parts[:2] == ['api', 'session'] and len(parts) == 4 and parts[3] == 'position':
            if self._require_auth() is None: return
            sess = _Handler._session_registry.get(parts[2])
            if not sess:
                self.send_error(404)
                return
            t = sess.writer.current_time if sess.writer else 0.0
            n = sess.writer.cue_count   if sess.writer else 0
            self._send_json({'current_time': round(t, 3), 'cue_count': n})

        elif parts[:2] == ['api', 'session'] and len(parts) == 4 and parts[3] == 'cues':
            if self._require_auth() is None: return
            sess = _Handler._session_registry.get(parts[2])
            if not sess:
                self.send_error(404)
                return
            cues = sess.writer.all_cue_data() if sess.writer else []
            self._send_json_raw(json.dumps(cues))

        elif parts[:1] == ['events'] and len(parts) == 2:
            if self._require_auth() is None: return
            self._sse_stream(parts[1])

        elif parts[:1] == ['webvtt'] and len(parts) == 2:
            if self._require_auth() is None: return
            sess = self._session_registry.get(parts[1])
            if not sess:
                self.send_error(404)
            else:
                body = sess.writer.flush().encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/vtt')
                self.send_header('Content-Length', str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?')[0]
        parts = [p for p in path.split('/') if p]

        if path == '/api/start':
            if self._require_auth() is None: return
            self._post_start()

        elif parts[:2] == ['api', 'stop'] and len(parts) == 3:
            if self._require_auth() is None: return
            self._post_stop(parts[2])

        elif path == '/api/queue':
            if (email := self._require_auth()) is None: return
            self._post_queue_add(email)

        else:
            self.send_error(405)

    # ── POST handlers ─────────────────────────────────────────────────────────

    def _post_start(self) -> None:
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode(errors='replace')
        params = urllib.parse.parse_qs(body)
        url = params.get('url', [''])[0].strip()

        if not url:
            self._send_json({'error': 'no url provided'}, 400)
            return

        session_id = ''
        cb = _Handler.start_callback
        if cb:
            session_id = cb(url)

        self._send_json({
            'session_id': session_id,
            'player_url': f'/player?id={session_id}',
        })

    def _post_stop(self, session_id: str) -> None:
        cb = _Handler.stop_callback
        if cb:
            threading.Thread(
                target=cb, args=(session_id,), daemon=True,
                name=f'session-stop-{session_id}',
            ).start()
        self._send_json({'ok': True})

    def _post_queue_add(self, email: str) -> None:
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode(errors='replace')
        params = urllib.parse.parse_qs(body)
        url = params.get('url', [''])[0].strip()
        if not url:
            self._send_json({'error': 'no url provided'}, 400)
            return
        item = {
            'id':         uuid.uuid4().hex,
            'url':        url,
            'added_by':   email,
            'added_at':   datetime.datetime.utcnow().isoformat() + 'Z',
            'status':     'pending',
            'session_id': None,
        }
        with _Handler._queue_lock:
            _Handler._queue.append(item)
            position = len(_Handler._queue)
        self._send_json({'id': item['id'], 'position': position})

    # ── API JSON builders ─────────────────────────────────────────────────────

    def _build_sessions_json(self) -> str:
        data = []
        for s in self._session_registry.values():
            recent = list(s.recent_cues)
            last_cue = recent[-1]['text'] if recent else None
            data.append({
                'id': s.id,
                'url': s.url,
                'video_id': s.video_id,
                'source_type': s.source_type,
                'uptime_s': round(s.uptime, 1),
                'cue_count': s.writer.cue_count if s.writer else 0,
                'last_cue': last_cue,
                'status': s.status,
                'error': s.error or None,
                'code': s.code,
            })
        return json.dumps(data)

    def _build_metrics_json(self) -> str:
        asr: Dict = {}
        provider = _Handler.metrics_provider
        if provider:
            try:
                asr = provider()
            except Exception:
                pass

        sessions = list(self._session_registry.values())
        payload = {
            'session_count': len(sessions),
            'asr': asr,
            'captions': {
                'total_cues': sum(
                    s.writer.cue_count for s in sessions if s.writer
                ),
            },
        }
        return json.dumps(payload)

    # ── SSE ───────────────────────────────────────────────────────────────────

    def _sse_stream(self, session_id: str) -> None:
        sess = self._session_registry.get(session_id)
        if sess is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self._cors()
        self.end_headers()
        if sess.writer:
            last = sess.writer.last_cue_data()
            if last:
                # Stitch recent fragments into a short tail so a late joiner
                # lands on ~2 readable lines instead of a 2-word fragment.
                tail = sess.writer.tail_text() or last['text']
                catchup = json.dumps({
                    'text':  tail,
                    'lines': [l for l in tail.split('\n') if l.strip()],
                    'start': f"{last['start']:.3f}",
                    'end':   f"{last['end']:.3f}",
                    'catchup': True,
                })
                try:
                    self.wfile.write(f'event: cue\ndata: {catchup}\n\n'.encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
        with sess.sse_lock:
            sess.sse_clients.append(self)
        try:
            while True:
                time.sleep(15)
                self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with sess.sse_lock:
                if self in sess.sse_clients:
                    sess.sse_clients.remove(self)

    def push_cue(self, cue_json: str) -> bool:
        try:
            self.wfile.write(f'event: cue\ndata: {cue_json}\n\n'.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _sse_watch_stream(self, code: str) -> None:
        with _Handler._code_lock:
            session_id = _Handler._code_registry.get(code)
        if not session_id:
            self.send_error(404)
            return
        self._sse_stream(session_id)

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _require_auth(self) -> 'str | None':
        if _Handler._auth is None:
            return 'dev@local'
        val = _parse_cookie(self.headers.get('Cookie', ''), _Handler._auth.cookie_name)
        email = _Handler._auth.verify_cookie(val) if val else None
        if email is None:
            self._send_json({'error': 'unauthorized'}, 401)
            return None
        return email

    def _send_cookie(self, name: str, value: str, max_age: int) -> None:
        secure = bool(_Handler._auth and _Handler._auth.cookie_secure)
        parts = [f'{name}={value}', f'Max-Age={max_age}',
                 'HttpOnly', 'SameSite=Lax', 'Path=/']
        if secure:
            parts.append('Secure')
        self.send_header('Set-Cookie', '; '.join(parts))

    # ── Auth routes ───────────────────────────────────────────────────────────

    def _get_login(self) -> None:
        if _Handler._auth is None:
            self.send_response(302)
            self.send_header('Location', '/dashboard')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        state = secrets.token_hex(16)
        self.send_response(302)
        self.send_header('Location', _Handler._auth.login_url(state))
        self._send_cookie('mc_state', state, 300)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _get_auth_callback(self) -> None:
        qs_str = self.path.split('?', 1)[1] if '?' in self.path else ''
        qs = urllib.parse.parse_qs(qs_str)
        code  = qs.get('code',  [''])[0]
        state = qs.get('state', [''])[0]

        expected_state = _parse_cookie(self.headers.get('Cookie', ''), 'mc_state')
        if not code or not state or state != expected_state:
            self.send_response(302)
            self.send_header('Location', '/?error=unauthorized')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        try:
            tokens   = _Handler._auth.exchange_code(code)
            id_token = tokens.get('id_token', '')
            claims   = _Handler._auth.verify_id_token(id_token)
            email    = claims.get('email', '')
            verified = claims.get('email_verified', False)
        except Exception as exc:
            print(f'[Auth] callback error: {exc}')
            self.send_response(302)
            self.send_header('Location', '/?error=auth_failed')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        if not verified or not _Handler._auth.is_allowed(email):
            self.send_response(302)
            self.send_header('Location', '/?error=unauthorized')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        self.send_response(302)
        self.send_header('Location', '/dashboard')
        self._send_cookie('mc_state', '', 0)
        self._send_cookie(
            _Handler._auth.cookie_name,
            _Handler._auth.make_cookie(email),
            _Handler._auth.session_max_age,
        )
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _get_logout(self) -> None:
        self.send_response(302)
        self.send_header('Location', '/')
        if _Handler._auth:
            self._send_cookie(_Handler._auth.cookie_name, '', 0)
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cors(self) -> None:
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, data: dict, status: int = 200) -> None:
        self._send_json_raw(json.dumps(data), status)

    def _send_json_raw(self, raw: str, status: int = 200) -> None:
        body = raw.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Public API ────────────────────────────────────────────────────────────────

class WebVTTServer:
    """
    API-only HTTP server for the MicroCaption backend.

    The web UI is served by client/serve.py (separate process).

    start_callback(url) → session_id   called on POST /api/start
    stop_callback(session_id)          called on POST /api/stop/<id>
    on_caption(text, start, end, session_id)  push a caption cue to one session
    register_session(session)          add a Session to the routing table
    unregister_session(session_id)     remove a Session from the routing table
    """

    def __init__(self, config: dict,
                 auth_cfg: Optional[Dict] = None,
                 start_callback: Optional[Callable[[str], str]] = None,
                 stop_callback: Optional[Callable[[str], None]] = None,
                 metrics_provider: Optional[Callable[[], Dict]] = None,
                 config_snapshot: Optional[Dict] = None) -> None:
        self._host: str = config.get('host', '0.0.0.0')
        self._port: int = config.get('port', 8765)
        self._lock = threading.Lock()
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        _Handler._session_registry = {}
        _Handler._registry_lock = self._lock
        _Handler._auth = (
            _AuthManager(auth_cfg) if (auth_cfg or {}).get('enabled') else None
        )
        _Handler._queue = []
        _Handler._queue_lock = threading.Lock()
        _Handler._code_registry = {}
        _Handler._code_lock = threading.Lock()
        _Handler.start_callback = start_callback
        _Handler.stop_callback = stop_callback
        _Handler.metrics_provider = metrics_provider
        _Handler.config_snapshot = config_snapshot or {}

    def register_session(self, session) -> None:
        with self._lock:
            while True:
                code = f'{secrets.randbelow(1_000_000):06d}'
                if code not in _Handler._code_registry:
                    break
            session.code = code
            _Handler._code_registry[code] = session.id
            _Handler._session_registry[session.id] = session

    def unregister_session(self, session_id: str) -> None:
        with self._lock:
            sess = _Handler._session_registry.pop(session_id, None)
            if sess and sess.code:
                _Handler._code_registry.pop(sess.code, None)

    def start(self) -> None:
        self._server = _ThreadedHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name='webvtt-http',
        )
        self._thread.start()
        print(f'[WebVTT] API server at http://localhost:{self._port}/')

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    def on_caption(self, text: str, start: float, end: float,
                   session_id: str) -> None:
        sess = _Handler._session_registry.get(session_id)
        if sess is None:
            return
        sess.writer.add_cue(text, start, end)
        sess.recent_cues.append({'ts': time.strftime('%H:%M:%S'), 'text': text})
        cue_json = json.dumps({
            'text': text,
            'lines': [ln for ln in text.split('\n') if ln.strip()],
            'start': f'{start:.3f}',
            'end': f'{end:.3f}',
        })
        dead = []
        with sess.sse_lock:
            clients = list(sess.sse_clients)
        for client in clients:
            if not client.push_cue(cue_json):
                dead.append(client)
        if dead:
            with sess.sse_lock:
                for c in dead:
                    if c in sess.sse_clients:
                        sess.sse_clients.remove(c)
