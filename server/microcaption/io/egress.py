"""
RTMP egress — re-mux a decoded A/V source back out with embedded CEA-608/708
closed captions in the H.264 SEI (the form YouTube Live ingests).

    source URI ──→ uridecodebin ──┬─ video → cccombiner → x264enc → h264parse ─┐
                                  │              ▲                              ├─ flvmux → sink
                                  └─ audio → voaacenc → aacparse ──────────────┘
    captions: CaptionInjector → appsrc → ccconverter → closedcaption/x-cea-708,cc_data
                                                          → cccombiner.caption

Recipe notes (proven in server/scratch/egress_spike.py):
  • Encoder MUST be x264enc — vah264enc can't link the caption pad.
  • cccombiner's caption pad must be fed format=cc_data (NOT cdp); x264enc only
    writes the A53/GA94 caption SEI from cc_data metas.
  • One CEA-608 byte-pair per video frame, PTS-aligned via a video pad probe so
    captions never drift from the picture.
"""
import collections
import queue as _queue
import threading
import time
from typing import Optional

import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from ..caption.packetizer_608 import FRAME_NULL
from ..caption.rollup_608 import RollUp608Encoder, EDM
from .base import AudioChunk

_ASR_SAMPLE_RATE = 16000


# ══════════════════════════════════════════════════════════════════════════════
# Caption bridge (M2)
# ══════════════════════════════════════════════════════════════════════════════
class CaptionInjector:
    """
    Turns caption *text* into a frame-paced CEA-608 byte-pair stream fed into a
    GStreamer ``cccombiner`` via ``appsrc``.

    Thread model: ``push_lines()`` is called from the ASR/assembler thread and
    only enqueues bytes.  A dedicated **pacer thread** (started with the
    pipeline) drains exactly one byte-pair per frame interval and pushes it to
    the appsrc, which ``do-timestamp``s it onto the pipeline running clock so
    cccombiner aligns it with the picture.  A null pair (0x80,0x80) is sent on
    idle frames to keep the cadence.

    The pacer runs on its own thread — NOT the video streaming thread — because
    pushing into cccombiner's caption pad from within its own upstream (video)
    streaming thread deadlock-guards to GST_FLOW_NOT_LINKED on a live pipeline.
    """

    # Cap the backlog so a flood of text can't grow memory without bound.  At
    # 30 fps the drain rate is 30 pairs/s ≈ 60 chars/s, far above real speech.
    _MAX_QUEUE = 2000

    def __init__(self, config: dict) -> None:
        self._encoder = RollUp608Encoder(
            int((config or {}).get('rollup_rows', 2)))
        self._queue: 'collections.deque' = collections.deque()
        self._lock = threading.Lock()
        self._fps = 30
        self._frame_dur = Gst.SECOND // 30  # refined once fps is known
        self.appsrc: Optional[Gst.Element] = None
        self._pusher: Optional[threading.Thread] = None
        self._running = False
        self._drops = 0
        # Running-times of actual video frames, fed by the egress video probe.
        # The pusher emits exactly one caption pair per frame, stamped with the
        # frame's own timestamp — CEA-608 is a frame-locked stream, so this 1:1
        # lockstep (not a free-running wall clock) is what keeps it in sync.
        self._ticks: '_queue.Queue' = _queue.Queue()

    # ── pipeline wiring ───────────────────────────────────────────────────────
    def build_branch(self, pipeline: Gst.Pipeline, fps: int) -> Gst.Element:
        """
        Create the appsrc → ccconverter → cc_data branch, add it to *pipeline*,
        and return the tail element whose src pad links to cccombiner.caption.
        """
        self._fps = max(1, fps)
        self._frame_dur = Gst.SECOND // self._fps

        appsrc = Gst.ElementFactory.make('appsrc', 'cc-appsrc')
        appsrc.set_property('format', Gst.Format.TIME)
        appsrc.set_property('is-live', True)
        # We stamp each buffer with the exact video-frame running time ourselves
        # (see _run), so the caption track is frame-locked to the picture — NOT
        # do-timestamp, whose wall-clock jitter desyncs the stateful 608 stream.
        appsrc.set_property('do-timestamp', False)
        appsrc.set_property('caps', Gst.Caps.from_string(
            f'closedcaption/x-cea-608,format=raw,framerate={fps}/1'))

        conv = Gst.ElementFactory.make('ccconverter', 'cc-conv')
        capsf = Gst.ElementFactory.make('capsfilter', 'cc-caps')
        capsf.set_property('caps', Gst.Caps.from_string(
            'closedcaption/x-cea-708,format=cc_data'))

        for el in (appsrc, conv, capsf):
            pipeline.add(el)
        appsrc.link(conv)
        conv.link(capsf)

        self.appsrc = appsrc
        return capsf

    # ── producer side (ASR thread) ────────────────────────────────────────────
    def push_text(self, text: str) -> None:
        """Append committed ASR text to the roll-up caption stream."""
        if not text or not text.strip():
            return
        pairs = self._encoder.add(text)
        with self._lock:
            if len(self._queue) + len(pairs) > self._MAX_QUEUE:
                # Flood: the pacer (≤ one pair/frame) can't keep up. Dropping
                # mid-sequence would desync the stateful 608 stream forever, so
                # instead clear, erase the screen, and let the encoder re-init
                # cleanly on the next push.
                self._queue.clear()
                self._queue.extend([EDM, EDM])
                self._encoder.reset()
                self._drops += 1
                print(f'[Caption] queue flood — reset (#{self._drops})')
                return
            self._queue.extend(pairs)

    def push_lines(self, lines) -> None:
        """Compat shim: join display rows and append as text."""
        if not lines:
            return
        self.push_text(' '.join(str(l) for l in lines if l))

    # ── frame-locked pusher (own thread) ───────────────────────────────────────
    def start(self) -> None:
        """Start the pusher thread. It emits one caption pair per video frame as
        frames arrive (via on_video_frame), so the caption track stays locked to
        the picture. Runs on its own thread — pushing into cccombiner from the
        video streaming thread deadlock-guards to NOT_LINKED on a live pipeline."""
        if self._running:
            return
        self._running = True
        self._pusher = threading.Thread(target=self._run, daemon=True,
                                        name='cc-pusher')
        self._pusher.start()

    def on_video_frame(self, running_time: int) -> None:
        """Called from the egress video pad probe, once per output frame."""
        if self._running:
            self._ticks.put(running_time)

    def _run(self) -> None:
        while self._running:
            try:
                rt = self._ticks.get(timeout=0.3)
            except _queue.Empty:
                continue
            self.emit_for_frame(rt)

    def emit_for_frame(self, pts=None) -> None:
        """Push exactly one 608 byte-pair (or a null pair) stamped with the
        given video-frame running time."""
        if self.appsrc is None:
            return
        with self._lock:
            pair = self._queue.popleft() if self._queue else FRAME_NULL
        buf = Gst.Buffer.new_allocate(None, 2, None)
        buf.fill(0, bytes(pair))
        if pts is not None and pts != Gst.CLOCK_TIME_NONE:
            buf.pts = pts
        buf.duration = self._frame_dur
        self.appsrc.emit('push-buffer', buf)

    def stop(self) -> None:
        self._running = False
        if self._pusher and self._pusher is not threading.current_thread():
            self._pusher.join(timeout=1.0)
        if self.appsrc is not None:
            self.appsrc.emit('end-of-stream')

    # backward-compat alias
    def close(self) -> None:
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Egress pipeline (M3)
# ══════════════════════════════════════════════════════════════════════════════
class EgressPipeline:
    """
    Decode an A/V source, re-encode with embedded closed captions, and mux back
    out to an RTMP endpoint (or a local file for validation).

    *dest* starting with ``rtmp`` selects ``rtmp2sink``; anything else is treated
    as a local file path (``filesink``).
    """

    def __init__(self, source_uri: str, dest: str, config: dict,
                 injector: Optional[CaptionInjector] = None) -> None:
        Gst.init(None)
        self._source_uri = source_uri
        self._dest = dest
        self._cfg = config or {}
        self._fps = int(self._cfg.get('fps', 30))
        self._delay_ms = int(self._cfg.get('caption_delay_ms', 0))
        # Input spec limits — streams above this are rejected (CPU re-encode is
        # only real-time-safe up to ~1080p60).
        self._max_w = int(self._cfg.get('max_width', 1920))
        self._max_h = int(self._cfg.get('max_height', 1080))
        self._max_fps = int(self._cfg.get('max_fps', 60))
        self.injector = injector

        self._pipeline: Optional[Gst.Pipeline] = None
        self._loop: Optional[GLib.MainLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cccombiner: Optional[Gst.Element] = None
        self._flvmux: Optional[Gst.Element] = None
        self._running = False
        self._ended = False
        self._end_cb = None
        self._audio_cb = None
        self._start_time = 0.0
        self._vsegment: Optional[Gst.Segment] = None

    # Adapter-compatible interface (mirrors io/base.InputOutputManager) so an
    # EgressPipeline can drive the ASR pipeline exactly like an input adapter.
    def set_audio_callback(self, cb) -> None:
        """Receive 16 kHz mono F32 AudioChunks for ASR (tapped off the source)."""
        self._audio_cb = cb

    def set_end_callback(self, cb) -> None:
        self._end_cb = cb

    @property
    def sample_rate(self) -> int:
        return _ASR_SAMPLE_RATE

    @property
    def start_epoch(self) -> float:
        return self._start_time

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._pipeline = self._build()
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::error', self._on_error)
        bus.connect('message::eos', self._on_eos)

        self._loop = GLib.MainLoop()
        self._running = True
        self._start_time = time.time()
        self._pipeline.set_state(Gst.State.PLAYING)
        self._thread = threading.Thread(
            target=self._loop.run, daemon=True, name='egress-loop')
        self._thread.start()
        if self.injector is not None:
            self.injector.start()   # begin pacing captions onto the clock

    def stop(self) -> None:
        self._running = False
        if self.injector:
            self.injector.stop()
        if self._pipeline:
            self._pipeline.send_event(Gst.Event.new_eos())
            # give the muxer a moment to finalise the file/stream
            for _ in range(20):
                if self._ended:
                    break
                time.sleep(0.05)
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop and self._loop.is_running():
            self._loop.quit()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)

    @property
    def is_running(self) -> bool:
        return self._running

    # ── construction ──────────────────────────────────────────────────────────
    def _build(self) -> Gst.Pipeline:
        p = Gst.Pipeline.new('egress')

        src = Gst.ElementFactory.make('uridecodebin', 'src')
        src.set_property('uri', self._source_uri)
        # Force software decoders so frames land in system memory.  Hardware
        # (NVDEC) decode yields video/x-raw(memory:CUDAMemory) which the CPU
        # caption/encode chain can't negotiate, and keeps one consistent path.
        try:
            src.set_property('force-sw-decoders', True)
        except Exception:
            pass

        # ── video chain: queue → normalize → cccombiner → x264 → parse → mux ──
        # Plain decoupling queue — do NOT use min-threshold-time to delay: on a
        # live stream it gates output whenever the buffer dips below the
        # threshold, producing chunky/stuttering A/V. (Captions therefore lag
        # by ~ASR latency, which is acceptable for live captioning.)
        vqueue = self._mk('queue', 'vqueue', max_size_buffers=0,
                          max_size_bytes=0, max_size_time=2 * Gst.SECOND)
        # Normalize arbitrary decoded video (any pixel format / framerate the
        # encoder sends) to a fixed format + framerate.  Without this, a real
        # feed at e.g. 29.97/60 fps or NV12 fails caps negotiation into
        # cccombiner/x264enc (the caption pad is locked to fps/1) and the whole
        # pipeline errors with not-negotiated (-4).
        vconv = Gst.ElementFactory.make('videoconvert', 'vconv')
        vrate = Gst.ElementFactory.make('videorate', 'vrate')
        vscale = Gst.ElementFactory.make('videoscale', 'vscale')
        vcaps = Gst.ElementFactory.make('capsfilter', 'vcaps')
        vcaps.set_property('caps', Gst.Caps.from_string(
            f'video/x-raw,format=I420,framerate={self._fps}/1'))
        cccombiner = Gst.ElementFactory.make('cccombiner', 'cc')
        # Give the aggregator slack so a caption pair pushed just after its video
        # frame passes the probe still lands on that frame.
        try:
            cccombiner.set_property('latency', 300 * Gst.MSECOND)
        except Exception:
            pass
        # speed-preset MUST be fast enough to encode in real time or the live
        # source backs up and the RTMP server drops us ("reader too slow").
        # veryfast (OBS's own default) is a good quality/speed balance and is
        # easily real-time at 1080p on this 24-thread CPU; bitrate should match
        # the incoming stream so we don't throw away quality on re-encode.
        x264 = self._mk('x264enc', 'venc', tune='zerolatency',
                        speed_preset=self._cfg.get('x264_speed_preset', 'veryfast'),
                        key_int_max=self._fps * 2,
                        bitrate=int(self._cfg.get('bitrate', 6000)))
        h264parse = Gst.ElementFactory.make('h264parse', 'vparse')

        # ── audio: tee → [egress: AAC, delayed] + [ASR tap: 16k mono F32] ─────
        atee = Gst.ElementFactory.make('tee', 'atee')
        # egress audio branch (delayed to match the held-back video)
        aqueue = self._mk('queue', 'aqueue', max_size_buffers=0,
                          max_size_bytes=0, max_size_time=2 * Gst.SECOND)
        aconv = Gst.ElementFactory.make('audioconvert', 'aconv')
        ares = Gst.ElementFactory.make('audioresample', 'ares')
        acaps = Gst.ElementFactory.make('capsfilter', 'acaps')
        # Keep the source rate (48 kHz) — don't resample to 44.1 kHz (lossy and
        # pointless; YouTube ingests 48 kHz).
        acaps.set_property('caps', Gst.Caps.from_string(
            'audio/x-raw,rate=48000,channels=2'))
        aenc = self._mk('voaacenc', 'aenc',
                        bitrate=int(self._cfg.get('audio_bitrate', 192000)))
        aparse = Gst.ElementFactory.make('aacparse', 'aparse')
        # ASR branch (NOT delayed — ASR must see audio as early as possible so
        # captions are ready by the time the delayed picture catches up)
        asr_q = self._mk('queue', 'asr-q', leaky=2)   # leaky=downstream
        asr_conv = Gst.ElementFactory.make('audioconvert', 'asr-conv')
        asr_res = Gst.ElementFactory.make('audioresample', 'asr-res')
        asr_caps = Gst.ElementFactory.make('capsfilter', 'asr-caps')
        asr_caps.set_property('caps', Gst.Caps.from_string(
            f'audio/x-raw,format=F32LE,channels=1,rate={_ASR_SAMPLE_RATE}'))
        asr_sink = Gst.ElementFactory.make('appsink', 'asr-sink')
        asr_sink.set_property('emit-signals', True)
        asr_sink.set_property('max-buffers', 20)
        asr_sink.set_property('drop', True)
        asr_sink.set_property('sync', False)

        # ── muxer + sink ──────────────────────────────────────────────────────
        flvmux = Gst.ElementFactory.make('flvmux', 'mux')
        flvmux.set_property('streamable', True)
        sink = self._make_sink()

        for el in (src, vqueue, vconv, vrate, vscale, vcaps,
                   cccombiner, x264, h264parse,
                   atee, aqueue, aconv, ares, acaps, aenc, aparse,
                   asr_q, asr_conv, asr_res, asr_caps, asr_sink,
                   flvmux, sink):
            p.add(el)

        # static links (everything except the dynamic uridecodebin pads)
        vqueue.link(vconv)
        vconv.link(vrate)
        vrate.link(vscale)
        vscale.link(vcaps)
        vcaps.link(cccombiner)
        cccombiner.link(x264)
        x264.link(h264parse)
        h264parse.link(flvmux)
        # audio egress branch
        atee.link(aqueue)
        aqueue.link(aconv)
        aconv.link(ares)
        ares.link(acaps)
        acaps.link(aenc)
        aenc.link(aparse)
        aparse.link(flvmux)
        # audio ASR-tap branch
        atee.link(asr_q)
        asr_q.link(asr_conv)
        asr_conv.link(asr_res)
        asr_res.link(asr_caps)
        asr_caps.link(asr_sink)
        asr_sink.connect('new-sample', self._on_asr_sample)
        flvmux.link(sink)

        # caption branch — the injector's pusher emits one pair per video frame,
        # driven by a probe on the (post-videorate, 30 fps) video just before
        # cccombiner so captions stay frame-locked to the picture.
        if self.injector is not None:
            tail = self.injector.build_branch(p, self._fps)
            tail.get_static_pad('src').link(
                cccombiner.get_request_pad('caption'))
            vcaps.get_static_pad('src').add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                self._on_video_frame)

        self._cccombiner = cccombiner
        self._flvmux = flvmux
        src.connect('pad-added', self._on_pad_added)
        return p

    def _on_pad_added(self, _src, pad: Gst.Pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.to_string() if caps else ''
        if name.startswith('video/'):
            if not self._check_video_spec(caps):
                return
            sink = self._pipeline.get_by_name('vqueue').get_static_pad('sink')
            if not sink.is_linked():
                pad.link(sink)
        elif name.startswith('audio/'):
            sink = self._pipeline.get_by_name('atee').get_static_pad('sink')
            if not sink.is_linked():
                pad.link(sink)
        else:
            fake = Gst.ElementFactory.make('fakesink', None)
            fake.set_property('sync', False)
            self._pipeline.add(fake)
            fake.sync_state_with_parent()
            pad.link(fake.get_static_pad('sink'))

    def _check_video_spec(self, caps: Gst.Caps) -> bool:
        """Reject inputs above the supported limit (default 1920x1080@60).
        Returns True if within spec, else fails the session and returns False."""
        st = caps.get_structure(0)
        ok_w, w = st.get_int('width')
        ok_h, h = st.get_int('height')
        ok_fr, fr_n, fr_d = st.get_fraction('framerate')
        fps = (fr_n / fr_d) if (ok_fr and fr_d) else 0
        if (ok_w and w > self._max_w) or (ok_h and h > self._max_h) \
                or (fps > self._max_fps + 0.5):
            self._fail(
                f'Input is {w}x{h}'
                + (f'@{round(fps)}fps' if fps else '')
                + f' — exceeds the {self._max_w}x{self._max_h}@{self._max_fps} '
                'limit. Lower your encoder/OBS output resolution or framerate.')
            return False
        return True

    def _fail(self, message: str) -> None:
        """Reject the stream with a user-facing message and tear down."""
        if self._ended:
            return
        print(f'[Egress] rejected: {message}')
        self._notify_end(message)
        # tear down off the streaming thread (pad-added) to avoid a deadlock
        GLib.idle_add(self._safe_null)

    def _safe_null(self) -> bool:
        self._running = False
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop and self._loop.is_running():
            self._loop.quit()
        return False

    def _on_video_frame(self, _pad, info) -> Gst.PadProbeReturn:
        # Track the segment so PTS → running time is correct (a live source's
        # PTS is not 0-based), then tick the caption pusher once per frame.
        if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
            ev = info.get_event()
            if ev is not None and ev.type == Gst.EventType.SEGMENT:
                self._vsegment = ev.parse_segment()
            return Gst.PadProbeReturn.OK
        buf = info.get_buffer()
        if buf is not None and self.injector is not None:
            pts = buf.pts
            if self._vsegment is not None and pts != Gst.CLOCK_TIME_NONE:
                rt = self._vsegment.to_running_time(Gst.Format.TIME, pts)
            else:
                rt = pts
            self.injector.on_video_frame(rt)
        return Gst.PadProbeReturn.OK

    def _on_asr_sample(self, appsink) -> Gst.FlowReturn:
        sample = appsink.emit('pull-sample')
        if sample is None or self._audio_cb is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            samples = np.frombuffer(mi.data, dtype=np.float32).copy()
            ts = time.time() - self._start_time
            self._audio_cb(AudioChunk(samples=samples, timestamp=ts,
                                      sample_rate=_ASR_SAMPLE_RATE))
        finally:
            buf.unmap(mi)
        return Gst.FlowReturn.OK

    # ── helpers ───────────────────────────────────────────────────────────────
    def _make_sink(self) -> Gst.Element:
        if self._dest.lower().startswith('rtmp'):
            sink = Gst.ElementFactory.make('rtmp2sink', 'sink')
            sink.set_property('location', self._dest)
        else:
            sink = Gst.ElementFactory.make('filesink', 'sink')
            sink.set_property('location', self._dest)
        return sink

    def _delay_ns(self) -> int:
        return self._delay_ms * Gst.MSECOND

    def _q_time(self) -> int:
        # headroom above the delay so the queue never blocks steady state
        return self._delay_ns() + 2 * Gst.SECOND

    @staticmethod
    def _mk(factory: str, name: str, **props) -> Gst.Element:
        el = Gst.ElementFactory.make(factory, name)
        for k, v in props.items():
            el.set_property(k.replace('_', '-'), v)
        return el

    # ── bus handlers ──────────────────────────────────────────────────────────
    def _notify_end(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        if self._end_cb:
            threading.Thread(target=self._end_cb, args=(reason,),
                             daemon=True).start()

    def _on_error(self, _bus, msg) -> None:
        err, dbg = msg.parse_error()
        print(f'[Egress] ERROR: {err.message}  ({dbg})')
        self._notify_end('error')

    def _on_eos(self, _bus, _msg) -> None:
        print('[Egress] EOS')
        self._notify_end('eos')
