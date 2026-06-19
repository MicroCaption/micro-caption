#!/usr/bin/env python3
"""
Live RTMP loopback proof for the relay captioner.

  fake encoder ──push rtmp──▶ mediamtx :1935 ──pull──▶ EgressPipeline ──▶ FLV file
                                                          (caption inject)

Confirms the full live path (real RTMP ingest, not a file) produces an output
carrying video + audio + embedded A53/GA94 captions.  No GPU/ASR — captions are
fed directly into the injector to isolate the media path.

Run from server/:  python3 scratch/egress_loopback_test.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from microcaption.io.egress import EgressPipeline, CaptionInjector

Gst.init(None)

INGEST = 'rtmp://localhost:1935/live/stream'
OUT = '/tmp/loopback_out.flv'
_HERE = os.path.dirname(__file__)
MEDIAMTX = os.path.join(_HERE, '..', 'vendor', 'mediamtx', 'mediamtx')
MEDIAMTX_CFG = os.path.join(_HERE, 'mediamtx.yml')


def _spawn_pusher():
    """A GStreamer 'OBS' pushing a live test pattern + tone to mediamtx."""
    cmd = [
        'gst-launch-1.0', '-e',
        'videotestsrc', 'is-live=true', 'pattern=ball', '!',
        'video/x-raw,framerate=30/1,width=640,height=360', '!',
        'x264enc', 'tune=zerolatency', 'bitrate=2000', 'key-int-max=30', '!',
        'h264parse', '!', 'flvmux', 'name=m', 'streamable=true', '!',
        'rtmp2sink', f'location={INGEST}',
        'audiotestsrc', 'is-live=true', 'wave=ticks', '!',
        'audioconvert', '!', 'voaacenc', '!', 'aacparse', '!', 'm.',
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def verify() -> bool:
    stats = {'v': 0, 'cc': 0, 'a': 0}
    p = Gst.parse_launch(
        f'filesrc location={OUT} ! flvdemux name=d '
        'd. ! queue ! h264parse ! '
        'video/x-h264,stream-format=byte-stream,alignment=au ! '
        'appsink name=v emit-signals=true sync=false '
        'd. ! queue ! aacparse ! appsink name=a emit-signals=true sync=false')

    def on_v(s):
        smp = s.emit('pull-sample')
        if smp:
            ok, mi = smp.get_buffer().map(Gst.MapFlags.READ)
            if ok:
                stats['v'] += 1
                if b'GA94' in bytes(mi.data):
                    stats['cc'] += 1
                smp.get_buffer().unmap(mi)
        return Gst.FlowReturn.OK

    def on_a(s):
        if s.emit('pull-sample'):
            stats['a'] += 1
        return Gst.FlowReturn.OK

    p.get_by_name('v').connect('new-sample', on_v)
    p.get_by_name('a').connect('new-sample', on_a)
    loop = GLib.MainLoop()
    bus = p.get_bus(); bus.add_signal_watch()
    bus.connect('message::error', lambda b, m: loop.quit())
    bus.connect('message::eos', lambda b, m: loop.quit())
    p.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(20, lambda: (loop.quit(), False)[1])
    loop.run(); p.set_state(Gst.State.NULL)
    print(f'  output: video AUs={stats["v"]}  with GA94 caption SEI={stats["cc"]}  '
          f'audio buffers={stats["a"]}')
    return stats['v'] > 0 and stats['cc'] > 0 and stats['a'] > 0


def main() -> int:
    if not os.path.exists(MEDIAMTX):
        sys.exit('mediamtx not found — run scratch/fetch_mediamtx.sh first')
    if os.path.exists(OUT):
        os.remove(OUT)

    print('── start mediamtx (RTMP server) ──')
    mtx = subprocess.Popen([MEDIAMTX, MEDIAMTX_CFG], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    print('── start fake encoder pushing to mediamtx ──')
    pusher = _spawn_pusher()
    time.sleep(2.5)   # let the publish establish before we pull

    print('── run EgressPipeline pulling the live RTMP, captioning ──')
    injector = CaptionInjector({'rollup_rows': 2})
    eg = EgressPipeline(INGEST, OUT, {'fps': 30, 'caption_delay_ms': 0},
                        injector=injector)
    eg.start()
    injector.push_lines(['LIVE RTMP LOOPBACK', 'CAPTIONS EMBEDDED OK'])
    for i in range(6):
        time.sleep(1.0)
        injector.push_lines([f'TICK {i}'])
    eg.stop()

    print('── teardown ──')
    pusher.terminate(); mtx.terminate()
    try:
        pusher.wait(timeout=5); mtx.wait(timeout=5)
    except Exception:
        pusher.kill(); mtx.kill()

    size = os.path.getsize(OUT) if os.path.exists(OUT) else 0
    print(f'  captioned output: {size} bytes')
    if size == 0:
        print('RESULT: FAIL ✗ no output produced')
        return 1

    print('── verify ──')
    ok = verify()
    print()
    print('RESULT:', 'PASS ✓ live RTMP → captioned restream works'
          if ok else 'FAIL ✗')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
