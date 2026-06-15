import time
import threading
from typing import Optional
import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from .base import InputOutputManager, AudioChunk

_SAMPLE_RATE = 16000


class AudioInAlsaAdapter(InputOutputManager):
    """
    GStreamer audio input via PulseAudio/ALSA (pulsesrc).

    Pipeline: pulsesrc → audioconvert → audioresample → F32LE 16kHz mono → appsink
    Works with PipeWire's PulseAudio compatibility layer (Ubuntu 26.04+).
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        Gst.init(None)
        self._pipeline: Optional[Gst.Pipeline] = None
        self._loop: Optional[GLib.MainLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._start_time: float = 0.0
        self._device: str = config.get('device', 'default')

    def start(self) -> None:
        device_clause = f'device="{self._device}" ' if self._device and self._device != 'default' else ''
        pipeline_str = (
            f'pulsesrc {device_clause}'
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

        self._loop = GLib.MainLoop()
        self._start_time = time.time()
        self._running = True
        self._pipeline.set_state(Gst.State.PLAYING)

        self._thread = threading.Thread(target=self._loop.run, daemon=True, name='gst-main-loop')
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop and self._loop.is_running():
            self._loop.quit()
        if self._thread:
            self._thread.join(timeout=3.0)

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
                self._callback(AudioChunk(samples=samples, timestamp=ts, sample_rate=_SAMPLE_RATE))
        finally:
            buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    def _on_bus_error(self, bus, message) -> None:
        err, debug = message.parse_error()
        print(f'[ALSA] GStreamer error: {err.message} ({debug})')
        self.stop()

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE

    @property
    def is_running(self) -> bool:
        return self._running


