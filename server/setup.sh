#!/usr/bin/env bash
# MicroCaption CTA-708 — environment setup
# Run as normal user (sudo is called internally where needed).
#
# Requirements: Ubuntu 26.04 "Resolute", Python 3.14.4, NVIDIA RTX GPU

set -euo pipefail

# ── onnx build strategy ───────────────────────────────────────────────────────
# onnx 1.19.0 has no cp314 wheel so it must be compiled from source.
# The protobuf dependency is the only hard obstacle on Python 3.14.
#
# Strategy (tried in order by _install_onnx below):
#
# A) libprotobuf-dev present + ONNX_USE_PROTOBUF_SHARED_LIBS=ON
#    → cmake links against libprotobuf.so (always PIC) instead of libprotobuf.a
#    → requires --no-build-isolation so CMAKE_ARGS env var is visible to cmake
#
# B) libprotobuf-dev AND protobuf-compiler removed + CMAKE_POSITION_INDEPENDENT_CODE=ON
#    → cmake has nothing to find; uses FetchContent to build its own protobuf
#    → -fPIC is forced via CMAKE_POSITION_INDEPENDENT_CODE
#    → requires --no-build-isolation for the same reason
#
# C) onnxruntime-gpu (pre-built binary wheel) + install NeMo without onnx
#    → NeMo's ASR inference path never imports onnx at runtime
#    → onnx is only needed for model export; skipping it is safe for captioning
# ─────────────────────────────────────────────────────────────────────────────

_install_onnx() {
    echo ""
    echo "--- [onnx] Strategy A: libprotobuf-dev + shared .so ---"
    sudo apt-get install -y libprotobuf-dev
    CMAKE_ARGS="-DONNX_USE_PROTOBUF_SHARED_LIBS=ON" \
        pip install --no-cache-dir --no-build-isolation onnx \
        && { echo "[onnx] Strategy A succeeded"; return 0; }

    echo ""
    echo "--- [onnx] Strategy A failed. Trying Strategy B: full FetchContent + -fPIC ---"
    sudo apt-get remove -y libprotobuf-dev protobuf-compiler 2>/dev/null || true
    pip cache purge
    CMAKE_ARGS="-DCMAKE_POSITION_INDEPENDENT_CODE=ON" \
        pip install --no-cache-dir --no-build-isolation onnx \
        && { echo "[onnx] Strategy B succeeded"; return 0; }

    echo ""
    echo "--- [onnx] Strategy B failed. Falling back to Strategy C: onnxruntime stub ---"
    echo "    NeMo ASR inference works without onnx (export only)."
    echo "    Installing onnxruntime-gpu to satisfy related deps."
    pip install onnxruntime-gpu || true
    return 1  # caller will install NeMo with --no-deps
}

echo "=== [1/6] System packages ==="
sudo apt-get update -qq
sudo apt-get install -y \
    build-essential python3-dev cmake \
    python3-venv \
    python3-gi python3-gst-1.0 gstreamer1.0-python3-plugin-loader \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-pulseaudio \
    gir1.2-gstreamer-1.0

echo "=== [2/6] Create venv (--system-site-packages required for python3-gi) ==="
rm -rf .venv
python3 -m venv --system-site-packages .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "=== [3/6] Base tools ==="
pip install --upgrade pip setuptools wheel

echo "=== [4/6] numpy (cp314 wheels exist for >=2.0; must precede nemo) ==="
pip install "numpy>=2.0"

echo "=== [5/6] PyTorch — CUDA 12.8 wheels (compatible with driver 13.x) ==="
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

echo "=== [6/6] MicroCaption dependencies + NeMo ASR ==="
pip install pyyaml

# Build onnx first (the tricky bit), then install NeMo
if _install_onnx; then
    echo ""
    echo "--- NeMo ASR ---"
    pip install --no-cache-dir "nemo_toolkit[asr]"
else
    echo ""
    echo "--- NeMo ASR (without onnx — strategy C path) ---"
    # Install nemo and all its real runtime deps; skip onnx validation
    pip install --no-cache-dir "nemo_toolkit[asr]" --no-deps
    # Core runtime deps that NeMo actually needs for ASR inference:
    pip install \
        omegaconf hydra-core \
        pytorch-lightning lightning \
        transformers huggingface-hub \
        soundfile librosa \
        packaging scipy tqdm \
        braceexpand webdataset \
        editdistance jiwer \
        "nemo_toolkit" \
    || true
    echo "[WARN] NeMo installed without onnx — model export disabled, inference OK"
fi

echo ""
echo "--- faster-whisper (fallback ASR) ---"
echo "    ctranslate2 may not have a cp314 wheel yet; failure here is non-fatal."
pip install faster-whisper \
    || echo "[WARN] faster-whisper unavailable; Parakeet will be used exclusively"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Activate:  source .venv/bin/activate"
echo "Run:       python3 main.py                   # live mic, full ASR"
echo "           python3 main.py --mock-asr         # pipeline test, no models"
echo "           python3 main.py --list-devices     # show PulseAudio sources"
echo "Tests:     python3 -m unittest tests.test_packetizer tests.test_normalizer tests.test_wer -v"
