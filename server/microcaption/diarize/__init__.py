"""
Speaker diarization — "who is speaking" labelling for captions.

Two tiers, mirroring the accuracy verifier's live/behind-live split:

  • SpeakerChangeDetector (live)  — one embedding per utterance; flags a turn
    change so the live caption shows the CEA-608/708 ">>" speaker-change mark.
  • DiarizationPass (behind live) — re-embeds committed utterances from buffered
    audio and clusters them into stable "SPEAKER N" labels written back onto the
    stored cues. This pass is the source of truth for who-spoke labels; the live
    ">>" marks are best-effort.

The whole layer degrades cleanly: if the speaker-embedding model can't load,
`SpeakerEmbedder.available` is False and both tiers become no-ops, leaving the
caption pipeline exactly as it was.
"""

from .embedder import SpeakerEmbedder, cosine, cosine_distance
from .change import SpeakerChangeDetector
from .clusterer import OnlineSpeakerClusterer
from .second_pass import DiarizationPass

__all__ = [
    'SpeakerEmbedder',
    'SpeakerChangeDetector',
    'OnlineSpeakerClusterer',
    'DiarizationPass',
    'cosine',
    'cosine_distance',
]
