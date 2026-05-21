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
        segments, _ = self._model.transcribe(samples, language='en', beam_size=1)
        return ' '.join(seg.text for seg in segments).strip()

    def unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
