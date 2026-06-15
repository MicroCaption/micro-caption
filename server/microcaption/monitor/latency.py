import threading
import time
from collections import deque
from typing import Optional


class LatencyMonitor:
    """Rolling-window latency statistics for the ASR inference loop."""

    def __init__(self, window: int = 50) -> None:
        self._window = window
        self._samples: deque[float] = deque(maxlen=window)
        self._lock = threading.Lock()
        self._count = 0
        self._start = time.monotonic()

    def record(self, latency_ms: float) -> None:
        with self._lock:
            self._samples.append(latency_ms)
            self._count += 1

    @property
    def count(self) -> int:
        return self._count

    @property
    def mean_ms(self) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            return sum(self._samples) / len(self._samples)

    @property
    def p95_ms(self) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            s = sorted(self._samples)
            idx = int(len(s) * 0.95)
            return s[min(idx, len(s) - 1)]

    @property
    def max_ms(self) -> Optional[float]:
        with self._lock:
            return max(self._samples) if self._samples else None

    @property
    def inferences_per_second(self) -> float:
        elapsed = time.monotonic() - self._start
        return self._count / elapsed if elapsed > 0 else 0.0

    def report(self) -> str:
        mean = self.mean_ms
        p95 = self.p95_ms
        mx = self.max_ms
        if mean is None:
            return f'ASR latency — no samples yet (total: {self._count})'
        ips = self.inferences_per_second
        return (
            f'ASR latency — mean: {mean:.0f} ms, p95: {p95:.0f} ms, '
            f'max: {mx:.0f} ms, rate: {ips:.2f}/s, total: {self._count}'
        )
