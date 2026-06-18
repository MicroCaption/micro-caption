"""Non-speech audio captioning (WCAG 2.1 AA — captions for meaningful sound).

ASR (Parakeet/Whisper) only transcribes speech. This package runs a parallel
AudioSet tagger over the same PCM stream and emits bracketed captions for
non-speech events ([APPLAUSE], [MUSIC], [LAUGHTER], ...). One fixed model and a
static label→caption map cover every event type — no per-event configuration.
"""
