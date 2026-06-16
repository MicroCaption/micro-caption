import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List


@dataclass
class Session:
    id: str
    url: str
    video_id: str
    source_type: str      # 'youtube' | 'stream'
    start_time: float     # time.monotonic()
    writer: Any           # WebVTTWriter (per-session)
    adapter: Any = None   # YouTubeAdapter — set after CDN resolve
    pipeline: Any = None  # ASRPipeline — set after backend starts
    verifier: Any = None  # AccuracyVerifier — second-pass accuracy scoring
    recent_cues: Deque = field(default_factory=lambda: deque(maxlen=50))
    # Per-segment accuracy comparison records (verifier ref vs live captions).
    accuracy_records: Deque = field(default_factory=lambda: deque(maxlen=500))
    accuracy_summary: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)   # wall-clock, for persisted logs
    sse_clients: List = field(default_factory=list)
    sse_lock: threading.Lock = field(default_factory=threading.Lock)
    status: str = 'starting'   # 'starting' | 'live' | 'ended' | 'error'
    error: str = ''
    code: str = ''             # 6-digit zero-padded, assigned by WebVTTServer.register_session

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def display_url(self) -> str:
        return (self.url[:72] + '…') if len(self.url) > 72 else self.url
