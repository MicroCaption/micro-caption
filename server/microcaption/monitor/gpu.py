import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional

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

    Runs one query thread (default every 1 s) and caches the latest snapshot so
    the HTTP metrics handler can read it without spawning a subprocess per
    request. Degrades gracefully to ``available: False`` when nvidia-smi is
    missing or fails, so the server runs fine on CPU-only / CI hosts.
    """

    def __init__(self, interval: float = 1.0) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._snapshot: Dict = {'available': False, 'reason': 'not polled yet'}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._smi = shutil.which('nvidia-smi')

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
            gpus.append({
                'index': int(_to_float(cols[0]) or 0),
                'name': cols[1],
                'util_gpu': _to_float(cols[2]),
                'util_mem': _to_float(cols[3]),
                'mem_used_mb': mem_used,
                'mem_total_mb': mem_total,
                'mem_pct': mem_pct,
                'temp_c': _to_float(cols[6]),
                'power_w': power_draw,
                'power_limit_w': power_limit,
                'power_pct': power_pct,
                'fan_pct': _to_float(cols[9]),
                'clock_sm_mhz': _to_float(cols[10]),
                'clock_mem_mhz': _to_float(cols[11]),
            })

        if not gpus:
            return {'available': False, 'reason': 'no GPUs reported'}

        return {'available': True, 'ts': time.time(), 'gpus': gpus}
