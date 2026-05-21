import numpy as np


class WhisperBackend:
    """faster-whisper ASR backend — used as fallback when Parakeet fails."""

    name = 'whisper'

    def __init__(self, config: dict) -> None:
        self._model_size: str = config.get('model', 'large-v3-turbo')
        self._device: str = config.get('device', 'cuda')
        self._compute_type: str = config.get('compute_type', 'float16')
        self._model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel  # type: ignore
        self._model = WhisperModel(
            self._model_size,
            device=self._device,
            compute_type=self._compute_type,
        )
        print(f'[Whisper] Loaded {self._model_size} on {self._device}')

    def transcribe(self, samples: np.ndarray) -> str:
        if self._model is None:
            raise RuntimeError('WhisperBackend.load() has not been called')
        segments, _ = self._model.transcribe(samples, language='en', beam_size=1)
        return ' '.join(seg.text for seg in segments).strip()

    def unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
