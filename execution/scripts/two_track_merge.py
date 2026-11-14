"""Merge a two-track call recording (mic = operator, system = remote) by SEGMENT.

Method ported from the recordings desk's hand procedure (merge_tracks.py plus the
FluidAudio-to-track energy mapping). Pure functions: no audio is read here, so
every rule is testable on synthetic segments.

Why by segment. Merging two ASR outputs word by word interleaves the speakers
when they overlap, and attaching a diarization label to each word splits one
person's sentence across several tags. Here each ASR segment (an utterance) stays
whole and carries the identity of the PHYSICAL track it came from; segments are
ordered by start time and consecutive segments of one speaker are joined.

Who is who. Track identity is physical: the mic track is the operator, the
system track is the remote side. Diarization (FluidAudio, run on the two tracks
mixed) only splits the REMOTE side into speakers. Each cluster is mapped to the
track that carries its energy, so a cluster that is really the operator's voice
leaking into the system mix is not named as a remote speaker.

Cross-talk. The operator's mic also picks up the remote side. A segment is
dropped as that leak only when its words are also found in the other track's ASR
over the same stretch AND it is the quieter of the two there. Level alone never
drops a segment. Dropped segments keep their text and reason in the raw evidence
and are counted in the header.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

SYSTEM = "System"
MIC = "Mic"

FRAME_SECONDS = 0.1

# A segment is cross-talk leakage only when its WORDS are found in the other
# track's ASR over the same stretch (the mic hearing the remote voice, or the
# reverse) AND it is the quieter of the two there, each track measured against
# its own speech level. Level alone never drops a segment: the operator's mic can
# sit 7 dB or more under the system track, and a real answer then looks "far
# below its own track" (a 55-minute acceptance call lost 12% of the operator's
# words to a level-only rule).
BLEED_MIN_OVERLAP = 0.5
BLEED_MARGIN_DB = 3.0
# Share of the segment's words found in the other track's text in that window.
BLEED_TEXT_CONTAINMENT = 0.6
# Fewer words than this prove nothing ("yeah", "okay"): never dropped on text.
BLEED_MIN_WORDS = 3
# ASR timestamps of one sound differ between the tracks by about a second.
BLEED_WINDOW_PAD_SECONDS = 1.5
# A cluster is assigned to a track only when one track clearly carries it.
MAP_MARGIN_DB = 3.0

# Speech frames only feed a track's reference level; below this is noise floor.
SPEECH_FLOOR_DB = -60.0
REFERENCE_PERCENTILE = 0.90

NO_SPEECH_TEXT = "[NO SPEECH DETECTED — local diarization and ASR returned no words.]"

_BRACKET_ONLY = re.compile(r"^\s*[\(\[][^\)\]]*[\)\]]\s*$")
_HAS_LETTER = re.compile(r"[^\W\d_]")


# --- segment hygiene ---------------------------------------------------------

def _key(token: str) -> str:
    return re.sub(r"\W", "", token.lower())


def collapse_repeats(text: str, *, max_n: int = 6, min_reps: int = 4) -> tuple[str, int]:
    """Collapse whisper's repetition loops: ``(text, tokens_removed)``.

    A phrase of 1 to ``max_n`` words repeated back to back ``min_reps`` or more
    times keeps its first two copies and a ``[…]`` marker. Only ADJACENT repeats count: the same phrase recurring
    across a segment is ordinary speech, a loop is not.
    """
    tokens = re.findall(r"\S+", text)
    removed = 0
    out: list[str] = []
    i = 0
    while i < len(tokens):
        collapsed = False
        for n in range(1, max_n + 1):
            gram = [_key(t) for t in tokens[i:i + n]]
            if len(gram) < n or not all(gram):
                continue
            reps = 1
            while (
                i + (reps + 1) * n <= len(tokens)
                and [_key(t) for t in tokens[i + reps * n:i + (reps + 1) * n]] == gram
            ):
                reps += 1
            if reps >= min_reps:
                out += tokens[i:i + 2 * n] + ["[…]"]
                removed += (reps - 2) * n
                i += reps * n
                collapsed = True
                break
        if not collapsed:
            out.append(tokens[i])
            i += 1
    return " ".join(out), removed


def clean_segments(segments: list[dict]) -> list[dict]:
    """Drop non-speech and empty segments. Loops are collapsed later, on the joined turn."""
    out = []
    for seg in segments:
        text = re.sub(r"\s+", " ", str(seg.get("text", ""))).strip()
        if not text or _BRACKET_ONLY.match(text) or not _HAS_LETTER.search(text):
            continue
        start, end = float(seg["start"]), float(seg["end"])
        if end < start:
            continue
        out.append({"start": start, "end": end, "text": text})
    out.sort(key=lambda s: (s["start"], s["end"]))
    return out


# --- energy ------------------------------------------------------------------

class EnergyProfile:
    """Per-frame level of one track, normalised to that track's own speech level.

    ``frames_db`` holds one RMS value (dB) per ``FRAME_SECONDS``. ``-inf`` is
    silence. The reference is a high percentile of the speech frames, so a track
    recorded 20 dB quieter than the other compares on equal footing.
    """

    def __init__(self, frames_db: list[float], frame_seconds: float = FRAME_SECONDS):
        self.frame_seconds = frame_seconds
        self.power = [10 ** (v / 10.0) if math.isfinite(v) else 0.0 for v in frames_db]
        speech = sorted(p for p, v in zip(self.power, frames_db) if math.isfinite(v) and v > SPEECH_FLOOR_DB)
        if speech:
            self.reference = speech[min(len(speech) - 1, int(len(speech) * REFERENCE_PERCENTILE))]
        else:
            self.reference = 0.0

    @property
    def usable(self) -> bool:
        return self.reference > 0.0

    def relative_db(self, spans: list[tuple[float, float]]) -> float | None:
        """Mean power over the spans relative to this track's reference, in dB."""
        if not self.usable:
            return None
        total, count = 0.0, 0
        for start, end in spans:
            lo = max(0, int(start / self.frame_seconds))
            hi = min(len(self.power), max(lo + 1, int(math.ceil(end / self.frame_seconds))))
            total += sum(self.power[lo:hi])
            count += max(0, hi - lo)
        if count == 0:
            return None
        mean = total / count
        if mean <= 0.0:
            return -120.0
        return 10 * math.log10(mean / self.reference)


def map_clusters_to_tracks(
    clusters: dict[str, list[tuple[float, float]]],
    energy: dict[str, EnergyProfile],
) -> dict[str, dict]:
    """Assign each diarization cluster to the track that carries its energy.

    Returns ``{cluster: {"track": SYSTEM|MIC|None, "margin_db": float|None}}``.
    ``None`` means neither track clearly carries it (inside ``MAP_MARGIN_DB``) or
    it could not be measured; nothing is guessed.
    """
    mapping: dict[str, dict] = {}
    for cluster, spans in clusters.items():
        system = energy.get(SYSTEM).relative_db(spans) if energy.get(SYSTEM) else None
        mic = energy.get(MIC).relative_db(spans) if energy.get(MIC) else None
        if system is None or mic is None:
            mapping[cluster] = {"track": None, "margin_db": None}
            continue
        margin = system - mic
        track = SYSTEM if margin >= MAP_MARGIN_DB else MIC if margin <= -MAP_MARGIN_DB else None
        mapping[cluster] = {"track": track, "margin_db": round(margin, 1)}
    return mapping


# --- cross-talk --------------------------------------------------------------

def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _covered(start: float, end: float, spans: list[tuple[float, float]]) -> float:
    """Seconds of [start, end] covered by the union of spans."""
    merged: list[list[float]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return sum(_overlap(start, end, a, b) for a, b in merged)


def _contained(own: str, other: str) -> float:
    """Share of ``own``'s words (multiset) that also occur in ``other``."""
    mine = [_key(t) for t in own.split() if _key(t)]
    theirs: dict[str, int] = {}
    for token in (_key(t) for t in other.split()):
        if token:
            theirs[token] = theirs.get(token, 0) + 1
    if not mine:
        return 0.0
    hit = 0
    for token in mine:
        if theirs.get(token, 0) > 0:
            theirs[token] -= 1
            hit += 1
    return hit / len(mine)


def drop_crosstalk(
    tracks: dict[str, list[dict]],
    energy: dict[str, EnergyProfile] | None,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Remove segments that are one track's leak of the other. Returns (kept, dropped).

    A segment is dropped only when (a) at least ``BLEED_MIN_WORDS`` of its words
    are mostly (``BLEED_TEXT_CONTAINMENT``) found in the other track's text around
    the same time, and (b) it is the quieter of the two tracks there by
    ``BLEED_MARGIN_DB``. Without level measurements nothing says which side is the
    leak, so nothing is dropped. Each dropped segment keeps its text, the matched
    share and the level margin so it can be recovered from the raw evidence.
    """
    if not energy or any(t not in energy or not energy[t].usable for t in (SYSTEM, MIC)):
        return tracks, []
    dropped: list[dict] = []
    kept: dict[str, list[dict]] = {}
    for track, segments in tracks.items():
        other = SYSTEM if track == MIC else MIC
        other_segments = tracks.get(other, [])
        kept[track] = []
        for seg in segments:
            span = [(seg["start"], seg["end"])]
            duration = max(seg["end"] - seg["start"], FRAME_SECONDS)
            lo, hi = seg["start"] - BLEED_WINDOW_PAD_SECONDS, seg["end"] + BLEED_WINDOW_PAD_SECONDS
            near = [o for o in other_segments if _overlap(lo, hi, o["start"], o["end"]) > 0]
            overlap = _covered(seg["start"], seg["end"], [(o["start"], o["end"]) for o in near]) / duration
            own_db = energy[track].relative_db(span)
            other_db = energy[other].relative_db(span)
            share = _contained(seg["text"], " ".join(o["text"] for o in near)) if near else 0.0
            margin = None if own_db is None or other_db is None else other_db - own_db
            bleed = (
                len(seg["text"].split()) >= BLEED_MIN_WORDS
                and overlap >= BLEED_MIN_OVERLAP
                and share >= BLEED_TEXT_CONTAINMENT
                and margin is not None
                and margin >= BLEED_MARGIN_DB
            )
            if bleed:
                dropped.append({**seg, "track": track, "reason": "text found on the other track and quieter here",
                                "text_match": round(share, 2), "margin_db": round(margin, 1)})
            else:
                kept[track].append(seg)
    return kept, dropped


# --- merge -------------------------------------------------------------------

@dataclass
class MergeResult:
    turns: list[dict]
    text: str
    stats: dict


def _cluster_spans(diarization: dict | None) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    if diarization and diarization.get("status") == "ok":
        for seg in diarization.get("segments") or []:
            out.setdefault(seg["speaker"], []).append((float(seg["start"]), float(seg["end"])))
    return out


def format_clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


GAP_MARKER = (
    "[SPEECH WITHHELD — the model returned text for this stretch but the output screen "
    "rejected it ({reason}: {detail}). It is not silence; the text is kept in the stored evidence.]"
)


def _plain(value: object, limit: int = 100) -> str:
    """One line with no brackets, so a marker or header cannot break its own pattern."""
    return re.sub(r"[\[\]\s]+", " ", str(value)).strip()[:limit]


def chunk_outcomes(chunks: list[dict]) -> dict:
    """Withheld, silent and still-looping chunks from one track's chunk records."""
    gaps, silent, looping = [], 0, 0
    for chunk in chunks or []:
        if chunk.get("kind") != "chunk":
            continue
        status = str(chunk.get("status", ""))
        if status.startswith("screened"):
            gaps.append({
                "start": float(chunk["start"]), "end": float(chunk.get("end", chunk["start"])),
                "reason": _plain(chunk.get("reason", "screened"), 40),
                "detail": _plain(chunk.get("detail", ""), 100),
            })
        elif status == "silent":
            silent += 1
        elif chunk.get("loop_after_rerun"):
            looping += 1
    return {"gaps": gaps, "silent": silent, "looping": looping}


def _header(stats: dict, diarization: dict | None) -> str:
    """One line, ``[Speakers: ...]`` and no ``]`` inside, so consumers strip it.

    States the method, the diarization outcome and mapping, that names are not
    established, and what was removed or withheld.
    """
    tracks = f"2 tracks ({MIC}, {SYSTEM}) merged by segment"
    if diarization is None:
        who = "remote side not split by speaker, diarization not run"
    elif diarization.get("status") != "ok":
        who = (f"remote side not split by speaker, FluidAudio offline failed "
               f"({diarization.get('category', 'unreadable output')})")
    elif not stats.get("energy_measured"):
        who = (f"FluidAudio offline on the mixed tracks found {diarization.get('speaker_count', 0)} clusters, "
               "not mapped to tracks, levels unmeasured")
    else:
        mapped = stats["clusters_by_track"]
        who = (
            f"FluidAudio offline on the mixed tracks found {diarization.get('speaker_count', 0)} clusters "
            f"(remote {mapped.get(SYSTEM, 0)}, mic {mapped.get(MIC, 0)}, unmapped {mapped.get('unmapped', 0)}) "
            "mapped to tracks by energy"
        )
    build = (diarization or {}).get("cli_build") if diarization and diarization.get("status") == "ok" else None
    if build:
        who += f", build {build}"
    parts = [tracks, who, "speaker names not established"]
    gaps = stats.get("gaps") or []
    if gaps:
        spans = ", ".join(
            f"{g['track']} {format_clock(g['start'])}\u2013{format_clock(g['end'])} {g['reason']}" for g in gaps[:5]
        )
        more = f" and {len(gaps) - 5} more" if len(gaps) > 5 else ""
        parts.append(f"{len(gaps)} chunks withheld by the output screen ({spans}{more}), text kept in the stored evidence")
    if stats.get("chunks_looping"):
        parts.append(f"{stats['chunks_looping']} chunks still looping after a re-run, loops collapsed")
    if stats.get("chunks_silent"):
        parts.append(f"{stats['chunks_silent']} chunks skipped as silent")
    if stats.get("bleed_dropped"):
        parts.append(f"{stats['bleed_dropped']} cross-talk segments dropped")
    elif stats.get("bleed_checked") is False:
        parts.append("cross-talk check skipped, level unmeasured")
    if stats.get("repeats_removed"):
        parts.append(f"{stats['repeats_removed']} repeated words collapsed")
    return "[Speakers: " + "; ".join(parts) + "]"


def merge_tracks(
    tracks: dict[str, list[dict]],
    *,
    diarization: dict | None = None,
    energy: dict[str, EnergyProfile] | None = None,
    stitch_seconds: float = 4.0,
    chunks: dict[str, list[dict]] | None = None,
) -> MergeResult:
    """Merge per-track ASR segments into one time-ordered dialogue.

    ``tracks`` is ``{SYSTEM: [segment], MIC: [segment]}``, each segment
    ``{"start", "end", "text"}`` in seconds on the shared timeline. ``chunks`` is
    each track's chunk QA records: a withheld chunk becomes a timed gap marker in
    the body and a count in the header; silent and still-looping chunks are
    counted in the header.
    """
    stats: dict = {"repeats_removed": 0, "energy_measured": bool(energy)}
    cleaned = {track: clean_segments(tracks.get(track, [])) for track in (SYSTEM, MIC)}
    stats["gaps"], stats["chunks_silent"], stats["chunks_looping"] = [], 0, 0
    for track in (SYSTEM, MIC):
        outcome = chunk_outcomes((chunks or {}).get(track, []))
        stats["gaps"] += [{**g, "track": track} for g in outcome["gaps"]]
        stats["chunks_silent"] += outcome["silent"]
        stats["chunks_looping"] += outcome["looping"]
    stats["gaps"].sort(key=lambda g: (g["start"], g["track"]))

    clusters = _cluster_spans(diarization)
    mapping = map_clusters_to_tracks(clusters, energy) if clusters and energy else {}
    stats["cluster_mapping"] = mapping
    by_track: dict = {}
    for info in mapping.values():
        key = info["track"] or "unmapped"
        by_track[key] = by_track.get(key, 0) + 1
    stats["clusters_by_track"] = by_track
    stats["bleed_checked"] = bool(energy) and all(
        t in energy and energy[t].usable for t in (SYSTEM, MIC)
    )
    kept, dropped = drop_crosstalk(cleaned, energy)
    stats["bleed_dropped"] = len(dropped)
    stats["bleed_segments"] = [
        {"track": d["track"], "start": round(d["start"], 2), "end": round(d["end"], 2), "text": d["text"],
         "reason": d["reason"], "text_match": d["text_match"], "margin_db": d["margin_db"]}
        for d in dropped
    ]

    # Remote speakers: clusters mapped to the system track, named by first appearance.
    remote_clusters = sorted(
        (c for c, info in mapping.items() if info["track"] == SYSTEM),
        key=lambda c: min(a for a, _ in clusters[c]),
    )
    remote_names = {c: f"S{i + 1}" for i, c in enumerate(remote_clusters)}

    def remote_label(seg: dict) -> str:
        if len(remote_clusters) <= 1:
            return SYSTEM
        overlap = {
            c: sum(_overlap(seg["start"], seg["end"], a, b) for a, b in clusters[c])
            for c in remote_clusters
        }
        best = max(sorted(overlap), key=overlap.get)
        return f"{SYSTEM}/{remote_names[best]}" if overlap[best] > 0 else f"{SYSTEM}/Unassigned"

    rows = []
    for track in (SYSTEM, MIC):
        for seg in kept[track]:
            rows.append({**seg, "track": track, "label": remote_label(seg) if track == SYSTEM else MIC})
    rows.sort(key=lambda r: (r["start"], r["end"], r["track"]))

    turns: list[dict] = []
    for row in rows:
        last = turns[-1] if turns else None
        if last and last["label"] == row["label"] and row["start"] - last["end"] <= stitch_seconds:
            last["text"] += " " + row["text"]
            last["end"] = max(last["end"], row["end"])
        else:
            turns.append({k: row[k] for k in ("start", "end", "track", "label", "text")})
    # Loops are collapsed on the joined turn: whisper emits one loop as many
    # consecutive segments, which no per-segment pass can see.
    for turn in turns:
        turn["text"], cut = collapse_repeats(turn["text"])
        stats["repeats_removed"] += cut

    gap_rows = [
        {"start": g["start"], "end": g["end"], "track": g["track"],
         "label": remote_label(g) if g["track"] == SYSTEM else MIC,
         "text": GAP_MARKER.format(reason=g["reason"], detail=g["detail"] or "no detail")}
        for g in stats["gaps"]
    ]
    if not turns and not gap_rows:
        return MergeResult([], NO_SPEECH_TEXT, stats)
    blocks = sorted(
        [(t["start"], t["end"], t["label"], t["text"]) for t in turns]
        + [(g["start"], g["end"], g["label"], g["text"]) for g in gap_rows]
    )
    body = "\n\n".join(
        f"[{format_clock(a)}\u2013{format_clock(b)}] [{label}]\n{text}" for a, b, label, text in blocks
    )
    return MergeResult(turns, f"{_header(stats, diarization)}\n\n{body}", stats)
