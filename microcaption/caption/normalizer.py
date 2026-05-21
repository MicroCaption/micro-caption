import re
import textwrap
from dataclasses import dataclass
from typing import List


@dataclass
class NormalizedCaption:
    lines: List[str]
    raw: str


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
