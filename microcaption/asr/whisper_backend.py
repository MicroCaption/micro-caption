import re

import numpy as np

# Whisper frequently hallucinates these phrases during silence or low-energy
# audio.  Normalized (lowercase, punctuation stripped) for matching.
_SILENCE_HALLUCINATIONS: frozenset[str] = frozenset({
    'thank you',
    'thank you very much',
    'thank you so much',
    'thanks for watching',
    'thank you for watching',
    'thank you for listening',
    'please subscribe',
    'like and subscribe',
    'see you next time',
    'bye',
    'bye bye',
})


def _is_hallucination(text: str) -> bool:
    """Return True if text is a known Whisper silence hallucination."""
    normalized = re.sub(r'[^\w\s]', '', text.lower()).strip()
    if normalized in _SILENCE_HALLUCINATIONS:
        return True
    # Catch repetitive single-token spam: "thank thank thank you you you"
    words = normalized.split()
    if len(words) >= 3 and len(set(words)) == 1:
        return True
    return False


class WhisperBackend:
    """faster-whisper ASR backend — used as fallback when Parakeet fails."""

    name = 'whisper'

    def __init__(self, config: dict) -> None:
        self._model_size: str = config.get('model', 'large-v3-turbo')
        self._device: str = config.get('device', 'cuda')
        self._compute_type: str = config.get('compute_type', 'float16')
        # Segments where no_speech_prob exceeds this are discarded.
        # Lower = more aggressive silence filtering. Default 0.5 is tighter
        # than faster-whisper's own default of 0.6.
        self._no_speech_threshold: float = config.get('no_speech_threshold', 0.5)
        self._model = None

    def load(self) -> None:
        import glob
        import os
        from faster_whisper import WhisperModel  # type: ignore
        # Search the HF hub cache for any snapshot dir matching this model name so
        # we load from disk and skip the network check entirely.
        cache_dir = os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub')
        model_key = self._model_size.replace('/', '--')
        pattern = os.path.join(cache_dir, f'*{model_key}*', 'snapshots', '*')
        matches = sorted(glob.glob(pattern))
        if matches:
            model_path = matches[-1]
            print(f'[Whisper] Using cached model at {model_path}')
        else:
            model_path = self._model_size
        self._model = WhisperModel(
            model_path,
            device=self._device,
            compute_type=self._compute_type,
        )
        print(f'[Whisper] Loaded {self._model_size} on {self._device}')

    def transcribe(self, samples: np.ndarray) -> str:
        if self._model is None:
            raise RuntimeError('WhisperBackend.load() has not been called')
        segments, _ = self._model.transcribe(
            samples,
            language='en',
            beam_size=1,
            # Disable context carry-over between chunks: prevents a single
            # hallucinated phrase from reinforcing itself across windows.
            condition_on_previous_text=False,
            no_speech_threshold=self._no_speech_threshold,
        )
        parts = []
        for seg in segments:
            if seg.no_speech_prob > self._no_speech_threshold:
                continue
            if _is_hallucination(seg.text):
                continue
            parts.append(seg.text)
        return ' '.join(parts).strip()

    def unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
