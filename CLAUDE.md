# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

Always use `./server/run.sh` to start the caption server — not `python3 main.py` directly. `run.sh` sets the CUDA library path required for Whisper inference:

    ./server/run.sh                        # idle server, submit URLs via web UI
    ./server/run.sh --youtube "<URL>"      # start and immediately caption a URL

**Always run with live ASR** — this machine has a GPU and Whisper loaded. Never use `--mock-asr` except when running tests with no GPU available (CI, unit testing). Mock mode produces fake captions and is not representative of real behaviour.

For the web UI, run the client dev server separately:

    cd client && python3 serve.py          # serves on http://localhost:3000

Caption API server runs on :8765. The two processes are independent — restarting the client does not affect ongoing captioning.

## Tests

No GPU, models, or network required. Run from `server/`:

    .venv/bin/python3 -m pytest tests/ -v
    .venv/bin/python3 -m pytest tests/test_packetizer.py -v   # just CEA-608/708 tests

The venv must be created with `--system-site-packages` so GStreamer's `python3-gi` apt bindings (installed via apt, not pip) are visible. Config lives in `server/config/settings.yaml`; any nested key can be overridden via `MC_*` env vars (e.g., `MC_ASR_PRIMARY=whisper`).

## Architecture

### Data Flow

    URL → YouTubeAdapter (yt-dlp + GStreamer souphttpsrc → F32LE 16 kHz mono PCM)
        → ASRPipeline (EnergyVAD → sliding window → WhisperBackend / ParakeetBackend)
        → on_caption() callback (fired from ASR worker thread)
        → CaptionNormalizer → CEA608Packetizer / DTVCC708Packetizer → PacketLogger
        → WebVTTServer.on_caption() → SSE fan-out to browser EventSource clients

### Session Model

Every URL submission creates an isolated `Session` (`server/microcaption/session.py`). Each session has its own `ASRPipeline` worker thread, `WebVTTWriter`, SSE client list, and 6-digit public watch code. The GPU model is shared: `SharedASRBackend` (`server/microcaption/asr/pipeline.py`) loads once and serializes all `transcribe()` calls via `threading.Lock`. At `int8` / `step_duration=1.0s` this supports ~6 concurrent streams at ~45% GPU duty cycle on an RTX 5070 Ti.

### Server / Client Split

`server/` is a Python-only process. It handles:
- **API routes** (`/api/*`) — JSON for sessions, metrics, config, queue, cues
- **SSE routes** (`/events/<id>`, `/events/watch/<code>`) — per-session caption streams
- **Auth routes** (`/login`, `/auth/callback`, `/logout`) — Google OAuth2 (disabled by default)
- **File routes** (`/webvtt/<id>`) — WebVTT download

`client/` is a static web app (vanilla HTML + CSS + JS, no build step). Pages fetch data from the server's `/api/*` and connect to `/events/*` for captions. In production (Docker Compose), nginx routes `/api/*` and `/events/*` to the server and all other paths to the client.

### Callback Contract (`main.py` ↔ `WebVTTServer`)

- `start_callback(url) → session_id` — called in HTTP handler on `POST /api/start`
- `stop_callback(session_id)` — called in HTTP handler on `POST /stop/<id>`
- `webvtt_server.on_caption(text, start, end, session_id)` — called from ASR worker thread; fans out to SSE clients
- `webvtt_server.register_session(session)` / `unregister_session(session_id)` — session lifecycle

### CEA-608/708 Packets

`CEA608Packetizer` generates odd-parity byte pairs (Line 21 / backward-compat 608).
`DTVCC708Packetizer` generates DTVCC service blocks with `cc_data` tuples (ATSC A/53 Part 4).
Both are stateless except for sequence counters. `CaptionNormalizer` wraps text to ≤32 chars/line, ≤2 lines.

### Auth

Google OAuth2 is disabled by default (`auth.enabled: false`). When disabled, all protected routes use `dev@local` identity. See `server/Auth_Next_Steps.md` to enable.

### Operational Notes

- `run.sh` hardcodes `LD_LIBRARY_PATH` for `/usr/local/lib/ollama/cuda_v12` (libcublas needed by ctranslate2/faster-whisper)
- `asr.primary` is currently `whisper`; Parakeet is the intended primary but `nemo_toolkit[asr]` has not installed due to Python 3.14 wheel gaps
- yt-dlp format selector: `-f bestaudio/best` (falls back to HLS when audio-only requires a JS runtime; Node.js must be installed for YouTube audio-only formats)
- Whisper hallucinations suppressed via `condition_on_previous_text=False` + `no_speech_prob` gate + blocklist in `server/microcaption/asr/whisper_backend.py`
