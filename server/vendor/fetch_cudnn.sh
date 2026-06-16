#!/usr/bin/env bash
# Fetch the real cuDNN 9.19.0.56 (CUDA 12) shared libraries from NVIDIA's
# redistributable archive into server/vendor/cudnn/lib.
#
# WHY THIS EXISTS:
#   PyTorch's Blackwell-capable builds (cu128/cu130) are compiled against
#   cuDNN 9.19/9.20, but as of 2026-06 NVIDIA only publishes *stub* cuDNN
#   wheels (no .so) to PyPI for those versions on this platform. The older
#   real wheel (9.10) is rejected by torch's exact-version check. So we
#   vendor the matching real cuDNN .so out-of-band and put it on
#   LD_LIBRARY_PATH (see server/run.sh). Parakeet/NeMo needs this; Whisper
#   (ctranslate2) does not.
#
# Re-run this after recreating the venv or bumping the torch CUDA build
# (match CUDNN_VER to `python -c "import torch;print(torch.backends.cudnn.version())"`).
set -euo pipefail
cd "$(dirname "$0")"

CUDNN_VER="9.19.0.56"
ARCHIVE="cudnn-linux-x86_64-${CUDNN_VER}_cuda12-archive"
URL="https://developer.download.nvidia.com/compute/cudnn/redist/cudnn/linux-x86_64/${ARCHIVE}.tar.xz"

if [ -f "cudnn/lib/libcudnn.so.9" ]; then
  echo "[fetch_cudnn] already present: cudnn/lib/libcudnn.so.9"
  exit 0
fi

echo "[fetch_cudnn] downloading ${ARCHIVE} ..."
tmp="$(mktemp -d)"
curl -fsSL "$URL" -o "$tmp/cudnn.tar.xz"
tar xf "$tmp/cudnn.tar.xz" -C "$tmp" --strip-components=1
mkdir -p cudnn/lib
cp -a "$tmp/lib/"libcudnn*.so* cudnn/lib/
rm -rf "$tmp"
echo "[fetch_cudnn] installed real cuDNN ${CUDNN_VER} -> $(pwd)/cudnn/lib"
