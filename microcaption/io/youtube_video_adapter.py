#!/usr/bin/env python3
"""
YouTube Adapter with Browser-based Live Caption Overlay.

Simplest working approach:
  1. yt-dlp extracts direct stream URL
  2. Browser plays video via WebVTT captions

Usage:
  python main.py --youtube "https://www.youtube.com/watch?v=VIDEO_ID"

Open http://localhost:8765/ in your browser to see:
  - Live video stream
  - Caption overlays (WebVTT)
"""

import subprocess
import threading
from typing import Callable, Optional


class YouTubeAdapter:
    """
    Extracts YouTube stream URL and provides captions via WebVTT.

    For full functionality with ASR:
      - Use the standard --alsa adapter with YouTube audio feed
      - OR use browser-based solution where browser plays video
        and receives captions from the WebVTT server
    """

    def __init__(self, config: dict):
        self._url: str = config.get('url', '')
        self._stop_event = threading.Event()
        self._stream_url: Optional[str] = None

    def start(self) -> None:
        """Extract stream URL."""
        if not self._url:
            raise ValueError("No YouTube URL configured")

        try:
            result = subprocess.run(
                ['yt-dlp', '-g', self._url],
                capture_output=True, text=True, check=True,
                timeout=30
            )
            self._stream_url = result.stdout.strip()
            print(f'[YouTube] Extracted stream URL: {self._stream_url}')
            print('[YouTube] Open http://localhost:8765/ in your browser')
            print('[YouTube] You can play the video yourself and captions will overlay')

        except subprocess.CalledProcessError as e:
            print(f'[YouTube] Failed to extract stream: {e}')
        except Exception as e:
            print(f'[YouTube] Error: {e}')

    def set_caption_callback(self, callback: Callable) -> None:
        """Set callback for real-time captions."""
        self._caption_callback = callback

    def set_audio_callback(self, callback: Callable) -> None:
        """Set callback for audio data (not used for YouTube)."""
        self._audio_callback = callback

    def on_audio(self, audio_data: bytes, timestamp: float) -> None:
        """Placeholder - browser plays video directly."""
        pass

    def stop(self) -> None:
        """Stop adapter."""
        self._stop_event.set()


def create_adapter(config: dict) -> YouTubeAdapter:
    """Factory function."""
    return YouTubeAdapter(config)
