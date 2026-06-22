#!/usr/bin/env python3
"""
parse_chatgpt_workouts.py — Reconstruct structured workout_session records from a
raw ChatGPT Fitness-GPT conversation export.

The conversation logs sets conversationally (incremental rep corrections, renames,
unit switches), so the USER inputs are not reliably parseable on their own. Instead
this parser harvests the ASSISTANT's running structured session summaries — the
"Current Session Summary" / per-exercise breakdown blocks — which the GPT maintains
in a stable format, and converts the latest/most-complete summary per session day
into the Gorilla `workout_session` schema.

Output: a reviewable JSON array of sessions (date, location, session_type,
exercises[{exercise_name, sets[{weight_kg, reps, set_type}]}], notes), plus a
per-session confidence flag and the raw assistant block it was parsed from, so a
human can verify before importing to Neotoma.

Usage:
  python3 parse_chatgpt_workouts.py <raw_conversation.json> [-o out.json] [--date YYYY-MM-DD]

Conventions handled:
  - First daily set of each exercise = warmup (operator's stated rule).
  - Remaining sets = working (failure) unless labeled otherwise.
  - Weights normalized to kg. Detects lbs-mode days ("track in lbs") and converts
    (1 lb = 0.45359237 kg), flagging the session for review.
  - Bodyweight movements: records added load where given; notes bodyweight.
  - Default location: Metropolitan Sagrada Família unless an override appears.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from datetime import datetime, date

LB_TO_KG = 0.45359237
DEFAULT_LOCATION = "Metropolitan Sagrada Família"

# ── Message extraction ──────────────────────────────────────────────────────

def load_messages(path: str) -> list[dict]:
    """Return time-ordered messages: [{ts, dt, role, text}]."""
    data = json.load(open(path))
    mapping = data.get("mapping", {})
    msgs = []
    for node in mapping.values():
        m = node.get("message")
        if not m or not m.get("author"):
            continue
        ct = m.get("content", {}) or {}
        parts = ct.get("parts") or ([ct["text"]] if ct.get("text") else [])
        text = "\n".join(p for p in parts if isinstance(p, str)).strip()
        ts = m.get("create_time")
        if not text or ts is None:
            continue
        msgs.append({
            "ts": ts,
            "dt": datetime.fromtimestamp(ts),
            "role": m["author"]["role"],
            "text": text,
        })
    msgs.sort(key=lambda x: x["ts"])
    return msgs


def group_by_date(msgs: list[dict]) -> dict[date, list[dict]]:
    out: dict[date, list[dict]] = {}
    for m in msgs:
        out.setdefault(m["dt"].date(), []).append(m)
    return out

# ── Set-line parsing ────────────────────────────────────────────────────────

# Matches assistant set lines like:
#   "60 kg × 10 (warm-up)"  "85 × 7"  "BW 80 × 6"  "10 kg x 15 (failure)"
#   "+20 kg × 20"  "37.5 × 6-7"
SET_LINE = re.compile(
    r"""(?P<bw>BW\s*)?            # optional bodyweight marker
        (?P<plus>\+)?            # optional added-weight marker
        (?P<weight>\d+(?:\.\d+)?)   # weight number
        \s*(?:kg|lb|lbs)?\s*       # optional unit
        [×x]\s*                     # times separator
        (?P<reps>\d+)              # reps
        (?:\s*[-–]\s*\d+)?          # optional rep range upper bound (take lower)
        \s*(?P<tag>\([^)]*\))?      # optional (warm-up)/(failure)/(PR) tag
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Matches markdown table set rows like:
#   "| 25  | 10 | Warmup |"   "| 42.5 | 6 | 🔥🏆 6-rep PR |"   "| 80 | 8 | ... |"
# Header/separator rows are rejected by requiring the first two cells numeric.
TABLE_ROW = re.compile(
    r"^\s*\|\s*(?P<bw>BW\s*|\+)?(?P<weight>\d+(?:\.\d+)?)\s*\|\s*(?P<reps>\d+)\s*\|\s*(?P<note>[^|]*?)\s*\|"
)

# Exercise header in assistant blocks, e.g.:
#   "### Exercise: Flat Barbell Bench Press"
#   "### 1. **Preacher Curl**"
#   "1. Bayesian Cable Curl"
#   "**Exercise: Cable Row**"
# Strategy: strip leading markdown (#, digits., *) and trailing *, then capture.
EXERCISE_HEADER = re.compile(
    r"^\s*(?:#{1,4}\s*)?(?:\d+\.\s*)?\*{0,2}\s*(?:Exercise:\s*)?(?P<name>[A-Za-z][A-Za-z0-9 '’()/.\-–&]+?)\s*\*{0,2}\s*$"
)

# Reject lines that are clearly not exercise headers. Word-boundaried so tokens
# like "PR" don't match inside real exercise names ("Preacher Curl").
_NON_EXERCISE = re.compile(
    r"\b(summary|interpretation|overview|session|back-off|context|notes?|takeaway|"
    r"analysis|breakdown|total|volume|rating|recommendations?|plan|focus|"
    r"PRs?|achieved|style|performed|logged|deeper|consolidated|conclusion|"
    r"macro|patterns?|targets?|estimate|streaks?|question)\b",
    re.I,
)

# Location appears as "**Location:** 🏋️ *Gym 5 — Nashville, TN*" or "@ Gym 5, Nashville".
# Strip leading emoji/markdown/whitespace, then capture a gym-like name.
LOCATION_RE = re.compile(
    r"(?:Location:?\**|@)\s*[^\w(]*\**\s*"
    r"(?P<loc>[A-Z][\w'’.\-]*(?:[ ,—\-][A-Za-z0-9][\w'’.\-]*){0,4})",
)
LBS_MODE_RE = re.compile(r"\b(track|indicate|log|in)\b.{0,25}\b(lbs?|pounds)\b", re.I)

# Fake "exercise" names that are really analysis/section headers leaking through.
_GARBAGE_EXERCISE = re.compile(
    r"\b(comparison|performance|core|summary|total|volume|overview|analysis|"
    r"benchmark|baseline|progression|pattern|result|takeaway|note|goal|"
    r"target|update|final|log|matrix|programming|order|completed|matrix)\b",
    re.I,
)

# Sentence/prose markers that disqualify a string from being an exercise name.
# Exercise names are short noun phrases; analysis takeaways are full sentences
# ("Your delts are operating in elite hypertrophy range.", "No assumptions.").
_PROSE_WORDS = re.compile(
    r"\b(your|you|you've|youve|i|i'll|i will|today|now|ensure|ensures|confirmed|"
    r"all-time|saturated|stimulated|covered|determines|maintain|correct|corrected|"
    r"officially|productive|elite|maximal|cleanly|precisely|moving|forward|"
    r"finished|done|undertraining|fluff|stays|inside|operating|extremely|highest|"
    r"strongest|complete|hit|noted|growth|recovery|adaptation|development)\b",
    re.I,
)


def _is_valid_exercise_name(name: str) -> bool:
    """An exercise name is a short noun phrase, not a sentence/takeaway."""
    n = name.strip()
    if not (3 <= len(n) <= 45):
        return False
    if not re.search(r"[a-z]", n):
        return False
    if n.endswith(".") or n.endswith("!") or n.endswith("?"):
        return False
    if len(n.split()) > 6:
        return False
    if _GARBAGE_EXERCISE.search(n) or _PROSE_WORDS.search(n):
        return False
    return True


def classify_set_type(note: str, is_first: bool) -> str:
    n = (note or "").lower()
    if "warm" in n:
        return "warmup"
    if any(k in n for k in ("fail", "pr", "top", "back-off", "back off", "working")):
        return "working"
    return "warmup" if is_first else "working"


def _add_set(current, weight, reps, note, bw, lbs_mode):
    w = float(weight)
    if lbs_mode:
        w = round(w * LB_TO_KG, 1)
    s = {"weight_kg": w, "reps": int(reps),
         "set_type": classify_set_type(note, len(current["sets"]) == 0)}
    if bw:
        s["bodyweight"] = True
    current["sets"].append(s)


def parse_assistant_block(text: str, lbs_mode: bool) -> list[dict]:
    """Parse one assistant message into [{exercise_name, sets:[...]}].

    Handles two formats the GPT uses interchangeably:
      (a) markdown tables  | weight | reps | note |
      (b) inline bullets   "- 80 kg × 8 (PR)"
    """
    exercises: list[dict] = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 1) Table set row?
        tr = TABLE_ROW.match(line)
        if tr and current is not None:
            # skip header rows where "weight" cell is actually a header word — guarded
            # by numeric capture, so safe. Skip if note looks like a column header.
            if tr.group("note").strip().lower() not in ("reps", "weight", "notes"):
                _add_set(current, tr.group("weight"), tr.group("reps"),
                         tr.group("note"), tr.group("bw"), lbs_mode)
                continue
        # 2) Exercise header?
        hdr = EXERCISE_HEADER.match(line)
        if hdr and not _NON_EXERCISE.search(line) and "|" not in line:
            name = hdr.group("name").strip().strip(":*").strip()
            if _is_valid_exercise_name(name):
                current = {"exercise_name": name, "sets": []}
                exercises.append(current)
                continue
        # 3) Inline set line?
        if current is not None:
            m = SET_LINE.search(line)
            if m:
                _add_set(current, m.group("weight"), m.group("reps"),
                         m.group("tag") or "", m.group("bw"), lbs_mode)
    # drop exercises with no sets, garbage names, or implausible loads (stray
    # rows from comparison/analysis tables leaking in as fake exercises).
    cleaned = []
    for e in exercises:
        name = e["exercise_name"]
        if not _is_valid_exercise_name(name):
            continue
        # implausible: any single-load set > 250 kg on a non-leg movement is
        # almost certainly an analysis artifact (e.g. cumulative volume).
        if any(s["weight_kg"] > 250 for s in e["sets"]) and not re.search(
            r"squat|press|deadlift|leg|hip|trap", name, re.I
        ):
            continue
        if e["sets"]:
            cleaned.append(e)
    return cleaned


def pick_best_block(day_msgs: list[dict]) -> tuple[str | None, bool]:
    """Choose the assistant message that yields the most complete session.

    Scoring prefers, in order: (1) more total sets, (2) more exercises with a
    plausible warmup-first pattern, (3) presence of explicit per-set notes (PR /
    warmup tags), which only the final analysis table carries. This avoids the
    note-less running summary winning a tie and flattening every set to warmup.
    Returns (block_text, lbs_mode).
    """
    lbs_mode = any(LBS_MODE_RE.search(m["text"]) for m in day_msgs if m["role"] == "user")
    best, best_key = None, (-1, -1, -1)
    for m in day_msgs:
        if m["role"] != "assistant":
            continue
        parsed = parse_assistant_block(m["text"], lbs_mode)
        n_sets = sum(len(e["sets"]) for e in parsed)
        if n_sets == 0:
            continue
        n_exercises = len(parsed)
        # A per-set Notes column marks the authoritative final analysis table.
        has_notes = 1 if re.search(r"\|\s*Reps\s*\|\s*Notes", m["text"], re.I) else 0
        # Composite score: completeness (sets) dominates, but a notes-bearing
        # analysis table gets a meaningful bonus so it beats an equally-sized or
        # slightly larger note-less running summary (which flattens set_type).
        score = n_sets * 10 + has_notes * 25 + n_exercises
        key = (score, has_notes, n_sets)
        if key >= best_key:
            best_key, best = key, m["text"]
    return best, lbs_mode


_LOC_NOISE = re.compile(r"\b(lbs?|kg|tn|today|now|session|log|final|updated)\b", re.I)


def detect_location(day_msgs: list[dict]) -> str:
    """Find the day's gym. Prefer an explicit override; default to home gym."""
    for m in day_msgs:
        for mt in LOCATION_RE.finditer(m["text"]):
            loc = mt.group("loc").strip(" *—-,")
            low = loc.lower()
            # skip the literal label and obvious non-locations
            if low in ("location", "goal", "result", "target", "today", "session"):
                continue
            # skip conversational fragments ("@ I'll add", "@ me", pronouns)
            if re.match(r"i['’]ll|i['’]m|me\b|you\b|we\b|here\b|the\b", low):
                continue
            if "sagrada" in low or "metropolitan" in low:
                return DEFAULT_LOCATION
            # strip trailing noise tokens (", TN", " lbs")
            loc = _LOC_NOISE.sub("", loc).strip(" *—-,")
            if len(loc) >= 3 and re.search(r"[A-Za-z]", loc):
                return loc
    return DEFAULT_LOCATION


def detect_session_type(day_msgs: list[dict]) -> str:
    for m in day_msgs:
        if m["role"] != "assistant":
            continue
        mt = re.search(r"Session\s+\w?\s*[—\-–].{0,40}?\((?P<st>[^)]+)\)", m["text"])
        if mt:
            return mt.group("st").strip()
    return ""

# ── Main ────────────────────────────────────────────────────────────────────

def build_sessions(msgs: list[dict], only_date: str | None) -> list[dict]:
    by_date = group_by_date(msgs)
    sessions = []
    for d in sorted(by_date):
        if only_date and d.isoformat() != only_date:
            continue
        day = by_date[d]
        block, lbs_mode = pick_best_block(day)
        exercises = parse_assistant_block(block, lbs_mode) if block else []
        if not exercises:
            sessions.append({
                "date": d.isoformat(),
                "location": detect_location(day),
                "session_type": detect_session_type(day),
                "exercises": [],
                "confidence": "empty",
                "notes": "No parseable structured assistant block found; needs manual review.",
            })
            continue
        total_sets = sum(len(e["sets"]) for e in exercises)
        sessions.append({
            "date": d.isoformat(),
            "started_at": day[0]["dt"].isoformat(),
            "location": detect_location(day),
            "session_type": detect_session_type(day),
            "exercises": exercises,
            "confidence": "high" if total_sets >= 6 else "low",
            "lbs_converted": lbs_mode,
            "notes": ("Weights converted from lbs." if lbs_mode else "")
                     + " First set of each exercise assumed warmup.",
        })
    return sessions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw_json")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--date", default=None, help="Only parse this YYYY-MM-DD (debug)")
    args = ap.parse_args()

    msgs = load_messages(args.raw_json)
    sessions = build_sessions(msgs, args.date)

    out = args.out or (args.raw_json.rsplit(".", 1)[0] + "_parsed_sessions.json")
    json.dump(sessions, open(out, "w"), ensure_ascii=False, indent=1)

    # Summary to stderr
    hi = sum(1 for s in sessions if s["confidence"] == "high")
    lo = sum(1 for s in sessions if s["confidence"] == "low")
    em = sum(1 for s in sessions if s["confidence"] == "empty")
    print(f"Parsed {len(sessions)} session-days → {out}", file=sys.stderr)
    print(f"  high-confidence: {hi}  low: {lo}  empty: {em}", file=sys.stderr)
    if sessions:
        print(f"  date range: {sessions[0]['date']} → {sessions[-1]['date']}", file=sys.stderr)


if __name__ == "__main__":
    main()
