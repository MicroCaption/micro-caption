from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional
import numpy as np


@dataclass
class AudioChunk:
    samples: np.ndarray   # float32 PCM, mono, 16 kHz
    timestamp: float      # wall-clock seconds since pipeline start
    sample_rate: int = 16000


AudioCallback = Callable[[AudioChunk], None]


class InputOutputManager(ABC):
    """Abstract base for all I/O adapters (ALSA, DeckLink, …)."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._callback: Optional[AudioCallback] = None

    def set_audio_callback(self, callback: AudioCallback) -> None:
        self._callback = callback

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def sample_rate(self) -> int: ...

    @property
    @abstractmethod
    def is_running(self) -> bool: ...
