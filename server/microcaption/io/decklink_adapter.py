import re
import threading
import time
from typing import Optional

import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from .base import InputOutputManager, AudioChunk

_SAMPLE_RATE = 16000


class DeckLinkAdapter(InputOutputManager):
    """
    Audio input from a Blackmagic DeckLink SDI sub-device (embedded SDI audio).

    Selected via the pseudo-URL ``sdi://<index>`` where ``<index>`` is the
    DeckLink ``device-number`` (a Duo 2 exposes four sub-devices, 0–3). The
    video stream is decoded and drained to a fakesink — the card needs to lock
    to the incoming video to deliver embedded audio — while the embedded audio
    is converted to F32LE/16 kHz/mono and pushed to the ASR pipeline, exactly
    like the other adapters.

    Requires the Blackmagic Desktop Video driver + runtime (see CLAUDE.md / the
    SDI plan); without it GStreamer's decklink elements can't open the card.

    Pipeline:
      decklinkvideosrc device-number=N ! fakesink            (SDI lock + drain)
      decklinkaudiosrc device-number=N connection=embedded
        ! audioconvert ! audioresample
        ! F32LE/16k/mono ! appsink
    """

    SCHEMES = ('sdi://',)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        Gst.init(None)
        # Index may arrive as an sdi:// url or as a plain config device_index.
        self._index: int = self._parse_index(config)
        self._pipeline: Optional[Gst.Pipeline] = None
        self._loop: Optional[GLib.MainLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._ended = False
        self._start_time: float = 0.0

    @classmethod
    def handles(cls, url: str) -> bool:
        return url.lower().startswith(cls.SCHEMES)

    @staticmethod
    def _parse_index(config: dict) -> int:
        url = str(config.get('url', '') or '')
        m = re.match(r'sdi://(\d+)', url, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return int(config.get('device_index', 0))

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        print(f'[DeckLink] Opening SDI sub-device {self._index}')
        pipeline_str = (
            f'decklinkvideosrc device-number={self._index} '
            f'! fakesink sync=false '
            f'decklinkaudiosrc device-number={self._index} connection=embedded '
            f'! audioconvert '
            f'! audioresample '
            f'! audio/x-raw,format=F32LE,channels=1,rate={_SAMPLE_RATE} '
            f'! appsink name=sink emit-signals=true max-buffers=20 drop=true'
        )
        self._pipeline = Gst.parse_launch(pipeline_str)
        sink = self._pipeline.get_by_name('sink')
        sink.connect('new-sample', self._on_new_sample)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::error', self._on_bus_error)
        bus.connect('message::eos', self._on_eos)
        bus.connect('message::element', self._on_element)

        self._loop = GLib.MainLoop()
        self._start_time = time.time()
        self._running = True
        self._pipeline.set_state(Gst.State.PLAYING)

        self._thread = threading.Thread(
            target=self._loop.run, daemon=True, name='gst-decklink-loop'
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop and self._loop.is_running():
            self._loop.quit()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE

    @property
    def start_epoch(self) -> float:
        """Wall-clock time (epoch seconds) of pipeline t=0 — used by the client
        to align the video clock to caption timings (see /api/session/sync)."""
        return self._start_time

    @property
    def is_running(self) -> bool:
        return self._running

    # ── internals ─────────────────────────────────────────────────────────────

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
                target=cb, args=(reason,), daemon=True, name='decklink-end-notify',
            ).start()

    def _on_element(self, bus, message) -> None:
        # decklinkvideosrc posts a 'no-signal' element message when the SDI
        # input has no (or a lost) source. Treat as an error end so the session
        # surfaces it instead of sitting silent.
        s = message.get_structure()
        name = s.get_name() if s else ''
        if 'no-signal' in (name or ''):
            print(f'[DeckLink] No SDI signal on sub-device {self._index}')
            self._notify_end('error')
            self.stop()

    def _on_bus_error(self, bus, message) -> None:
        err, debug = message.parse_error()
        print(f'[DeckLink] GStreamer error: {err.message}')
        if debug:
            print(f'[DeckLink] Debug: {debug}')
        self._notify_end('error')
        self.stop()

    def _on_eos(self, bus, message) -> None:
        print('[DeckLink] Input ended (EOS).')
        self._notify_end('eos')
        self.stop()
