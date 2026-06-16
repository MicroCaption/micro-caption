"""
SessionStore — filesystem persistence for per-stream caption logs.

Each stream gets one JSON file under ``log_dir`` (default ``logs/sessions/``)
holding its metadata, full cue list, accuracy comparison records, and summary.
A small ``index.json`` lists the most recent streams so the Logs UI can list
them after a server restart. Only the last ``retain_sessions`` streams are kept
on disk; older files are pruned.

Writes are atomic (temp file + os.replace) and throttled to one checkpoint per
``checkpoint_seconds`` per stream, except finalisation which always writes.
"""

import json
import os
import threading
import time
from typing import List, Optional


class SessionStore:
    def __init__(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._dir: str = config.get('log_dir', 'logs/sessions')
        self._retain: int = int(config.get('retain_sessions', 20))
        self._checkpoint_s: float = float(config.get('checkpoint_seconds', 5.0))
        self._lock = threading.Lock()
        self._index: List[dict] = []          # newest first
        self._last_write: dict = {}           # id → monotonic ts of last checkpoint

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        os.makedirs(self._dir, exist_ok=True)
        self._index = self._load_index()

    def _index_path(self) -> str:
        return os.path.join(self._dir, 'index.json')

    def _detail_path(self, session_id: str) -> str:
        # session ids are hex; guard against path traversal regardless.
        safe = ''.join(c for c in session_id if c.isalnum() or c in '-_')
        return os.path.join(self._dir, f'{safe}.json')

    def _load_index(self) -> List[dict]:
        try:
            with open(self._index_path()) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    # ── writes ────────────────────────────────────────────────────────────────

    def register(self, summary: dict, detail: dict) -> None:
        """Create the index entry + detail file for a new stream, then prune."""
        with self._lock:
            self._upsert_index(summary)
            self._write_detail(detail)
            self._write_index()
            self._prune()

    def save(self, summary: dict, detail: dict, final: bool = False) -> None:
        """Checkpoint a stream's log. Throttled unless ``final`` is set."""
        sid = summary.get('id')
        if not sid:
            return
        with self._lock:
            now = time.monotonic()
            if not final:
                last = self._last_write.get(sid, 0.0)
                if now - last < self._checkpoint_s:
                    # Still refresh the lightweight index entry so the list view
                    # stays current; defer the heavier detail write.
                    self._upsert_index(summary)
                    return
            self._last_write[sid] = now
            self._upsert_index(summary)
            self._write_detail(detail)
            self._write_index()

    # ── reads (for the API) ───────────────────────────────────────────────────

    def list_summaries(self) -> List[dict]:
        with self._lock:
            return [dict(s) for s in self._index]

    def get_detail(self, session_id: str) -> Optional[dict]:
        try:
            with open(self._detail_path(session_id)) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    # ── internals (call with lock held) ───────────────────────────────────────

    def _upsert_index(self, summary: dict) -> None:
        sid = summary.get('id')
        self._index = [s for s in self._index if s.get('id') != sid]
        self._index.insert(0, dict(summary))
        self._index.sort(key=lambda s: s.get('created_at', 0), reverse=True)

    def _write_detail(self, detail: dict) -> None:
        self._atomic_write(self._detail_path(detail['id']), detail)

    def _write_index(self) -> None:
        self._atomic_write(self._index_path(), self._index)

    def _prune(self) -> None:
        """Keep only the newest ``retain_sessions`` streams on disk."""
        if len(self._index) <= self._retain:
            return
        for stale in self._index[self._retain:]:
            sid = stale.get('id')
            try:
                os.remove(self._detail_path(sid))
            except FileNotFoundError:
                pass
            self._last_write.pop(sid, None)
        self._index = self._index[:self._retain]

    def _atomic_write(self, path: str, data) -> None:
        tmp = f'{path}.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
