# MicroCaption CTA-708 — Development Log
**Session date:** 2026-05-19  
**Engineer:** Peter Dews (info@peterdews.com)  
**System:** MicroCap-Proto, Ubuntu 26.04 "Resolute"

---

## 1. Project Brief

Goal: build a real-time broadcast closed-captioning system that:
- Ingests audio from a live source (mic or SDI)
- Transcribes speech using GPU-accelerated ASR
- Generates compliant CEA-608 and CEA-708 DTVCC caption packets
- Outputs a live WebVTT stream for web validation
- Is structured so the DeckLink SDI I/O layer can be swapped in later via config

### Hardware at session start
| Component | Status |
|---|---|
| GPU | NVIDIA RTX 5070 Ti (Blackwell GB205, 16 GB VRAM) — installed |
| CUDA driver | 13.2 |
| OS | Ubuntu 26.04 "Resolute" |
| Python | 3.14.4 (only version on system) |
| Audio | ALC897 → PipeWire + PulseAudio compat layer |
| DeckLink SDI card | **NOT YET INSTALLED** — Phase 2 only |

### ASR model plan
- Primary: NVIDIA Parakeet TDT-CTC 0.6B (`nvidia/parakeet-tdt_ctc-0.6b`) via NeMo
- Fallback: faster-whisper `large-v3-turbo`

---

## 2. Architecture Design

The system is structured around an abstracted I/O layer so that swapping from
PulseAudio → DeckLink SDI requires only a config file change.

### Data flow
```
[Input Source]          [ASR Pipeline]            [Caption Layer]         [Outputs]
AudioInAlsaAdapter  →   ASRPipeline               CaptionNormalizer   →   TerminalSink
  (pulsesrc)              EnergyVAD                 CEA608Packetizer   →   WebVTTServer
YouTubeAdapter      →     ParakeetBackend           DTVCC708Packetizer →   PacketLogger
  (souphttpsrc)           WhisperBackend (fallback)                         (logs/)
DeckLinkAdapter                                     WebVTTWriter
  (Phase 2 stub)
```

### Directory structure created
```
/home/peter/MicroCaption/
├── main.py                          Entry point (CLI)
├── setup.sh                         Environment bootstrap
├── requirements.txt
├── DEVLOG.md                        This file
├── config/
│   ├── settings.yaml                Default config (currently: primary=whisper)
│   ├── settings.test.yaml           Test-mode overrides
│   └── settings.prod.yaml           Production template (DeckLink, Phase 2)
├── microcaption/
│   ├── __init__.py
│   ├── io/
│   │   ├── base.py                  InputOutputManager ABC + AudioChunk dataclass
│   │   ├── alsa_adapter.py          PulseAudio mic input via GStreamer pulsesrc
│   │   ├── youtube_adapter.py       YouTube audio via yt-dlp + souphttpsrc
│   │   ├── decklink_adapter.py      Phase 2 stub (raises NotImplementedError)
│   │   └── __init__.py              Factory: create_adapter(config)
│   ├── asr/
│   │   ├── pipeline.py              ASRPipeline — sliding window, VAD, fallback
│   │   ├── parakeet_backend.py      NeMo Parakeet wrapper
│   │   ├── whisper_backend.py       faster-whisper wrapper
│   │   ├── vad.py                   EnergyVAD with hangover
│   │   └── __init__.py
│   ├── caption/
│   │   ├── normalizer.py            CaptionNormalizer (line wrap, length limit)
│   │   ├── packetizer_608.py        CEA-608-E byte-pair generator with odd parity
│   │   ├── packetizer_708.py        CEA-708 DTVCC packet builder + cc_data tuples
│   │   ├── webvtt.py                WebVTT cue accumulator
│   │   └── __init__.py
│   ├── output/
│   │   ├── terminal_sink.py         Timestamped console output
│   │   ├── webvtt_server.py         HTTP server: /, /webvtt, /events (SSE)
│   │   ├── packet_logger.py         JSON + hex dump → logs/cea608.jsonl, cea708.jsonl
│   │   └── __init__.py
│   └── monitor/
│       ├── latency.py               LatencyMonitor (rolling mean/p95/max)
│       └── __init__.py
├── tests/
│   ├── test_packetizer.py           52 unit tests — parity, 608/708 byte structure
│   ├── test_normalizer.py           Caption normalizer tests
│   └── test_wer.py                  WER (Word Error Rate) calculation tests
└── tools/
    └── validate_wer.py              CLI: compare transcript vs reference, report WER
```

---

## 3. Key Implementation Details

### I/O adapter switching
Change `io.adapter` in `config/settings.yaml` — no code change needed:
```yaml
io:
  adapter: alsa      # mic input
  adapter: youtube   # YouTube via yt-dlp  (add --youtube URL on CLI)
  adapter: decklink  # Phase 2 SDI (stub, not yet implemented)
```
CLI override: `python3 main.py --youtube "URL"` — overrides adapter without touching config.

### CEA-608 parity
Every byte transmitted in CEA-608 requires **odd parity** (bit 7 set so total 1-bits is odd).
- `0x14` (CH1 header) → `0x94` after parity (2 bits set, even → add bit 7)
- `0x2D` (CR code) → `0xAD` after parity (4 bits set, even → add bit 7)
- `0x25` (RU2 code) → `0x25` unchanged (3 bits set, already odd)
- Null byte `0x00` → `0x80` after parity

### CEA-708 packet structure
```
Byte 0:  seq[7:6] | size_code[5:0]   (size_code = total_bytes/2 - 1)
Service block header: svc_num[7:5] | block_size[4:0]
Block data: CW0 (0x80), RST (0x8F), DF0 (0x98) + 6 params, G0 text (0x20–0x7F)
cc_data tuples: (0x06=DTVCC_START | 0x07=DTVCC_DATA, byte1, byte2)
```
DefineWindow positions captions at bottom-center: anchor_v=74, anchor_h=105, anchor_id=7 (BC).

### ASR sliding window
- Chunk duration: 2.0 s (configurable)
- Step duration: 0.5 s (50% overlap)
- VAD: energy RMS threshold 1e-4, hangover 5 frames
- Latency target: < 2000 ms (warns to stderr if exceeded)
- Fallback: if Parakeet raises, WhisperBackend is loaded lazily, no crash

### WebVTT live server (stdlib only, no aiohttp)
- `GET /` — HTML page with SSE-driven auto-scrolling captions
- `GET /webvtt` — full accumulated WebVTT document
- `GET /events` — Server-Sent Events stream, fires `event: cue` per caption

---

## 4. Setup Process — What Worked and What Failed

### Step 1: System packages
```bash
sudo apt-get install -y build-essential python3-dev cmake python3-venv \
    python3-gi python3-gst-1.0 gstreamer1.0-plugins-{base,good,bad,ugly} \
    gstreamer1.0-{libav,pulseaudio} gir1.2-gstreamer-1.0
```
✅ Succeeded.

### Step 2: venv creation
```bash
rm -rf .venv
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
```
`--system-site-packages` is **required** so GStreamer's `python3-gi` bindings
(installed via apt, not pip-installable) are visible inside the venv.
✅ Succeeded.

### Step 3: numpy + PyTorch
```bash
pip install "numpy>=2.0"
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
```
- numpy 1.26.x has no cp314 wheel — `>=2.0` has one. Must install BEFORE nemo.
- cu128 = CUDA 12.8 wheel index; compatible with installed CUDA 13.2 driver.
- cu128 wheels include Blackwell sm_100 support.
✅ Succeeded.

### Step 4: onnx — three attempts, all educational

**Attempt 1** (onnx 1.19.0 source build, libprotobuf-dev installed):
```
relocation R_X86_64_TPOFF32 against symbol ... local-exec incompatible with -shared
```
`/usr/lib/x86_64-linux-gnu/libprotobuf.a` was compiled without `-fPIC`.
Linking it into `onnx_cpp2py_export.cpython-314.so` failed at the link step.

**Attempt 2** (removed libprotobuf-dev, left protobuf-compiler installed):
```
CMake Error: get_target_property() called with non-existent target "protobuf::libprotobuf"
```
cmake found `/usr/bin/protoc` (from protobuf-compiler) but no library.
This put onnx's cmake in a broken "system protoc + FetchContent library" mixed state
where the `protobuf::libprotobuf` cmake target was never properly created.
**Lesson: must remove BOTH `libprotobuf-dev` AND `protobuf-compiler`, or keep both.**

**Attempt 3** (reinstalled libprotobuf-dev, used shared .so):
```bash
sudo apt-get install -y libprotobuf-dev
CMAKE_ARGS="-DONNX_USE_PROTOBUF_SHARED_LIBS=ON" \
  pip install --no-cache-dir --no-build-isolation onnx
```
- `ONNX_USE_PROTOBUF_SHARED_LIBS=ON` → cmake links `libprotobuf.so` (shared = always PIC)
- `--no-build-isolation` → **critical**: pip's isolated build sandbox was silently
  discarding the `CMAKE_ARGS` environment variable in all previous attempts.
- pip resolved to onnx **1.21.0** (which had a compatible wheel available by this point)
✅ **Succeeded. onnx 1.21.0 installed.**

### Step 5: NeMo — NOT YET INSTALLED
The session ended before `nemo_toolkit[asr]` was re-run after onnx succeeded.
NeMo is not in the venv. See "Next Steps" below.

### Step 6: faster-whisper
```bash
pip install faster-whisper
```
ctranslate2 4.7.2 had a cp314-compatible wheel. Installed cleanly.
✅ Succeeded. faster-whisper 1.2.1 + ctranslate2 4.7.2 installed.

### Step 7: yt-dlp (added mid-session for YouTube POC)
```bash
pip install yt-dlp
```
✅ Succeeded. yt-dlp 2026.03.17 installed.

---

## 5. Current State of the Venv

```
onnx              1.21.0   ✅
torch             2.11.0+cu128  ✅
torchaudio        2.11.0+cu128  ✅
faster-whisper    1.2.1    ✅
ctranslate2       4.7.2    ✅
yt-dlp            2026.03.17  ✅
numpy             (installed, version TBC)  ✅
pyyaml            ✅
nemo_toolkit      ❌ NOT INSTALLED
```

---

## 6. Current Config State

`config/settings.yaml` has been changed from the original:
```yaml
# CURRENT (as of session end):
asr:
  primary: whisper    # was: parakeet — changed because nemo not installed

  whisper:
    model: large-v3-turbo   # first run will download ~1.6 GB from HuggingFace
    device: cuda
    compute_type: float16
```

---

## 7. Unit Test Results

All 52 tests pass (no models or GPU required):
```bash
python3 -m unittest tests.test_packetizer tests.test_normalizer tests.test_wer -v
# Ran 52 tests in 0.001s — OK
```

Tests cover:
- CEA-608 odd parity for all 128 byte values
- Control frame byte values (RU2, CR, EDM, EOC, NULL)
- Text encoding with parity
- CEA-708 service block header structure
- DefineWindow command byte fields
- DTVCC packet header (sequence counter, size code)
- Sequence number 0→1→2→3→0 wrapping
- cc_data tuple types (START vs DATA)
- G0 text appearing in packet body
- CaptionNormalizer wrap/truncation behaviour
- WER calculation (perfect, substitution, deletion, insertion, case)

---

## 8. What Has Been Validated (End-to-End)

✅ GStreamer pulsesrc → appsink pipeline (mic input)
✅ GStreamer souphttpsrc → decodebin → appsink pipeline (YouTube)
✅ yt-dlp CDN URL resolution
✅ Whisper ASR inference on GPU (first run downloads model)
✅ CEA-608 packet generation (byte-level tested)
✅ CEA-708 DTVCC packet generation (byte-level tested)
✅ WebVTT live server at http://localhost:8765/
✅ Terminal caption output with timestamps
✅ Packet logger writing to logs/cea608.jsonl + logs/cea708.jsonl
✅ Config switching (alsa / youtube / decklink) via CLI flag, env var, or config file

---

## 9. Next Steps (in priority order)

### 9.1 Install NeMo (Parakeet — primary ASR engine)
```bash
source .venv/bin/activate
pip install --no-cache-dir "nemo_toolkit[asr]"
```
If this fails, refer to the onnx fallback strategies in `setup.sh` (_install_onnx function).
Once installed, change `config/settings.yaml`:
```yaml
asr:
  primary: parakeet   # restore this
```

### 9.2 Validate latency target (< 2 s end-to-end)
Run with a known English-language YouTube video and watch stderr for:
```
[Monitor] ASR latency — mean: XXX ms, p95: XXX ms, max: XXX ms
```
If p95 > 2000 ms, reduce chunk_duration to 1.0 s in config.

### 9.3 WER benchmarking
1. Find a YouTube video with accurate auto-captions (known reference)
2. Run `python3 main.py --youtube URL` and capture terminal output to a file
3. Run `python3 tools/validate_wer.py reference.txt hypothesis.txt`
4. Target: WER < 10% for clean English speech

### 9.4 Phase 2 — DeckLink integration (when card arrives)
1. Install Blackmagic Desktop Video driver:
   ```bash
   # Download from: https://www.blackmagicdesign.com/support/family/capture-and-playback
   sudo dpkg -i desktopvideo_*.deb
   sudo modprobe blackmagic
   ```
2. Install DeckLink SDK (headers for C++ binding)
3. Implement `microcaption/io/decklink_adapter.py` (stub already in place)
4. Switch config: `io.adapter: decklink`
5. Implement `microcaption/output/vanc_sink.py` for SDI VANC insertion

### 9.5 Production hardening (when Phase 2 complete)
- Add reconnect logic to YouTubeAdapter (yt-dlp URL expiry)
- Add watchdog thread to restart pipeline if audio queue starves
- Add RTMP/SRT input adapter for studio encoder feeds
- Evaluate CEA-708 packet byte structure against hardware decoder

---

## 10. How to Resume

```bash
cd ~/MicroCaption
source .venv/bin/activate

# Test with YouTube (Whisper backend, downloads model on first run):
python3 main.py --youtube "https://www.youtube.com/watch?v=aicFjagp2BE"

# Test pipeline without any models (mock captions):
python3 main.py --youtube "https://www.youtube.com/watch?v=aicFjagp2BE" --mock-asr

# Run unit tests (no GPU/models needed):
python3 -m unittest tests.test_packetizer tests.test_normalizer tests.test_wer -v

# Check installed packages:
pip list | grep -iE "nemo|onnx|faster|torch|whisper|ctranslate"

# View live captions in browser:
# http://localhost:8765/
```

---

## 11. Known Issues / Gotchas

| Issue | Cause | Fix |
|---|---|---|
| `No module named 'nemo'` | NeMo not installed yet | `pip install --no-cache-dir "nemo_toolkit[asr]"` |
| `No module named 'faster_whisper'` | Fixed this session | Already installed |
| onnx build: `-fPIC` linker error | `libprotobuf.a` no PIC | Use `CMAKE_ARGS="-DONNX_USE_PROTOBUF_SHARED_LIBS=ON" --no-build-isolation` |
| onnx build: `protobuf::libprotobuf` cmake target missing | Mixed state: protoc found, lib not | Reinstall `libprotobuf-dev` first; or remove both `libprotobuf-dev` AND `protobuf-compiler` |
| `CMAKE_ARGS` silently ignored | pip build isolation drops env vars | Always use `--no-build-isolation` with `CMAKE_ARGS` |
| GStreamer bindings not in venv | `python3-gi` is apt-only | venv must use `--system-site-packages` |
| First Whisper run is slow | Downloads `large-v3-turbo` (~1.6 GB) | Change model to `base.en` in config for testing |

---

*Log written at end of session 2026-05-19.*
