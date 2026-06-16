#!/usr/bin/env bash
# MicroCaption launcher — sets up CUDA library path and starts the pipeline.
# Usage:
#   ./run.sh              # start server; submit YouTube URLs via http://localhost:8765/
#   ./run.sh --mock-asr   # pipeline test, no model needed
#
# CUDA library path (order matters):
#   1. vendor/cudnn/lib  — real cuDNN 9.19 for Parakeet/NeMo (torch). The PyPI
#      cuDNN wheel for this version is a stub with no .so, so we vendor the real
#      libs out-of-band; run ./vendor/fetch_cudnn.sh once to populate it.
#   2. ollama/cuda_v12   — libcublas (CUDA 12) needed by ctranslate2/faster-whisper.

set -euo pipefail
cd "$(dirname "$0")"

export LD_LIBRARY_PATH="$(pwd)/vendor/cudnn/lib:/usr/local/lib/ollama/cuda_v12${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONUNBUFFERED=1

# Mirror output to logs/server.log (viewable in the web UI Logs page) while
# still printing to the console. (No `exec` so the tee pipeline stays alive.)
mkdir -p logs
../.venv/bin/python3 -u main.py "$@" 2>&1 | tee -a logs/server.log
