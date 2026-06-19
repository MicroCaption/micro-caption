"""
Unit coverage for the caption bridge (CaptionInjector).

These exercise the pure queue/drain logic — no live GStreamer pipeline, no GPU,
no network — so they run in CI alongside the rest of the suite.  The full
encode→SEI→mux round-trip is covered by server/scratch/egress_*.py.
"""
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from microcaption.io.egress import CaptionInjector
from microcaption.caption.packetizer_608 import FRAME_NULL

Gst.init(None)


class _FakeAppsrc:
    """Records buffers pushed via emit('push-buffer', buf)."""
    def __init__(self):
        self.pushed = []

    def emit(self, signal, buf=None):
        if signal == 'push-buffer':
            self.pushed.append(buf)
            return Gst.FlowReturn.OK
        return None


def test_push_lines_enqueues_byte_pairs():
    inj = CaptionInjector({'rollup_rows': 2})
    assert len(inj._queue) == 0
    inj.push_lines(['HELLO WORLD'])
    assert len(inj._queue) > 0
    for pair in inj._queue:
        assert len(pair) == 2
        assert all(0 <= b <= 0xFF for b in pair)


def test_empty_lines_are_noop():
    inj = CaptionInjector({})
    inj.push_lines([])
    inj.push_lines(None)
    assert len(inj._queue) == 0


def test_emit_drains_one_pair_per_frame():
    inj = CaptionInjector({'rollup_rows': 2})
    inj.appsrc = _FakeAppsrc()
    inj.push_lines(['HI'])
    depth = len(inj._queue)
    assert depth >= 1

    # each frame drains exactly one queued pair
    for i in range(depth):
        inj.emit_for_frame(i * inj._frame_dur)
    assert len(inj._queue) == 0
    assert len(inj.appsrc.pushed) == depth
    # buffers are the 2-byte caption pairs (timestamping is done by appsrc
    # do-timestamp on the live clock, not here)
    first = inj.appsrc.pushed[0]
    assert first.get_size() == 2


def test_idle_frames_send_null_padding():
    inj = CaptionInjector({})
    inj.appsrc = _FakeAppsrc()
    assert len(inj._queue) == 0  # nothing queued

    inj.emit_for_frame(0)
    assert len(inj.appsrc.pushed) == 1
    ok, mi = inj.appsrc.pushed[0].map(Gst.MapFlags.READ)
    assert ok
    assert tuple(bytes(mi.data)) == FRAME_NULL
    inj.appsrc.pushed[0].unmap(mi)


def test_flood_is_capped():
    inj = CaptionInjector({'rollup_rows': 2})
    for _ in range(5000):
        inj.push_lines(['THIS IS A LONG LINE OF CAPTION TEXT'])
    # backlog must not grow without bound
    assert len(inj._queue) <= CaptionInjector._MAX_QUEUE + 200
