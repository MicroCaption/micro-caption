"""
HTTP server — endpoints:

  GET  /                — Control room: all active sessions + add-stream form
  GET  /player/<id>     — Per-session player page with live caption overlay
  GET  /events/<id>     — Per-session Server-Sent Events stream
  GET  /webvtt/<id>     — Per-session WebVTT download
  POST /start           — Start new session (form redirect → /player/<id>)
  POST /api/start       — Start new session (JSON response → {session_id})
  POST /stop/<id>       — Stop one session
  GET  /monitor         — Aggregate ASR latency + per-session table
  GET  /config          — Active configuration viewer
  GET  /logs            — Combined recent-caption log across all sessions
  GET  /api/sessions    — All sessions as JSON
  GET  /api/metrics     — Global metrics JSON
  GET  /api/config      — Config JSON
  GET  /status          — Backward-compat status JSON
"""

import json
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Callable, Dict, List, Optional

from ..caption.webvtt import WebVTTWriter


def _video_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname in ('youtu.be',):
        return parsed.path.lstrip('/').split('/')[0]
    if parsed.hostname and 'youtube' in parsed.hostname:
        vid = urllib.parse.parse_qs(parsed.query).get('v', [''])[0]
        if vid:
            return vid
        parts = [p for p in parsed.path.split('/') if p]
        if len(parts) >= 2 and parts[0] in ('shorts', 'live', 'embed'):
            return parts[1]
    return ''


def _esc(s: str) -> str:
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


# ── Shared styles ─────────────────────────────────────────────────────────────

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
main { max-width: 1140px; margin: 0 auto; padding: 26px 20px; }
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
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }
.dot-live    { background: #3a9a4a; animation: blink 1.4s infinite; }
.dot-starting{ background: #9a8a2a; animation: blink 1.4s infinite; }
.dot-error   { background: #9a3a3a; }
.dot-idle    { background: #2a2a2a; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.45} }
.live    { color: #3a9a4a; }
.idle    { color: #444; }
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
/* ── Session cards ─────────────────────────────────────────────────────────── */
.sessions-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}
.session-card {
  background: #0f0f0f; border: 1px solid #1c1c1c; border-radius: 6px;
  padding: 18px 20px; border-left: 3px solid #1c1c1c;
}
.session-card.live     { border-left-color: #3a9a4a; }
.session-card.starting { border-left-color: #8a7a2a; }
.session-card.error    { border-left-color: #7a2a2a; }
.card-header { display: flex; align-items: center; gap: 7px; margin-bottom: 9px; }
.card-status {
  font-size: 0.68em; letter-spacing: 0.1em; text-transform: uppercase;
  color: #444;
}
.session-card.live     .card-status { color: #3a9a4a; }
.session-card.starting .card-status { color: #8a7a2a; }
.session-card.error    .card-status { color: #7a2a2a; }
.card-id { color: #383838; font-size: 0.72em; margin-right: auto; }
.badge {
  font-size: 0.62em; padding: 2px 7px; border-radius: 3px;
  text-transform: uppercase; letter-spacing: 0.07em;
}
.badge-yt     { background: #141c2c; color: #3a5a9a; border: 1px solid #1e2e4a; }
.badge-stream { background: #141e1a; color: #2a6a4a; border: 1px solid #1e3028; }
.card-url  { color: #444; font-size: 0.76em; margin-bottom: 7px; word-break: break-all; }
.card-stats{ color: #333; font-size: 0.72em; margin-bottom: 10px; }
.card-cue  { color: #555; font-size: 0.82em; font-style: italic; min-height: 2.2em; margin-bottom: 12px; }
.card-cue.idle { color: #252525; }
.card-footer { display: flex; justify-content: flex-end; gap: 8px; align-items: center; }
.btn-watch {
  color: #3a9a4a; text-decoration: none; font-size: 0.78em;
  padding: 5px 13px; border: 1px solid #1a4a2a; border-radius: 4px;
}
.btn-watch:hover { background: #0f2a1a; }
.btn-stop {
  background: none; border: 1px solid #3a1a1a; color: #633;
  font-size: 0.72em; padding: 4px 10px; border-radius: 3px; cursor: pointer;
}
.btn-stop:hover { border-color: #7a2a2a; color: #a44; }
.empty-state {
  color: #252525; font-size: 0.85em; font-style: italic;
  text-align: center; padding: 50px 20px;
}
.hint { color: #2c2c2c; font-size: 0.72em; }
"""


def _nav(active: str) -> str:
    tabs = [('Streams', '/'), ('Monitor', '/monitor'),
            ('Config', '/config'), ('Logs', '/logs')]
    links = ''.join(
        f'<a href="{h}" class="nav-link'
        f'{" active" if lbl.lower() == active else ""}">{lbl}</a>'
        for lbl, h in tabs
    )
    return (f'<nav><span class="nav-logo">MicroCaption</span>'
            f'{links}</nav>')


def _wrap(title: str, active: str, body: str, script: str = '') -> str:
    st = f'<script>{script}</script>' if script else ''
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<title>MicroCaption — {title}</title>'
            f'<style>{_SHARED_CSS}</style></head><body>'
            f'{_nav(active)}'
            f'<main>{body}</main>{st}</body></html>')


# ── Control room JS ───────────────────────────────────────────────────────────

_CONTROL_ROOM_JS = r"""
function escH(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmtUp(s){
  if(s==null)return'—';
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=Math.floor(s%60);
  return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(sc).padStart(2,'0');
}
function cardHtml(s){
  const badge=s.source_type==='youtube'
    ?'<span class="badge badge-yt">YouTube</span>'
    :'<span class="badge badge-stream">Stream</span>';
  const dotCls=s.status==='live'?'dot-live':s.status==='starting'?'dot-starting':'dot-error';
  const statusLbl=s.status.toUpperCase();
  const urlD=s.url.length>64?s.url.slice(0,64)+'…':s.url;
  const cueHtml=s.last_cue
    ?`<div class="card-cue">“${escH(s.last_cue.slice(0,110)+(s.last_cue.length>110?'…':''))}”</div>`
    :'<div class="card-cue idle">No captions yet…</div>';
  const errHtml=s.error?`<div style="color:#7a2a2a;font-size:.72em;margin-bottom:8px">${escH(s.error)}</div>`:'';
  return `<div class="session-card ${escH(s.status)}">
<div class="card-header">
  <span class="dot ${dotCls}"></span>
  <span class="card-status">${statusLbl}</span>
  <span class="card-id">${escH(s.id)}</span>
  ${badge}
  <button class="btn-stop" onclick="stopSess('${escH(s.id)}')">&#9632;</button>
</div>
<div class="card-url">${escH(urlD)}</div>
<div class="card-stats">${fmtUp(s.uptime_s)}&nbsp;&middot;&nbsp;${s.cue_count}&nbsp;cues</div>
${errHtml}${cueHtml}
<div class="card-footer">
  <a href="/player/${escH(s.id)}" target="_blank" rel="noopener" class="btn-watch">&#9654;&nbsp;Watch live</a>
</div>
</div>`;
}
async function refresh(){
  try{
    const data=await(await fetch('/api/sessions')).json();
    const wrap=document.getElementById('sessions-grid-wrap');
    const cnt=document.getElementById('stream-count');
    if(cnt)cnt.textContent=data.length+' active';
    if(!wrap)return;
    if(!data.length){
      wrap.innerHTML='<div class="sessions-grid"><div class="empty-state">No active streams — add one below.</div></div>';
      return;
    }
    wrap.innerHTML='<div class="sessions-grid">'+data.map(cardHtml).join('')+'</div>';
  }catch(e){}
}
async function stopSess(id){
  try{await fetch('/stop/'+id,{method:'POST'});}catch(e){}
  refresh();
}
const addForm=document.getElementById('add-form');
if(addForm){
  addForm.addEventListener('submit',async e=>{
    e.preventDefault();
    const inp=document.getElementById('url-input');
    const btn=document.getElementById('add-btn');
    const url=inp.value.trim();
    if(!url)return;
    btn.disabled=true; btn.textContent='Starting…';
    try{
      const r=await fetch('/api/start',{
        method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:'url='+encodeURIComponent(url),
      });
      const d=await r.json();
      if(d.session_id){
        inp.value='';
        window.open('/player/'+d.session_id,'_blank','noopener');
        await refresh();
      } else if(d.error){
        alert('Error: '+d.error);
      }
    }catch(e){alert('Request failed: '+e);}
    finally{btn.disabled=false; btn.textContent='▶ Caption';}
  });
}
refresh();
setInterval(refresh,2000);
"""


# ── Player page ───────────────────────────────────────────────────────────────

def _make_player_html(video_id: str, source_url: str,
                      source_type: str, session_id: str) -> str:
    is_yt = (source_type == 'youtube')

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
    #status.live    {{ color: #4c4; }}
    #status.waiting {{ color: #555; }}
    #status.error   {{ color: #c44; }}
    #footer a {{ color: #333; text-decoration: none; }}
    #footer a:hover {{ color: #666; }}
  </style>
{yt_api_tag}</head>
<body>
  <div id="topbar">
    <h1>MicroCaption — Live ASR</h1>
    <div style="display:flex;gap:8px">
      <a href="/">← Control room</a>
      <button id="stop-btn" onclick="stopSession()">&#9632; Stop</button>
    </div>
  </div>
  <div id="player-wrap">
{player_iframe}
    <div id="caption-bar"></div>
  </div>
  <div id="footer">
    <span id="status" class="waiting">Connecting&hellip;</span>
    <span><a href="/webvtt/{session_id}">Download WebVTT</a></span>
  </div>

  <script>
    const bar = document.getElementById('caption-bar');
    const status = document.getElementById('status');

    async function stopSession() {{
      await fetch('/stop/{session_id}', {{method:'POST'}});
      window.location.href = '/';
    }}

    let pendingText   = '';
    let displayedText = '';
    let lastRenderTime = 0;
    let renderTimer   = null;
    let clearTimer    = null;
    let videoPaused   = false;
{yt_pause_js}

    const DWELL_MS      = 5000;
    const MIN_STABLE_MS = 2000;
    const MAX_CHARS     = 32;

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

    const es = new EventSource('/events/{session_id}');

    es.addEventListener('cue', e => {{
      if (videoPaused) return;
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
<body>Session not found &mdash; redirecting&hellip;</body>
</html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    # Session registry — shared across all handler instances via class variables.
    # The registry dict maps session_id → Session; populated by WebVTTServer.
    _session_registry: Dict = {}
    _registry_lock: threading.Lock = None

    # Callbacks set by WebVTTServer.__init__
    start_callback: Optional[Callable[[str], str]] = None  # url → session_id
    stop_callback: Optional[Callable[[str], None]] = None  # session_id → None
    metrics_provider: Optional[Callable[[], Dict]] = None
    config_snapshot: Dict = {}

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    # ── routing ───────────────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split('?')[0]
        parts = [p for p in path.split('/') if p]

        if path in ('/', '/dashboard'):
            self._send(self._page_control_room().encode(), 'text/html')

        elif parts[:1] == ['player'] and len(parts) == 2:
            sess = self._session_registry.get(parts[1])
            if not sess:
                self._send(_NO_SESSION_HTML.encode(), 'text/html')
            else:
                html = _make_player_html(
                    sess.video_id, sess.url, sess.source_type, sess.id)
                self._send(html.encode(), 'text/html')

        elif parts[:1] == ['events'] and len(parts) == 2:
            self._sse_stream(parts[1])

        elif parts[:1] == ['webvtt'] and len(parts) == 2:
            sess = self._session_registry.get(parts[1])
            if not sess:
                self.send_error(404)
            else:
                self._send(sess.writer.flush().encode(), 'text/vtt')

        elif path == '/monitor':
            self._send(self._page_monitor().encode(), 'text/html')
        elif path == '/config':
            self._send(self._page_config().encode(), 'text/html')
        elif path == '/logs':
            self._send(self._page_logs().encode(), 'text/html')

        elif path == '/api/sessions':
            self._send(self._build_sessions_json().encode(), 'application/json')
        elif path == '/api/metrics':
            self._send(self._build_metrics_json().encode(), 'application/json')
        elif path == '/api/config':
            self._send(
                json.dumps(_Handler.config_snapshot, indent=2).encode(),
                'application/json',
            )
        elif path == '/status':
            sessions = list(self._session_registry.values())
            payload = json.dumps({
                'active': len(sessions) > 0,
                'session_count': len(sessions),
                'sessions': [{'id': s.id, 'status': s.status} for s in sessions],
            })
            self._send(payload.encode(), 'application/json')

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?')[0]
        parts = [p for p in path.split('/') if p]

        if path == '/start':
            self._post_start(redirect=True)
        elif path == '/api/start':
            self._post_start(redirect=False)
        elif parts[:1] == ['stop'] and len(parts) == 2:
            self._post_stop(parts[1])
        elif path == '/stop':
            # Deprecated — no session_id; redirect to control room
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.send_error(405)

    # ── POST handlers ─────────────────────────────────────────────────────────

    def _post_start(self, redirect: bool) -> None:
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode(errors='replace')
        params = urllib.parse.parse_qs(body)
        url = params.get('url', [''])[0].strip()

        if not url:
            if redirect:
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                self._send_json({'error': 'no url provided'}, 400)
            return

        session_id = ''
        cb = _Handler.start_callback
        if cb:
            # Access via class to avoid descriptor binding adding spurious 'self'.
            session_id = cb(url)

        if redirect:
            dest = f'/player/{session_id}' if session_id else '/'
            self.send_response(303)
            self.send_header('Location', dest)
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self._send_json({
                'session_id': session_id,
                'player_url': f'/player/{session_id}',
            })

    def _post_stop(self, session_id: str) -> None:
        cb = _Handler.stop_callback
        if cb:
            threading.Thread(
                target=cb, args=(session_id,), daemon=True,
                name=f'session-stop-{session_id}',
            ).start()
        self.send_response(303)
        self.send_header('Location', '/')
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ── page renderers ────────────────────────────────────────────────────────

    def _page_control_room(self) -> str:
        sessions = list(self._session_registry.values())
        stream_count = len(sessions)

        if sessions:
            cards = ''.join(self._session_card_html(s) for s in sessions)
            grid_html = f'<div class="sessions-grid">{cards}</div>'
        else:
            grid_html = (
                '<div class="sessions-grid">'
                '<div class="empty-state">No active streams — add one below.</div>'
                '</div>'
            )

        body = (
            f'<div style="display:flex;align-items:baseline;gap:10px;margin-bottom:14px">'
            f'<h2>Active Streams</h2>'
            f'<span id="stream-count" style="color:#333;font-size:.78em">'
            f'{stream_count} active</span>'
            f'</div>'
            f'<div id="sessions-grid-wrap">{grid_html}</div>'
            f'<h2 style="margin-top:24px">Add Stream</h2>'
            f'<div class="card">'
            f'<form id="add-form" class="row">'
            f'<input type="url" id="url-input"'
            f' placeholder="YouTube, Twitch, peg.tv, or any yt-dlp URL…" required>'
            f'<button id="add-btn" class="btn-primary" type="submit">'
            f'&#9654;&nbsp;Caption</button>'
            f'</form>'
            f'</div>'
            f'<p class="hint" style="margin-top:8px">'
            f'Audio extracted server-side via yt-dlp — no third-party APIs.</p>'
        )
        return _wrap('Streams', 'streams', body, _CONTROL_ROOM_JS)

    def _session_card_html(self, sess) -> str:
        recent = list(sess.recent_cues)
        last_cue = recent[-1]['text'] if recent else ''
        cue_count = sess.writer.cue_count if sess.writer else 0
        up = sess.uptime
        uptime_str = f'{int(up//3600):02d}:{int((up%3600)//60):02d}:{int(up%60):02d}'
        badge_cls = 'badge-yt' if sess.source_type == 'youtube' else 'badge-stream'
        type_label = 'YouTube' if sess.source_type == 'youtube' else 'Stream'
        url_d = _esc((sess.url[:64] + '…') if len(sess.url) > 64 else sess.url)
        dot_cls = {
            'live': 'dot-live', 'starting': 'dot-starting', 'error': 'dot-error',
        }.get(sess.status, 'dot-idle')
        cue_html = (
            f'<div class="card-cue">“{_esc(last_cue[:110])}'
            f'{"…" if len(last_cue) > 110 else ""}”</div>'
            if last_cue else
            '<div class="card-cue idle">No captions yet…</div>'
        )
        err_html = (
            f'<div style="color:#7a2a2a;font-size:.72em;margin-bottom:8px">'
            f'{_esc(sess.error)}</div>'
        ) if sess.error else ''

        return (
            f'<div class="session-card {sess.status}">'
            f'<div class="card-header">'
            f'<span class="dot {dot_cls}"></span>'
            f'<span class="card-status">{sess.status.upper()}</span>'
            f'<span class="card-id">{_esc(sess.id)}</span>'
            f'<span class="badge {badge_cls}">{type_label}</span>'
            f'<button class="btn-stop" onclick="stopSess(\'{_esc(sess.id)}\')">&#9632;</button>'
            f'</div>'
            f'<div class="card-url">{url_d}</div>'
            f'<div class="card-stats">{uptime_str}&nbsp;&middot;&nbsp;{cue_count}&nbsp;cues</div>'
            f'{err_html}{cue_html}'
            f'<div class="card-footer">'
            f'<a href="/player/{_esc(sess.id)}" target="_blank" rel="noopener" class="btn-watch">'
            f'&#9654;&nbsp;Watch live</a>'
            f'</div>'
            f'</div>'
        )

    def _page_monitor(self) -> str:
        sessions = list(self._session_registry.values())
        session_rows = ''.join(
            f'<tr>'
            f'<td>{_esc(s.id)}</td>'
            f'<td>{s.source_type}</td>'
            f'<td style="color:{"#3a9a4a" if s.status=="live" else "#555"}">{s.status}</td>'
            f'<td>—</td>'
            f'<td>{s.writer.cue_count if s.writer else 0}</td>'
            f'<td><a href="/player/{_esc(s.id)}" target="_blank"'
            f' style="color:#3a9a4a">Watch</a></td>'
            f'</tr>'
            for s in sessions
        ) or '<tr><td colspan="6" class="stub">No active sessions.</td></tr>'

        body = (
            '<div class="grid2">'
            '<div><h2>ASR Latency (aggregate)</h2><div class="card">'
            '<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>'
            '<tr><td>Mean</td><td id="lat-mean">—</td></tr>'
            '<tr><td>p95</td><td id="lat-p95">—</td></tr>'
            '<tr><td>Max</td><td id="lat-max">—</td></tr>'
            '<tr><td>Rate</td><td id="lat-rate">—</td></tr>'
            '<tr><td>Total inferences</td><td id="lat-total">—</td></tr>'
            '</tbody></table></div></div>'
            '<div><h2>System</h2><div class="card">'
            '<div><div class="stat-label">Active streams</div>'
            '<div class="stat-val" id="sys-streams">—</div></div>'
            '<div style="margin-top:14px"><div class="stat-label">Total cues</div>'
            '<div class="stat-val" id="sys-cues">—</div></div>'
            '<div style="margin-top:14px"><div class="stat-label">GPU util</div>'
            '<div class="stat-val stub">Phase 2</div></div>'
            '</div></div>'
            '</div>'
            '<h2 style="margin-top:20px">Sessions</h2>'
            '<div class="card">'
            '<table><thead><tr>'
            '<th>ID</th><th>Type</th><th>Status</th><th>Uptime</th><th>Cues</th><th></th>'
            f'</tr></thead><tbody id="sessions-tbody">{session_rows}</tbody></table></div>'
            '<p class="ts" id="refresh-ts" style="margin-top:8px">Refreshing every 2 s…</p>'
        )
        script = (
            'function fms(v){return v==null?"—":v.toFixed(0)+" ms";}'
            'function fup(s){if(s==null)return "—";'
            'const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=Math.floor(s%60);'
            'return String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(sc).padStart(2,"0");}'
            'async function refresh(){'
            'try{'
            'const[m,ss]=await Promise.all(['
            'fetch("/api/metrics").then(r=>r.json()),'
            'fetch("/api/sessions").then(r=>r.json())]);\n'
            'const g=s=>document.getElementById(s);\n'
            'g("lat-mean").textContent=fms(m.asr?.mean_ms);\n'
            'g("lat-p95").textContent=fms(m.asr?.p95_ms);\n'
            'g("lat-max").textContent=fms(m.asr?.max_ms);\n'
            'g("lat-rate").textContent=m.asr?.rate!=null?m.asr.rate.toFixed(2)+"/s":"—";\n'
            'g("lat-total").textContent=m.asr?.total??"—";\n'
            'if(g("sys-streams"))g("sys-streams").textContent=m.session_count??"—";\n'
            'if(g("sys-cues"))g("sys-cues").textContent=m.captions?.total_cues??"—";\n'
            'const tb=g("sessions-tbody");\n'
            'if(tb&&ss.length){'
            'tb.innerHTML=ss.map(s=>`<tr><td>${s.id}</td><td>${s.source_type}</td>'
            '<td style="color:${s.status==="live"?"#3a9a4a":"#555"}">${s.status}</td>'
            '<td>${fup(s.uptime_s)}</td><td>${s.cue_count}</td>'
            '<td><a href="/player/${s.id}" target="_blank" style="color:#3a9a4a">Watch</a></td>'
            '</tr>`).join("");'
            '}else if(tb){'
            'tb.innerHTML=\'<tr><td colspan="6" class="stub">No active sessions.</td></tr>\';'
            '}'
            'g("refresh-ts").textContent="Last update: "+new Date().toLocaleTimeString();'
            '}catch(e){}}'
            'refresh();setInterval(refresh,2000);'
        )
        return _wrap('Monitor', 'monitor', body, script)

    def _page_config(self) -> str:
        cfg_json = _esc(json.dumps(_Handler.config_snapshot, indent=2))
        body = (
            '<h2>Active configuration</h2>'
            f'<div class="card"><pre>{cfg_json}</pre></div>'
            '<p class="stub" style="margin-top:6px">'
            'Per-field editing and hot-reload — Phase 2</p>'
        )
        return _wrap('Config', 'config', body)

    def _page_logs(self) -> str:
        all_cues: list = []
        for s in self._session_registry.values():
            for c in s.recent_cues:
                all_cues.append({
                    'session': s.id,
                    'ts': c['ts'],
                    'text': c['text'],
                })
        all_cues.sort(key=lambda c: c['ts'], reverse=True)
        all_cues = all_cues[:100]

        rows = ''.join(
            f'<tr>'
            f'<td class="ts">{_esc(c["ts"])}</td>'
            f'<td class="ts">{_esc(c["session"])}</td>'
            f'<td>{_esc(c["text"])}</td>'
            f'</tr>'
            for c in all_cues
        ) or '<tr><td colspan="3" class="stub">No captions yet this session.</td></tr>'

        body = (
            '<h2>Recent captions</h2>'
            '<div class="card">'
            '<table><thead><tr><th>Time</th><th>Session</th><th>Caption</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            '<p style="margin-top:8px;font-size:.75em;color:#333">'
            '<span class="stub">WebVTT download per session via the player page &mdash; '
            'SRT / PDF export Phase 2</span></p>'
            '<h2 style="margin-top:20px">Packet logs</h2>'
            '<div class="card"><p class="stub">Live packet log viewer — Phase 2</p></div>'
        )
        return _wrap('Logs', 'logs', body)

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
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
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

    # ── helpers ───────────────────────────────────────────────────────────────

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Public API ────────────────────────────────────────────────────────────────

class WebVTTServer:
    """
    HTTP server managing the multi-stream caption control room and SSE fan-out.

    start_callback(url) → session_id   called on POST /start or /api/start
    stop_callback(session_id)          called on POST /stop/<id>
    on_caption(text, start, end, session_id)  push a caption cue to one session
    register_session(session)          add a Session to the routing table
    unregister_session(session_id)     remove a Session from the routing table
    """

    def __init__(self, config: dict,
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
        _Handler.start_callback = start_callback
        _Handler.stop_callback = stop_callback
        _Handler.metrics_provider = metrics_provider
        _Handler.config_snapshot = config_snapshot or {}

    def register_session(self, session) -> None:
        with self._lock:
            _Handler._session_registry[session.id] = session

    def unregister_session(self, session_id: str) -> None:
        with self._lock:
            _Handler._session_registry.pop(session_id, None)

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
