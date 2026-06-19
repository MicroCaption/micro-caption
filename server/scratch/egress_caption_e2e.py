#!/usr/bin/env python3
"""
End-to-end caption check: push a live test stream through the REAL
EgressPipeline (with the roll-up encoder), feeding realistic streaming ASR
fragments, then pull the output back, extract the A53/CEA-608 cc_data from the
H.264 SEI, and render it through a roll-up decoder to confirm it's readable.

Uses the already-running mediamtx on :1935 (separate path, won't disturb OBS).
Run from server/:  python3 scratch/egress_caption_e2e.py
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
from scratch.test_rollup_608 import RollUpDecoder

Gst.init(None)
ING = 'rtmp://localhost:1935/live/capE2E'
OUT = '/tmp/cap_e2e.flv'

SCRIPT = ('The city council meeting will now come to order . '
          'Please rise for the pledge of allegiance . '
          'The first item on the agenda is the budget review '
          'for fiscal year twenty twenty six . Motion carries '
          'five to zero . Thank you all for attending .').split()


def push_source():
    return subprocess.Popen(
        ['gst-launch-1.0', '-e', 'videotestsrc', 'is-live=true', 'pattern=ball',
         '!', 'video/x-raw,framerate=30/1,width=1280,height=720', '!',
         'x264enc', 'tune=zerolatency', 'bitrate=4000', 'key-int-max=30', '!',
         'h264parse', '!', 'flvmux', 'name=m', 'streamable=true', '!',
         'rtmp2sink', f'location={ING}',
         'audiotestsrc', 'is-live=true', '!', 'audioconvert', '!',
         'voaacenc', '!', 'aacparse', '!', 'm.'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def extract_608_pairs(flv):
    """Pull field-1 CEA-608 byte pairs out of the A53 SEI in each AU."""
    pairs = []
    p = Gst.parse_launch(
        f'filesrc location={flv} ! flvdemux name=d '
        'd. ! queue ! h264parse ! '
        'video/x-h264,stream-format=byte-stream,alignment=au ! '
        'appsink name=v emit-signals=true sync=false '
        'd. ! queue ! fakesink sync=false')

    def parse_au(au):
        idx = au.find(b'GA94')
        while idx != -1:
            # GA94, user_data_type_code, cc_count byte, em byte, then triples
            cc_count = au[idx + 5] & 0x1F
            off = idx + 7
            for _ in range(cc_count):
                if off + 3 > len(au):
                    break
                v, b1, b2 = au[off], au[off + 1], au[off + 2]
                off += 3
                if (v & 0x04) and (v & 0x03) == 0:   # cc_valid, field-1 608
                    pairs.append((b1, b2))
            idx = au.find(b'GA94', idx + 4)

    def on_v(s):
        smp = s.emit('pull-sample')
        if smp:
            ok, mi = smp.get_buffer().map(Gst.MapFlags.READ)
            if ok:
                parse_au(bytes(mi.data))
                smp.get_buffer().unmap(mi)
        return Gst.FlowReturn.OK

    p.get_by_name('v').connect('new-sample', on_v)
    loop = GLib.MainLoop()
    bus = p.get_bus(); bus.add_signal_watch()
    bus.connect('message::eos', lambda b, m: loop.quit())
    bus.connect('message::error', lambda b, m: loop.quit())
    p.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(20, lambda: (loop.quit(), False)[1])
    loop.run(); p.set_state(Gst.State.NULL)
    return pairs


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    src = push_source()
    time.sleep(3)

    inj = CaptionInjector({'rollup_rows': 2})
    eg = EgressPipeline(ING, OUT,
                        {'fps': 30, 'caption_delay_ms': 0,
                         'max_width': 1920, 'max_height': 1080, 'max_fps': 60},
                        injector=inj)
    eg.start()
    # feed committed fragments a few words at a time, paced like real ASR
    i = 0
    while i < len(SCRIPT):
        inj.push_text(' '.join(SCRIPT[i:i + 3]))
        i += 3
        time.sleep(0.8)
    time.sleep(1.5)
    eg.stop(); src.terminate()
    try:
        src.wait(3)
    except Exception:
        src.kill()

    pairs = extract_608_pairs(OUT)
    dec = RollUpDecoder()
    dec.feed(pairs)
    rows = dec.render()
    print(f'extracted {len(pairs)} field-1 608 pairs')
    print('final visible rows on screen:')
    for r in rows:
        print(f'  |{r}|')
    joined = ' '.join(rows).lower()
    ok = len(pairs) > 0 and any(w in joined for w in ['attending', 'thank', 'motion'])
    print()
    print('RESULT:', 'PASS ✓ captions embed + render readably'
          if ok else 'FAIL ✗ (rows above)')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
