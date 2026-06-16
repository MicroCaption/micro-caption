# MicroCaption — Devlog Index

A living index of development sessions. Each row links to a full devlog in this
directory. **Newest first.** When you finish a session, add a `DEVLOG_SESSION<N>_<date>.md`
and a one-line row here.

| # | Date | Branch | Focus |
|---|------|--------|-------|
| [8](DEVLOG_SESSION8_2026-06-16_12-15.md) | 2026-06-16 | `feat/adding-logger-and-accuracy-checker` | Accuracy-first captioning; dual-pass verifier scoring live captions; per-stream logs UI + filesystem persistence |
| [7](DEVLOG_SESSION7_2026-06-16_01-55.md) | 2026-06-16 | `feat/make-captions-readable` | Diagnose bursty GPU readings; honest duty-cycle chart (fast poll + rolling average) |
| [6](DEVLOG_SESSION6_2026-06-15_23-23.md) | 2026-06-15 | `feat/make-captions-readable` | GPU monitoring readout + real-time performance charts on the Monitor page |
| [5](DEVLOG_SESSION5_2026-06-15_22-30.md) | 2026-06-15 | `feat/make-captions-readable` | Readable captions: end-of-stream handling, caption-log replay fix, fullscreen captions, button standardization |
| [4](DEVLOG_SESSION4_2026-06-15.md) | 2026-06-15 | `feat/landing-page` | Public watch viewer, dashboard hardening (QR, no-flicker), replay mode, mobile responsiveness |
| [3](DEVLOG_SESSION3_2026-05-22_00-54.md) | 2026-05-22 | `YouTube-Ingest---ASR-Validation` | Multi-page control dashboard, concurrent multi-stream support, GPU performance tuning |
| [2](DEVLOG_SESSION2_2026-05-21.md) | 2026-05-21 | `YouTube-Ingest---ASR-Validation` | YouTube→Whisper→browser pipeline live end-to-end; Tailscale Funnel; multi-session web UI |
| [1](DEVLOG_SESSION1_2026-05-19.md) | 2026-05-19 | initial build | Project brief; CTA-708 real-time captioning system bootstrap (ASR, WebVTT, CEA-608/708) |
| [0](DEVLOG_SESSION0_2026-03-23.md) | 2026-03-23 | pre-history | Origin log: browser-based YouTube player + Whisper ASR (migrated from the old `session_log.md`) |

---

## Conventions

- **Devlog files:** `DEVLOG_SESSION<N>_<YYYY-MM-DD>[_HH-MM].md`, one per working session.
- **Standard header:** title, `**Date:**`, `**Branch:**`, engineer, system, AI pair programmer.
- **Typical sections:** Session Goals → Diagnosis / What Was Built → Files Changed →
  Testing / Verification → Incident Notes → How to Resume → Open Items.
- **Keep this index current:** add the new row at the top of the table when a session lands.
