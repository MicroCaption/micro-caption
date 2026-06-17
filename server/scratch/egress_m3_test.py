#!/usr/bin/env python3
"""
M2 + M3 validation — drive the real EgressPipeline + CaptionInjector.

  1. synthesize a muxed A/V source file (no captions),
  2. run it through EgressPipeline with a CaptionInjector fed real text,
  3. confirm the OUTPUT carries video + audio + A53/GA94 caption SEI.

Run from server/:  python3 scratch/egress_m3_test.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from microcaption.io.egress import EgressPipeline, CaptionInjector

Gst.init(None)

SRC = '/tmp/egress_src.flv'
OUT = '/tmp/egress_out.flv'
FPS = 30
N = 150


def make_source() -> None:
    """A 5 s A/V file with NO captions — stands in for the RTMP ingest."""
    p = Gst.parse_launch(
        f'videotestsrc is-live=false num-buffers={N} pattern=smpte ! '
        f'video/x-raw,format=I420,width=640,height=360,framerate={FPS}/1 ! '
        f'x264enc tune=zerolatency key-int-max={FPS} ! h264parse ! '
        f'flvmux name=m streamable=true ! filesink location={SRC} '
        'audiotestsrc is-live=false num-buffers=220 wave=ticks ! '
        'audioconvert ! audioresample ! audio/x-raw,rate=44100,channels=2 ! '
        'voaacenc ! aacparse ! m.')
    loop = GLib.MainLoop()
    bus = p.get_bus(); bus.add_signal_watch()
    bus.connect('message::error', lambda b, m: (print('src ERR',
                m.parse_error()[0].message), loop.quit()))
    bus.connect('message::eos', lambda b, m: loop.quit())
    p.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(20, lambda: (loop.quit(), False)[1])
    loop.run(); p.set_state(Gst.State.NULL)
    print(f'  source: {os.path.getsize(SRC)} bytes')


_asr_chunks = {'n': 0, 'samples': 0}


def run_egress() -> None:
    injector = CaptionInjector({'rollup_rows': 2})
    eg = EgressPipeline(f'file://{SRC}', OUT, {'fps': FPS}, injector=injector)

    def on_audio(chunk):
        _asr_chunks['n'] += 1
        _asr_chunks['samples'] += len(chunk.samples)

    eg.set_audio_callback(on_audio)   # mimic ASRPipeline.on_audio
    eg.start()
    # feed captions just like the ASR callback will
    injector.push_lines(['LIVE CAPTION TEST', 'RTMP RELAY EGRESS'])
    time.sleep(0.3)
    injector.push_lines(['SECOND CAPTION LINE'])
    # wait for the file to drain to EOS
    for _ in range(200):
        if eg._ended:
            break
        time.sleep(0.05)
    eg.stop()
    print(f'  output: {os.path.getsize(OUT) if os.path.exists(OUT) else 0} bytes')
    print(f'  ASR tap: {_asr_chunks["n"]} chunks, '
          f'{_asr_chunks["samples"]} samples @16k mono')


def verify() -> bool:
    stats = {'v_au': 0, 'cc_au': 0, 'a_buf': 0}
    p = Gst.parse_launch(
        f'filesrc location={OUT} ! flvdemux name=d '
        'd. ! queue ! h264parse ! '
        'video/x-h264,stream-format=byte-stream,alignment=au ! '
        'appsink name=v emit-signals=true sync=false '
        'd. ! queue ! aacparse ! appsink name=a emit-signals=true sync=false')

    def on_v(sink):
        s = sink.emit('pull-sample')
        if not s:
            return Gst.FlowReturn.OK
        ok, mi = s.get_buffer().map(Gst.MapFlags.READ)
        if ok:
            stats['v_au'] += 1
            if b'GA94' in bytes(mi.data):
                stats['cc_au'] += 1
            s.get_buffer().unmap(mi)
        return Gst.FlowReturn.OK

    def on_a(sink):
        if sink.emit('pull-sample'):
            stats['a_buf'] += 1
        return Gst.FlowReturn.OK

    p.get_by_name('v').connect('new-sample', on_v)
    p.get_by_name('a').connect('new-sample', on_a)
    loop = GLib.MainLoop()
    bus = p.get_bus(); bus.add_signal_watch()
    bus.connect('message::error', lambda b, m: (print('verify ERR',
                m.parse_error()[0].message), loop.quit()))
    bus.connect('message::eos', lambda b, m: loop.quit())
    p.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(20, lambda: (loop.quit(), False)[1])
    loop.run(); p.set_state(Gst.State.NULL)

    print(f'  video AUs={stats["v_au"]}  with GA94 caption SEI={stats["cc_au"]}  '
          f'audio buffers={stats["a_buf"]}')
    return stats['v_au'] > 0 and stats['cc_au'] > 0 and stats['a_buf'] > 0


if __name__ == '__main__':
    print('── make source A/V (no captions) ──')
    make_source()
    print('── run EgressPipeline + CaptionInjector ──')
    run_egress()
    print('── verify output: A + V + embedded captions ──')
    ok = verify()
    print()
    print('RESULT:', 'PASS ✓ captioned A/V restream produced'
          if ok else 'FAIL ✗')
    sys.exit(0 if ok else 1)
