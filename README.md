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
