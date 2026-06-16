import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

# Fields we ask nvidia-smi for, in order. Keep in sync with _FIELDS parsing below.
_QUERY = (
    'index,name,utilization.gpu,utilization.memory,'
    'memory.used,memory.total,temperature.gpu,'
    'power.draw,power.limit,fan.speed,clocks.sm,clocks.mem'
)


def _to_float(tok: str) -> Optional[float]:
    tok = tok.strip()
    if not tok or tok in ('[N/A]', 'N/A', '[Not Supported]'):
        return None
    try:
        return float(tok)
    except ValueError:
        return None


class GpuMonitor:
    """Background poller for NVIDIA GPU stats via ``nvidia-smi``.

    Polls fast (default every 0.25 s) and caches the latest snapshot so the HTTP
    metrics handler can read it without spawning a subprocess per request.
    Because the ASR pipeline is bursty (one inference every ``step_duration``),
    a single instantaneous ``nvidia-smi`` reading sawtooths between ~0 % and a
    tall spike. To show the true *duty cycle* we keep a short rolling window of
    the fast samples and expose a smoothed ``*_avg`` alongside each spiky value.

    Degrades gracefully to ``available: False`` when nvidia-smi is missing or
    fails, so the server runs fine on CPU-only / CI hosts.
    """

    def __init__(self, interval: float = 0.25, avg_window_s: float = 5.0) -> None:
        self._interval = interval
        self._win = max(1, round(avg_window_s / interval)) if interval > 0 else 1
        self._lock = threading.Lock()
        self._snapshot: Dict = {'available': False, 'reason': 'not polled yet'}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._smi = shutil.which('nvidia-smi')
        # index → rolling window of recent samples, for duty-cycle averages
        self._util_hist: Dict[int, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self._win))
        self._power_hist: Dict[int, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self._win))

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._smi is None:
            with self._lock:
                self._snapshot = {'available': False, 'reason': 'nvidia-smi not found'}
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name='gpu-monitor',
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Dict:
        with self._lock:
            return dict(self._snapshot)

    # ── internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _roll(hist: Deque[float], value: Optional[float]) -> Optional[float]:
        """Append a sample and return the mean of the rolling window."""
        if value is not None:
            hist.append(value)
        if not hist:
            return None
        return round(sum(hist) / len(hist), 1)

    def _loop(self) -> None:
        while not self._stop.is_set():
            snap = self._query()
            with self._lock:
                self._snapshot = snap
            self._stop.wait(self._interval)

    def _query(self) -> Dict:
        try:
            out = subprocess.run(
                [self._smi, f'--query-gpu={_QUERY}',
                 '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=5,
            )
        except Exception as exc:  # pragma: no cover - depends on host
            return {'available': False, 'reason': f'nvidia-smi error: {exc}'}

        if out.returncode != 0:
            reason = (out.stderr or '').strip() or f'exit {out.returncode}'
            return {'available': False, 'reason': reason}

        gpus: List[Dict] = []
        for line in out.stdout.strip().splitlines():
            cols = [c.strip() for c in line.split(',')]
            if len(cols) < 12:
                continue
            mem_used = _to_float(cols[4])
            mem_total = _to_float(cols[5])
            mem_pct = (
                round(100.0 * mem_used / mem_total, 1)
                if mem_used is not None and mem_total else None
            )
            power_draw = _to_float(cols[7])
            power_limit = _to_float(cols[8])
            power_pct = (
                round(100.0 * power_draw / power_limit, 1)
                if power_draw is not None and power_limit else None
            )
            idx = int(_to_float(cols[0]) or 0)
            util = _to_float(cols[2])

            # Rolling duty-cycle averages over the recent fast samples.
            util_avg = self._roll(self._util_hist[idx], util)
            power_avg = self._roll(self._power_hist[idx], power_draw)
            power_pct_avg = (
                round(100.0 * power_avg / power_limit, 1)
                if power_avg is not None and power_limit else None
            )

            gpus.append({
                'index': idx,
                'name': cols[1],
                'util_gpu': util,
                'util_gpu_avg': util_avg,
                'util_mem': _to_float(cols[3]),
                'mem_used_mb': mem_used,
                'mem_total_mb': mem_total,
                'mem_pct': mem_pct,
                'temp_c': _to_float(cols[6]),
                'power_w': power_draw,
                'power_w_avg': power_avg,
                'power_limit_w': power_limit,
                'power_pct': power_pct,
                'power_pct_avg': power_pct_avg,
                'fan_pct': _to_float(cols[9]),
                'clock_sm_mhz': _to_float(cols[10]),
                'clock_mem_mhz': _to_float(cols[11]),
                'avg_window_s': round(self._win * self._interval, 1),
            })

        if not gpus:
            return {'available': False, 'reason': 'no GPUs reported'}

        return {'available': True, 'ts': time.time(), 'gpus': gpus}
