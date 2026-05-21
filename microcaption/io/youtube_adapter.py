import subprocess
import threading
import time
from typing import Optional

import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from .base import InputOutputManager, AudioChunk

_SAMPLE_RATE = 16000


class YouTubeAdapter(InputOutputManager):
    """
    Audio input from a YouTube URL via yt-dlp + GStreamer souphttpsrc.

    yt-dlp resolves the video to a CDN audio-only stream URL without
    downloading the file.  GStreamer then decodes and resamples to
    float32 PCM at 16 kHz — identical format to AudioInAlsaAdapter.

    Pipeline: souphttpsrc → decodebin → audioconvert → audioresample → appsink
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        Gst.init(None)
        self._url: str = config.get('url', '')
        self._pipeline: Optional[Gst.Pipeline] = None
        self._loop: Optional[GLib.MainLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._start_time: float = 0.0

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._url:
            raise ValueError('YouTubeAdapter: no URL provided (set io.youtube.url in config)')

        cdn_url = self._resolve_cdn_url(self._url)
        self._pipeline = self._build_pipeline(cdn_url)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::error', self._on_bus_error)
        bus.connect('message::eos', self._on_eos)

        self._loop = GLib.MainLoop()
        self._start_time = time.time()
        self._running = True
        self._pipeline.set_state(Gst.State.PLAYING)

        self._thread = threading.Thread(
            target=self._loop.run, daemon=True, name='gst-yt-loop'
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop and self._loop.is_running():
            self._loop.quit()
        if self._thread:
            self._thread.join(timeout=3.0)

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE

    @property
    def is_running(self) -> bool:
        return self._running

    # ── internals ─────────────────────────────────────────────────────────────

    def _resolve_cdn_url(self, youtube_url: str) -> str:
        print(f'[YouTube] Resolving stream URL via yt-dlp…')
        result = subprocess.run(
            ['yt-dlp', '-g', '-f', 'bestaudio', '--no-playlist',
             '--no-check-certificate', youtube_url],
            capture_output=True, text=True, check=True,
        )
        # yt-dlp may return multiple lines (video + audio for DASH); take the last
        # line which is the audio-only URL when -f bestaudio is used.
        url = result.stdout.strip().split('\n')[-1]
        print(f'[YouTube] CDN URL resolved ({len(url)} chars)')
        return url

    def _build_pipeline(self, cdn_url: str) -> Gst.Pipeline:
        # Escape any quotes in the URL (should be rare but possible)
        safe_url = cdn_url.replace('"', '%22')
        pipeline_str = (
            f'souphttpsrc location="{safe_url}" is-live=false '
            f'! decodebin '
            f'! audioconvert '
            f'! audioresample '
            f'! audio/x-raw,format=F32LE,channels=1,rate={_SAMPLE_RATE} '
            f'! appsink name=sink emit-signals=true max-buffers=20 drop=true'
        )
        pipeline = Gst.parse_launch(pipeline_str)
        sink = pipeline.get_by_name('sink')
        sink.connect('new-sample', self._on_new_sample)
        return pipeline

    def _on_new_sample(self, appsink) -> Gst.FlowReturn:
        sample = appsink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            samples = np.frombuffer(mapinfo.data, dtype=np.float32).copy()
            ts = time.time() - self._start_time
            if self._callback:
                self._callback(AudioChunk(samples=samples, timestamp=ts,
                                          sample_rate=_SAMPLE_RATE))
        finally:
            buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    def _on_bus_error(self, bus, message) -> None:
        err, debug = message.parse_error()
        print(f'[YouTube] GStreamer error: {err.message}')
        if debug:
            print(f'[YouTube] Debug: {debug}')
        self.stop()

    def _on_eos(self, bus, message) -> None:
        print('[YouTube] Stream ended (EOS).')
        self.stop()
