"""execution/daemons/apis/review_carry.py — which lenses a fix needs re-run.

Operator rulings, 2026-09-29 (Phase A3 plan ``ent_c2fa995ec1058a3e7b8b5b20``):

* ``rereview_only_blockers_and_touched_areas`` — after a fix, re-run only the
  lenses that blocked, plus any lens whose AREA the fix touched. The other
  lenses' sign-offs carry forward.
* ``combined_pm_qa_ux_pass`` — pm, qa and ux review in ONE mid-tier pass that
  still posts each lens's own verdict comment, in the exact per-lens format the
  approval gate parses.

This module holds the pure decisions, shared by the Apis panel dispatch
(``swarm_dispatch``) and the approval gate (``approve_pr_as_app``), so both
apply one carry rule. The dispatcher also forces a pending gate's owner to
re-run (``forced``); the gate reads no gate state and relies on that re-run
having produced a current-head verdict. It extends the existing carry-forward
(a signed-off gate stays signed off, and only its owner is re-seated when it is
pending) from gates to lens verdicts; it adds no second selection path.

Fail-closed by construction, on every input:

* every changed file belongs to EVERY lens unless it is on a short explicit
  allowlist of paths a lens provably does not own (docs, tests, release notes);
* an empty delta after a head change is unknown, and carries nothing;
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
# INVERTED default (security and arch reviews of ateles#1368): every file
# belongs to EVERY lens, unless it is on the short allowlist below of paths a
# lens provably does not own. `review_panel.LENSES[*].diff_patterns` decide who
# is SEATED on a panel; missing one there is harmless, so they must never be
# read in reverse as "owned by this lens alone". They appear here only to
# widen: a path that is security-sensitive keeps every lens.
#
# The allowlist is deliberately narrow and written out, not derived. Rows are
# tried in order and the FIRST match wins, so a specific row must come before a
# general one it overlaps (a release-note row behind a general docs row never
# fires, ateles#1368 round 2):
#   * agent prompts under `docs/agents/` are behaviour: every lens;
#   * release notes (`CHANGELOG*.md`, `docs/release(s)/`): pm, and ux for prose;
#   * foundation docs under `docs/foundation/` (design): arch and ux;
#   * prose-only doc directories (`guide`, `archive`, `plans`, `private`): ux.
#     Other `docs/` directories are NOT allowlisted: architecture, subsystems,
#     specs, developer and manifest docs are the interface rules arch gates on,
#     and instruction docs ship to every client, so an unlisted doc keeps every
#     lens (arch review of ateles#1368);
#   * test modules, `test_*.py` and `*_test.py` (not `conftest.py`): qa.
# Anything else, and above all code, workflows, dependency manifests, agent
# prompts and security paths, keeps every lens. The test module pins one real
# path per row, so a row that never fires fails a test.
_ALLOWLIST: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"^docs/agents/"), ALL_LENSES),
    (re.compile(r"(^|/)CHANGELOG[^/]*\.md$|^docs/releases?/.*\.md$"), frozenset({"pm", "ux"})),
    (re.compile(r"^docs/foundation/.*\.md$"), frozenset({"arch", "ux"})),
    (re.compile(r"^docs/(guide|archive|plans|private)/.*\.md$"), frozenset({"ux"})),
    (re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$"), frozenset({"qa"})),
)

# Paths that keep every lens whatever the allowlist says: workflows, dependency
# manifests, and anything a security surface pattern claims.
_ALWAYS_ALL: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)\.github/"),
    re.compile(
        r"(^|/)(package(-lock)?\.json|pyproject\.toml|setup\.(py|cfg)|Pipfile(\.lock)?"
        r"|requirements[^/]*\.txt|poetry\.lock|yarn\.lock|pnpm-lock\.yaml)$"
    ),
)
_SECURITY_LENS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for lens in LENSES
    if lens.lens == "security"
    for pattern in lens.diff_patterns
)


def _keeps_every_lens(path: str) -> bool:
    return (
        any(p.search(path) for p in _ALWAYS_ALL)
        or any(p.search(path) for p in _SECURITY_LENS_PATTERNS)
        or any(fragment in path for fragment in model_tiering.SECURITY_SENSITIVE_PATH_FRAGMENTS)
    )


def lenses_for_path(path: str) -> frozenset[str]:
    """The lenses whose area *path* falls in: EVERY lens unless the path is on
    the allowlist and is not a security-sensitive path."""
    if _keeps_every_lens(path):
        return ALL_LENSES
    for pattern, lenses in _ALLOWLIST:
        if pattern.search(path):
            return lenses
    return ALL_LENSES


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
            reasons[lens] = (
                f"delta since {cand.head[:7]} unreadable; re-run this lens on the "
                "current head, then run the gate again"
            )
            continue
        if not delta.files:
            # The head moved, so "no changed file" is a measurement that found
            # nothing, not proof of nothing: unknown, and unknown carries nothing.
            rerun.add(lens)
            reasons[lens] = (
                f"delta since {cand.head[:7]} is empty although the head moved, so "
                "the change is unknown; re-run this lens on the current head, then "
                "run the gate again"
            )
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


_TRAILING_ARTIFACT_RES = (
    re.compile(r"^\s*\U0001f916\s*Generated with \[Claude Code\]\(https://claude\.com/claude-code\)\s*$"),
    re.compile(r"^\s*Co-Authored-By:.*<noreply@anthropic\.com>\s*$", re.I),
    re.compile(r"^\s*\*\*\U0001f916[^\n]*Ateles swarm[^\n]*\*\*\s*$"),
)


def strip_trailing_artifact(block: str) -> str:
    """*block* without trailing attribution or artifact lines the model added
    after its findings (a harness footer, or a header line of its own). Only
    whole trailing lines are removed; the header and verdict lines at the top
    of a block are never touched."""
    lines = block.rstrip("\n").split("\n")
    while len(lines) > 2 and (
        not lines[-1].strip() or any(r.match(lines[-1]) for r in _TRAILING_ARTIFACT_RES)
    ):
        lines.pop()
    return "\n".join(lines) + "\n"


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
        blocks[lens] = strip_trailing_artifact(text[line_end + 1 : end].strip("\n") + "\n")
    return blocks


def combined_provenance(lens: str, lenses: Sequence[str], run_as: str) -> str:
    """One line saying this verdict came from a combined pass, so the record
    does not read as a solo run of the lens's own agent."""
    extra = " It ran diff-only: nothing was executed." if lens == "qa" else ""
    return (
        f"_Combined {'/'.join(lenses)} pass, run as {run_as or 'the first lens agent'}; "
        f"this is the {lens} lens's own verdict.{extra}_"
    )


def compose_combined_comment(block: str, marker: str, provenance: str = "") -> str:
    """The PR comment for one lens of a combined pass: the head marker line,
    then the lens's own block starting with its header and verdict, then a
    provenance line. The layout up to the verdict is the one a lens posting
    alone produces, so `lens_own_verdict` reads it."""
    tail = f"\n{provenance}\n" if provenance else ""
    return f"{marker}\n{block.strip()}\n{tail}"
