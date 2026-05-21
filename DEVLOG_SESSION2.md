# MicroCaption — Development Log, Session 2
**Session date:** 2026-05-21  
**Engineer:** Peter Dews (info@peterdews.com)  
**Branch:** `YouTube-Ingest---ASR-Validation`  
**System:** MicroCap-Proto, Ubuntu 26.04 "Resolute"  
**AI pair programmer:** Claude Sonnet 4.6

---

## 1. Session Goals

Session 1 (see `DEVLOG.md`) established the core CEA-608/CEA-708 caption pipeline, unit-tested the packet layer, and laid out the adapter architecture. Everything was validated at the code level but not run end-to-end against a real YouTube source.

Session 2 goals:
1. Get the YouTube → Whisper → browser-overlay pipeline actually running against a live YouTube URL
2. Fix all bugs blocking that end-to-end path
3. Expose the demo publicly via Tailscale Funnel
4. Build a proper multi-session web UI so the demo can be handed to others without touching the command line

---

## 2. Files Changed — Complete Record

### 2.1 `microcaption/io/__init__.py` — Critical import fix

**Problem:** The file imported `YouTubeAdapter` twice:
```python
from .youtube_adapter import YouTubeAdapter       # real GStreamer adapter
from .youtube_video_adapter import YouTubeAdapter  # stub — silently overwrites the above
```
The second import silently replaced the real GStreamer-based adapter with `youtube_video_adapter.py`, which is a shell that does nothing — no audio extraction, no GStreamer pipeline, no callback. Any run with `--youtube URL` would resolve the CDN URL but deliver zero audio to the ASR pipeline.

**Fix:** Removed the stub import. The real adapter (`youtube_adapter.py`) is now the only `YouTubeAdapter`.

```python
# Before
from .youtube_adapter import YouTubeAdapter
from .youtube_video_adapter import YouTubeAdapter  # ← removed

# After
from .youtube_adapter import YouTubeAdapter        # GStreamer-based, correct
```

---

### 2.2 `microcaption/output/webvtt_server.py` — Major rewrite

This file received the most changes across the session. The full evolution:

#### 2.2.1 ThreadingMixIn — fix for blocked concurrent connections

**Problem:** Python's `HTTPServer` is single-threaded by default. The SSE stream handler (`_sse_stream`) holds its connection open indefinitely, looping on `time.sleep(15)` to send keepalive pings. Because the server is single-threaded, this permanently blocked the `serve_forever()` loop — meaning any second request (a new browser tab, a second user, even a refresh) would hang with no response until the SSE client disconnected.

This was the root cause of "the page isn't loading when I open it on another machine."

**Fix:** Added `ThreadingMixIn` so each connection (including long-lived SSE streams) runs in its own thread:

```python
from socketserver import ThreadingMixIn

class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
```

#### 2.2.2 SSE cue payload — added `lines` field

**Problem:** `on_caption()` was sending:
```python
{'text': 'The quick brown fox', 'start': '1.234', 'end': '3.456'}
```
But the YouTube player JavaScript expected `d.lines` (an array). Since `d.lines` was `undefined`, the player's `if (!d.lines || !d.lines.length)` branch cleared the caption div every time — meaning captions were received but immediately erased.

**Fix:** Added `lines` to every SSE event:
```python
cue_json = json.dumps({
    'text': text,
    'lines': [l for l in text.split('\n') if l.strip()],
    'start': f'{start:.3f}',
    'end': f'{end:.3f}',
})
```

#### 2.2.3 Landing page + URL submission form

Replaced the text-only caption-scroll page at `/` with a proper web UI:

- **`GET /`** — Landing page with a YouTube URL input form and a "Caption" submit button. If a session is active, a "now playing" bar appears with a link to `/player` and a Stop button.
- **`POST /start`** — Receives the submitted URL, extracts the video ID immediately (so `/player` is ready before the background yt-dlp resolution completes), fires `start_callback(url)` in a daemon thread, then redirects to `/player`.
- **`GET /player`** — Full-page YouTube iframe embed with SSE caption overlay. Includes "← New video" and "■ Stop" buttons in the top bar.
- **`POST /stop`** — Fires `stop_callback()`, clears `video_id`, redirects to `/`.
- **`GET /webvtt`** — Full accumulated WebVTT document download.
- **`GET /status`** — JSON status endpoint `{active, video_id, cue_count}`.

#### 2.2.4 Race condition fix — video ID set before redirect

**Problem:** `POST /start` was:
1. Starting `start_session(url)` in a background thread
2. Immediately redirecting to `/player`

`start_session` calls `webvtt_server.set_video_id(video_id)` but this runs in the background thread. The browser follows the redirect before the thread has run, so `/player` sees `video_id = ''` and serves the "no active session" redirect page.

**Fix:** Extract the video ID synchronously in the POST handler before starting the background thread:
```python
_Handler.video_id = _video_id_from_url(url)   # ← set immediately, before thread
threading.Thread(target=cb, args=(url,), ...).start()
# redirect now — /player already has the video ID
```

#### 2.2.5 Python descriptor protocol bug — callback called with wrong argument count

**Problem:** `_Handler.start_callback` is a plain function stored as a class variable. When accessed via `self.start_callback` (an instance), Python's descriptor protocol wraps it as a bound method — effectively prepending `self` as the first argument. So `self.start_callback(url)` called `start_session(handler_instance, url)`, which failed with "takes 1 positional argument but 2 were given."

**Fix:** Access the callback through the class, not the instance:
```python
cb = _Handler.start_callback   # plain function reference, no binding
if cb:
    threading.Thread(target=cb, args=(url,), ...).start()
```
Same pattern applied to `stop_callback`.

#### 2.2.6 Stop button — both pages

Added a `■ Stop` button to:
- The player page topbar (alongside "← New video")
- The "now playing" bar on the landing page

Both POST to `/stop`, which stops the adapter and redirects to `/`.

---

### 2.3 `microcaption/asr/whisper_backend.py` — Local cache resolution

**Problem:** `WhisperModel(model_size_or_path)` calls `huggingface_hub.snapshot_download()` on startup to check for updates, even when the model is fully cached locally. This required `httpx` (a transitive dependency not in our venv) and added unnecessary network latency.

Additionally, the cached model was from `mobiuslabsgmbh` (not the standard `Systran` repo), so hardcoded path construction wouldn't find it.

**Fix:** Glob-search the HF hub cache for any snapshot matching the model name, and pass the local path directly to `WhisperModel`:
```python
pattern = os.path.join(cache_dir, f'*{model_key}*', 'snapshots', '*')
matches = sorted(glob.glob(pattern))
if matches:
    model_path = matches[-1]   # newest snapshot
```
This loads from disk instantly, no network needed.

---

### 2.4 `microcaption/caption/webvtt.py` — Added `reset()`

Added a `reset()` method to `WebVTTWriter` so accumulated cues are cleared when a new session starts (otherwise cues from video A would appear in the WebVTT download for video B):

```python
def reset(self) -> None:
    self._cues = []
    self._cue_index = 1
```

---

### 2.5 `microcaption/monitor/latency.py` — Guard against empty sample window

**Problem:** `report()` formatted `mean`, `p95`, and `max` with `:.0f`, but these properties return `None` when no samples have been recorded. This crashed the latency reporter thread on its first fire (30 seconds after startup, before any audio had been processed).

**Fix:**
```python
def report(self) -> str:
    mean = self.mean_ms
    if mean is None:
        return f'ASR latency — no samples yet (total: {self._count})'
    ...
```

---

### 2.6 `main.py` — Full restructure for persistent server mode

#### Previous behaviour
`main.py` required a `--youtube URL` argument at startup. The YouTube adapter was instantiated once and ran for the lifetime of the process. Changing videos required restarting the process.

#### New behaviour
The process starts without any URL. The ASR pipeline loads immediately (Whisper model warm in GPU memory). The web UI accepts YouTube URLs at runtime and the backend swaps adapters on demand — no restart needed.

**Key additions:**

`_extract_video_id(url)` — parses YouTube video IDs from both `youtube.com/watch?v=` and `youtu.be/` formats.

`_stop_current()` — safely stops and discards the current `YouTubeAdapter`, if any. Thread-safe via `_session_lock`.

`stop_session()` — calls `_stop_current()`, resets the VTT writer, clears `asr_pipeline._last_text` (prevents the first new caption being suppressed as a duplicate of the last caption from the previous video).

`start_session(url)` — full session swap:
1. Stops current adapter via `_stop_current()`
2. Resets VTT writer and dedup state
3. Updates `webvtt_server` video ID for the player page
4. Creates a new `YouTubeAdapter` with the new URL
5. Wires audio callback to the ASR pipeline
6. Starts the adapter (yt-dlp resolution + GStreamer pipeline)

Both callbacks are passed into `WebVTTServer` at construction time.

---

### 2.7 `run.sh` — New launcher script

Created to bake in the CUDA library path that the system requires. `libcublas.so.12` is present on this machine inside Ollama's CUDA directory but is not on the default `LD_LIBRARY_PATH`, causing `ctranslate2` (faster-whisper's inference engine) to fail to load.

```bash
export LD_LIBRARY_PATH="/usr/local/lib/ollama/cuda_v12${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONUNBUFFERED=1
exec .venv/bin/python3 -u main.py "$@"
```

**Always use `./run.sh` to start the backend**, not `python3 main.py` directly, or Whisper inference will fail silently on GPU.

---

### 2.8 `.venv/` — New virtual environment

Created a new venv with `--system-site-packages` (required so GStreamer's `python3-gi` bindings, installed via apt, are visible inside the venv).

The `faster-whisper` stack was copied from the old venv that had been moved to the system Trash (`/home/peter/.local/share/Trash/files/MicroCaption/.venv/`). Packages copied:

| Package | Version |
|---|---|
| faster-whisper | 1.2.1 |
| ctranslate2 | 4.7.2 |
| ctranslate2.libs | (bundled .so files) |
| av | 17.0.1 |
| av.libs | (bundled ffmpeg .so files) |
| tokenizers | 0.22.2 |
| huggingface_hub | 1.15.0 |
| httpx | 0.28.1 |
| httpcore | 1.0.9 |
| anyio | 4.13.0 |
| h11 | 0.16.0 |
| tqdm | (latest) |
| filelock | (latest) |
| fsspec | (latest) |
| packaging | (latest) |

`pyyaml` was installed fresh via pip. All other dependencies (numpy, GStreamer, yt-dlp) are available system-wide and visible via `--system-site-packages`.

---

## 3. Infrastructure — Tailscale Funnel

See `TAILSCALE_FUNNEL_SETUP.md` for full replication instructions.

**Summary of what was configured:**

- Machine name on tailnet: `microcap-proto`
- Tailnet name: `tail737e71.ts.net`
- Public URL: `https://microcap-proto.tail737e71.ts.net/`
- Funnel command: `tailscale funnel --bg 8765`
- Operator permission set so funnel runs without sudo: `sudo tailscale set --operator=peter`
- Funnel config persists across reboots automatically

The Funnel proxies `https://microcap-proto.tail737e71.ts.net/ → http://127.0.0.1:8765`. Tailscale handles TLS termination with a Let's Encrypt certificate (auto-renewed). The backend only speaks plain HTTP on localhost; HTTPS is provided entirely by the Funnel layer.

---

## 4. End-to-End Data Flow (as built)

```
User browser (any machine)
    │
    │  HTTPS (Tailscale Funnel / TLS terminated by Tailscale)
    ▼
https://microcap-proto.tail737e71.ts.net/
    │
    │  HTTP proxy → 127.0.0.1:8765
    ▼
WebVTTServer (Python http.server + ThreadingMixIn)
    │
    ├── GET /          → Landing page (URL form)
    ├── POST /start    → start_session(url) → redirect /player
    ├── POST /stop     → stop_session()     → redirect /
    ├── GET /player    → YouTube iframe + SSE caption overlay
    ├── GET /events    → SSE stream (one thread per connected client)
    └── GET /webvtt    → full WebVTT document download

POST /start triggers:
    yt-dlp -g -f bestaudio URL
        → CDN audio-only stream URL
        → GStreamer: souphttpsrc → decodebin → audioconvert
                  → audioresample → audio/x-raw F32LE 16kHz mono
                  → appsink → on_new_sample()
                  → AudioChunk(samples, timestamp) → ASRPipeline.on_audio()

ASRPipeline (worker thread):
    sliding window: chunk=2.0s, step=0.5s
    EnergyVAD (RMS threshold 1e-4, hangover 5 frames)
    WhisperBackend.transcribe(samples)
        → faster-whisper large-v3-turbo on CUDA (RTX 5070 Ti)
        → ~6ms per 2s chunk at float16
    CaptionResult → on_caption()

on_caption():
    CaptionNormalizer → NormalizedCaption (max 32 chars/line, 2 lines)
    TerminalSink      → timestamped stdout
    WebVTTServer      → SSE push to all connected /events clients
    CEA608Packetizer  → odd-parity byte pairs → PacketLogger → logs/cea608.jsonl
    DTVCC708Packetizer → cc_data tuples → PacketLogger → logs/cea708.jsonl

Browser (GET /player):
    YouTube iframe plays video (independent stream from YouTube's CDN)
    EventSource('/events') receives SSE cues
    showLines() updates #caption-bar overlay
```

---

## 5. Known Limitations of the Current Prototype

| Issue | Detail | Planned fix |
|---|---|---|
| Audio/video sync drift | The backend pulls audio via yt-dlp/GStreamer independently of the YouTube iframe in the browser. The two streams diverge over time. | Phase 2: synchronise using YouTube IFrame API `getCurrentTime()` to seek the ASR output |
| Single session | Only one video can be processed at a time. A second submission stops the first. | Intentional for POC — multi-session would require per-session ASR workers |
| No reconnect on yt-dlp URL expiry | YouTube CDN URLs expire (~6 hours). Long streams will silently stop delivering audio. | Add watchdog + yt-dlp re-resolution loop to `YouTubeAdapter` |
| No NeMo / Parakeet | The primary ASR engine (NVIDIA Parakeet via NeMo) is not installed — onnx build issues on Python 3.14. Whisper is primary. | Re-attempt `nemo_toolkit[asr]` install when a cp314 wheel is available |
| libcublas path manual | `run.sh` hardcodes the Ollama CUDA path. If Ollama is removed or updated, this breaks. | Install CUDA toolkit or nvidia Python wheels to provide a stable path |

---

## 6. Unit Test Status

All 52 original unit tests continue to pass:
```bash
.venv/bin/python3 -m unittest tests.test_packetizer tests.test_normalizer tests.test_wer -v
# Ran 52 tests in 0.001s — OK
```
No new unit tests were written this session (infrastructure/integration work).

---

## 7. How to Resume / Restart

```bash
cd /home/peter/Dev/micro-caption

# Start the demo server (Whisper loads in ~3s, model already cached)
./run.sh

# Open in browser (local)
# http://localhost:8765/

# Open externally (Tailscale Funnel — must be running)
# https://microcap-proto.tail737e71.ts.net/

# Verify Funnel is active
tailscale funnel status

# Re-enable Funnel if it was reset
tailscale funnel --bg 8765

# Stop the server
Ctrl-C  (or kill $(lsof -ti:8765))
```

---

*Log written at end of session 2026-05-21.*
