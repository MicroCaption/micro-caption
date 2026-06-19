# RTMP Relay Captioner

Take a live RTMP feed (OBS / a hardware encoder), caption it with live ASR, and
re-stream a coherent A/V output **with the captions embedded as CEA-608/708
closed captions in the H.264 SEI** — the form **YouTube Live** ingests.

```
OBS / encoder ──push rtmp──▶ mediamtx :1935 ──pull──▶ MicroCaption ──push rtmp──▶ YouTube
                             (RTMP server)            (caption + re-mux)          (Live)
```

> **YouTube only.** Twitch does **not** ingest embedded closed captions; only
> YouTube Live reliably reads CEA-608/708 from the RTMP H.264 SEI.

## One-time setup

Receiving an RTMP *push* needs an RTMP server in front (GStreamer's `rtmp2src`
only *pulls*). We use [mediamtx](https://github.com/bluenviron/mediamtx):

```bash
server/scratch/fetch_mediamtx.sh          # downloads the binary into server/vendor/
```

## Running a relay

1. **Start the RTMP server:**
   ```bash
   server/vendor/mediamtx/mediamtx server/scratch/mediamtx.yml
   ```
2. **Start MicroCaption** (live ASR, GPU): `./server/run.sh`
3. **Start the client UI:** `cd client && python3 serve.py`
4. **Point your encoder at us.** In OBS → Settings → Stream → Custom:
   - Server: `rtmp://<this-box-ip>:1935/live/stream`
   - Stream key: `stream` (or anything; the path is the key)
5. **Start the relay** in the dashboard → **Relay → YouTube** tab:
   - **Source:** `rtmp://localhost:1935/live/stream` (pre-filled from config)
   - **Destination:** your YouTube ingest URL + key,
     e.g. `rtmp://a.rtmp.youtube.com/live2/xxxx-xxxx-xxxx-xxxx`
   - **Start relay.**

Captions appear on the YouTube watch page (enable CC). Tune
`io.egress.caption_delay_ms` in `settings.yaml` so captions line up with the
picture (≈ ASR latency, default 2500 ms).

## How it works

- `server/microcaption/io/egress.py`
  - `EgressPipeline` — `uridecodebin → [video → cccombiner → x264enc] +
    [audio → AAC] → flvmux → rtmp2sink`, and taps the audio to a 16 kHz mono
    appsink for ASR (one decode, shared clock). Drop-in `InputOutputManager`.
  - `CaptionInjector` — ASR caption text → CEA-608 pairs → frame-paced
    `cc_data` via `ccconverter`, pushed from its **own pacer thread** (pushing
    into `cccombiner` from the video streaming thread dead­locks to
    `NOT_LINKED` on a live pipeline) and stamped onto the running clock by
    `appsrc do-timestamp` so captions align with the picture.
- Dispatch: `start_session(url, dest)` in `server/main.py` → a `relay` session;
  the ASR caption callback feeds `sess.injector.push_lines(...)`.

### Encoder gotchas (hard-won)
- Embedding requires **`x264enc`** (CPU); `vah264enc` can't link the caption
  pad. NVENC/CUDA stay free for Parakeet.
- `cccombiner`'s caption pad must be fed **`format=cc_data`**, *not* `cdp` —
  `x264enc` only writes the A53/`GA94` SEI from `cc_data` metas.

## Validating without YouTube

- `server/scratch/egress_loopback_test.py` — spins up mediamtx + a fake encoder,
  runs the real `EgressPipeline`, and verifies the output carries video + audio
  + `GA94` caption SEI, then decodes the CEA-608 text back. No GPU/YouTube.
- `server/scratch/egress_m3_test.py` — same against a file source.
- `server/scratch/egress_spike.py` — the original embed feasibility proof.
