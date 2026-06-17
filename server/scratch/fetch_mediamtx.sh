#!/usr/bin/env bash
# Fetch the mediamtx RTMP server binary into server/vendor/mediamtx/.
#
# Receiving an RTMP *push* (from OBS / a hardware encoder) needs an RTMP server;
# GStreamer's rtmp2src only *pulls*. mediamtx accepts the push on :1935 and we
# ingest its local republish (rtmp://localhost:1935/live/<key>).
#
# Topology:
#   OBS / encoder ──push──▶ mediamtx :1935 ──pull──▶ MicroCaption (caption+remux) ──push──▶ YouTube
set -euo pipefail
cd "$(dirname "$0")/../vendor"
URL=$(curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
      | grep -o 'https://[^"]*linux_amd64.tar.gz' | head -1)
echo "Downloading $URL"
curl -sL "$URL" -o /tmp/mediamtx.tar.gz
mkdir -p mediamtx
tar -xzf /tmp/mediamtx.tar.gz -C mediamtx
echo "Installed: $(./mediamtx/mediamtx --version)"
echo "Run it with:  server/vendor/mediamtx/mediamtx"
