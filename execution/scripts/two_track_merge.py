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

Cross-talk. The operator's mic also picks up the remote side. A matched PAIR of
segments (the same words on both tracks over the same stretch) is decided once:
the quieter copy, and only that one, is removed. Level alone never removes a
segment, and nothing is removed without a timed marker in the body, a count and
span in the header, and its text kept in the raw evidence.

Nothing disappears silently. A chunk the output screen withheld, a chunk skipped
as silent, an audible chunk with no recognised words and a removed cross-talk
segment each leave a timed marker in the body and a track and time span in the
header.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

SYSTEM = "System"
MIC = "Mic"

FRAME_SECONDS = 0.1

# --- cross-talk thresholds ----------------------------------------------------
# A copy is removed only when (a) its words are found in the other track's words
# over the same stretch, (b) it is the quieter copy of the pair there, each track
# measured against its own speech level, and (c) the pair really overlaps. Level
# alone never removes a segment: the operator's mic can sit 7 dB or more under
# the system track, and a real answer then looks "far below its own track".
BLEED_MIN_OVERLAP = 0.5
BLEED_MARGIN_DB = 3.0
# Share of the segment's words, and of its content words, found in the other
# track's words for the overlapping stretch.
BLEED_TEXT_CONTAINMENT = 0.6
# Fewer words than this prove nothing ("yeah", "okay"): never removed on text.
BLEED_MIN_WORDS = 3
# Content words: five letters or more (language-independent; four would keep that, what,
# with, have, this), and at least two.
CONTENT_WORD_LETTERS = 5
BLEED_MIN_CONTENT_WORDS = 2
# ASR timestamps of one sound differ between the tracks by about a second.
BLEED_WINDOW_PAD_SECONDS = 1.5
# Shortest shared stretch whose level is trusted; below it the union is measured.
BLEED_MIN_COMMON_SECONDS = 0.3
# A cluster is assigned to a track only when one track clearly carries it.
MAP_MARGIN_DB = 3.0

# Speech frames only feed a track's reference level; below this is noise floor.
SPEECH_FLOOR_DB = -60.0
REFERENCE_PERCENTILE = 0.90

NO_SPEECH_TEXT = "[NO SPEECH DETECTED — local diarization and ASR returned no words.]"

_BRACKET_ONLY = re.compile(r"^\s*[\(\[][^\)\]]*[\)\]]\s*$")
_HAS_LETTER_OR_DIGIT = re.compile(r"[^\W_]")

KIND_WITHHELD = "withheld"
KIND_SILENT = "silent"
KIND_EMPTY = "empty"
KIND_CROSSTALK = "crosstalk"
KIND_DISCARDED = "discarded"

MARKERS = {
    KIND_DISCARDED: (
        "[NON-SPEECH TAG DROPPED — whisper returned only a bracketed tag or punctuation for this stretch; "
        "the text is kept in the stored evidence.]"
    ),
    KIND_WITHHELD: (
        "[SPEECH WITHHELD — the model returned text for this stretch but the output screen "
        "rejected it ({reason}: {detail}). It is not silence; the text is kept in the stored evidence.]"
    ),
    KIND_SILENT: "[SKIPPED AS SILENT — this stretch measured below the speech floor and was not transcribed.]",
    KIND_EMPTY: "[NO WORDS RECOGNISED — this stretch is audible but the model returned no words for it.]",
    KIND_CROSSTALK: (
        "[CROSS-TALK REMOVED — words repeated from the other track and quieter here; "
        "the text is kept in the stored evidence.]"
    ),
}
# What a marker starts with: the validator counts these in the transcript.
MARKER_PREFIX = {kind: text.split(" — ")[0] for kind, text in MARKERS.items()}
GAP_MARKER = MARKERS[KIND_WITHHELD]


# --- segment hygiene ---------------------------------------------------------

def _key(token: str) -> str:
    return re.sub(r"\W", "", token.lower())


# Short phrases (up to SHORT_PHRASE_WORDS) loop at 4 copies; sentence-length phrases
# (up to MAX_PHRASE_WORDS) at 3, since three back-to-back copies of a whole sentence
# is never speech.
SHORT_PHRASE_WORDS = 6
MAX_PHRASE_WORDS = 40


def collapse_repeats(text: str, *, max_n: int = MAX_PHRASE_WORDS, min_reps: int = 4) -> tuple[str, int]:
    """Collapse whisper's repetition loops: ``(text, tokens_removed)``.

    A phrase of 1 to ``SHORT_PHRASE_WORDS`` words repeated back to back
    ``min_reps`` or more times, or a phrase of up to ``max_n`` words repeated 3 or
    more times, keeps its first two copies and a ``[…]`` marker. Only ADJACENT
    repeats count: the same phrase recurring across a segment is ordinary speech,
    a loop is not.
    """
    tokens = re.findall(r"\S+", text)
    keys = [_key(t) for t in tokens]
    removed = 0
    out: list[str] = []
    i = 0
    while i < len(tokens):
        collapsed = False
        for n in range(1, max_n + 1):
            if i + n >= len(tokens) or not keys[i] or keys[i] != keys[i + n]:
                continue                       # the phrase cannot repeat straight away
            gram = keys[i:i + n]
            if not all(gram):
                continue
            needed = min_reps if n <= SHORT_PHRASE_WORDS else min(3, min_reps)
            reps = 1
            while i + (reps + 1) * n <= len(tokens) and keys[i + reps * n:i + (reps + 1) * n] == gram:
                reps += 1
            if reps >= needed:
                out += tokens[i:i + 2 * n] + ["[…]"]
                removed += (reps - 2) * n
                i += reps * n
                collapsed = True
                break
        if not collapsed:
            out.append(tokens[i])
            i += 1
    return " ".join(out), removed


def clean_segments(segments: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split segments into ``(kept, discarded)``. Loops are collapsed later, on the joined turn.

    A segment is kept when it holds a letter OR a digit: whisper writes "twenty
    twenty five" as "2025." and "$300." and "2,500" are answers, not noise. Only a
    wholly bracketed tag such as "(inaudible)", or a segment with no letter or digit
    at all, is discarded, and the caller discloses every discard.
    """
    kept, discarded = [], []
    for seg in segments:
        text = re.sub(r"\s+", " ", str(seg.get("text", ""))).strip()
        if not text:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        if end < start:
            continue
        item = {"start": start, "end": end, "text": text}
        if _BRACKET_ONLY.match(text) or not _HAS_LETTER_OR_DIGIT.search(text):
            discarded.append(item)
        else:
            kept.append(item)
    kept.sort(key=lambda s: (s["start"], s["end"]))
    discarded.sort(key=lambda s: (s["start"], s["end"]))
    return kept, discarded


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


def _slice_tokens(seg: dict, lo: float, hi: float) -> list[str]:
    """The words of ``seg`` that fall in [lo, hi], assuming an even speaking rate."""
    tokens = seg["text"].split()
    duration = seg["end"] - seg["start"]
    if not tokens:
        return []
    if duration <= 0:
        return tokens
    first = max(0, int(math.floor(len(tokens) * (max(lo, seg["start"]) - seg["start"]) / duration)))
    last = min(len(tokens), int(math.ceil(len(tokens) * (min(hi, seg["end"]) - seg["start"]) / duration)))
    return tokens[first:max(first, last)]


def _contained(own: list[str], other: list[str]) -> float:
    """Share of ``own``'s words (multiset) that also occur in ``other``."""
    mine = [k for k in (_key(t) for t in own) if k]
    theirs: dict[str, int] = {}
    for token in (_key(t) for t in other):
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


def _is_copy_of(own: dict, other: dict) -> float:
    """How much of ``own``'s words are found in the part of ``other`` it overlaps.

    Returns 0.0 unless it clears every text rule: enough words, enough content
    words, the share of all words and the share of content words both at the
    threshold. Narrow on purpose: only the overlapping stretch of ``other`` counts.
    """
    own_tokens = own["text"].split()
    if len(own_tokens) < BLEED_MIN_WORDS:
        return 0.0
    window = _slice_tokens(
        other, own["start"] - BLEED_WINDOW_PAD_SECONDS, own["end"] + BLEED_WINDOW_PAD_SECONDS
    )
    content = [t for t in own_tokens if len(_key(t)) >= CONTENT_WORD_LETTERS]
    if len(content) < BLEED_MIN_CONTENT_WORDS:
        return 0.0
    share = _contained(own_tokens, window)
    content_share = _contained(content, [t for t in window if len(_key(t)) >= CONTENT_WORD_LETTERS])
    return share if share >= BLEED_TEXT_CONTAINMENT and content_share >= BLEED_TEXT_CONTAINMENT else 0.0


def drop_crosstalk(
    tracks: dict[str, list[dict]],
    energy: dict[str, EnergyProfile] | None,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Remove the leaked copy of each matched pair. Returns (kept, dropped).

    A pair is one System segment and one Mic segment that overlap in time. It is
    decided ONCE: when the quieter copy's words are found in the other copy over
    the stretch they share, and it is quieter there by ``BLEED_MARGIN_DB``, that
    copy is removed and the other is protected, so one utterance is never lost
    from both tracks. Without level measurements nothing says which copy is the
    leak, so nothing is removed. Each removed segment keeps its text, the matched
    share, the level margin and the partner's span for recovery.
    """
    kept = {track: list(segments) for track, segments in tracks.items()}
    if not energy or any(t not in energy or not energy[t].usable for t in (SYSTEM, MIC)):
        return kept, []
    system, mic = tracks.get(SYSTEM, []), tracks.get(MIC, [])
    dropped_ids: dict[int, dict] = {}
    protected: set[int] = set()
    for a in system:
        for b in mic:
            lo, hi = a["start"] - BLEED_WINDOW_PAD_SECONDS, a["end"] + BLEED_WINDOW_PAD_SECONDS
            if _overlap(lo, hi, b["start"], b["end"]) <= 0:
                continue
            if id(a) in dropped_ids or id(b) in dropped_ids:
                continue
            start, end = max(a["start"], b["start"]), min(a["end"], b["end"])
            if end - start < BLEED_MIN_COMMON_SECONDS:
                start, end = min(a["start"], b["start"]), max(a["end"], b["end"])
            span = [(start, end)]
            system_db, mic_db = energy[SYSTEM].relative_db(span), energy[MIC].relative_db(span)
            if system_db is None or mic_db is None:
                continue
            margin = system_db - mic_db          # > 0: the mic copy is the quieter one
            if margin >= BLEED_MARGIN_DB:
                leak, other, track, gap = b, a, MIC, margin
            elif -margin >= BLEED_MARGIN_DB:
                leak, other, track, gap = a, b, SYSTEM, -margin
            else:
                continue
            if id(leak) in protected:
                continue
            duration = max(leak["end"] - leak["start"], FRAME_SECONDS)
            if _overlap(leak["start"], leak["end"], other["start"] - BLEED_WINDOW_PAD_SECONDS,
                        other["end"] + BLEED_WINDOW_PAD_SECONDS) / duration < BLEED_MIN_OVERLAP:
                continue
            share = _is_copy_of(leak, other)
            if not share:
                continue
            dropped_ids[id(leak)] = {
                **leak, "track": track, "reason": "text found on the other track and quieter here",
                "text_match": round(share, 2), "margin_db": round(gap, 1),
                "matched": {"track": SYSTEM if track == MIC else MIC,
                            "start": round(other["start"], 2), "end": round(other["end"], 2)},
            }
            protected.add(id(other))
    for track in kept:
        kept[track] = [s for s in kept[track] if id(s) not in dropped_ids]
    dropped = sorted(dropped_ids.values(), key=lambda d: (d["start"], d["track"]))
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


def _plain(value: object, limit: int = 100) -> str:
    """One line with no brackets, so a marker or header cannot break its own pattern."""
    return re.sub(r"[\[\]\s]+", " ", str(value)).strip()[:limit]


def chunk_outcomes(chunks: list[dict]) -> dict:
    """Withheld, silent, empty and still-looping chunks from one track's chunk records.

    ``gaps`` has one entry per chunk, with its ``kind``; ``looping`` lists the
    spans of chunks that still looped after the re-run.
    """
    gaps, looping = [], []
    for chunk in chunks or []:
        if chunk.get("kind") != "chunk":
            continue
        start = float(chunk.get("start", 0.0))
        end = float(chunk.get("end", start))
        status = str(chunk.get("status", ""))
        if status.startswith("screened"):
            gaps.append({"kind": KIND_WITHHELD, "start": start, "end": end,
                         "reason": _plain(chunk.get("reason", "screened"), 40),
                         "detail": _plain(chunk.get("detail", ""), 100)})
        elif status == "silent":
            gaps.append({"kind": KIND_SILENT, "start": start, "end": end, "reason": "silent", "detail": ""})
        elif status == "empty":
            gaps.append({"kind": KIND_EMPTY, "start": start, "end": end, "reason": "no words", "detail": ""})
        elif chunk.get("loop_after_rerun"):
            looping.append({"start": start, "end": end})
    return {"gaps": gaps, "looping": looping}


def _noun(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _runs(items: list[dict], stitch: float) -> list[dict]:
    """Join items of one track and kind that touch (or nearly) into spans."""
    runs: list[dict] = []
    for item in sorted(items, key=lambda i: (i["track"], i["start"])):
        last = runs[-1] if runs else None
        if last and last["track"] == item["track"] and item["start"] - last["end"] <= stitch:
            last["end"] = max(last["end"], item["end"])
            last["count"] += 1
        else:
            runs.append({"track": item["track"], "start": item["start"], "end": item["end"], "count": 1})
    return sorted(runs, key=lambda r: (r["start"], r["track"]))


def _span_list(items: list[dict], stitch: float, limit: int = 5) -> str:
    runs = _runs(items, stitch)
    shown = ", ".join(f"{r['track']} {format_clock(r['start'])}–{format_clock(r['end'])}" for r in runs[:limit])
    return shown + (f" and {len(runs) - limit} more" if len(runs) > limit else "")


def _header(stats: dict, diarization: dict | None, stitch: float = 4.0) -> str:
    """One line, ``[Speakers: ...]`` and no ``]`` inside, so consumers strip it.

    States the method, the diarization outcome and mapping, that names are not
    established, and everything removed or withheld, each with track and span.
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
    withheld = [g for g in gaps if g["kind"] == KIND_WITHHELD]
    if withheld:
        spans = ", ".join(
            f"{g['track']} {format_clock(g['start'])}–{format_clock(g['end'])} {g['reason']}" for g in withheld[:5]
        )
        more = f" and {len(withheld) - 5} more" if len(withheld) > 5 else ""
        parts.append(f"{_noun(len(withheld), 'chunk')} withheld by the output screen ({spans}{more}), "
                     "text kept in the stored evidence")
    silent = [g for g in gaps if g["kind"] == KIND_SILENT]
    if silent:
        parts.append(f"{_noun(len(silent), 'chunk')} skipped as silent ({_span_list(silent, stitch)})")
    empty = [g for g in gaps if g["kind"] == KIND_EMPTY]
    if empty:
        parts.append(f"{_noun(len(empty), 'audible chunk')} with no words recognised ({_span_list(empty, stitch)})")
    looping = stats.get("looping_chunks") or []
    if looping:
        parts.append(f"{_noun(len(looping), 'chunk')} still looping after a re-run ({_span_list(looping, stitch)}), "
                     "loops collapsed")
    bleed = stats.get("bleed_segments") or []
    if bleed:
        parts.append(f"{_noun(len(bleed), 'cross-talk segment')} removed ({_span_list(bleed, stitch)}), "
                     "text kept in the stored evidence")
    gone = stats.get("discarded_segments") or []
    if gone:
        parts.append(f"{_noun(len(gone), 'non-speech segment')} dropped ({_span_list(gone, stitch)}), "
                     "text kept in the stored evidence")
    if stats.get("bleed_checked"):
        parts.append("leaks not matched by text are kept, so a Mic turn may repeat a few remote words")
    elif stats.get("bleed_checked") is False:
        parts.append("cross-talk check skipped, level unmeasured")
    if stats.get("repeats_removed"):
        parts.append(f"{stats['repeats_removed']} repeated words collapsed")
    parts += [_plain(note, 200) for note in stats.get("notes") or []]
    return "[Speakers: " + "; ".join(parts) + "]"


def merge_tracks(
    tracks: dict[str, list[dict]],
    *,
    diarization: dict | None = None,
    energy: dict[str, EnergyProfile] | None = None,
    stitch_seconds: float = 4.0,
    chunks: dict[str, list[dict]] | None = None,
    notes: list[str] | None = None,
) -> MergeResult:
    """Merge per-track ASR segments into one time-ordered dialogue.

    ``tracks`` is ``{SYSTEM: [segment], MIC: [segment]}``, each segment
    ``{"start", "end", "text"}`` in seconds on the shared timeline. ``chunks`` is
    each track's chunk QA records: every withheld, silent or empty chunk becomes
    a timed marker in the body and a track-and-span count in the header, as does
    every removed cross-talk segment and every chunk still looping after a
    re-run. ``notes`` are extra header clauses (for example a long-call note).
    """
    stats: dict = {"repeats_removed": 0, "energy_measured": bool(energy), "notes": list(notes or []),
                   "stitch_seconds": stitch_seconds}
    cleaned, discarded = {}, []
    for track in (SYSTEM, MIC):
        cleaned[track], gone = clean_segments(tracks.get(track, []))
        discarded += [{**g, "track": track} for g in gone]
    discarded.sort(key=lambda g: (g["start"], g["track"]))
    stats["segments_discarded"] = len(discarded)
    stats["discarded_segments"] = [
        {"track": g["track"], "start": round(g["start"], 2), "end": round(g["end"], 2), "text": g["text"]}
        for g in discarded
    ]
    stats["gaps"], stats["looping_chunks"] = [], []
    for track in (SYSTEM, MIC):
        outcome = chunk_outcomes((chunks or {}).get(track, []))
        stats["gaps"] += [{**g, "track": track} for g in outcome["gaps"]]
        stats["looping_chunks"] += [{**span, "track": track} for span in outcome["looping"]]
    stats["gaps"].sort(key=lambda g: (g["start"], g["track"]))
    stats["chunks_withheld"] = sum(g["kind"] == KIND_WITHHELD for g in stats["gaps"])
    stats["chunks_silent"] = sum(g["kind"] == KIND_SILENT for g in stats["gaps"])
    stats["chunks_empty"] = sum(g["kind"] == KIND_EMPTY for g in stats["gaps"])
    stats["chunks_looping"] = len(stats["looping_chunks"])

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
         "reason": d["reason"], "text_match": d["text_match"], "margin_db": d["margin_db"],
         "matched": d["matched"]}
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

    def label_of(track: str, seg: dict) -> str:
        return remote_label(seg) if track == SYSTEM else MIC

    rows = []
    for track in (SYSTEM, MIC):
        for seg in kept[track]:
            rows.append({**seg, "track": track, "label": label_of(track, seg)})
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

    # Timed markers: one per withheld chunk; runs of adjacent silent, empty or
    # cross-talk stretches of one track become one marker each.
    markers: list[dict] = []
    for g in stats["gaps"]:
        if g["kind"] == KIND_WITHHELD:
            markers.append({"start": g["start"], "end": g["end"], "kind": g["kind"], "track": g["track"],
                            "label": label_of(g["track"], g),
                            "text": MARKERS[g["kind"]].format(reason=g["reason"], detail=g["detail"] or "no detail")})
    removed = [{**g, "track": g["track"]} for g in stats["gaps"] if g["kind"] in (KIND_SILENT, KIND_EMPTY)]
    removed += [{"kind": KIND_CROSSTALK, "start": d["start"], "end": d["end"], "track": d["track"]} for d in dropped]
    removed += [{"kind": KIND_DISCARDED, "start": g["start"], "end": g["end"], "track": g["track"]} for g in discarded]
    for kind in (KIND_SILENT, KIND_EMPTY, KIND_CROSSTALK, KIND_DISCARDED):
        for run in _runs([r for r in removed if r["kind"] == kind], stitch_seconds):
            markers.append({"start": run["start"], "end": run["end"], "kind": kind, "track": run["track"],
                            "label": label_of(run["track"], run), "text": MARKERS[kind]})
    stats["markers"] = {kind: sum(m["kind"] == kind for m in markers) for kind in MARKERS}

    if not turns and not markers:
        return MergeResult([], NO_SPEECH_TEXT, stats)
    blocks = sorted(
        [(t["start"], t["end"], t["label"], t["text"]) for t in turns]
        + [(m["start"], m["end"], m["label"], m["text"]) for m in markers]
    )
    body = "\n\n".join(
        f"[{format_clock(a)}–{format_clock(b)}] [{label}]\n{text}" for a, b, label, text in blocks
    )
    return MergeResult(turns, f"{_header(stats, diarization, stitch_seconds)}\n\n{body}", stats)
