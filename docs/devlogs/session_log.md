# MicroCaption — Devlog Index

Table of contents and summary for all development session logs. Each session
has its own `DEVLOG_SESSION{N}_{date}.md` file in this directory; this file is
the running index. **Add a new row here whenever you add a devlog.**

| # | Date | Branch | AI | Summary |
|---|------|--------|----|---------|
| [0](DEVLOG_SESSION0_2026-03-23.md) | 2026-03-23 | — | — | Initial prototype log: browser-based YouTube player with caption overlay, yt-dlp stream extraction, WebVTT server endpoints (`/`, `/webvtt`, `/events`, `/youtube/ID`). Real-time YouTube→Whisper audio not yet wired (mock/mic captions only). |
| [1](DEVLOG_SESSION1_2026-05-19.md) | 2026-05-19 | — | — | Project brief & architecture. Built core CEA-608/CEA-708 DTVCC packetizer pipeline, unit-tested the packet layer, laid out the swappable I/O adapter architecture (mic/SDI). Validated at code level; not yet run end-to-end. |
| [2](DEVLOG_SESSION2_2026-05-21.md) | 2026-05-21 | Sonnet 4.6 | Got YouTube → GStreamer → Whisper → CEA-608/708 → browser overlay running end-to-end against a live URL. Fixed blocking bugs, exposed the demo publicly via Tailscale Funnel, built the multi-session web UI. |
| [3](DEVLOG_SESSION3_2026-05-22_00-54.md) | 2026-05-22 | Sonnet 4.6 | Multi-page control dashboard (Dashboard, Monitor, Config, Logs), multiple concurrent / non-YouTube streams, Whisper hallucination suppression, multi-stream architecture, and GPU performance analysis & tuning. |
| [4](DEVLOG_SESSION4_2026-06-15.md) | 2026-06-15 | Sonnet 4.6 | Public watch viewer (`/watch/<code>`), dashboard hardening (QR codes, no-flicker updates, live caption sync), replay mode, mobile-responsive landing page, and fixed the dashboard card duplicate bug. |
| [5](DEVLOG_SESSION5_2026-06-15_22-30.md) | 2026-06-15 | Opus 4.8 | Caption readability/UX pass: end-of-stream handling, caption buffering for the caption-only viewer, fixed caption-log replay, fullscreen caption visibility, and standardized button styling site-wide. |
| [6](DEVLOG_SESSION6_2026-06-15_23-23.md) | 2026-06-15 | Opus 4.8 | Built the Monitor page from a stub into a real performance-observability surface: GPU utilization readout plus real-time detail charts, ahead of GPU stress testing. (commit `5a8bfe4`) |
| [7](DEVLOG_SESSION7_2026-06-16_01-55.md) | 2026-06-16 | Opus 4.8 | Diagnose-and-fix: GPU-utilization chart sawtoothing with a single stream. Explained the spike cause and smoothed the chart to report honest duty cycle (Option A). (commit `c420df8`) |

---

## Branch lineage

- **Sessions 2–3:** `YouTube-Ingest---ASR-Validation` (initially `update/youtube-asr-validation`)
- **Session 4:** `feat/landing-page`
- **Sessions 5–7:** `feat/make-captions-readable`

## Conventions

- One devlog per working session, named `DEVLOG_SESSION{N}_{YYYY-MM-DD}.md`
  (optionally `_{HH-MM}` when multiple sessions share a day).
- Common sections: Session Goals, Files Changed, Testing/Verification,
  How to Resume, Open Items.
