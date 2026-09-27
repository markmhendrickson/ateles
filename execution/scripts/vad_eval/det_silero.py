"""Silero VAD scorer.

Uses the reference `silero_vad` package (torch), NOT a hand-rolled ONNX call.
That distinction is load-bearing: an earlier hand-rolled ONNX implementation in
this evaluation mishandled the model's recurrent `state` tensor and returned
~0.001 for EVERY input, including loud, unambiguous operator speech that
webrtcvad scored 0.91. Those numbers looked like a decisive negative result and
were entirely an artifact of the harness. Validate any scorer against a
known-good case before believing what it says (see README.md).
"""
import numpy as np

_SR = 16000
_WIN = 512  # required window size at 16kHz


class SileroScorer:
    def __init__(self):
        import torch
        from silero_vad import load_silero_vad
        torch.set_grad_enabled(False)
        self._torch = torch
        self.m = load_silero_vad(onnx=False)
        self.name = "silero_vad"

    def probs(self, pcm):
        """Per-window speech probability over a PCM16 mono 16kHz segment."""
        if pcm.size < _WIN:
            return np.zeros(0, dtype=np.float32)
        x = self._torch.from_numpy((pcm.astype(np.float32) / 32768.0).copy())
        self.m.reset_states()
        out = [float(self.m(x[i * _WIN:(i + 1) * _WIN], _SR))
               for i in range(x.numel() // _WIN)]
        return np.array(out, dtype=np.float32)

    def score(self, pcm):
        """Fraction of windows above 0.5 — comparable to webrtcvad's speech fraction."""
        p = self.probs(pcm)
        return float((p >= 0.5).mean()) if p.size else 0.0

    def score_mean(self, pcm):
        p = self.probs(pcm)
        return float(p.mean()) if p.size else 0.0

    def score_p90(self, pcm):
        p = self.probs(pcm)
        return float(np.percentile(p, 90)) if p.size else 0.0
