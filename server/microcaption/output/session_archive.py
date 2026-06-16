"""
Persistence for ended sessions' captioning history.

Each ended session is written to ``logs/sessions/<id>.json`` and summarised in
``logs/sessions/index.json`` so the Logs page can show historical streams and
their full caption history after the live session is gone. The on-disk format
matches the richer archives produced by earlier accuracy/diarization runs
(``records`` / ``accuracy_summary`` are left empty when those aren't computed).
"""
import json
import os
import threading
import time

_LOCK = threading.Lock()

# Fields mirrored into index.json (the lightweight per-session summary list).
_INDEX_FIELDS = ('id', 'url', 'video_id', 'source_type', 'created_at',
                 'status', 'accuracy', 'segment_count', 'cue_count')


def _dir() -> str:
    return os.path.join('logs', 'sessions')


def safe_id(sid: str) -> 'str | None':
    """Only allow simple alphanumeric ids — never a path fragment."""
    return sid if (sid and sid.isalnum()) else None


def write_session(sess) -> None:
    """Persist an ended session's cues + metadata. Best-effort (never raises)."""
    sid = safe_id(getattr(sess, 'id', ''))
    if not sid:
        return
    try:
        cues = sess.writer.all_cue_data() if sess.writer else []
        created = getattr(getattr(sess, 'adapter', None), 'start_epoch', None) or time.time()
        rec = {
            'id': sess.id,
            'url': sess.url,
            'video_id': sess.video_id,
            'source_type': sess.source_type,
            'created_at': created,
            'status': sess.status or 'ended',
            'accuracy': None,
            'segment_count': 0,
            'cue_count': len(cues),
            'accuracy_summary': {},
            'records': [],
            'cues': cues,
        }
        d = _dir()
        os.makedirs(d, exist_ok=True)
        with _LOCK:
            with open(os.path.join(d, sid + '.json'), 'w') as f:
                json.dump(rec, f)
            _update_index_locked(rec)
    except Exception as exc:
        print(f'[Archive] failed to write session {sid}: {exc}')


def _update_index_locked(rec: dict) -> None:
    idx_path = os.path.join(_dir(), 'index.json')
    try:
        idx = json.load(open(idx_path)) if os.path.exists(idx_path) else []
    except Exception:
        idx = []
    idx = [e for e in idx if e.get('id') != rec['id']]
    idx.append({k: rec.get(k) for k in _INDEX_FIELDS})
    with open(idx_path, 'w') as f:
        json.dump(idx, f)


def read_index() -> list:
    p = os.path.join(_dir(), 'index.json')
    try:
        return json.load(open(p)) if os.path.exists(p) else []
    except Exception:
        return []


def read_session(sid: str) -> 'dict | None':
    sid = safe_id(sid)
    if not sid:
        return None
    p = os.path.join(_dir(), sid + '.json')
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None
