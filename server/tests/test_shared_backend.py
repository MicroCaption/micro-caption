import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np

from microcaption.asr.pipeline import SharedASRBackend


class _FakeBackend:
    """Controllable stand-in for a real ASR backend (no GPU)."""

    def __init__(self, name, out):
        self.name = name
        self._out = out
        self.fail = False
        self.calls = 0

    def transcribe_words(self, samples):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f'{self.name} boom')
        return self._out

    def transcribe(self, samples):
        if self.fail:
            raise RuntimeError(f'{self.name} boom')
        return self.name

    def unload(self):
        pass


_AUDIO = np.zeros(16, dtype=np.float32)


class SupervisedBackendTest(unittest.TestCase):
    def _make(self, fresh_parakeet):
        """SharedASRBackend wired to fakes. `pk0` serves first; on recovery the
        loader hands out `fresh_parakeet`."""
        cfg = {'primary': 'parakeet', 'preload_fallback': True,
               'parakeet': {'recovery_interval_sec': 0.05}}
        sb = SharedASRBackend(cfg)
        pk0 = _FakeBackend('parakeet', [(' hi', 0.0, 0.5)])
        wh = _FakeBackend('whisper', [(' wh', 0.0, 0.5)])
        handed = {'first': True}

        def load_pk():
            if handed['first']:
                handed['first'] = False
                return pk0
            return fresh_parakeet

        sb._load_parakeet = load_pk
        sb._load_whisper = lambda: wh
        return sb, pk0, wh

    def test_primary_serves_then_fails_over_and_recovers(self):
        fresh = _FakeBackend('parakeet', [(' back', 0.0, 0.5)])
        sb, pk0, wh = self._make(fresh)
        sb.load()

        # Parakeet primary serves normally.
        self.assertEqual(sb.active_name, 'parakeet')
        self.assertEqual(sb.transcribe_words(_AUDIO), [(' hi', 0.0, 0.5)])

        # Parakeet breaks → the SAME call is retried on Whisper (no caption lost).
        pk0.fail = True
        self.assertEqual(sb.transcribe_words(_AUDIO), [(' wh', 0.0, 0.5)])
        self.assertEqual(sb.active_name, 'whisper')

        # Recovery thread reloads a healthy Parakeet and switches back.
        deadline = time.time() + 3.0
        while time.time() < deadline and sb.active_name != 'parakeet':
            time.sleep(0.05)
        self.assertEqual(sb.active_name, 'parakeet')
        self.assertEqual(sb.transcribe_words(_AUDIO), [(' back', 0.0, 0.5)])
        sb.unload()

    def test_inference_never_raises_when_both_fail(self):
        # Even if Whisper also fails, calls return safe-empty (worker survives).
        fresh = _FakeBackend('parakeet', [])
        sb, pk0, wh = self._make(fresh)
        sb.load()
        pk0.fail = True
        wh.fail = True
        self.assertEqual(sb.transcribe_words(_AUDIO), [])
        self.assertEqual(sb.transcribe(_AUDIO), '')
        sb._running = False  # stop recovery thread
        sb.unload()

    def test_supports_words_is_always_true(self):
        fresh = _FakeBackend('parakeet', [])
        sb, _, _ = self._make(fresh)
        sb.load()
        self.assertTrue(sb.supports_words)
        sb.unload()


if __name__ == '__main__':
    unittest.main()
