import numpy as np


class ParakeetBackend:
    """NeMo Parakeet-TDT-CTC-0.6B ASR backend."""

    name = 'parakeet'

    def __init__(self, config: dict) -> None:
        self._model_name: str = config.get('model', 'nvidia/parakeet-tdt_ctc-0.6b')
        self._device: str = config.get('device', 'cuda')
        self._model = None

    def load(self) -> None:
        import nemo.collections.asr as nemo_asr  # type: ignore
        self._model = nemo_asr.models.ASRModel.from_pretrained(
            model_name=self._model_name
        )
        self._model = self._model.to(self._device)
        self._model.eval()
        print(f'[Parakeet] Loaded {self._model_name} on {self._device}')

    def transcribe(self, samples: np.ndarray) -> str:
        if self._model is None:
            raise RuntimeError('ParakeetBackend.load() has not been called')
        results = self._model.transcribe([samples])
        if not results:
            return ''
        r = results[0]
        # NeMo returns Hypothesis objects or plain strings depending on version
        return r.text if hasattr(r, 'text') else str(r)

    def unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
