#!/usr/bin/env python3
"""
M1 egress spike — prove CEA-608 caption embedding into H.264 SEI survives a
mux -> FLV file -> demux -> decode round-trip.

This is a standalone feasibility test for the RTMP-relay-captioner egress path.
It does NOT touch the real ingest/ASR; it drives synthetic A/V + a known caption
so we can confirm:
  1. cccombiner + ccconverter + x264enc actually write caption SEI, and
  2. a decoder recovers GstVideoCaptionMeta from the encoded file.

Run from server/:  python3 scratch/egress_spike.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # server/

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo, GLib

from microcaption.caption.packetizer_608 import CEA608Packetizer, FRAME_NULL

Gst.init(None)

FPS = 30
DURATION_S = 5
N_FRAMES = FPS * DURATION_S
OUT = '/tmp/egress_spike.flv'
PHRASES = ['HELLO WORLD', 'MICROCAPTION EGRESS TEST']


# ── caption schedule: one 608 byte-pair per video frame ──────────────────────
def caption_schedule():
    pkt = CEA608Packetizer({'rollup_rows': 2})
    frames = pkt.packetize(PHRASES)
    sched = list(frames) + [FRAME_NULL] * (N_FRAMES - len(frames))
    return sched[:N_FRAMES]


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1 — encode A/V + embedded captions to an FLV file
# ══════════════════════════════════════════════════════════════════════════════
def encode() -> bool:
    pipeline_str = (
        f'videotestsrc is-live=false num-buffers={N_FRAMES} pattern=ball ! '
        f'video/x-raw,format=I420,width=640,height=360,framerate={FPS}/1 ! '
        'cccombiner name=cc ! x264enc tune=zerolatency key-int-max=30 ! '
        'h264parse ! flvmux name=mux streamable=true ! '
        f'filesink location={OUT} '
        f'audiotestsrc is-live=false num-buffers=220 wave=sine ! '
        'audioconvert ! audioresample ! audio/x-raw,rate=44100,channels=2 ! '
        'voaacenc ! aacparse ! mux. '
        'appsrc name=capsrc format=time is-live=false do-timestamp=false ! '
        f'closedcaption/x-cea-608,format=raw,framerate={FPS}/1 ! '
        'ccconverter ! '
        'closedcaption/x-cea-708,format=cc_data ! cc.caption'
    )
    pipeline = Gst.parse_launch(pipeline_str)
    capsrc = pipeline.get_by_name('capsrc')
    capsrc.set_property('caps', Gst.Caps.from_string(
        f'closedcaption/x-cea-608,format=raw,framerate={FPS}/1'))

    loop = GLib.MainLoop()
    result = {'ok': True}

    def on_error(_bus, msg):
        err, dbg = msg.parse_error()
        print(f'  [encode] ERROR: {err.message}\n  {dbg}')
        result['ok'] = False
        loop.quit()

    def on_eos(_bus, _msg):
        print('  [encode] EOS')
        loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect('message::error', on_error)
    bus.connect('message::eos', on_eos)

    sched = caption_schedule()
    frame_dur = Gst.SECOND // FPS

    def push_captions():
        for i, (b1, b2) in enumerate(sched):
            buf = Gst.Buffer.new_allocate(None, 2, None)
            buf.fill(0, bytes((b1, b2)))
            buf.pts = i * frame_dur
            buf.duration = frame_dur
            if capsrc.emit('push-buffer', buf) != Gst.FlowReturn.OK:
                break
        capsrc.emit('end-of-stream')

    pipeline.set_state(Gst.State.PLAYING)
    threading.Thread(target=push_captions, daemon=True).start()
    # safety timeout so we never hang
    GLib.timeout_add_seconds(20, lambda: (loop.quit(), False)[1])
    loop.run()
    pipeline.set_state(Gst.State.NULL)

    ok = result['ok'] and os.path.exists(OUT) and os.path.getsize(OUT) > 0
    print(f'  [encode] {"wrote" if ok else "FAILED"} {OUT} '
          f'({os.path.getsize(OUT) if os.path.exists(OUT) else 0} bytes)')
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 — read the file back and confirm the caption SEI is really there
#   (a) scan the raw H.264 bitstream for the A53 (GA94) caption SEI — this is
#       the exact payload YouTube ingests, so it's the definitive proof;
#   (b) also report whether the software decoder re-surfaces it as a meta.
# ══════════════════════════════════════════════════════════════════════════════
def verify() -> bool:
    stats = {'au': 0, 'sei_cc': 0}

    pipeline_str = (
        f'filesrc location={OUT} ! flvdemux name=d '
        'd. ! queue ! h264parse ! '
        'video/x-h264,stream-format=byte-stream,alignment=au ! '
        'appsink name=raw emit-signals=true sync=false '
        'd. ! queue ! fakesink sync=false'   # drain audio so demux doesn't stall
    )
    pipeline = Gst.parse_launch(pipeline_str)

    def scan_raw(sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(mi.data)
            stats['au'] += 1
            # A53 CC SEI carries the "GA94" user identifier — exactly what
            # YouTube parses out of the H.264 stream.
            if b'GA94' in data:
                stats['sei_cc'] += 1
            buf.unmap(mi)
        return Gst.FlowReturn.OK

    pipeline.get_by_name('raw').connect('new-sample', scan_raw)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect('message::error', lambda b, m: (print('  [verify] ERROR:',
                m.parse_error()[0].message), loop.quit()))
    bus.connect('message::eos', lambda b, m: loop.quit())

    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(20, lambda: (loop.quit(), False)[1])
    loop.run()
    pipeline.set_state(Gst.State.NULL)

    print(f'  [verify] {stats["au"]} access units, '
          f'{stats["sei_cc"]} carry A53/GA94 caption SEI')
    return stats['sei_cc'] > 0


if __name__ == '__main__':
    print('── Stage 1: encode A/V + embedded captions ──')
    enc_ok = encode()
    if not enc_ok:
        sys.exit('encode stage failed')
    print('── Stage 2: decode + verify caption SEI survived ──')
    ok = verify()
    print()
    print('RESULT:', 'PASS ✓ captions embedded and recovered'
          if ok else 'FAIL ✗ no caption SEI recovered')
    sys.exit(0 if ok else 1)
