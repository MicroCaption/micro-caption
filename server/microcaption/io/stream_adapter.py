import threading
import time
from typing import Optional

import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from .base import InputOutputManager, AudioChunk

_SAMPLE_RATE = 16000


class StreamAdapter(InputOutputManager):
    """
    Audio input from a live A/V stream URL handled directly by GStreamer —
    no yt-dlp resolution step (unlike YouTubeAdapter).

    Supports the push/pull live protocols a broadcast encoder produces:
    rtmp(s)://, rtsp://, srt://. (Stage A: ingest + ASR monitoring only;
    video is decoded and drained, not yet passed through to an egress.)

    Pipeline: uridecodebin → [audio pad] → audioconvert → audioresample
              → F32LE/16k/mono → appsink
                              → [video/other pad] → fakesink (drained)
    """

    # URL schemes this adapter claims, in preference to YouTubeAdapter.
    SCHEMES = ('rtmp://', 'rtmps://', 'rtsp://', 'srt://')

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        Gst.init(None)
        self._url: str = config.get('url', '')
        self._pipeline: Optional[Gst.Pipeline] = None
        self._loop: Optional[GLib.MainLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._audio_sinkpad: Optional[Gst.Pad] = None
        self._running = False
        self._ended = False
        self._start_time: float = 0.0

    @classmethod
    def handles(cls, url: str) -> bool:
        """True if this adapter should own the URL (live-stream scheme)."""
        return url.lower().startswith(cls.SCHEMES)

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._url:
            raise ValueError('StreamAdapter: no URL provided')

        print(f'[Stream] Opening live source: {self._url[:80]}')
        self._pipeline = self._build_pipeline(self._url)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::error', self._on_bus_error)
        bus.connect('message::eos', self._on_eos)

        self._loop = GLib.MainLoop()
        self._start_time = time.time()
        self._running = True
        self._pipeline.set_state(Gst.State.PLAYING)

        self._thread = threading.Thread(
            target=self._loop.run, daemon=True, name='gst-stream-loop'
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop and self._loop.is_running():
            self._loop.quit()
        # Don't join from the GLib loop thread itself (EOS/error handlers run
        # there) — that would raise "cannot join current thread".
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE

    @property
    def start_epoch(self) -> float:
        """Wall-clock time (epoch seconds) of stream-clock t=0 — used by the
        client to align the (delayed) video clock to caption timings."""
        return self._start_time

    @property
    def is_running(self) -> bool:
        return self._running

    # ── internals ─────────────────────────────────────────────────────────────

    def _build_pipeline(self, url: str) -> Gst.Pipeline:
        pipeline = Gst.Pipeline.new('stream')

        src = Gst.ElementFactory.make('uridecodebin', 'src')
        src.set_property('uri', url)
        # Live source: don't accumulate a large rebuffering queue, keep latency low.
        try:
            src.set_property('use-buffering', False)
        except Exception:
            pass

        conv = Gst.ElementFactory.make('audioconvert', 'conv')
        resample = Gst.ElementFactory.make('audioresample', 'resample')
        capsfilter = Gst.ElementFactory.make('capsfilter', 'caps')
        capsfilter.set_property('caps', Gst.Caps.from_string(
            f'audio/x-raw,format=F32LE,channels=1,rate={_SAMPLE_RATE}'))
        sink = Gst.ElementFactory.make('appsink', 'sink')
        sink.set_property('emit-signals', True)
        sink.set_property('max-buffers', 20)
        sink.set_property('drop', True)

        for el in (src, conv, resample, capsfilter, sink):
            pipeline.add(el)
        conv.link(resample)
        resample.link(capsfilter)
        capsfilter.link(sink)

        sink.connect('new-sample', self._on_new_sample)
        # uridecodebin exposes decoded pads dynamically (a muxed stream yields
        # both audio and video) — link audio to ASR, drain everything else.
        self._audio_sinkpad = conv.get_static_pad('sink')
        src.connect('pad-added', self._on_pad_added)
        return pipeline

    def _on_pad_added(self, src, pad: Gst.Pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.to_string() if caps else ''
        if name.startswith('audio/'):
            if self._audio_sinkpad and not self._audio_sinkpad.is_linked():
                pad.link(self._audio_sinkpad)
            return
        # Non-audio (typically video): drain to a fakesink so the demuxer
        # doesn't stall on an unlinked branch.
        fakesink = Gst.ElementFactory.make('fakesink', None)
        fakesink.set_property('sync', False)
        self._pipeline.add(fakesink)
        fakesink.sync_state_with_parent()
        pad.link(fakesink.get_static_pad('sink'))

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

    def _notify_end(self, reason: str) -> None:
        """Fire the end callback exactly once, off the GLib loop thread."""
        if self._ended:
            return
        self._ended = True
        cb = self._end_callback
        if cb:
            threading.Thread(
                target=cb, args=(reason,), daemon=True, name='stream-end-notify',
            ).start()

    def _on_bus_error(self, bus, message) -> None:
        err, debug = message.parse_error()
        print(f'[Stream] GStreamer error: {err.message}')
        if debug:
            print(f'[Stream] Debug: {debug}')
        self._notify_end('error')
        self.stop()

    def _on_eos(self, bus, message) -> None:
        print('[Stream] Stream ended (EOS).')
        self._notify_end('eos')
        self.stop()
