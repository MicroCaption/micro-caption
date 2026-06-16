import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, List


@dataclass
class Session:
    id: str
    url: str
    video_id: str
    source_type: str      # 'youtube' | 'stream' | 'sdi'
    start_time: float     # time.monotonic()
    writer: Any           # WebVTTWriter (per-session)
    adapter: Any = None   # YouTubeAdapter — set after CDN resolve
    pipeline: Any = None  # ASRPipeline — set after backend starts
    recent_cues: Deque = field(default_factory=lambda: deque(maxlen=50))
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
