"""
WebVTT cue writer.

Produces standard WebVTT output for live stream validation.
Reference: https://www.w3.org/TR/webvtt1/
"""

import time
from typing import List, Optional


def _fmt_time(seconds: float) -> str:
    """Format seconds as WebVTT timestamp HH:MM:SS.mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'


class WebVTTWriter:
    """
    Accumulates caption cues and serialises them as WebVTT.

    Thread-safe for appending; flushing to string is safe from any thread.
    """

    def __init__(self) -> None:
        self._cues: List[str] = []
        self._cue_data: List[dict] = []
        self._cue_index = 1

    def add_cue(self, text: str, start: float, end: float,
                position: str = 'line:90%,end align:center') -> None:
        """Append a WebVTT cue."""
        cue = (
            f'{self._cue_index}\n'
            f'{_fmt_time(start)} --> {_fmt_time(end)} {position}\n'
            f'{text}\n'
        )
        self._cues.append(cue)
        self._cue_data.append({'start': start, 'end': end, 'text': text})
        self._cue_index += 1

    def header(self) -> str:
        return 'WEBVTT\n\n'

    def flush(self) -> str:
        """Return the full WebVTT document (header + all cues)."""
        return self.header() + '\n'.join(self._cues)

    def last_cues(self, n: int = 10) -> str:
        """Return the last n cues as a partial WebVTT document."""
        return self.header() + '\n'.join(self._cues[-n:])

    def new_cues_since(self, index: int) -> List[str]:
        """Return cues added after the given 1-based index."""
        return self._cues[index - 1:] if index <= len(self._cues) else []

    def reset(self) -> None:
        """Clear all cues — call when starting a new video session."""
        self._cues = []
        self._cue_data = []
        self._cue_index = 1

    @property
    def current_time(self) -> float:
        """End timestamp of the last cue, or 0.0 if no cues yet."""
        return self._cue_data[-1]['end'] if self._cue_data else 0.0

    def last_cue_data(self) -> 'dict | None':
        """Most recent structured cue dict, or None."""
        return self._cue_data[-1] if self._cue_data else None

    def all_cue_data(self) -> List[dict]:
        """Snapshot of all structured cues — safe to read from any thread."""
        return list(self._cue_data)

    @property
    def cue_count(self) -> int:
        return len(self._cues)
