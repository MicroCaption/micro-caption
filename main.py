#!/usr/bin/env python3
"""
MicroCaption — CTA-708 Real-Time Captioning System
Entry point.

Usage:
  python main.py                    # start server, submit URLs via web UI
  python main.py --mock-asr         # skip model loading; inject placeholder captions
  python main.py --list-devices     # list PulseAudio sources and exit

Open http://localhost:8765/ (or the Tailscale Funnel URL) in a browser,
paste a YouTube URL, and click Caption.

Environment variable overrides (prefix MC_):
  MC_CONFIG               path to config file
  MC_ASR_PRIMARY          parakeet | whisper
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
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname in ('youtu.be',):
        return parsed.path.lstrip('/')
    if parsed.hostname and 'youtube' in parsed.hostname:
        return urllib.parse.parse_qs(parsed.query).get('v', [''])[0]
    return ''


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: dict) -> None:
    _overrides = {
        'MC_IO_ADAPTER':         ('io', 'adapter'),
        'MC_ASR_PRIMARY':        ('asr', 'primary'),
        'MC_OUTPUT_WEBVTT_PORT': ('output', 'webvtt', 'port'),
        'MC_ALSA_DEVICE':        ('io', 'alsa', 'device'),
    }
    for env_key, path in _overrides.items():
        val = os.environ.get(env_key)
        if val is not None:
            node = cfg
            for p in path[:-1]:
                node = node.setdefault(p, {})
            try:
                node[path[-1]] = int(val)
            except ValueError:
                node[path[-1]] = val


def main() -> None:
    parser = argparse.ArgumentParser(description='MicroCaption CTA-708 captioning system')
    parser.add_argument('--config', default=os.environ.get('MC_CONFIG', 'config/settings.yaml'))
    parser.add_argument('--mock-asr', action='store_true',
                        help='Skip model loading; inject placeholder captions')
    parser.add_argument('--list-devices', action='store_true',
                        help='List PulseAudio sources and exit')
    args = parser.parse_args()

    if args.list_devices:
        os.execlp('pactl', 'pactl', 'list', 'sources', 'short')

    cfg = load_config(args.config)

    print(f'[Main] MicroCaption starting — mode: {cfg.get("mode", "test")}')

    # ── Deferred imports ──────────────────────────────────────────────────────
    from microcaption.io.youtube_adapter import YouTubeAdapter
    from microcaption.asr.pipeline import ASRPipeline, CaptionResult
    from microcaption.caption.normalizer import CaptionNormalizer
    from microcaption.caption.packetizer_608 import CEA608Packetizer
    from microcaption.caption.packetizer_708 import DTVCC708Packetizer
    from microcaption.caption.webvtt import WebVTTWriter
    from microcaption.output.terminal_sink import TerminalSink
    from microcaption.output.webvtt_server import WebVTTServer
    from microcaption.output.packet_logger import PacketLogger

    # ── Instantiate caption components ────────────────────────────────────────
    normalizer = CaptionNormalizer(cfg.get('caption', {}).get('normalizer', {}))
    pkt608     = CEA608Packetizer(cfg.get('caption', {}).get('packetizer', {}))
    pkt708     = DTVCC708Packetizer(cfg.get('caption', {}).get('packetizer', {}))
    vtt_writer = WebVTTWriter()
    terminal   = TerminalSink(cfg.get('output', {}).get('terminal', {}))
    pkt_logger = PacketLogger(cfg.get('caption', {}).get('packet_log', {}))
    pkt_logger.open()

    verbose_packets = cfg.get('output', {}).get('packet_log', {}).get('verbose', False)

    # ── ASR pipeline ──────────────────────────────────────────────────────────
    asr_pipeline = ASRPipeline(cfg.get('asr', {})) if not args.mock_asr else None

    # ── Caption handler ───────────────────────────────────────────────────────
    webvtt_server = None  # assigned below after server starts

    def on_caption(result: CaptionResult) -> None:
        norm = normalizer.normalize(result.text)
        if not norm.lines:
            return
        terminal.on_caption(result)
        if webvtt_server:
            webvtt_server.on_caption(
                normalizer.to_display_string(norm),
                result.start_time,
                result.end_time,
            )
        frames608 = pkt608.packetize(norm.lines)
        raw708, cc_data = pkt708.packetize(norm.lines)
        dump608 = pkt_logger.log_608(frames608, norm.raw)
        dump708 = pkt_logger.log_708(raw708, cc_data, norm.raw)
        if verbose_packets:
            print(dump608)
            print(dump708)

    if asr_pipeline:
        asr_pipeline.set_caption_callback(on_caption)
        asr_pipeline.start()
    else:
        def _mock_injector():
            from microcaption.asr.pipeline import CaptionResult
            t, idx = 0.0, 0
            phrases = [
                'This is a mock caption for pipeline testing.',
                'The audio and packet layers are active.',
                'Waiting for ASR model to be loaded.',
                'CEA-608 and CEA-708 packets are being generated.',
            ]
            while True:
                r = CaptionResult(text=phrases[idx % len(phrases)],
                                  start_time=t, end_time=t + 2.0, backend='mock')
                on_caption(r)
                t += 2.0
                idx += 1
                time.sleep(2.0)
        threading.Thread(target=_mock_injector, daemon=True, name='mock-asr').start()

    # ── Session management ────────────────────────────────────────────────────
    _session_lock = threading.Lock()
    _current_adapter: list = [None]  # mutable container so closure can write it

    def _stop_current() -> None:
        """Stop the active adapter (shared by start_session and stop_session)."""
        with _session_lock:
            old = _current_adapter[0]
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass
            _current_adapter[0] = None

    def stop_session() -> None:
        """Stop the active session, return UI to the landing page."""
        print('[Session] Stopping session.')
        _stop_current()
        vtt_writer.reset()
        if asr_pipeline:
            asr_pipeline._last_text = ''

    def start_session(url: str) -> None:
        """Stop any running YouTube adapter and start a fresh one for the given URL."""
        _stop_current()

        video_id = _extract_video_id(url)
        print(f'[Session] Starting new session — video_id={video_id!r}')

        # Reset state for the new session
        vtt_writer.reset()
        if asr_pipeline:
            asr_pipeline._last_text = ''

        # Update the player page immediately so the redirect lands on the right video
        if webvtt_server:
            webvtt_server.set_video_id(video_id)

        # Build and start the new adapter
        yt_cfg = dict(cfg.get('io', {}).get('youtube', {}))
        yt_cfg['url'] = url
        adapter = YouTubeAdapter(yt_cfg)

        if asr_pipeline:
            adapter.set_audio_callback(asr_pipeline.on_audio)
        else:
            adapter.set_audio_callback(lambda _: None)

        with _session_lock:
            _current_adapter[0] = adapter

        adapter.start()

    # ── WebVTT / web server ───────────────────────────────────────────────────
    def _metrics_provider() -> dict:
        m = asr_pipeline.latency_monitor if asr_pipeline else None
        backend = cfg.get('asr', {}).get('primary', 'mock' if not asr_pipeline else '—')
        return {
            'backend': backend,
            'mean_ms': m.mean_ms if m else None,
            'p95_ms': m.p95_ms if m else None,
            'max_ms': m.max_ms if m else None,
            'rate': m.inferences_per_second if m else None,
            'total': m.count if m else 0,
        }

    webvtt_cfg = cfg.get('output', {}).get('webvtt', {})
    if webvtt_cfg.get('enabled', True):
        webvtt_server = WebVTTServer(
            webvtt_cfg,
            vtt_writer,
            start_callback=start_session,
            stop_callback=stop_session,
            metrics_provider=_metrics_provider,
            config_snapshot=cfg,
        )
        webvtt_server.start()
        port = webvtt_cfg.get('port', 8765)
        print(f'[Main] Open http://localhost:{port}/ to submit a YouTube URL')

    # ── Latency reporter ──────────────────────────────────────────────────────
    report_interval = cfg.get('monitor', {}).get('report_interval', 30)
    def _reporter():
        while True:
            time.sleep(report_interval)
            if asr_pipeline:
                print(f'[Monitor] {asr_pipeline.latency_monitor.report()}', file=sys.stderr)
    threading.Thread(target=_reporter, daemon=True, name='latency-reporter').start()

    # ── Block until SIGINT / SIGTERM ──────────────────────────────────────────
    print('[Main] Pipeline ready. Waiting for YouTube URL via web UI. Ctrl-C to stop.')
    stop_event = threading.Event()

    def _sigint(sig, frame):
        print('\n[Main] Shutting down…')
        stop_event.set()

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)
    stop_event.wait()

    with _session_lock:
        if _current_adapter[0]:
            _current_adapter[0].stop()
    if asr_pipeline:
        asr_pipeline.stop()
    if webvtt_server:
        webvtt_server.stop()
    pkt_logger.close()
    print('[Main] Stopped.')


if __name__ == '__main__':
    main()
