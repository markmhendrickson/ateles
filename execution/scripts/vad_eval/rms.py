"""Reproduce production's sustained RMS: ffmpeg astats 3s windows, p95."""
import subprocess, re
_RMS_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-?inf)", re.I)

def sustained_rms_db(path, start_s, end_s, percentile=0.95):
    dur = max(0.0, end_s - start_s)
    if dur <= 0:
        return None
    proc = subprocess.run(
        ['ffmpeg', '-nostdin', '-hide_banner',
         '-ss', f'{start_s:.3f}', '-t', f'{dur:.3f}', '-i', path,
         '-ac', '1', '-ar', '16000',
         '-af', 'astats=metadata=1:reset=1:length=3,'
                'ametadata=print:key=lavfi.astats.Overall.RMS_level',
         '-f', 'null', '/dev/null'],
        capture_output=True, text=True, timeout=120)
    vals = [float(m) for m in _RMS_RE.findall(proc.stderr or '') if 'inf' not in m.lower()]
    if not vals:
        return None
    o = sorted(vals)
    return o[min(len(o)-1, int(percentile*len(o)))]
