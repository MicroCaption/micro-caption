# MicroCaption

**MicroCaption** is a real-time closed-captioning system that generates CTA-608 and CTA-708 compliant broadcast captions. It supports multiple input sources (microphone, YouTube), GPU-accelerated speech recognition, and a web-based caption viewer.

---

## Features

- **Real-time captioning** with low-latency GPU-accelerated speech recognition
- **Multiple input sources**: Microphone (ALSA/PulseAudio), YouTube (browser embed), DeckLink SDI (Phase 2)
- **CTA-608/CTA-708 compliant** caption packet generation with proper odd parity encoding
- **WebVTT live server** for browser-based caption display
- **Whisper Turbo** (fallback) or **Parakeet TDT-CTC** (primary) ASR models
- **Server-Sent Events (SSE)** for real-time caption streaming
- **Packet logging** for CEA-608 and CEA-708 debug analysis

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     MicroCaption Live Captioning                  │
├─────────────────────────────────────────────────────────────────┤
│  Input:                                                          │
│    • Microphone (alsa/PulseAudio)                                 │
│    • YouTube (browser embed via iframe)                           │
│    • DeckLink SDI (Phase 2)                                       │
│                                                                   │
│  ASR (GPU-accelerated):                                           │
│    • Parakeet TDT-CTC (nvidia/parakeet-tdt_ctc-0.6b)            │
│    • Whisper large-v3-turbo (fallback)                            │
│    • EnergyVAD for speech activity detection                      │
│                                                                   │
│  Caption Processing:                                              │
│    • CaptionNormalizer (line wrapping, truncation)                │
│    • CEA-608 Packetizer (odd parity encoding)                     │
│    • CEA-708 DTVCC Packetizer (service blocks)                    │
│                                                                   │
│  Output:                                                          │
│    • Terminal sink (timestamped captions)                         │
│    • WebVTT HTTP server (live captions at :8765)                 │
│    • Packet logger (logs/cea608.jsonl, cea708.jsonl)             │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Install Dependencies

```bash
# System packages
sudo apt-get install -y build-essential python3-dev cmake python3-venv \
    python3-gi python3-gst-1.0 gstreamer1.0-plugins-{base,good,bad,ugly} \
    gstreamer1.0-{libav,pulseaudio} gir1.2-gstreamer-1.0

# Setup
chmod +x setup.sh && ./setup.sh

# Activate virtual environment
source .venv/bin/activate
```

### 2. Run Tests (no GPU/models required)

```bash
python3 -m unittest tests.test_packetizer tests.test_normalizer tests.test_wer -v
```

### 3. Live Captioning (Microphone)

```bash
python3 main.py
# Open http://localhost:8765/ in a browser
```

### 4. Test Mode (no GPU required)

```bash
python3 main.py --mock-asr
# Or with YouTube POC
python3 main.py --mock-asr --youtube "https://www.youtube.com/watch?v=YOUR_VIDEO_ID"
```

### 5. YouTube Video with Captions

```bash
python3 main.py --youtube "https://www.youtube.com/watch?v=aicFjagp2BE"
# Open http://localhost:8765/youtube/YOUR_VIDEO_ID
```

---

## Configuration

Configuration is stored in `config/settings.yaml`. Environment variables (prefix `MC_`) override config values:

```bash
export MC_IO_ADAPTER=decklink
export MC_OUTPUT_WEBVTT_PORT=9000
python3 main.py
```

### Key Config Options

| Section | Options | Default |
|---------|---------|---------|
| `io.adapter` | `alsa`, `youtube`, `decklink` | `alsa` |
| `asr.primary` | `parakeet`, `whisper` | `whisper` |
| `asr.whisper.model` | `large-v3-turbo`, `base.en` | `large-v3-turbo` |
| `caption.normalizer.max_lines` | `1`, `2` | `2` |
| `output.webvtt.port` | any port | `8765` |

---

## Components

### Input Adapters

- **ALSA Adapter** (`microcaption/io/alsa_adapter.py`): Microphone input via GStreamer `pulsesrc`
- **YouTube Adapter** (`microcaption/io/youtube_adapter.py`): Extracts audio from YouTube videos
- **DeckLink Adapter** (`microcaption/io/decklink_adapter.py`): Phase 2 SDI input (stub)

### ASR Backends

- **Parakeet Backend** (`microcaption/asr/parakeet_backend.py`): NeMo Parakeet TDT-CTC model
- **Whisper Backend** (`microcaption/asr/whisper_backend.py`): faster-whisper large-v3-turbo
- **EnergyVAD** (`microcaption/asr/vad.py`): Voice activity detection with RMS threshold

### Caption Processing

- **Normalizer** (`microcaption/caption/normalizer.py`): Text normalization and line wrapping
- **CEA-608 Packetizer** (`microcaption/caption/packetizer_608.py`): 608-E byte encoding
- **CEA-708 Packetizer** (`microcaption/caption/packetizer_708.py`): DTVCC service blocks

### Output Sinks

- **Terminal Sink** (`microcaption/output/terminal_sink.py`): Timestamped console output
- **WebVTT Server** (`microcaption/output/webvtt_server.py`): HTTP server with SSE
- **Packet Logger** (`microcaption/output/packet_logger.py`): JSONL logging

### Monitoring

- **Latency Monitor** (`microcaption/monitor/latency.py`): Tracks inference latency (mean, p95, max)

---

## API Usage

```python
from microcaption.io import create_adapter
from microcaption.asr.pipeline import ASRPipeline
from microcaption.caption.normalizer import CaptionNormalizer
from microcaption.caption.packetizer_608 import CEA608Packetizer
from microcaption.output.webvtt_server import WebVTTServer

# Create components
cfg = {'io': {'adapter': 'alsa'}, 'asr': {'primary': 'whisper'}}
adapter = create_adapter(cfg)
pipeline = ASRPipeline(cfg.get('asr', {}))
normalizer = CaptionNormalizer(cfg.get('caption', {}).get('normalizer', {}))
packetizer = CEA608Packetizer(cfg.get('caption', {}).get('packetizer', {}))

# Wire up callback
def on_caption(result):
    print(f"Caption: {result.text}")

pipeline.set_caption_callback(on_caption)
adapter.set_audio_callback(pipeline.on_audio)
```

---

## File Structure

```
MicroCaption/
├── main.py                    # Entry point (CLI)
├── setup.sh                   # Environment bootstrap
├── requirements.txt           # Python dependencies
├── config/
│   ├── settings.yaml         # Default configuration
│   ├── settings.test.yaml    # Test mode overrides
│   └── settings.prod.yaml    # Production template
├── microcaption/
│   ├── __init__.py
│   ├── io/                   # Input adapters (alsa, youtube, decklink)
│   ├── asr/                  # ASR backends (parakeet, whisper, vad)
│   ├── caption/              # Caption processing (normalizer, packetizers)
│   ├── output/               # Output sinks (terminal, webvtt, logger)
│   └── monitor/              # Latency monitoring
├── tests/                    # Unit tests
├── tools/                    # Utility scripts (validate_wer.py)
├── logs/                     # Generated packet logs
└── players/                  # Video player overlay
```

---

## Testing

### Unit Tests

```bash
# All tests (no GPU/models required)
python3 -m unittest tests.test_packetizer tests.test_normalizer tests.test_wer -v
```

### WER Benchmarking

```bash
# Compare transcript against reference
python3 tools/validate_wer.py reference.txt hypothesis.txt
```

---

## Known Limitations

1. **YouTube Real-time Captioning**: Currently displays YouTube video via browser embed. Real-time captioning of YouTube audio requires additional GStreamer audio extraction (future work).

2. **NeMo Installation**: The primary Parakeet ASR engine requires `nemo_toolkit[asr]` to be installed. The config defaults to Whisper as fallback.

---

## License

This project is developed for demonstration and educational purposes.

---

## Contact

- **Developer**: Peter Dews (info@peterdews.com)
- **Project**: MicroCaption CTA-708 Real-Time Captioning System
- **System**: Ubuntu 26.04 "Resolute" with NVIDIA RTX GPU support
# MicroCaption — CTA-708 & CEA-608 Real-Time Closed Captioning System

MicroCaption is a real-time, low-latency closed captioning prototype designed to ingest live audio, perform automatic speech recognition (ASR), normalize transcripts to broadcasting standards, package them into CEA-608/CTA-708 bitstreams, and deliver WebVTT caption streams to a web-based client interface.

It features a modular adapter architecture, dual-backend ASR capabilities (NVIDIA Parakeet and Faster-Whisper), a multi-session Web Dashboard, and built-in support for secure public ingress via Tailscale Funnel.

---

## ── System Architecture ──

The system operates as a multi-layered pipeline:

```
                  ┌──────────────────────────────┐
                  │   Audio Sources / Ingest     │
                  │ (ALSA Mic / YouTube Stream)  │
                  └──────────────┬───────────────┘
                                 │ GStreamer / F32LE 16kHz
                                 ▼
                  ┌──────────────────────────────┐
                  │    Shared ASR Backend        │
                  │ (GPU-Accelerated Model VRAM) │
                  └──────────────┬───────────────┘
                                 │ Text Segments
                                 ▼
                  ┌──────────────────────────────┐
                  │      Caption Pipeline        │
                  │ (VAD / Normalizer / Decimator)│
                  └──────────────┬───────────────┘
                                 │ Normalized Lines (max 32 char, 2 lines)
                                 ▼
         ┌───────────────────────┴───────────────────────┐
         ▼                                               ▼
┌──────────────────┐                            ┌──────────────────┐
│  Packet Layer    │                            │  Delivery Layer  │
│ (CEA-608/708 hex)│                            │(WebVTT Server/SSE)│
└────────┬─────────┘                            └────────┬─────────┘
         ▼                                               ▼
┌──────────────────┐                            ┌──────────────────┐
│ logs/cea608.jsonl│                            │ Browser Dashboard│
│ logs/cea708.jsonl│                            │  & Video Overlay │
└──────────────────┘                            └──────────────────┘
```

1. **Ingest Layer**: Captures audio using GStreamer. Ingests raw audio feeds (microphone via PulseAudio/ALSA or streaming media URLs resolved using `yt-dlp`). Resamples audio to mono float32 16kHz before forwarding to ASR.
2. **ASR Inference Layer**: Runs GPU-accelerated speech-to-text. Coordinates model loading in CUDA memory via `SharedASRBackend`. Employs a mutex lock to serialize multi-session transcription tasks, maintaining high throughput on consumer GPU hardware.
3. **Caption Normalization**: Constrains output text to a television safe-title area (max 32 characters per line, max 2 rollup lines) and formats typography using `CaptionNormalizer`.
4. **Packetization**: Encodes lines into CEA-608 odd-parity byte pairs and CEA-708 DTVCC data packets, logging the hex output for SDI insertion.
5. **WebVTT & SSE Server**: Serves a multi-page web client dashboard, pushes live caption frames to connected viewers using Server-Sent Events (SSE), and generates WebVTT downloads.

---

## ── Key Features ──

* **Concurrent Multi-Session Control Room**: Manage and monitor multiple independent streaming sessions from a single unified web dashboard.
* **Dual ASR Backends**: NVIDIA Parakeet (via `nemo_toolkit`) as primary for CTC-based streaming ASR, or `faster-whisper` (Whisper `large-v3-turbo` on CUDA) as the default high-accuracy fallback.
* **Smart Silence & Hallucination Suppression**: Uses Energy VAD thresholding, segment confidence gating, and a strict semantic blocklist to prevent Whisper from outputting repeating loops during quiet intervals.
* **Real-Time Latency Monitoring**: Visualizes ASR metrics (mean, p95, and max transcription latency) and processing rates directly on the monitor dashboard.
* **CEA-608 & CEA-708 Compliance**: Logs fully serialized closed caption stream byte dumps for diagnostic inspection and broadcast validation.
* **Tailscale Funnel Integration**: Secure, zero-config HTTPS deployment for public demonstrations with auto-renewed TLS certificates.

---

## ── System Requirements ──

* **Operating System**: Ubuntu 26.04 "Resolute" (also verified on Ubuntu 24.04).
* **Processor & GPU**: Modern x86_64 CPU; NVIDIA RTX GPU with CUDA-enabled drivers (compatible with CUDA 12.8).
* **Python**: Python 3.14.4.
* **GStreamer**: System-wide installation of GStreamer libraries, python bindings, and plugins.

---

## ── Installation & Build ──

MicroCaption requires system-level dependencies for GStreamer and audio loopbacks. Setup is automated using the `setup.sh` script, which builds a Python virtual environment linking local GStreamer Python bindings.

### 1. Execute Setup Script
Run the script as a regular user. It will internally request `sudo` credentials to install apt packages:
```bash
./setup.sh
```

The setup script handles:
* Installing GStreamer packages, PulseAudio development libs, `cmake`, and system compilation utilities.
* Creating the virtual environment (`.venv`) using the `--system-site-packages` flag so GStreamer's `python3-gi` bindings are accessible inside the environment.
* Installing `numpy`, `PyYAML`, `PyJWT[crypto]`, and PyTorch configured for CUDA 12.8.
* Compiling and installing `onnx` from source (applying workarounds for Protobuf packaging on Python 3.14).
* Downloading and configuring ASR packages (`nemo_toolkit[asr]` and `faster-whisper`).

### 2. Activate Virtual Environment
Once setup is complete, activate the environment:
```bash
source .venv/bin/activate
```

---

## ── Running the Application ──

### Launcher Script (Recommended)
Always start the application using the launcher script:
```bash
./run.sh
```
`run.sh` automatically configures `PYTHONUNBUFFERED=1` and dynamically appends the system's CUDA runtime libraries (e.g., Ollama's `libcublas.so` directory) to `LD_LIBRARY_PATH` to ensure GPU-accelerated transcription loads without linking errors.

### Starting with CLI Arguments
If executing the entry point manually:
```bash
# Standard Live Mode (using ALSA default audio input, loaded ASR models)
python3 main.py

# Pipeline Mock Mode (runs without GPU/model loading; uses simulated captions for tests)
python3 main.py --mock-asr

# Custom Configuration File
python3 main.py --config config/settings.test.yaml

# List Available PulseAudio Input Sources
python3 main.py --list-devices
```

---

## ── Configuration & Overrides ──

System settings are managed via YAML files located in [config/](file:///home/joe/Development/micro-caption/config).

### Configuration Profiles

* **[config/settings.yaml](file:///home/joe/Development/micro-caption/config/settings.yaml)**: Default config for local testing, utilizing the system microphone (`alsa` adapter) and Whisper `large-v3-turbo` in `int8` quantization.
* **[config/settings.test.yaml](file:///home/joe/Development/micro-caption/config/settings.test.yaml)**: Optimized overrides for fast offline testing. Speeds up iterations by using mock inputs, short chunk windows (1.0s), and verbose hex debug output.
* **[config/settings.prod.yaml](file:///home/joe/Development/micro-caption/config/settings.prod.yaml)**: Config template ready for hardware integration, preparing inputs for SDI (via DeckLink PCI cards) and forcing OAuth2 user validation.

### Key Configuration Directives

```yaml
io:
  adapter: alsa          # alsa | decklink (Phase 2 input)
  alsa:
    device: default      # PulseAudio source name

asr:
  primary: whisper       # parakeet | whisper
  chunk_duration: 2.0    # Duration of audio window evaluated
  step_duration: 1.0     # Time to advance window per step (1.0s overlap)
  whisper:
    model: large-v3-turbo
    compute_type: int8   # float16 | int8 (int8 halves GPU load with minimal WER impact)
```

### Environment Overrides
You can override configuration keys by prefixing them with `MC_` in the terminal:
```bash
# Override ASR Engine and HTTP Server Port
MC_ASR_PRIMARY=parakeet MC_OUTPUT_WEBVTT_PORT=9000 ./run.sh
```

Supported environment variables:
* `MC_CONFIG`: Path to the YAML settings file.
* `MC_ASR_PRIMARY`: Switch target ASR engine (`parakeet` | `whisper`).
* `MC_OUTPUT_WEBVTT_PORT`: HTTP dashboard server port (defaults to `8765`).
* `MC_ALSA_DEVICE`: Input device descriptor.
* `MC_AUTH_CLIENT_ID`, `MC_AUTH_CLIENT_SECRET`, `MC_AUTH_SESSION_SECRET`, `MC_AUTH_REDIRECT_URI`: Parameters to configure Google OAuth2 sign-in.

---

## ── Using the Web Dashboard ──

Once launched, access the web control room by visiting:
**`http://localhost:8765/`**

### Landing Page & Control Room
* **Dashboard**: Displays active caption sessions. Shows system status indicators (yellow `STARTING`, green `LIVE`, red `ERROR`), stream type badges, uptime, generated cue count, and the last transcribed sentence.
* **Add Stream**: Submit a URL (e.g. YouTube stream or HLS stream). The system extracts the audio track using `yt-dlp` and boots up a dedicated session in the background.
* **Stop Session**: Halts audio ingestion, kills background threads, and resets session accumulators.

### Video Player with Live Caption Overlay
Clicking **Watch** on an active session opens the browser-based player:
* **Embedded View**: Embeds the video feed side-by-side with a floating, high-contrast, black-background caption box.
* **SSE Sync**: The overlay reads from the `/events/<session_id>` endpoint using Server-Sent Events, updating text elements synchronously without client polling.
* **WebVTT Export**: Click **WebVTT** in the navigation bar to download the complete WebVTT file of transcribed captions generated during the session.

---

## ── Tailscale Funnel Public Setup ──

Expose your local development dashboard securely on the public internet using Tailscale Funnel.

1. **Verify Tailscale Login**:
   ```bash
   tailscale status
   ```
2. **Enable Funnel in Tailnet**:
   Visit the [Tailscale Admin ACL Console](https://login.tailscale.com/admin/acls) and add the `funnel` node attribute:
   ```json
   "nodeAttrs": [
     {
       "target": ["*"],
       "attr": ["funnel"]
     }
   ]
   ```
3. **Grant Normal User Access** (removes the requirement for sudo on funnel commands):
   ```bash
   sudo tailscale set --operator=$USER
   ```
4. **Initiate Public Funnel Ingress**:
   ```bash
   tailscale funnel --bg 8765
   ```
   *The `--bg` flag saves the configuration persistently, ensuring public ingress restarts automatically following system reboots.*
5. **Access the Public URL**:
   Your dashboard is now accessible securely on:
   `https://<machine-name>.<tailnet-name>.ts.net/`

---

## ── Testing & Validation ──

### 1. Running Unit Tests
Validate the formatting, normalization, and packaging layers by running the test suite:
```bash
python3 -m unittest tests.test_packetizer tests.test_normalizer tests.test_wer -v
```

### 2. Word Error Rate (WER) Validation Tool
To assess speech recognition accuracy, use the included validation script to calculate Word Error Rates (WER) using Levenshtein distance:
```bash
# Compare a reference text transcript against the generated CEA-608 JSONL log
python3 tools/validate_wer.py reference_transcript.txt logs/cea608.jsonl --from-jsonl

# Compare two raw text transcriptions
python3 tools/validate_wer.py reference.txt hypothesis.txt

# Output detailed breakdown (substitutions, deletions, insertions) in JSON format
python3 tools/validate_wer.py reference.txt hypothesis.txt --json
```

---

## ── Known Limitations & Roadmap ──

* **Audio/Video Sync Drift**: When playing YouTube videos via the browser player, GStreamer pulls the audio track independently. Over extended periods, the browser video feed and the processed ASR caption pipeline can experience sync drift.
  * *Roadmap*: Leverage the YouTube IFrame API's `getCurrentTime()` function on the client side to periodically synchronize the ASR pipeline's playhead position.
* **YouTube CDN URL Expiration**: Live streams running for more than 6 hours can drop out when YouTube CDN URLs expire.
  * *Roadmap*: Implement an automated watchdog inside `YouTubeAdapter` to re-resolve URLs via `yt-dlp` upon stream termination.
* **Batch Inference**: Currently, the `SharedASRBackend` transcribes streams sequentially using a global mutex.
  * *Roadmap*: Redesign `SharedASRBackend` to accumulate audio chunks across all active sessions and dispatch them in a single batch GPU call to scale concurrent stream capacity beyond ~12 streams.
