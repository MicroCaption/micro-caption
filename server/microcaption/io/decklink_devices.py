"""
Read-only DeckLink (Blackmagic) device enumeration and live signal status.

Used by the dashboard SDI picker (`GET /api/sdi/devices`) — independent of any
session. Talks to the card purely through GStreamer's `decklink` plugin, so it
needs the Blackmagic Desktop Video driver + runtime installed. Until that's in
place every call degrades gracefully to an empty device list / 'unknown' status
rather than raising.

  list_devices()        → [{index, persistent_id, label, connection,
                            can_input, can_output}, ...]
  device_status(index)  → {signal_locked: bool|None, mode: str, cached: bool}

`device_status` is cached with a short TTL because probing briefly opens the
sub-device, and the dashboard polls.
"""

import threading
import time
from typing import Optional

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

_STATUS_TTL_SEC = 2.0
_PROBE_TIMEOUT_SEC = 1.5

_lock = threading.Lock()
_status_cache: dict[int, tuple[float, dict]] = {}   # index → (expires_at, status)
_gst_inited = False


def _ensure_gst() -> None:
    global _gst_inited
    if not _gst_inited:
        Gst.init(None)
        _gst_inited = True


def _struct_get(props, key, default=None):
    """Best-effort read of a key from a Gst.Structure (handles missing key)."""
    if props is None:
        return default
    try:
        if props.has_field(key):
            return props.get_value(key)
    except Exception:
        pass
    return default


def _enumerate(klass: str) -> dict[int, dict]:
    """Return {device-number: {label, persistent_id, connection}} for a klass
    ('Video/Source', 'Audio/Source', 'Video/Sink', ...) restricted to decklink."""
    out: dict[int, dict] = {}
    monitor = Gst.DeviceMonitor.new()
    try:
        monitor.add_filter(klass, None)
    except Exception:
        return out
    monitor.start()
    try:
        for dev in monitor.get_devices() or []:
            klass_str = dev.get_device_class() or ''
            display = dev.get_display_name() or ''
            props = dev.get_properties()
            # The decklink provider tags devices with 'device.api'='decklink'
            # (older builds put 'decklink' in the display name instead).
            api = _struct_get(props, 'device.api', '')
            if 'decklink' not in (api or '').lower() and 'decklink' not in display.lower():
                continue
            idx = _struct_get(props, 'device-number', None)
            if idx is None:
                idx = _struct_get(props, 'device.number', None)
            if idx is None:
                continue
            idx = int(idx)
            out.setdefault(idx, {
                'label': display or f'DeckLink {idx}',
                'persistent_id': _struct_get(props, 'persistent-id', None),
                'connection': _struct_get(props, 'connection', 'sdi'),
            })
    finally:
        monitor.stop()
    return out


def list_devices() -> list[dict]:
    """Enumerate DeckLink sub-devices and whether each can be in / out.

    Never raises — returns [] if the driver/plugin is unavailable."""
    try:
        _ensure_gst()
        with _lock:
            vsrc = _enumerate('Video/Source')
            asrc = _enumerate('Audio/Source')
            vsink = _enumerate('Video/Sink')
    except Exception as exc:
        print(f'[DeckLink] enumerate failed: {exc}')
        return []

    indices = sorted(set(vsrc) | set(asrc) | set(vsink))
    devices = []
    for idx in indices:
        meta = vsrc.get(idx) or asrc.get(idx) or vsink.get(idx) or {}
        devices.append({
            'index': idx,
            'persistent_id': meta.get('persistent_id'),
            'label': meta.get('label', f'DeckLink {idx}'),
            'connection': meta.get('connection', 'sdi'),
            'can_input': idx in vsrc or idx in asrc,
            'can_output': idx in vsink,
        })
    return devices


def _probe_signal(index: int) -> dict:
    """Briefly open the sub-device's video input to check SDI lock.

    Returns {'signal_locked': bool|None, 'mode': str}. A successful preroll
    (PAUSED reached) means the card locked to an incoming signal; a no-signal
    element message or timeout means no lock. None = couldn't determine
    (driver/plugin missing)."""
    pipeline = None
    try:
        pipeline = Gst.parse_launch(
            f'decklinkvideosrc device-number={index} ! '
            f'fakesink sync=false name=sink'
        )
    except Exception:
        return {'signal_locked': None, 'mode': ''}

    bus = pipeline.get_bus()
    locked: Optional[bool] = None
    mode = ''
    deadline = time.time() + _PROBE_TIMEOUT_SEC
    try:
        pipeline.set_state(Gst.State.PAUSED)
        while time.time() < deadline:
            msg = bus.timed_pop_filtered(
                int(0.2 * Gst.SECOND),
                Gst.MessageType.ERROR | Gst.MessageType.ELEMENT
                | Gst.MessageType.ASYNC_DONE | Gst.MessageType.STATE_CHANGED,
            )
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ELEMENT:
                s = msg.get_structure()
                name = s.get_name() if s else ''
                if 'no-signal' in (name or ''):
                    locked = False
                    break
            elif msg.type == Gst.MessageType.ERROR:
                locked = False
                break
            elif msg.type == Gst.MessageType.ASYNC_DONE:
                # Preroll completed → a valid input format was detected.
                locked = True
                # Pull the negotiated caps for the mode label.
                sink = pipeline.get_by_name('sink')
                pad = sink.get_static_pad('sink') if sink else None
                caps = pad.get_current_caps() if pad else None
                if caps and caps.get_size() > 0:
                    st = caps.get_structure(0)
                    w = st.get_value('width') if st.has_field('width') else '?'
                    h = st.get_value('height') if st.has_field('height') else '?'
                    mode = f'{w}x{h}'
                break
    except Exception:
        locked = None
    finally:
        pipeline.set_state(Gst.State.NULL)
    return {'signal_locked': locked, 'mode': mode}


def device_status(index: int) -> dict:
    """Cached SDI lock status for a sub-device. Safe to poll."""
    now = time.time()
    with _lock:
        cached = _status_cache.get(index)
        if cached and cached[0] > now:
            return {**cached[1], 'cached': True}
    try:
        _ensure_gst()
        status = _probe_signal(index)
    except Exception as exc:
        print(f'[DeckLink] status probe {index} failed: {exc}')
        status = {'signal_locked': None, 'mode': ''}
    with _lock:
        _status_cache[index] = (now + _STATUS_TTL_SEC, status)
    return {**status, 'cached': False}
