from dataclasses import dataclass
import numpy as np


@dataclass
class VADResult:
    is_speech: bool
    rms: float
    energy_db: float


class EnergyVAD:
    """
    Simple energy-based Voice Activity Detector with hangover.

    Conservative by design — keeps the gate open for `hangover_frames`
    windows after energy drops below threshold to avoid clipping sentence endings.
    """

    def __init__(self, rms_threshold: float = 1e-4, hangover_frames: int = 5) -> None:
        self._threshold = rms_threshold
        self._hangover = hangover_frames
        self._counter = 0  # frames remaining in hangover

    def process(self, samples: np.ndarray) -> VADResult:
        rms = float(np.sqrt(np.mean(samples ** 2)))
        energy_db = 20.0 * np.log10(max(rms, 1e-10))

        if rms >= self._threshold:
            self._counter = self._hangover
            is_speech = True
        elif self._counter > 0:
            self._counter -= 1
            is_speech = True
        else:
            is_speech = False

        return VADResult(is_speech=is_speech, rms=rms, energy_db=energy_db)

    def reset(self) -> None:
        self._counter = 0
