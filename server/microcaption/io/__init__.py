from .base import InputOutputManager, AudioChunk, AudioCallback
from .alsa_adapter import AudioInAlsaAdapter
from .decklink_adapter import DeckLinkAdapter
from .youtube_adapter import YouTubeAdapter
from .stream_adapter import StreamAdapter


def create_adapter(config: dict) -> InputOutputManager:
    """Factory — returns the adapter named in config['io']['adapter']."""
    adapter_name = config.get('io', {}).get('adapter', 'alsa')
    io_cfg = config.get('io', {})

    if adapter_name == 'alsa':
        return AudioInAlsaAdapter(io_cfg.get('alsa', {}))
    elif adapter_name == 'youtube':
        return YouTubeAdapter(io_cfg.get('youtube', {}))
    elif adapter_name == 'decklink':
        return DeckLinkAdapter(io_cfg.get('decklink', {}))
    elif adapter_name == 'stream':
        return StreamAdapter(io_cfg.get('stream', {}))
    else:
        raise ValueError(f"Unknown io.adapter: {adapter_name!r}. Valid: alsa, youtube, decklink, stream")


__all__ = [
    'InputOutputManager', 'AudioChunk', 'AudioCallback',
    'AudioInAlsaAdapter', 'YouTubeAdapter', 'DeckLinkAdapter', 'StreamAdapter',
    'create_adapter',
]
