"""Validate the harness against known-good cases before trusting any score.

Checks, in order:
  1. The RMS reimplementation reproduces production's recorded `rms_db`.
  2. webrtcvad reproduces the published inversion on the Georgian case
     (pyproject.toml / ateles#631: fabrication 0.75 vs genuine 0.42).

A detector score is only meaningful once these pass.
"""
import os, sys, json, glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audio, rms, det_webrtc  # noqa: E402

REC = os.path.expanduser("~/Documents/data/recordings")
# The published case: chunk 69, recorded at -31.6 dBFS, Georgian script.
CASE_FILE = "20260901 1026 mic"
CASE_CHUNK = 69


def find_case():
    j = os.path.join(REC, CASE_FILE + "_live.jsonl")
    if not os.path.exists(j):
        return None
    for line in open(j, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("chunk") == CASE_CHUNK:
            return d
    return None


def main():
    for ext in (".mp4", ".m4a"):
        aud = os.path.join(REC, CASE_FILE + ext)
        if os.path.exists(aud):
            break
    else:
        print("SKIP: corpus audio not present on this machine")
        return 0

    d = find_case()
    if not d:
        print("SKIP: published case not found in corpus")
        return 0

    ok = True

    # 1. RMS statistic matches production's recorded value.
    measured = rms.sustained_rms_db(aud, d["start_s"], d["end_s"])
    recorded = d.get("rms_db")
    print(f"RMS  : harness {measured:.1f} dBFS vs recorded {recorded} dBFS")
    if measured is None or abs(measured - recorded) > 1.0:
        print("  FAIL: RMS reimplementation does not match production")
        ok = False
    else:
        print("  ok (within 1 dB)")

    # 2. webrtcvad reproduces the published inversion on this clip.
    pcm = audio.segment(aud, d["start_s"], d["end_s"])
    fab = det_webrtc.WebrtcScorer(0).score(pcm)

    gen = []
    s0 = det_webrtc.WebrtcScorer(0)
    for line in open(os.path.join(REC, CASE_FILE + "_live.jsonl"),
                     encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        t = (r.get("text") or "").strip()
        if not t or r.get("chunk") == CASE_CHUNK:
            continue
        if r.get("start_s") is None or r.get("end_s") is None:
            continue
        if any(ord(c) > 0x2000 for c in t):   # skip other non-Latin rows
            continue
        gen.append(s0.score(audio.segment(aud, r["start_s"], r["end_s"])))

    mean_gen = sum(gen) / len(gen) if gen else float("nan")
    print(f"VAD  : fabrication {fab:.2f} vs genuine mean {mean_gen:.2f} "
          f"(published 0.75 vs 0.42, n_genuine={len(gen)})")
    if not (fab > mean_gen):
        print("  FAIL: published inversion did not reproduce")
        ok = False
    else:
        print("  ok (inversion reproduced on this clip)")

    print("\nHARNESS VALID" if ok else "\nHARNESS INVALID — do not trust scores")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
