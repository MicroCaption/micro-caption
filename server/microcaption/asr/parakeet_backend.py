import numpy as np


class ParakeetBackend:
    """
    NVIDIA NeMo Parakeet-TDT ASR backend — the primary engine.

    Runs in-process via NeMo (torch). See CLAUDE.md "ASR Environment" for the
    install requirements (torch cu128 + vendored cuDNN 9.19). Whisper is the
    hot-standby fallback when this backend raises (handled by SharedASRBackend).

    Default model is ``nvidia/parakeet-tdt-0.6b-v2`` (public). The older
    ``parakeet-tdt_ctc-0.6b`` is gated on Hugging Face (401 without a token).
    """

    name = 'parakeet'
    # Exposes word-level timestamps, which the streaming LocalAgreement loop in
    # ASRPipeline relies on. Must match WhisperBackend so the pipeline can fail
    # over between the two without changing inference mode.
    supports_words = True

    def __init__(self, config: dict) -> None:
        self._model_name: str = config.get('model', 'nvidia/parakeet-tdt-0.6b-v2')
        self._device: str = config.get('device', 'cuda')
        # Fallback seconds-per-frame if NeMo doesn't return 'start'/'end' in
        # seconds (older versions only give integer offsets). 0.08 s = 10 ms
        # window stride × 8× subsampling for the 0.6b conformer encoder.
        self._time_stride: float = config.get('time_stride', 0.08)
        self._model = None

    def load(self) -> None:
        import logging
        # NeMo is extremely chatty at INFO; quiet it so it doesn't drown the logs.
        logging.getLogger('nemo_logger').setLevel(logging.ERROR)
        import nemo.collections.asr as nemo_asr  # type: ignore
        self._model = nemo_asr.models.ASRModel.from_pretrained(model_name=self._model_name)
        self._model = self._model.to(self._device)
        self._model.eval()
        print(f'[Parakeet] Loaded {self._model_name} on {self._device}')

    def transcribe(self, samples: np.ndarray) -> str:
        if self._model is None:
            raise RuntimeError('ParakeetBackend.load() has not been called')
        results = self._model.transcribe([samples], verbose=False)
        if not results:
            return ''
        r = results[0]
        text = r.text if hasattr(r, 'text') else str(r)
        return (text or '').strip()

    def transcribe_words(self, samples: np.ndarray) -> list[tuple[str, float, float]]:
        """
        Transcribe with word-level timestamps for the streaming pipeline.

        Returns a list of (word, start, end) tuples in order. Each word is
        prefixed with a leading space so callers can join the fragments
        directly (matching WhisperBackend's contract). Timestamps are in
        seconds relative to the start of ``samples``.
        """
        if self._model is None:
            raise RuntimeError('ParakeetBackend.load() has not been called')
        results = self._model.transcribe([samples], timestamps=True, verbose=False)
        if not results:
            return []
        hyp = results[0]
        ts = getattr(hyp, 'timestamp', None) or {}
        entries = ts.get('word') or []
        words: list[tuple[str, float, float]] = []
        for e in entries:
            # NeMo returns dicts; prefer seconds, fall back to offset × stride.
            word = (e.get('word') or '').strip()
            if not word:
                continue
            start = e.get('start')
            end = e.get('end')
            if start is None:
                start = float(e.get('start_offset', 0)) * self._time_stride
            if end is None:
                end = float(e.get('end_offset', 0)) * self._time_stride
            words.append((' ' + word, float(start), float(end)))
        return words

    def unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
