import re
import textwrap
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class NormalizedCaption:
    lines: List[str]
    raw: str


def speaker_marker(speaker: Optional[str] = None,
                   speaker_change: bool = False) -> str:
    """CEA-608/708 speaker prefix for a caption fragment.

    '>> SPEAKER 1: ' when a label is known (behind-live diarization),
    '>> '           for a bare speaker-change mark (live, best-effort),
    ''              when neither applies.
    """
    if speaker:
        return f'>> {speaker}: '
    if speaker_change:
        return '>> '
    return ''


def apply_speaker_marker(lines: List[str], speaker: Optional[str] = None,
                         speaker_change: bool = False) -> List[str]:
    """Prepend the speaker marker to the first display line (for byte-level
    CEA-608/708 packetization). Returns a new list; input is unchanged."""
    prefix = speaker_marker(speaker, speaker_change)
    if not prefix or not lines:
        return list(lines)
    return [prefix + lines[0]] + list(lines[1:])


class CaptionNormalizer:
    """
    Converts raw ASR transcript into broadcast-safe caption lines.

    Applies:
    - Whitespace / punctuation cleanup
    - Line length limiting (default 32 chars — CEA-608 safe title area)
    - Line count limiting (default 2 — roll-up mode standard)
    - Basic profanity-safe pass-through (no filtering in this phase)
    """

    def __init__(self, config: dict) -> None:
        self._max_len: int = config.get('max_line_length', 32)
        self._max_lines: int = config.get('max_lines', 2)

    def normalize(self, text: str) -> NormalizedCaption:
        # Collapse whitespace
        cleaned = re.sub(r'\s+', ' ', text).strip()

        # Hard wrap at max_line_length characters, breaking on word boundaries
        wrapped = textwrap.wrap(cleaned, width=self._max_len, break_long_words=True)

        # Keep only the last max_lines lines (roll-up semantics: oldest scrolls off)
        lines = wrapped[-self._max_lines:] if len(wrapped) > self._max_lines else wrapped

        return NormalizedCaption(lines=lines, raw=cleaned)

    def to_display_string(self, caption: NormalizedCaption) -> str:
        return '\n'.join(caption.lines)
