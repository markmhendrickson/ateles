"""Extract a PCM16 mono segment at a given sample rate via ffmpeg."""
import subprocess, numpy as np

def segment(path, start_s, end_s, rate=16000):
    dur = max(0.0, end_s - start_s)
    if dur <= 0:
        return np.zeros(0, dtype=np.int16)
    cmd = ['ffmpeg', '-nostdin', '-v', 'quiet',
           '-ss', f'{start_s:.3f}', '-t', f'{dur:.3f}', '-i', path,
           '-ac', '1', '-ar', str(rate), '-f', 's16le', '-']
    out = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(out, dtype=np.int16)

def dbfs(pcm):
    if pcm.size == 0:
        return None
    x = pcm.astype(np.float64) / 32768.0
    r = float(np.sqrt(np.mean(x * x)))
    if r <= 0:
        return -float('inf')
    return 20.0 * np.log10(r)
