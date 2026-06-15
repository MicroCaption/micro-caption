from .base import InputOutputManager


class DeckLinkAdapter(InputOutputManager):
    """
    Placeholder stub for Blackmagic DeckLink SDI audio/video input.

    Phase 2 implementation — requires:
      - Physical DeckLink card installed in PCIe slot
      - Blackmagic Desktop Video driver & SDK headers
      - DeckLink Python bindings or ctypes wrapper

    Do not attempt to instantiate this until the card arrives and
    the SDK is installed. The ALSA adapter is the active backend for
    the DeckLink-bypass phase.
    """

    def start(self) -> None:
        raise NotImplementedError(
            "DeckLinkAdapter is not implemented yet (Phase 2). "
            "Set io.adapter=alsa in config/settings.yaml for current development."
        )

    def stop(self) -> None:
        pass

    @property
    def sample_rate(self) -> int:
        return 48000  # SDI embedded audio is 48 kHz

    @property
    def is_running(self) -> bool:
        return False
