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
    from microcaption.asr.pipeline import ASRPipeline, CaptionResult, SharedASRBackend
    from microcaption.caption.normalizer import CaptionNormalizer, apply_speaker_marker
    from microcaption.diarize import (
        SpeakerEmbedder, SpeakerChangeDetector, DiarizationPass,
    )
    from microcaption.caption.packetizer_608 import CEA608Packetizer
    from microcaption.caption.packetizer_708 import DTVCC708Packetizer
    from microcaption.caption.webvtt import WebVTTWriter
    from microcaption.output.terminal_sink import TerminalSink
    from microcaption.output.webvtt_server import WebVTTServer
    from microcaption.output.packet_logger import PacketLogger
    from microcaption.monitor import GpuMonitor
    from microcaption.accuracy import AccuracyVerifier, SessionStore

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

    # ── Per-stream caption log + accuracy persistence ─────────────────────────
    accuracy_cfg = cfg.get('accuracy', {})
    store = SessionStore(accuracy_cfg)
    store.open()

    verbose_packets = cfg.get('output', {}).get('packet_log', {}).get('verbose', False)

    # ── Shared ASR backend — loaded once, used by all sessions ───────────────
    shared_backend: SharedASRBackend = None
    if not args.mock_asr:
        shared_backend = SharedASRBackend(cfg.get('asr', {}))
        shared_backend.load()

    # ── Shared speaker-embedding model — loaded once for diarization ─────────
    # Degrades cleanly: if the model can't load, embedder.available is False and
    # the whole speaker layer no-ops (no ">>" marks, no SPEAKER labels).
    diar_cfg = cfg.get('diarize', {})
    diar_enabled = diar_cfg.get('enabled', False) and not args.mock_asr
    speaker_embedder = None
    if diar_enabled:
        speaker_embedder = SpeakerEmbedder(diar_cfg)
        speaker_embedder.load()
        if not speaker_embedder.available:
            print(f'[Diarize] disabled — {speaker_embedder.load_error}')

    # ── Session registry ──────────────────────────────────────────────────────
    _sessions: dict = {}          # session_id → Session
    _sessions_lock = threading.Lock()
    webvtt_server = None          # assigned below after server starts

    # ── Per-stream log payload builders (for SessionStore + logs API) ─────────
    def _summary_of(sess) -> dict:
        acc = sess.accuracy_summary or {}
        return {
            'id': sess.id,
            'url': sess.url,
            'video_id': sess.video_id,
            'source_type': sess.source_type,
            'created_at': sess.created_at,
            'status': sess.status,
            'accuracy': acc.get('accuracy'),
            'segment_count': acc.get('segment_count', 0),
            'cue_count': sess.writer.cue_count if sess.writer else 0,
        }

    def _detail_of(sess) -> dict:
        d = _summary_of(sess)
        d['accuracy_summary'] = dict(sess.accuracy_summary or {})
        d['records'] = list(sess.accuracy_records)
        d['cues'] = sess.writer.all_cue_data() if sess.writer else []
        return d

    def make_record_callback(session_id: str):
        def on_record(record: dict, summary: dict) -> None:
            with _sessions_lock:
                sess = _sessions.get(session_id)
            if not sess:
                return
            sess.accuracy_records.append(record)
            sess.accuracy_summary = summary
            store.save(_summary_of(sess), _detail_of(sess))
        return on_record

    def make_diar_update_callback(session_id: str):
        """Behind-live diarization has assigned SPEAKER N labels to committed
        cues — write them onto the stored cues, tell replay/archive consumers,
        and re-checkpoint the persisted log."""
        def on_update(updates: list) -> None:
            with _sessions_lock:
                sess = _sessions.get(session_id)
            if not sess or not sess.writer:
                return
            changed = False
            for u in updates:
                if sess.writer.set_cue_speaker(
                        u['start'], u['speaker'], u.get('speaker_change', False)):
                    changed = True
            if not changed:
                return
            if webvtt_server:
                webvtt_server.push_speaker_update(session_id, updates)
            store.save(_summary_of(sess), _detail_of(sess))
        return on_update

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
                # Send the words + the live ">>" flag; the client renders the
                # marker so it can be styled separately from the caption text.
                webvtt_server.on_caption(
                    normalizer.to_display_string(norm),
                    result.start_time,
                    result.end_time,
                    session_id,
                    speaker_change=result.speaker_change,
                )
            # For byte-level CEA-608/708, the ">>" mark is literal characters, so
            # bake it into the first line before packetizing.
            lines608 = apply_speaker_marker(norm.lines, speaker_change=result.speaker_change)
            frames608 = pkt608.packetize(lines608)
            raw708, cc_data = pkt708.packetize(lines608)
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
        source_type = 'youtube' if video_id else 'stream'
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
        # Persist a stub immediately so the stream shows up in the Logs UI from
        # the moment it starts (and survives a restart even with no captions).
        store.register(_summary_of(sess), _detail_of(sess))

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
            if sess.verifier:
                try:
                    sess.verifier.stop()
                except Exception:
                    pass
            if sess.diarizer:
                try:
                    sess.diarizer.stop()
                except Exception:
                    pass
            if sess.pipeline:
                try:
                    sess.pipeline.stop()
                except Exception:
                    pass
            # Finalise the persisted log so it survives a restart.
            store.save(_summary_of(sess), _detail_of(sess), final=True)

        def _launch():
            try:
                if args.mock_asr:
                    sess.status = 'live'
                    _run_mock(session_id, caption_cb)
                    return

                asr_cfg = cfg.get('asr', {})

                # Live speaker-change detection (">>" marks). Per-session state
                # (previous-utterance embedding), shared embedding model. None
                # when diarization is disabled/unavailable → no live marks.
                change_fn = None
                if speaker_embedder and speaker_embedder.available:
                    detector = SpeakerChangeDetector(speaker_embedder, diar_cfg)
                    change_fn = detector.is_change

                pipeline = ASRPipeline(
                    asr_cfg,
                    shared_backend=shared_backend,
                    worker_name=f'asr-{session_id}',
                    speaker_change_fn=change_fn,
                )
                pipeline.set_caption_callback(caption_cb)

                # Second, slower accuracy-verification pass (record & verify
                # behind live). Shares the one GPU model; never blocks live.
                verifier = None
                if asr_cfg.get('verifier', {}).get('enabled', True) and shared_backend:
                    verifier = AccuracyVerifier(
                        shared_backend, asr_cfg,
                        get_live_cues=sess.writer.all_cue_data,
                        on_record=make_record_callback(session_id),
                        worker_name=f'verifier-{session_id}',
                    )
                    sess.verifier = verifier

                # Behind-live diarization pass — stable SPEAKER N labels written
                # back onto stored cues. Never blocks live.
                diarizer = None
                if speaker_embedder and speaker_embedder.available:
                    diarizer = DiarizationPass(
                        speaker_embedder, diar_cfg,
                        get_live_cues=sess.writer.all_cue_data,
                        on_update=make_diar_update_callback(session_id),
                        worker_name=f'diarizer-{session_id}',
                    )
                    sess.diarizer = diarizer

                yt_cfg = dict(cfg.get('io', {}).get('youtube', {}))
                yt_cfg['url'] = url
                adapter = YouTubeAdapter(yt_cfg)

                def _feed_audio(chunk):
                    pipeline.on_audio(chunk)
                    if verifier:
                        verifier.on_audio(chunk)
                    if diarizer:
                        diarizer.on_audio(chunk)
                adapter.set_audio_callback(_feed_audio)
                adapter.set_end_callback(_on_source_end)

                sess.adapter = adapter
                sess.pipeline = pipeline

                pipeline.start()
                if verifier:
                    verifier.start()
                if diarizer:
                    diarizer.start()
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
        sess.status = 'ended'
        if webvtt_server:
            webvtt_server.unregister_session(session_id)
        if sess.adapter:
            try:
                sess.adapter.stop()
            except Exception:
                pass
        if sess.verifier:
            try:
                sess.verifier.stop()
            except Exception:
                pass
        if sess.diarizer:
            try:
                sess.diarizer.stop()
            except Exception:
                pass
        if sess.pipeline:
            try:
                sess.pipeline.stop()
            except Exception:
                pass
        # Finalise the persisted log before the session leaves the registry.
        store.save(_summary_of(sess), _detail_of(sess), final=True)

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
        # Accuracy-first: audio is only ever dropped under sustained GPU overload.
        # Surface it so a saturated server is visible rather than silently lossy.
        dropped = sum(p.dropped_chunks for p in pipelines)

        return {
            'backend': backend_name,
            'mean_ms': round(mean, 1) if mean is not None else None,
            'p95_ms':  round(p95,  1) if p95  is not None else None,
            'max_ms':  round(mmax, 1) if mmax is not None else None,
            'rate':    round(rate, 3),
            'total':   total_count,
            'dropped_audio_chunks': dropped,
        }

    # ── Per-stream logs providers (active sessions + persisted history) ───────
    def _logs_provider() -> list:
        with _sessions_lock:
            live = {sid: _summary_of(s) for sid, s in _sessions.items()}
        summaries = list(live.values())
        for s in store.list_summaries():
            if s.get('id') not in live:
                summaries.append(s)
        summaries.sort(key=lambda s: s.get('created_at', 0), reverse=True)
        return summaries

    def _log_detail_provider(session_id: str):
        with _sessions_lock:
            sess = _sessions.get(session_id)
        if sess is not None:
            return _detail_of(sess)
        return store.get_detail(session_id)

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
            logs_provider=_logs_provider,
            log_detail_provider=_log_detail_provider,
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
