#!/usr/bin/env bash
# MicroCaption launcher — sets up CUDA library path and starts the pipeline.
# Usage:
#   ./run.sh              # start server; submit YouTube URLs via http://localhost:8765/
#   ./run.sh --mock-asr   # pipeline test, no model needed

set -euo pipefail
cd "$(dirname "$0")"

export LD_LIBRARY_PATH="/usr/local/lib/ollama/cuda_v12${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONUNBUFFERED=1

exec ../.venv/bin/python3 -u main.py "$@"
