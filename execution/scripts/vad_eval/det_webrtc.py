"""webrtcvad scorer: fraction of 30ms frames judged speech."""
import webrtcvad, numpy as np

class WebrtcScorer:
    def __init__(self, aggressiveness=2, rate=16000):
        self.v = webrtcvad.Vad(aggressiveness)
        self.rate = rate
        self.name = f'webrtcvad(a={aggressiveness})'
    def score(self, pcm):
        if pcm.size == 0:
            return 0.0
        frame = int(self.rate * 0.03)  # 30ms
        n = pcm.size // frame
        if n == 0:
            return 0.0
        b = pcm.tobytes()
        speech = 0
        for i in range(n):
            chunk = b[i*frame*2:(i+1)*frame*2]
            try:
                if self.v.is_speech(chunk, self.rate):
                    speech += 1
            except Exception:
                pass
        return speech / n
