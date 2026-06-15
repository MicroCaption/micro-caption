# MicroCaption — Tailscale Funnel Setup Guide

This document records exactly how the public-facing demo URL was created for MicroCaption and provides step-by-step instructions for replicating it from scratch on a new machine or after a Tailscale reset.

---

## What Tailscale Funnel Is

Tailscale Funnel is a feature that exposes a locally-running HTTP server to the **public internet** (not just your private Tailscale network) via a stable `*.ts.net` HTTPS URL. Tailscale handles:

- DNS (`machinename.tailnetname.ts.net` → publicly routable)
- TLS termination (Let's Encrypt certificate, auto-renewed)
- Reverse-proxying inbound HTTPS traffic to your local HTTP port

The backend only needs to bind to `localhost:8765` (plain HTTP). The Funnel layer sits in front of it and handles everything else. No port-forwarding, no firewall rules, no Nginx config.

---

## Prerequisites

### 1. Tailscale installed and logged in

```bash
# Check Tailscale is running and authenticated
tailscale status
```

You should see your machine listed with a `100.x.x.x` IP. If not, install Tailscale:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

### 2. Funnel enabled on your tailnet

Tailscale Funnel must be explicitly enabled in your tailnet's ACL policy. This is a one-time step per tailnet (not per machine).

To enable it, visit:
```
https://login.tailscale.com/admin/acls
```

Add the following to your ACL policy JSON under the top-level object (alongside `"acls"`, `"hosts"`, etc.):

```json
"nodeAttrs": [
  {
    "target": ["*"],
    "attr": ["funnel"]
  }
]
```

Alternatively, when you first run `tailscale funnel`, the CLI will print a direct URL to approve Funnel for your specific node — clicking it is equivalent.

### 3. Operator permission (removes sudo requirement)

By default, `tailscale funnel` requires `sudo`. Set yourself as the operator once so you can run it as a normal user:

```bash
sudo tailscale set --operator=$USER
```

This persists across reboots. You only need to do it once per machine.

---

## Starting the Funnel

With the backend running on port 8765:

```bash
# Start MicroCaption backend first
./run.sh

# In another terminal (or as a background command), start the Funnel
tailscale funnel --bg 8765
```

The `--bg` flag runs the Funnel in the background and makes the configuration **persistent** — it survives process restarts and reboots. You do not need to re-run this command after a system reboot as long as the Tailscale daemon is running.

Expected output:
```
Available on the internet:

https://microcap-proto.tail737e71.ts.net/
|-- proxy http://127.0.0.1:8765

Funnel started and running in the background.
To disable the proxy, run: tailscale funnel --https=443 off
```

Your public URL is the `https://` line. The format is always:
```
https://<machine-name>.<tailnet-name>.ts.net/
```

---

## Verifying the Funnel

```bash
# Check Funnel status
tailscale funnel status
```

Expected output when active:
```
# Funnel on:
#     - https://microcap-proto.tail737e71.ts.net

https://microcap-proto.tail737e71.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:8765
```

Test that the public URL is reachable and the backend is responding:
```bash
curl -s https://microcap-proto.tail737e71.ts.net/ | grep '<title>'
# Should return: <title>MicroCaption — Live ASR Demo</title>
```

If `curl` hangs or returns nothing, the most common cause is the backend is not running on port 8765. Start it with `./run.sh`.

---

## This Machine's Specific Configuration

| Setting | Value |
|---|---|
| Machine name | `microcap-proto` |
| Tailnet | `tail737e71.ts.net` |
| Public URL | `https://microcap-proto.tail737e71.ts.net/` |
| Local backend port | `8765` |
| Funnel mode | Background (`--bg`), persistent |
| Operator set | Yes (`sudo tailscale set --operator=peter`) |
| Funnel ACL approved via | `https://login.tailscale.com/f/funnel?node=nAkFM8nw6Q11CNTRL` |

---

## Stopping / Disabling the Funnel

```bash
# Disable the Funnel (stops public access, removes persistent config)
tailscale funnel --https=443 off

# Or reset all serve/funnel config entirely
tailscale funnel reset
```

After disabling, the `*.ts.net` URL will return a Tailscale error page. To re-enable, run `tailscale funnel --bg 8765` again.

---

## Embedding in an External Website

Because the Funnel provides a valid HTTPS URL with a Let's Encrypt certificate, it can be embedded in any HTTPS website without mixed-content errors:

```html
<iframe
  src="https://microcap-proto.tail737e71.ts.net/"
  width="1280"
  height="780"
  frameborder="0"
  allowfullscreen
  allow="autoplay; encrypted-media">
</iframe>
```

The embedded page shows the URL submission form. After a URL is submitted, the iframe transitions to the video player with live captions overlaid.

**Note:** The iframe approach works for demonstration purposes. For production embedding, a better architecture would be to serve the video player page directly from your own domain and have the caption data fetched from the Funnel URL as a separate API call — this avoids iframe sandboxing restrictions around autoplay and fullscreen.

---

## Troubleshooting

### "Funnel is not enabled on your tailnet"

Visit the Tailscale admin console and add the `funnel` node attribute to your ACL policy (see Prerequisites section). Or click the approval URL printed by the CLI.

### "Access denied: serve config denied"

You need `sudo` or the operator permission. Run:
```bash
sudo tailscale set --operator=$USER
```
Then retry `tailscale funnel --bg 8765`.

### Page loads but hangs / shows nothing

The backend is not running. Start it:
```bash
./run.sh
```

### Page loads but captions never appear

1. Check the backend log: `cat /tmp/microcaption.log`
2. Look for `[Session] Starting new session` — if missing, the URL form POST is not reaching `start_session`
3. Look for `[YouTube] Resolving stream URL` — if missing, the YouTubeAdapter is not starting
4. Look for `libcublas` errors — if present, the CUDA library path is wrong. Check that `./run.sh` is being used (not `python3 main.py` directly)

### Funnel URL resolves to 100.x.x.x instead of a public IP

This can happen when querying DNS from within the Tailscale network (MagicDNS returns the Tailscale IP for internal routing). External machines not on the tailnet will see the correct Tailscale edge IP. This is expected behaviour — the Funnel still works correctly for external access.

### "No active session" on /player after submitting URL

This was a fixed bug (race condition — see DEVLOG_SESSION2.md §2.2.4). If it reappears, verify the `do_POST` handler in `webvtt_server.py` sets `_Handler.video_id` before starting the background thread.

---

## Full Startup Sequence (from cold boot)

```bash
# 1. Navigate to project
cd /home/peter/Dev/micro-caption

# 2. Start the MicroCaption backend (Whisper loads in ~3 seconds)
./run.sh

# 3. Verify Funnel is still active (persists from last session)
tailscale funnel status

# 4. If Funnel is not active, re-enable it
tailscale funnel --bg 8765

# 5. Open the demo
# External: https://microcap-proto.tail737e71.ts.net/
# Local:    http://localhost:8765/
```

---

*Written 2026-05-21. Describes the configuration as established in development session 2.*
