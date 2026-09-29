"""execution/daemons/apis/review_carry.py — which lenses a fix needs re-run.

Operator rulings, 2026-09-29 (Phase A3 plan ``ent_c2fa995ec1058a3e7b8b5b20``):

* ``rereview_only_blockers_and_touched_areas`` — after a fix, re-run only the
  lenses that blocked, plus any lens whose AREA the fix touched. The other
  lenses' sign-offs carry forward.
* ``combined_pm_qa_ux_pass`` — pm, qa and ux review in ONE mid-tier pass that
  still posts each lens's own verdict comment, in the exact per-lens format the
  approval gate parses.

This module holds the pure decisions, shared by the Apis panel dispatch
(``swarm_dispatch``) and the approval gate (``approve_pr_as_app``) so the two
cannot disagree about what may be carried. It extends the existing carry-forward
(a signed-off gate stays signed off, and only its owner is re-seated when it is
pending) from gates to lens verdicts; it adds no second selection path.

Fail-closed by construction, on every input:

* a changed file that matches no lens area maps to EVERY lens;
* a lens that blocked, or whose latest verdict is not an explicit clear, is
  never carried;
* a delta that cannot be read carries nothing;
* a lens with no earlier clear verdict has nothing to carry.

The delta is ``review_delta.interdiff`` (ateles#1365): the fix's OWN change, so
a merge of the base branch cancels out and never widens the touched areas.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

import model_tiering
import review_delta
from review_panel import LENSES

log = logging.getLogger("apis.review_carry")

GITHUB_API = "https://api.github.com"

# Every lens the panel registry knows. An unmapped file re-runs all of them.
ALL_LENSES: frozenset[str] = frozenset(lens.lens for lens in LENSES)

# Lenses that review together in one mid-tier pass (ruling
# `combined_pm_qa_ux_pass`). security and arch stay separate, on the top tier.
COMBINED_LENSES: tuple[str, ...] = ("pm", "qa", "ux")

# ── Lens areas ──────────────────────────────────────────────────────────────
# Derived from the classifiers that already exist, never retyped:
#   * every panel lens's own ``diff_patterns`` (review_panel.LENSES) — the
#     surfaces that pull that lens into a panel are the surfaces it owns;
#   * ``model_tiering.SECURITY_SENSITIVE_PATH_FRAGMENTS`` for security.
# The extras below cover the areas the operator named that the registry does
# not pattern (qa: tests and CI; pm: release artifacts; ux: docs and runbooks;
# arch: the design docs and contracts). A path in NO area is not "safe": it is
# unmapped, and unmapped means every lens (`lenses_for_path`).
_EXTRA_AREA_PATTERNS: dict[str, tuple[str, ...]] = {
    "qa": (
        r"(^|/)test_[^/]*\.py$",
        r"_test\.py$",
        r"(^|/)tests?/",
        r"(^|/)conftest\.py$",
        r"(^|/)\.github/workflows/",
        r"(^|/)pytest\.ini$",
    ),
    "pm": (
        r"(^|/)CHANGELOG",
        r"(^|/)docs/releases?/",
        r"(^|/)release[_-]notes",
    ),
    "ux": (
        r"\.md$",
        r"(^|/)runbooks?/",
    ),
    "arch": (
        r"(^|/)docs/foundation/",
        r"(^|/)contracts?/",
    ),
}


def _compile_areas() -> dict[str, tuple[re.Pattern[str], ...]]:
    areas: dict[str, list[re.Pattern[str]]] = {}
    for lens in LENSES:
        patterns = [re.compile(p) for p in lens.diff_patterns]
        patterns += [re.compile(p) for p in _EXTRA_AREA_PATTERNS.get(lens.lens, ())]
        if patterns:
            areas[lens.lens] = patterns
    return {name: tuple(patterns) for name, patterns in areas.items()}


_AREA_PATTERNS = _compile_areas()


def lenses_for_path(path: str) -> frozenset[str]:
    """The lenses whose area *path* falls in. A path in no area maps to EVERY
    lens: an unclassified change is never assumed safe to skip a review for."""
    hit = {
        lens
        for lens, patterns in _AREA_PATTERNS.items()
        if any(p.search(path) for p in patterns)
    }
    if any(fragment in path for fragment in model_tiering.SECURITY_SENSITIVE_PATH_FRAGMENTS):
        hit.add("security")
    return frozenset(hit) if hit else ALL_LENSES


def lenses_touched(files: Iterable[str]) -> frozenset[str]:
    """Union of ``lenses_for_path`` over a delta's files (empty delta: none)."""
    touched: set[str] = set()
    for path in files:
        touched |= lenses_for_path(path)
    return frozenset(touched)


# ── Earlier verdicts and the carry decision ─────────────────────────────────


@dataclass(frozen=True)
class LensRecord:
    """One lens verdict comment on a PR, at the head it was written for."""

    lens: str
    head: str  # full 40-hex commit the verdict is pinned to
    cleared: bool  # an explicit clear (sign_off_is_warranted), nothing else
    verdict: str = ""  # the lens's own verdict token, "" when unreadable
    url: str = ""


def carry_candidate(
    records: Sequence[LensRecord], current_head: str
) -> LensRecord | None:
    """The earlier clear verdict that could be carried, or None.

    Only the lens's LATEST verdict counts, so a lens that blocked after an
    earlier clear is a blocker, never a carry; and every verdict it wrote at
    that head must be a clear. A lens already reviewed at the current head has
    a fresh verdict, not a carried one.
    """
    if not records:
        return None
    last = records[-1]
    if last.head == current_head:
        return None
    # Every verdict at that head must be a clear: `last` itself, and any earlier
    # comment the lens wrote there (a block it later re-posted a clear over).
    if any(r.head == last.head and not r.cleared for r in records):
        return None
    return last


def candidate_heads(
    lenses: Iterable[str],
    records: Mapping[str, Sequence[LensRecord]],
    current_head: str,
) -> set[str]:
    """The earlier heads whose delta to the current head must be read."""
    heads: set[str] = set()
    for lens in lenses:
        found = carry_candidate(records.get(lens, ()), current_head)
        if found is not None:
            heads.add(found.head)
    return heads


@dataclass(frozen=True)
class Carried:
    """A lens sign-off carried to the current head, with its provenance."""

    lens: str
    head: str  # the head the verdict was written for
    verdict: str
    url: str
    untouched: bool = True  # the fix since `head` touched none of its areas


@dataclass(frozen=True)
class RerunSelection:
    rerun: frozenset[str]
    carried: dict[str, Carried] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)


def select_rerun(
    panel: Iterable[str],
    records: Mapping[str, Sequence[LensRecord]],
    current_head: str,
    deltas: Mapping[str, review_delta.Delta | None],
    *,
    forced: Iterable[str] = (),
) -> RerunSelection:
    """Split *panel* into lenses to re-run and lenses whose sign-off carries.

    ``deltas`` maps an earlier head to the interdiff from it to the current
    head (None: unreadable). ``forced`` are lenses that must run regardless, such
    as the owner of a still-pending gate (the existing Lanius carry-forward:
    only a pending gate's owner is re-seated).
    """
    forced_set = {str(f).lower() for f in forced}
    rerun: set[str] = set()
    carried: dict[str, Carried] = {}
    reasons: dict[str, str] = {}
    for lens in panel:
        if lens in forced_set:
            rerun.add(lens)
            reasons[lens] = "owns a gate still pending"
            continue
        cand = carry_candidate(records.get(lens, ()), current_head)
        if cand is None:
            rerun.add(lens)
            reasons[lens] = "no clear earlier verdict to carry (blocked, unreadable, or never reviewed)"
            continue
        delta = deltas.get(cand.head)
        if delta is None:
            rerun.add(lens)
            reasons[lens] = f"delta since {cand.head[:7]} unreadable"
            continue
        touched = lenses_touched(delta.files)
        if lens in touched:
            rerun.add(lens)
            reasons[lens] = f"the fix touched its area since {cand.head[:7]}"
            continue
        carried[lens] = Carried(lens, cand.head, cand.verdict, cand.url)
        reasons[lens] = f"carried from {cand.head[:7]}: the fix touched none of its areas"
    return RerunSelection(frozenset(rerun), carried, reasons)


async def fetch_interdiff(
    client,
    *,
    repo: str,
    base_ref: str,
    old_head: str,
    new_head: str,
    headers: Mapping[str, str],
) -> review_delta.Delta | None:
    """The PR's own change between two heads, or None when it cannot be read.

    Both sides are three-dot compares against the base branch, exactly as
    ``review_delta`` documents, so a merge of the base cancels. Any failure —
    a head GitHub no longer has, a truncated file list, a network error — is
    None: the caller carries nothing.
    """
    if not (base_ref and old_head and new_head):
        return None
    try:
        sides: list[list[dict]] = []
        for sha in (old_head, new_head):
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/compare/{quote(base_ref, safe='/')}...{sha}",
                headers=dict(headers),
            )
            resp.raise_for_status()
            files = review_delta.compare_files(resp.json())
            if files is None:
                return None
            sides.append(files)
        return review_delta.interdiff(sides[0], sides[1])
    except Exception as exc:  # unreadable delta: fail closed, carry nothing
        log.warning(f"[review_carry] delta {old_head[:7]}..{new_head[:7]} unreadable: {exc}")
        return None


# ── Combined pm/qa/ux pass ──────────────────────────────────────────────────


def combined_delimiter(lens: str) -> str:
    return f"=====REVIEW:{lens}====="


def split_combined_reply(stdout: str | None, lenses: Sequence[str]) -> dict[str, str]:
    """Each lens's own block from a combined reply; a lens whose block is
    missing or duplicated is absent from the result (the caller runs it alone).

    A block is the text between its delimiter line and the next delimiter. It
    is returned as written: the caller validates it with the same reader the
    approval gate uses, and this function never repairs a block.
    """
    text = stdout or ""
    positions: list[tuple[int, str]] = []
    for lens in lenses:
        found = [m.start() for m in re.finditer(
            rf"(?m)^{re.escape(combined_delimiter(lens))}[ \t]*$", text)]
        if len(found) != 1:
            continue
        positions.append((found[0], lens))
    positions.sort()
    blocks: dict[str, str] = {}
    for i, (start, lens) in enumerate(positions):
        line_end = text.find("\n", start)
        if line_end < 0:
            continue
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        blocks[lens] = text[line_end + 1 : end].strip("\n") + "\n"
    return blocks


def compose_combined_comment(block: str, marker: str) -> str:
    """The PR comment for one lens of a combined pass: the head marker line,
    then the lens's own block starting with its header and verdict. The layout
    is the one a lens posting alone produces, so `lens_own_verdict` reads it."""
    return f"{marker}\n{block.strip()}\n"
