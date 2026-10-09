"""Pre-transcription speech-presence for the live tailer (ateles#777 Path B).

The RMS silence gate answers "was there sustained energy." On a noisy mic that
is not the same question as "was there speech": ateles#777's labelled session
has a fabrication at −39.0 dB sitting 2.6 dB above real speech at −36.4 dB, so
no loudness threshold is correct.

This module answers a different question with WebRTC VAD: what fraction of
20 ms frames in the slice the detector calls speech. Measured on the #777
windows (``20260907 1114 mic.mp4``, 16 kHz mono, aggressiveness 3):

    ch0 speech 0.531   ch1 speech 0.117   ch2 fabrication 0.050
    ch3 silence 0.013   ch4 speech 0.090    ch5 fabrication 0.066

A floor of 0.075 keeps ch0/ch1/ch4 and drops ch2/ch5. That floor is a
measurement on this recording, not a claim that VAD discriminates every
fabrication. ateles#631 measured the opposite on a *loud* fabrication
(−31.6 dBFS scored *more* speech-like than real speech). Those cases still
need Whisper ``no_speech_prob`` when a backend surfaces it; they are not
solved here, and this module does not fake them with phrase matching.

Fail-open: a missing webrtcvad install, an unreadable wav, or a VAD exception
returns None / "transcribe". A broken detector must never drop audio.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

DEFAULT_VAD_AGGRESSIVENESS = int(
    os.environ.get("LIVE_TRANSCRIPT_VAD_AGGRESSIVENESS", "3")
)
DEFAULT_MIN_SPEECH_FRACTION = float(
    os.environ.get("LIVE_TRANSCRIPT_MIN_SPEECH_FRACTION", "0.075")
)
DEFAULT_MAX_NO_SPEECH_PROB = float(
    os.environ.get("LIVE_TRANSCRIPT_MAX_NO_SPEECH_PROB", "0.6")
)

SKIPPED_BELOW_THRESHOLD = "below_threshold"
SKIPPED_VAD_NON_SPEECH = "vad_non_speech"
SKIPPED_NO_SPEECH_PROB = "no_speech_prob"

_FRAME_MS = 20
_SAMPLE_WIDTH = 2
_ALLOWED_RATES = (8000, 16000, 32000, 48000)


def measure_speech_fraction(
    wav_path: Path,
    *,
    aggressiveness: int = DEFAULT_VAD_AGGRESSIVENESS,
) -> float | None:
    """Fraction of 20 ms frames WebRTC VAD calls speech, or None on failure."""
    try:
        import webrtcvad
    except Exception:  # noqa: BLE001 — optional layer must not be fatal
        return None

    try:
        with wave.open(str(wav_path), "rb") as fh:
            if fh.getnchannels() != 1 or fh.getsampwidth() != _SAMPLE_WIDTH:
                return None
            rate = fh.getframerate()
            if rate not in _ALLOWED_RATES:
                return None
            pcm = fh.readframes(fh.getnframes())
        vad = webrtcvad.Vad(aggressiveness)
        frame_bytes = int(rate * (_FRAME_MS / 1000.0)) * _SAMPLE_WIDTH
        if frame_bytes <= 0 or len(pcm) < frame_bytes:
            return None
        speech = 0
        total = 0
        for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            total += 1
            try:
                if vad.is_speech(pcm[offset : offset + frame_bytes], rate):
                    speech += 1
            except Exception:  # noqa: BLE001 — one bad frame must not drop the slice
                return None
        if total == 0:
            return None
        return speech / total
    except Exception:  # noqa: BLE001
        return None


def classify_pre_transcription(
    rms_db: float | None,
    speech_frac: float | None,
    *,
    silence_threshold_db: float,
    min_speech_fraction: float = DEFAULT_MIN_SPEECH_FRACTION,
) -> str:
    """Decide whether to skip a slice before calling Whisper.

    Returns ``below_threshold``, ``vad_non_speech``, or ``transcribe``.
    ``None`` measurements fail open into the next layer, then into transcribe.
    """
    if rms_db is not None and rms_db < silence_threshold_db:
        return SKIPPED_BELOW_THRESHOLD
    if speech_frac is not None and speech_frac < min_speech_fraction:
        return SKIPPED_VAD_NON_SPEECH
    return "transcribe"


def apply_no_speech_prob(
    record: dict,
    segments: list[dict] | None,
    *,
    max_no_speech_prob: float = DEFAULT_MAX_NO_SPEECH_PROB,
) -> bool:
    """Suppress a transcribed chunk when every segment reports no-speech.

    No-op when ``segments`` is missing or empty — the local whisper-cli path
    does not currently populate these fields, and inventing them from the
    transcript text is the banned phrase-matching resolution. Returns True
    when the record was rewritten as a no-speech skip.
    """
    if not segments:
        return False
    if not record.get("ok") or record.get("silence") or not record.get("text"):
        return False
    probs: list[float] = []
    for segment in segments:
        try:
            probs.append(float(segment.get("no_speech_prob")))
        except (TypeError, ValueError):
            return False
    if not probs or any(p <= max_no_speech_prob for p in probs):
        return False
    text = record.get("text") or ""
    record["filtered_text"] = text
    record["text"] = ""
    record["silence"] = True
    record["skipped"] = SKIPPED_NO_SPEECH_PROB
    record["no_speech_prob"] = round(min(probs), 4)
    return True
