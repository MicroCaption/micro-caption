# MicroCaption — Devlog Index

Table of contents and summary for all development session logs. Each session
has its own `DEVLOG_SESSION{N}_{date}.md` file in this directory; this file is
the running index, **most recent first**. **Add a new row at the top of the table whenever you add a devlog.**

| # | Date | Branch | AI | Summary |
|---|------|--------|----|---------|
| [10](DEVLOG_SESSION10_2026-06-16_19-12.md) | 2026-06-16 | Opus 4.8 | **Pivot RTMP → SDI** (Blackmagic **DeckLink Duo 2**, already in the box). Found the card present but **Desktop Video driver not installed** (no kernel module/runtime → GStreamer enumerates zero devices); the `decklink` + `closedcaption` plugins (incl. `decklinkvideosink cc-line` VANC insert + Duo 2 duplex `profile`) are present. Shipped **Stage 1** (new `decklink_devices.py` enumeration + cached signal status, `GET /api/sdi/devices`, dashboard SDI source toggle + device picker + live connector status strip) and **Stage 2** (real `DeckLinkAdapter`: `sdi://<index>` embedded SDI audio → ASR; `main.py` dispatch; `source_type='sdi'`). 73 tests pass; degrades gracefully without the driver. **Stage 0 (driver install + reboot) is next, user-run**; Stage 3 (SDI-out passthrough + CEA-708 VANC) planned, gated on the card. |
| [9](DEVLOG_SESSION9_2026-06-16_18-55.md) | 2026-06-16 | Opus 4.8 | **Scope pivot** to a broadcast inline pass-through captioner (RTMP/SDI **in** → caption → RTMP/SDI **out**, captions embedded) — YouTube was only ever a dev source. Mapped the gap (adapter selection was a no-op; YouTube adapter is yt-dlp+`souphttpsrc` HTTP-only; no video plane or egress; DeckLink still a stub) and agreed a staged plan A–D. Shipped **Stage A**: new `StreamAdapter` (rtmp/rtsp/srt ingest via GStreamer `uridecodebin`, no yt-dlp, dynamic-pad audio→ASR / video→fakesink) + scheme-based dispatch in `main.py`. Monitor-only; egress (Stage C) to be planned. |
| [8](DEVLOG_SESSION8_2026-06-16_18-00.md) | 2026-06-16 | Opus 4.8 | Re-prioritized for live captioning (accuracy→readability→sync). Got **Parakeet** running in-process on Python 3.14 (uv + CMake flag + torch cu128 + vendored real cuDNN 9.19) as primary with **Whisper hot-standby auto-failover/recovery**; Phase-1 broadcast-style video↔caption sync (delay + cache-then-reveal + timeline pacer, fixed an ~8 s caption-lag calibration bug); built the **Logs** page into a Heroic-style log manager with stream-history archiving. |
| [7](DEVLOG_SESSION7_2026-06-16_01-55.md) | 2026-06-16 | Opus 4.8 | Diagnose-and-fix: GPU-utilization chart sawtoothing with a single stream. Explained the spike cause and smoothed the chart to report honest duty cycle (Option A). (commit `c420df8`) |
| [6](DEVLOG_SESSION6_2026-06-15_23-23.md) | 2026-06-15 | Opus 4.8 | Built the Monitor page from a stub into a real performance-observability surface: GPU utilization readout plus real-time detail charts, ahead of GPU stress testing. (commit `5a8bfe4`) |
| [5](DEVLOG_SESSION5_2026-06-15_22-30.md) | 2026-06-15 | Opus 4.8 | Caption readability/UX pass: end-of-stream handling, caption buffering for the caption-only viewer, fixed caption-log replay, fullscreen caption visibility, and standardized button styling site-wide. |
| [4](DEVLOG_SESSION4_2026-06-15.md) | 2026-06-15 | Sonnet 4.6 | Public watch viewer (`/watch/<code>`), dashboard hardening (QR codes, no-flicker updates, live caption sync), replay mode, mobile-responsive landing page, and fixed the dashboard card duplicate bug. |
| [3](DEVLOG_SESSION3_2026-05-22_00-54.md) | 2026-05-22 | Sonnet 4.6 | Multi-page control dashboard (Dashboard, Monitor, Config, Logs), multiple concurrent / non-YouTube streams, Whisper hallucination suppression, multi-stream architecture, and GPU performance analysis & tuning. |
| [2](DEVLOG_SESSION2_2026-05-21.md) | 2026-05-21 | Sonnet 4.6 | Got YouTube → GStreamer → Whisper → CEA-608/708 → browser overlay running end-to-end against a live URL. Fixed blocking bugs, exposed the demo publicly via Tailscale Funnel, built the multi-session web UI. |
| [1](DEVLOG_SESSION1_2026-05-19.md) | 2026-05-19 | — | — | Project brief & architecture. Built core CEA-608/CEA-708 DTVCC packetizer pipeline, unit-tested the packet layer, laid out the swappable I/O adapter architecture (mic/SDI). Validated at code level; not yet run end-to-end. |
| [0](DEVLOG_SESSION0_2026-03-23.md) | 2026-03-23 | — | — | Initial prototype log: browser-based YouTube player with caption overlay, yt-dlp stream extraction, WebVTT server endpoints (`/`, `/webvtt`, `/events`, `/youtube/ID`). Real-time YouTube→Whisper audio not yet wired (mock/mic captions only). |

---

## Branch lineage

- **Sessions 2–3:** `YouTube-Ingest---ASR-Validation` (initially `update/youtube-asr-validation`)
- **Session 4:** `feat/landing-page`
- **Sessions 5–7:** `feat/make-captions-readable`
- **Sessions 8–10:** `feat/the-forest`

## Conventions

- One devlog per working session, named `DEVLOG_SESSION{N}_{YYYY-MM-DD}.md`
  (optionally `_{HH-MM}` when multiple sessions share a day).
- Common sections: Session Goals, Files Changed, Testing/Verification,
  How to Resume, Open Items.
