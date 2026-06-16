#!/usr/bin/env python3
"""
Speaker-embedding validation tool.

Loads the configured sherpa-onnx speaker model and prints the pairwise cosine
similarity between a set of audio clips. Use it to confirm the model works and to
pick the diarization thresholds before a live run:

  • same speaker   → HIGH similarity (typically ~0.6–0.9)
  • different voices → LOW  similarity (typically ~0.0–0.3)

Then set, in settings.yaml:
    diarize.live.change_threshold        ≈ a cosine DISTANCE (1 - sim) that sits
                                           between your same- and different-pairs
    diarize.second_pass.cluster_threshold ≈ a cosine SIMILARITY in that same gap

Usage (from server/, in the venv):
  python tools/validate_diarization.py clipA1.wav clipA2.wav clipB1.wav
  python tools/validate_diarization.py --model models/foo.onnx a.wav b.wav

Audio may be any format/sample-rate librosa can read; it's resampled to mono
16 kHz float32 to match the live pipeline.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from microcaption.diarize.embedder import SpeakerEmbedder, cosine


def _load_config_model() -> str:
    """Best-effort read of diarize.model from settings.yaml (so the tool uses
    the same model the server will). Returns '' if unavailable."""
    try:
        import yaml
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, 'config', 'settings.yaml')) as f:
            cfg = yaml.safe_load(f) or {}
        return (cfg.get('diarize', {}) or {}).get('model', '') or ''
    except Exception:
        return ''


def _load_audio(path: str) -> np.ndarray:
    import librosa
    samples, _ = librosa.load(path, sr=16000, mono=True)
    return np.ascontiguousarray(samples, dtype=np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description='Validate speaker embeddings.')
    ap.add_argument('clips', nargs='+', help='audio files to compare')
    ap.add_argument('--model', default=None,
                    help='path to a sherpa-onnx speaker .onnx (default: diarize.model)')
    ap.add_argument('--device', default='cpu', choices=['cpu', 'cuda'])
    args = ap.parse_args()

    model = args.model or _load_config_model()
    if not model:
        print('No model: pass --model or set diarize.model in settings.yaml',
              file=sys.stderr)
        return 2

    embedder = SpeakerEmbedder({'model': model, 'device': args.device})
    embedder.load()
    if not embedder.available:
        print(f'Embedder unavailable: {embedder.load_error}', file=sys.stderr)
        return 1
    print(f'Model: {model}  (embedding dim={embedder.dim})\n')

    names, embs = [], []
    for path in args.clips:
        try:
            emb = embedder.embed(_load_audio(path))
        except Exception as exc:
            print(f'  ! {path}: failed to load/embed ({exc})', file=sys.stderr)
            emb = None
        if emb is None:
            print(f'  ! {path}: no embedding (too short / error) — skipped',
                  file=sys.stderr)
            continue
        names.append(os.path.basename(path))
        embs.append(emb)

    if len(embs) < 2:
        print('Need at least two embeddable clips to compare.', file=sys.stderr)
        return 1

    width = max(len(n) for n in names)
    print('Pairwise cosine similarity (1.0 = same voice):\n')
    print(' ' * (width + 2) + '  '.join(f'{i:>5}' for i in range(len(names))))
    for i, ni in enumerate(names):
        row = '  '.join(f'{cosine(embs[i], embs[j]):5.2f}' for j in range(len(names)))
        print(f'{ni:<{width}}  [{i}] {row}')
    print('\nIndex → file:')
    for i, n in enumerate(names):
        print(f'  [{i}] {n}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
