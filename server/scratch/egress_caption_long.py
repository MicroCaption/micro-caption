#!/usr/bin/env python3
"""
Longer caption reproduction: run ~35s of continuous text through the real
EgressPipeline and decode the FULL 608 stream to spot where it desyncs.

Run from server/:  python3 scratch/egress_caption_long.py
"""
import os
import subprocess
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from microcaption.io.egress import EgressPipeline, CaptionInjector

Gst.init(None)
ING = 'rtmp://localhost:1935/live/capLONG'
OUT = '/tmp/cap_long.flv'

LINE = ('the city council meeting will now come to order please rise for the '
        'pledge of allegiance the first item on the agenda is the budget review '
        'for fiscal year twenty twenty six ')


class FullDecoder:
    """Roll-up decoder that records every completed row (full transcript)."""
    def __init__(self):
        self.win = deque([''], maxlen=2)
        self.history = []
        self._last = None

    def feed(self, pairs):
        for p0, p1 in pairs:
            b1, b2 = p0 & 0x7F, p1 & 0x7F
            if b1 == 0:
                continue
            if 0x10 <= b1 <= 0x1F:
                pair = (b1, b2)
                if pair == self._last:
                    self._last = None
                    continue
                self._last = pair
                if 0x10 <= b1 <= 0x17 and 0x40 <= b2 <= 0x7F:
                    continue                      # PAC
                if b2 == 0x2D:                    # CR
                    self.history.append(self.win[-1])
                    self.win.append('')
                elif b2 == 0x2C:                  # EDM
                    self.win = deque([''], maxlen=2)
            else:
                self._last = None
                if 0x20 <= b1 <= 0x7F:
                    self.win[-1] += chr(b1)
                if 0x20 <= b2 <= 0x7F:
                    self.win[-1] += chr(b2)

    def transcript(self):
        return self.history + [r for r in self.win if r]


def push_source():
    return subprocess.Popen(
        ['gst-launch-1.0', '-e', 'videotestsrc', 'is-live=true', '!',
         'video/x-raw,framerate=60/1,width=1920,height=1080', '!',
         'x264enc', 'tune=zerolatency', 'bitrate=4000', 'key-int-max=30', '!',
         'h264parse', '!', 'flvmux', 'name=m', 'streamable=true', '!',
         'rtmp2sink', f'location={ING}',
         'audiotestsrc', 'is-live=true', '!', 'audioconvert', '!',
         'voaacenc', '!', 'aacparse', '!', 'm.'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def extract_pairs(flv):
    pairs = []
    p = Gst.parse_launch(
        f'filesrc location={flv} ! flvdemux name=d '
        'd. ! queue ! h264parse ! video/x-h264,stream-format=byte-stream,alignment=au ! '
        'appsink name=v emit-signals=true sync=false  d. ! queue ! fakesink sync=false')

    def parse(au):
        i = au.find(b'GA94')
        while i != -1:
            cc = au[i + 5] & 0x1F
            o = i + 7
            for _ in range(cc):
                if o + 3 > len(au):
                    break
                v, b1, b2 = au[o], au[o + 1], au[o + 2]
                o += 3
                if (v & 0x04) and (v & 0x03) == 0:
                    pairs.append((b1, b2))
            i = au.find(b'GA94', i + 4)

    def on_v(s):
        smp = s.emit('pull-sample')
        if smp:
            ok, mi = smp.get_buffer().map(Gst.MapFlags.READ)
            if ok:
                parse(bytes(mi.data)); smp.get_buffer().unmap(mi)
        return Gst.FlowReturn.OK
    p.get_by_name('v').connect('new-sample', on_v)
    loop = GLib.MainLoop(); bus = p.get_bus(); bus.add_signal_watch()
    bus.connect('message::eos', lambda b, m: loop.quit())
    bus.connect('message::error', lambda b, m: loop.quit())
    p.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(30, lambda: (loop.quit(), False)[1])
    loop.run(); p.set_state(Gst.State.NULL)
    return pairs


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    src = push_source(); time.sleep(3)
    inj = CaptionInjector({'rollup_rows': 2})
    eg = EgressPipeline(ING, OUT, {'fps': 30, 'caption_delay_ms': 0,
                                   'max_width': 1920, 'max_height': 1080,
                                   'max_fps': 60}, injector=inj)
    eg.start()
    words = (LINE * 6).split()
    i = 0
    t_end = time.time() + 32
    while time.time() < t_end and i < len(words):
        inj.push_text(' '.join(words[i:i + 2]))   # ~2 words every 0.5s ≈ realistic
        i += 2
        time.sleep(0.5)
    time.sleep(1.5)
    eg.stop(); src.terminate()
    try:
        src.wait(3)
    except Exception:
        src.kill()

    pairs = extract_pairs(OUT)
    dec = FullDecoder(); dec.feed(pairs)
    rows = dec.transcript()
    print(f'pushed {i} words, extracted {len(pairs)} pairs, {len(rows)} rows')
    print('--- decoded transcript (each line = one roll-up row) ---')
    for r in rows:
        print(f'  |{r}|')
    decoded = ' '.join(rows).lower()
    expected_words = (LINE * 6).split()[:i]
    # count how many expected words survived in order before any corruption
    good = 0
    dt = decoded.split()
    for w in expected_words:
        if w in dt:
            good += 1
    print(f'\nwords intact: {good}/{i}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
