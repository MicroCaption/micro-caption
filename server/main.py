#!/usr/bin/env python3
"""
MicroCaption — CTA-708 Real-Time Captioning System
Entry point.

Usage:
  python main.py                    # start server, submit URLs via web UI
  python main.py --mock-asr         # skip model loading; inject placeholder captions
  python main.py --list-devices     # list PulseAudio sources and exit

Open http://localhost:8765/ in a browser, paste a URL, and click Caption.
Multiple streams can run concurrently — each gets its own card in the control room.

Environment variable overrides (prefix MC_):
  MC_CONFIG               path to config file
  MC_ASR_PRIMARY          parakeet | whisper
  MC_OUTPUT_WEBVTT_PORT   8765
"""

import os
import sys

# ── CUDA library discovery ────────────────────────────────────────────────────
# ctranslate2 dlopens libcublas.so.12 and friends at inference time.  When
# nvidia-*-cu12 wheels are installed inside the venv (not system-wide) their
# lib dirs must be on LD_LIBRARY_PATH before any dlopen call.  We set it here,
# at the very top before any imports, and re-exec so the updated environment is
# visible to the dynamic linker from process start.
def _bootstrap_cuda_libs() -> None:
    if os.environ.get('_MC_CUDA_BOOTSTRAP'):
        return  # already re-exec'd — don't loop
    lib_dirs = []
    for sp in sys.path:
        nvidia_dir = os.path.join(sp, 'nvidia')
        if not os.path.isdir(nvidia_dir):
            continue
        for root, dirs, _ in os.walk(nvidia_dir):
            if 'lib' in dirs:
                lib_dirs.append(os.path.join(root, 'lib'))
        break
    if not lib_dirs:
        return
    existing = os.environ.get('LD_LIBRARY_PATH', '')
    new_path = ':'.join(lib_dirs) + (':' + existing if existing else '')
    if existing == new_path:
        return
    os.environ['LD_LIBRARY_PATH'] = new_path
    os.environ['_MC_CUDA_BOOTSTRAP'] = '1'
    os.environ['PYTHONUNBUFFERED'] = '1'
    os.execve(sys.executable, [sys.executable, '-u'] + sys.argv, os.environ)

_bootstrap_cuda_libs()
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import signal
import threading
import time
import urllib.parse
import uuid

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
        'MC_IO_ADAPTER':           ('io', 'adapter'),
        'MC_ASR_PRIMARY':          ('asr', 'primary'),
        'MC_OUTPUT_WEBVTT_PORT':   ('output', 'webvtt', 'port'),
        'MC_ALSA_DEVICE':          ('io', 'alsa', 'device'),
        'MC_AUTH_CLIENT_ID':       ('auth', 'google_client_id'),
        'MC_AUTH_CLIENT_SECRET':   ('auth', 'google_client_secret'),
        'MC_AUTH_SESSION_SECRET':  ('auth', 'session_secret'),
        'MC_AUTH_REDIRECT_URI':    ('auth', 'redirect_uri'),
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
    from microcaption.session import Session
    from microcaption.io.youtube_adapter import YouTubeAdapter
    from microcaption.io.stream_adapter import StreamAdapter
    from microcaption.io.decklink_adapter import DeckLinkAdapter
    from microcaption.asr.pipeline import ASRPipeline, CaptionResult, SharedASRBackend
    from microcaption.caption.normalizer import CaptionNormalizer
    from microcaption.caption.packetizer_608 import CEA608Packetizer
    from microcaption.caption.packetizer_708 import DTVCC708Packetizer
    from microcaption.caption.webvtt import WebVTTWriter
    from microcaption.output.terminal_sink import TerminalSink
    from microcaption.output.webvtt_server import WebVTTServer
    from microcaption.output.packet_logger import PacketLogger
    from microcaption.monitor import GpuMonitor

    # ── GPU monitor — background nvidia-smi poller (no-op without a GPU) ──────
    _mon_cfg = cfg.get('monitor', {})
    gpu_monitor = GpuMonitor(
        interval=_mon_cfg.get('gpu_interval', 0.25),
        avg_window_s=_mon_cfg.get('gpu_avg_window', 5.0),
    )
    gpu_monitor.start()

    # ── Shared caption components (stateless, one instance each) ─────────────
    normalizer = CaptionNormalizer(cfg.get('caption', {}).get('normalizer', {}))
    pkt608     = CEA608Packetizer(cfg.get('caption', {}).get('packetizer', {}))
    pkt708     = DTVCC708Packetizer(cfg.get('caption', {}).get('packetizer', {}))
    terminal   = TerminalSink(cfg.get('output', {}).get('terminal', {}))
    pkt_logger = PacketLogger(cfg.get('caption', {}).get('packet_log', {}))
    pkt_logger.open()

    verbose_packets = cfg.get('output', {}).get('packet_log', {}).get('verbose', False)

    # ── Shared ASR backend — loaded once, used by all sessions ───────────────
    shared_backend: SharedASRBackend = None
    if not args.mock_asr:
        shared_backend = SharedASRBackend(cfg.get('asr', {}))
        shared_backend.load()

    # ── Session registry ──────────────────────────────────────────────────────
    _sessions: dict = {}          # session_id → Session
    _sessions_lock = threading.Lock()
    webvtt_server = None          # assigned below after server starts

    # ── Per-session caption callback factory ──────────────────────────────────
    def make_caption_callback(session_id: str):
        def on_caption(result: CaptionResult) -> None:
            with _sessions_lock:
                sess = _sessions.get(session_id)
            if not sess:
                return
            # With the streaming backend, result.text is an append-only
            # committed fragment (a few words), not a full window snapshot.
            # The client (CaptionPacer) stitches and paces these; here we still
            # normalize + packetize each fragment for WebVTT / CEA-608/708.
            norm = normalizer.normalize(result.text)
            if not norm.lines:
                return
            terminal.on_caption(result)
            if webvtt_server:
                webvtt_server.on_caption(
                    normalizer.to_display_string(norm),
                    result.start_time,
                    result.end_time,
                    session_id,
                )
            frames608 = pkt608.packetize(norm.lines)
            raw708, cc_data = pkt708.packetize(norm.lines)
            dump608 = pkt_logger.log_608(frames608, norm.raw)
            dump708 = pkt_logger.log_708(raw708, cc_data, norm.raw)
            if verbose_packets:
                print(dump608)
                print(dump708)
        return on_caption

    # ── Mock injector (per session, --mock-asr mode only) ─────────────────────
    def _run_mock(session_id: str, caption_cb) -> None:
        phrases = [
            'This is a mock caption for pipeline testing.',
            'The audio and packet layers are active.',
            'Multiple concurrent streams are supported.',
            'CEA-608 and CEA-708 packets are being generated.',
        ]
        t, idx = 0.0, 0
        while True:
            with _sessions_lock:
                if session_id not in _sessions:
                    break
            r = CaptionResult(
                text=phrases[idx % len(phrases)],
                start_time=t, end_time=t + 2.0, backend='mock',
            )
            caption_cb(r)
            t += 2.0
            idx += 1
            time.sleep(2.0)

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def start_session(url: str) -> str:
        """
        Register a new session synchronously (returns session_id immediately),
        then resolve the CDN URL and start the adapter in a background thread.
        """
        session_id = uuid.uuid4().hex[:8]
        video_id = _extract_video_id(url)
        if DeckLinkAdapter.handles(url):
            source_type = 'sdi'
        elif video_id:
            source_type = 'youtube'
        else:
            source_type = 'stream'
        print(f'[Session] Starting {session_id} — type={source_type} url={url[:80]}')

        writer = WebVTTWriter()
        sess = Session(
            id=session_id,
            url=url,
            video_id=video_id,
            source_type=source_type,
            start_time=time.monotonic(),
            writer=writer,
            status='starting',
        )

        with _sessions_lock:
            _sessions[session_id] = sess
        if webvtt_server:
            webvtt_server.register_session(sess)

        caption_cb = make_caption_callback(session_id)

        def _on_source_end(reason: str) -> None:
            # The source reached end-of-stream (or errored mid-playback). Keep
            # the session registered so its caption log stays viewable, but mark
            # it 'ended' and release the GPU pipeline worker.
            if sess.status == 'ended':
                return
            print(f'[Session] {session_id} ended ({reason})')
            sess.status = 'ended'
            if reason == 'error' and not sess.error:
                sess.error = 'stream error'
            if sess.pipeline:
                try:
                    sess.pipeline.stop()
                except Exception:
                    pass

        def _launch():
            try:
                if args.mock_asr:
                    sess.status = 'live'
                    _run_mock(session_id, caption_cb)
                    return

                pipeline = ASRPipeline(
                    cfg.get('asr', {}),
                    shared_backend=shared_backend,
                    worker_name=f'asr-{session_id}',
                )
                pipeline.set_caption_callback(caption_cb)

                if DeckLinkAdapter.handles(url):
                    # SDI input via Blackmagic DeckLink — sdi://<sub-device>.
                    dl_cfg = dict(cfg.get('io', {}).get('decklink', {}))
                    dl_cfg['url'] = url
                    adapter = DeckLinkAdapter(dl_cfg)
                elif StreamAdapter.handles(url):
                    # Live broadcast source (rtmp/rtsp/srt): GStreamer reads it
                    # directly, no yt-dlp resolve step.
                    stream_cfg = dict(cfg.get('io', {}).get('stream', {}))
                    stream_cfg['url'] = url
                    adapter = StreamAdapter(stream_cfg)
                else:
                    yt_cfg = dict(cfg.get('io', {}).get('youtube', {}))
                    yt_cfg['url'] = url
                    adapter = YouTubeAdapter(yt_cfg)
                adapter.set_audio_callback(pipeline.on_audio)
                adapter.set_end_callback(_on_source_end)

                sess.adapter = adapter
                sess.pipeline = pipeline

                pipeline.start()
                adapter.start()          # blocks during yt-dlp CDN resolve
                if sess.status != 'ended':   # a very short clip may EOS already
                    sess.status = 'live'

            except Exception as exc:
                print(f'[Session] {session_id} failed: {exc}')
                sess.status = 'error'
                sess.error = str(exc)

        threading.Thread(
            target=_launch, daemon=True, name=f'session-start-{session_id}',
        ).start()
        return session_id

    def stop_session(session_id: str) -> None:
        with _sessions_lock:
            sess = _sessions.pop(session_id, None)
        if sess is None:
            return
        print(f'[Session] Stopping {session_id}')
        if sess.status not in ('ended', 'error'):
            sess.status = 'ended'
        # Persist captioning history so it stays viewable on the Logs page.
        from microcaption.output import session_archive
        session_archive.write_session(sess)
        if webvtt_server:
            webvtt_server.unregister_session(session_id)
        if sess.adapter:
            try:
                sess.adapter.stop()
            except Exception:
                pass
        if sess.pipeline:
            try:
                sess.pipeline.stop()
            except Exception:
                pass

    # ── Metrics provider ──────────────────────────────────────────────────────

    def _metrics_provider() -> dict:
        with _sessions_lock:
            pipelines = [s.pipeline for s in _sessions.values() if s.pipeline]
        backend_name = (
            shared_backend.name if shared_backend else
            ('mock' if args.mock_asr else '—')
        )
        if not pipelines:
            return {'backend': backend_name, 'total': 0}

        monitors = [p.latency_monitor for p in pipelines]
        total_count = sum(m.count for m in monitors)
        active = [m for m in monitors if m.count > 0]

        mean = (sum(m.mean_ms for m in active if m.mean_ms) / len(active)
                if active else None)
        p95  = max((m.p95_ms  for m in active), default=None)
        mmax = max((m.max_ms  for m in active), default=None)
        rate = sum(m.inferences_per_second for m in monitors)

        return {
            'backend': backend_name,
            'mean_ms': round(mean, 1) if mean is not None else None,
            'p95_ms':  round(p95,  1) if p95  is not None else None,
            'max_ms':  round(mmax, 1) if mmax is not None else None,
            'rate':    round(rate, 3),
            'total':   total_count,
        }

    # ── WebVTT / web server ───────────────────────────────────────────────────
    webvtt_cfg = cfg.get('output', {}).get('webvtt', {})
    if webvtt_cfg.get('enabled', True):
        webvtt_server = WebVTTServer(
            webvtt_cfg,
            auth_cfg=cfg.get('auth', {}),
            start_callback=start_session,
            stop_callback=stop_session,
            metrics_provider=_metrics_provider,
            gpu_provider=gpu_monitor.snapshot,
            config_snapshot=cfg,
        )
        webvtt_server.start()
        port = webvtt_cfg.get('port', 8765)
        print(f'[Main] API: http://localhost:{port}/ — UI: http://localhost:3000/ (run client/serve.py)')

    # ── Latency reporter ──────────────────────────────────────────────────────
    report_interval = cfg.get('monitor', {}).get('report_interval', 30)

    def _reporter():
        while True:
            time.sleep(report_interval)
            with _sessions_lock:
                pipelines = [s.pipeline for s in _sessions.values() if s.pipeline]
            if not pipelines:
                continue
            total = sum(p.latency_monitor.count for p in pipelines)
            active = [p.latency_monitor for p in pipelines if p.latency_monitor.count > 0]
            if active:
                mean = sum(m.mean_ms for m in active if m.mean_ms) / len(active)
                print(
                    f'[Monitor] Sessions: {len(pipelines)}  '
                    f'inferences: {total}  avg-mean: {mean:.0f} ms',
                    file=sys.stderr,
                )

    threading.Thread(target=_reporter, daemon=True, name='latency-reporter').start()

    # ── Block until SIGINT / SIGTERM ──────────────────────────────────────────
    print('[Main] Control room ready. Ctrl-C to stop.')
    stop_event = threading.Event()

    def _sigint(sig, frame):
        print('\n[Main] Shutting down…')
        stop_event.set()

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)
    stop_event.wait()

    # Stop all active sessions
    with _sessions_lock:
        session_ids = list(_sessions.keys())
    for sid in session_ids:
        stop_session(sid)

    if shared_backend:
        shared_backend.unload()
    if webvtt_server:
        webvtt_server.stop()
    pkt_logger.close()
    print('[Main] Stopped.')


if __name__ == '__main__':
    main()
