import time
from ..asr.pipeline import CaptionResult


class TerminalSink:
    """Writes captions to stdout with optional timestamp and backend tag."""

    def __init__(self, config: dict) -> None:
        self._show_ts = config.get('show_timestamps', True)
        self._show_backend = config.get('show_backend', True)
        self._t0 = time.time()

    def on_caption(self, result: CaptionResult) -> None:
        parts = []
        if self._show_ts:
            elapsed = time.time() - self._t0
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = elapsed % 60
            parts.append(f'[{h:02d}:{m:02d}:{s:05.2f}]')
        if self._show_backend:
            parts.append(f'({result.backend})')
        parts.append(result.text)
        print(' '.join(parts), flush=True)
