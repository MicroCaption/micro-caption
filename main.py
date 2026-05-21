#!/usr/bin/env python3
"""
MicroCaption — CTA-708 Real-Time Captioning System
Entry point.

Usage:
  python main.py                          # mic input, default config
  python main.py --config config/settings.test.yaml
  python main.py --mock-asr              # skip model load; inject placeholder text
  python main.py --youtube URL           # caption YouTube video with video overlay
  MC_CONFIG=config/settings.prod.yaml python main.py

Environment variable overrides (prefix MC_):
  MC_IO_ADAPTER       alsa | decklink
  MC_ASR_PRIMARY      parakeet | whisper
  MC_OUTPUT_WEBVTT_PORT   8765
"""

import argparse
import os
import signal
import sys
import threading
import time
import urllib.parse

import yaml


def _extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname in ('youtu.be',):
        return parsed.path.lstrip('/')
    if parsed.hostname and 'youtube' in parsed.hostname:
        vid = urllib.parse.parse_qs(parsed.query).get('v', [''])[0]
        return vid
    return ''


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)

    # Apply MC_* environment variable overrides (dot-path → nested dict)
    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: dict) -> None:
    _overrides = {
        'MC_IO_ADAPTER':           ('io', 'adapter'),
        'MC_ASR_PRIMARY':          ('asr', 'primary'),
        'MC_OUTPUT_WEBVTT_PORT':   ('output', 'webvtt', 'port'),
        'MC_ALSA_DEVICE':          ('io', 'alsa', 'device'),
    }
    for env_key, path in _overrides.items():
        val = os.environ.get(env_key)
        if val is not None:
            node = cfg
            for p in path[:-1]:
                node = node.setdefault(p, {})
            # Coerce to int where the value should be numeric
            try:
                node[path[-1]] = int(val)
            except ValueError:
                node[path[-1]] = val


def _deep_get(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if d != {} else default


def main() -> None:
    parser = argparse.ArgumentParser(description='MicroCaption CTA-708 captioning system')
    parser.add_argument('--config', default=os.environ.get('MC_CONFIG', 'config/settings.yaml'))
    parser.add_argument('--mock-asr', action='store_true',
                        help='Skip model loading; inject placeholder captions (I/O + packet test)')
    parser.add_argument('--list-devices', action='store_true',
                        help='List PulseAudio sources and exit')
    parser.add_argument('--youtube', metavar='URL',
                        help='Caption audio from a YouTube URL instead of the microphone')
    args = parser.parse_args()

    if args.list_devices:
        os.execlp('pactl', 'pactl', 'list', 'sources', 'short')

    cfg = load_config(args.config)

    # --youtube overrides the I/O adapter without touching the config file
    if args.youtube:
        cfg.setdefault('io', {})['adapter'] = 'youtube'
        cfg['io'].setdefault('youtube', {})['url'] = args.youtube

    print(f'[Main] MicroCaption starting — mode: {cfg.get("mode", "test")}')
    print(f'[Main] I/O adapter: {cfg.get("io", {}).get("adapter", "alsa")}')

    # ── Imports (deferred so --list-devices and --help are instant) ──────────
    from microcaption.io import create_adapter
    from microcaption.asr.pipeline import ASRPipeline, CaptionResult
    from microcaption.caption.normalizer import CaptionNormalizer
    from microcaption.caption.packetizer_608 import CEA608Packetizer
    from microcaption.caption.packetizer_708 import DTVCC708Packetizer
    from microcaption.caption.webvtt import WebVTTWriter
    from microcaption.output.terminal_sink import TerminalSink
    from microcaption.output.webvtt_server import WebVTTServer
    from microcaption.output.packet_logger import PacketLogger

    # ── Instantiate components ────────────────────────────────────────────────
    io_adapter = create_adapter(cfg)

    asr_pipeline = ASRPipeline(cfg.get('asr', {})) if not args.mock_asr else None

    normalizer    = CaptionNormalizer(cfg.get('caption', {}).get('normalizer', {}))
    pkt608        = CEA608Packetizer(cfg.get('caption', {}).get('packetizer', {}))
    pkt708        = DTVCC708Packetizer(cfg.get('caption', {}).get('packetizer', {}))

    vtt_writer    = WebVTTWriter()
    terminal      = TerminalSink(cfg.get('output', {}).get('terminal', {}))
    pkt_logger    = PacketLogger(cfg.get('caption', {}).get('packet_log', {}))
    pkt_logger.open()

    webvtt_cfg    = cfg.get('output', {}).get('webvtt', {})
    webvtt_server = None
    video_id = _extract_video_id(args.youtube) if args.youtube else ''
    if webvtt_cfg.get('enabled', True):
        webvtt_server = WebVTTServer(webvtt_cfg, vtt_writer, video_id=video_id)
        webvtt_server.start()

    verbose_packets = cfg.get('output', {}).get('packet_log', {}).get('verbose', False)

    # ── Caption handler (called on ASR worker thread) ─────────────────────────
    def on_caption(result: CaptionResult) -> None:
        norm = normalizer.normalize(result.text)
        if not norm.lines:
            return

        # Terminal
        terminal.on_caption(result)

        # WebVTT
        if webvtt_server:
            webvtt_server.on_caption(
                normalizer.to_display_string(norm),
                result.start_time,
                result.end_time,
            )

        # Packet logs
        frames608 = pkt608.packetize(norm.lines)
        raw708, cc_data = pkt708.packetize(norm.lines)

        dump608 = pkt_logger.log_608(frames608, norm.raw)
        dump708 = pkt_logger.log_708(raw708, cc_data, norm.raw)

        if verbose_packets:
            print(dump608)
            print(dump708)

    # ── Wire up the pipeline ──────────────────────────────────────────────────
    if asr_pipeline:
        asr_pipeline.set_caption_callback(on_caption)
        io_adapter.set_audio_callback(asr_pipeline.on_audio)
        asr_pipeline.start()
    else:
        # Mock mode: inject a placeholder caption every 2 s
        def _mock_injector():
            from microcaption.asr.pipeline import CaptionResult
            t = 0.0
            phrases = [
                'This is a mock caption for pipeline testing.',
                'The audio and packet layers are active.',
                'Waiting for ASR model to be loaded.',
                'CEA-608 and CEA-708 packets are being generated.',
            ]
            idx = 0
            while True:
                r = CaptionResult(text=phrases[idx % len(phrases)],
                                  start_time=t, end_time=t + 2.0, backend='mock')
                on_caption(r)
                t += 2.0
                idx += 1
                time.sleep(2.0)
        threading.Thread(target=_mock_injector, daemon=True, name='mock-asr').start()
        io_adapter.set_audio_callback(lambda _: None)

    # ── Latency reporter ──────────────────────────────────────────────────────
    report_interval = cfg.get('monitor', {}).get('report_interval', 30)
    def _reporter():
        while True:
            time.sleep(report_interval)
            if asr_pipeline:
                print(f'[Monitor] {asr_pipeline.latency_monitor.report()}', file=sys.stderr)
    threading.Thread(target=_reporter, daemon=True, name='latency-reporter').start()

    # ── Start I/O and block ───────────────────────────────────────────────────
    io_adapter.start()
    print('[Main] Pipeline running. Ctrl-C to stop.')
    if webvtt_server:
        port = webvtt_cfg.get('port', 8765)
        if video_id:
            print(f'[Main] Open http://localhost:{port}/ — YouTube video with live ASR captions')
        else:
            print(f'[Main] Open http://localhost:{port}/ — live caption stream')

    stop_event = threading.Event()

    def _sigint(sig, frame):
        print('\n[Main] Shutting down…')
        stop_event.set()

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    stop_event.wait()

    io_adapter.stop()
    if asr_pipeline:
        asr_pipeline.stop()
    if webvtt_server:
        webvtt_server.stop()
    pkt_logger.close()

    print('[Main] Stopped.')


if __name__ == '__main__':
    main()
