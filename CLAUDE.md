# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

Always use `./server/run.sh` to start the caption server — not `python3 main.py` directly. `run.sh` sets the CUDA library path required for Parakeet (cuDNN) and Whisper (cuBLAS) inference:

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

### ASR Environment (torch / NeMo / cuDNN on Python 3.14)

The venv is Python 3.14. torch + NeMo (for Parakeet) DO install here, but with caveats — capture them or a fresh setup will fail:

- **Install with `uv`, not plain pip.** pip's resolver hits `resolution-too-deep` on NeMo's dependency tree; `uv pip install torch "nemo_toolkit[asr]"` resolves a clean wheel set (NeMo 2.7.3, onnx 1.22 abi3, numpy stays 2.x).
- **Set `CMAKE_POLICY_VERSION_MINIMUM=3.5`** during install — a few small C++ deps (e.g. `kaldialign`) have no cp314 wheel and fail to build against this box's CMake 4.2 without it.
- **torch must be the `cu128` build** (`pip install "torch==2.11.0+cu128" --index-url https://download.pytorch.org/whl/cu128`). The RTX 5070 Ti is Blackwell (sm_120); `cu126` lacks sm_120 kernels. `cu130` would also work but its cuDNN wheel is a stub.
- **cuDNN is vendored out-of-band.** torch cu128 needs cuDNN 9.19, but NVIDIA's PyPI cuDNN wheel for it has no `.so` (stub). Run `server/vendor/fetch_cudnn.sh` once to download the real libs into `server/vendor/cudnn/lib`; `run.sh` puts that dir first on `LD_LIBRARY_PATH`.
- **Parakeet model:** use `nvidia/parakeet-tdt-0.6b-v2` (public). `nvidia/parakeet-tdt_ctc-0.6b` is gated (HF 401).

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

- `run.sh` sets `LD_LIBRARY_PATH` to `server/vendor/cudnn/lib` (real cuDNN 9.19 for Parakeet/torch — see ASR Environment below) then `/usr/local/lib/ollama/cuda_v12` (libcublas needed by ctranslate2/faster-whisper)
- `asr.primary` is `parakeet` (`nvidia/parakeet-tdt-0.6b-v2`, in-process via NeMo); Whisper (`faster-whisper`) is the hot-standby fallback. Both run in the same process and share the GPU.
- yt-dlp format selector: `-f bestaudio/best` (falls back to HLS when audio-only requires a JS runtime; Node.js must be installed for YouTube audio-only formats)
- Whisper hallucinations suppressed via `condition_on_previous_text=False` + `no_speech_prob` gate + blocklist in `server/microcaption/asr/whisper_backend.py`
