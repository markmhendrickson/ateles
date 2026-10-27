"""execution/daemons/apis/review_rounds.py — the three-round review cap.

Operator ruling, 2026-10-27 (agent_policy ``ent_d0f1a840e549b3b299f62397``,
bootstrap rule): a repair unit that has not merged after three build, review
and correct rounds is split or cut in scope before another round runs.

A *round* is one head of the PR that received at least one lens verdict. The
count is read from the swarm's own lens verdict comments and their
``<!-- review:<lens> commit=<sha> -->`` markers, through the same reader the
approval gate uses (``swarm_dispatch.lens_records`` over
``lens_authors.scope_lens_comments``), so a marker an outside account posts on
a public repo is never a round, and a lens the registry does not know is never
a round.

The head being approved is round ``prior + 1``, where ``prior`` is the number
of OTHER distinct heads that already received a verdict. The cap is reached
when ``prior >= ROUND_CAP``: three heads have been reviewed and the PR is still
not approved, so the head in front of the tool would be a fourth round or
later. Re-running the tool on the third round's own head is not a fourth round
(``prior`` is 2 there), so a round-three approval is never blocked by this.

Counting is deliberately generous: a verdict comment whose verdict the parser
cannot read still spent the round. A rebase or empty push that a lens re-ran on
also spent one. The escape hatch for a case the operator accepts is explicit
and logged (``approve_pr_as_app --allow-round-overrun <reason>``); there is no
environment setting, because an ambient value would lift the cap for every PR
without anyone naming one.

Pure: no network, no daemon, no Neotoma.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ROUND_CAP = 3
CAP_REACHED = "round cap reached: split or cut scope"

# A reason is logged on the PR in the approval body, so it is kept short and
# stripped of anything that could read as markup or as a swarm marker.
MAX_REASON_CHARS = 300
MIN_REASON_CHARS = 10


@dataclass(frozen=True)
class RoundStatus:
    """Where the PR's review rounds stand against the cap."""

    current_head: str
    prior_heads: tuple[str, ...]  # distinct OTHER heads that got a verdict
    cap: int = ROUND_CAP
    current_has_verdict: bool = False
    override_reason: str = ""

    @property
    def round_number(self) -> int:
        """The round the current head is (or would be)."""
        return len(self.prior_heads) + 1

    @property
    def capped(self) -> bool:
        return len(self.prior_heads) >= self.cap

    @property
    def refused(self) -> bool:
        """The approval must be refused: capped and no logged override."""
        return self.capped and not self.override_reason

    def describe(self) -> str:
        """Plain-words status line for the tool's output."""
        seen = ", ".join(h[:7] for h in self.prior_heads) or "none"
        if not self.capped:
            line = (
                f"review rounds: this head is round {self.round_number} of {self.cap} "
                f"(earlier reviewed heads: {seen})."
            )
            if self.override_reason:
                line += (
                    " --allow-round-overrun was given but is not needed here; it is not "
                    "recorded on the approval."
                )
            return line
        facts = (
            f"{len(self.prior_heads)} earlier heads already received lens verdicts "
            f"({seen}); this head would be round {self.round_number}, past the cap of {self.cap}."
        )
        if self.override_reason:
            return (
                f"round cap overrun allowed by --allow-round-overrun: {self.override_reason} "
                f"({facts} Without the override this would read: {CAP_REACHED}.)"
            )
        return (
            f"{CAP_REACHED}. {facts} Refusing to approve this verdict set. Split the unit or "
            "cut its scope and open a fresh PR; if the operator has granted an overrun "
            "for this PR, re-run with --allow-round-overrun '<reason>'."
        )

    def approval_note(self) -> str:
        """The line recorded in the approval review body, or "" when none is owed."""
        if not (self.capped and self.override_reason):
            return ""
        return (
            f"Round cap overrun allowed: this is review round {self.round_number} "
            f"(cap {self.cap}). Reason given: {self.override_reason}"
        )


class OverrunReasonError(ValueError):
    """The ``--allow-round-overrun`` reason is unusable."""


def clean_reason(raw: str) -> str:
    """The override reason as it will be logged, or ``OverrunReasonError``.

    Whitespace is collapsed and angle brackets are dropped so the text cannot
    open an HTML comment (a swarm marker) in the approval body; the length is
    capped. `@`, square brackets and backticks are dropped too, so the logged
    reason cannot mention an account or build a link, and control or bidi
    characters are replaced by spaces. A reason that is empty, or too short to say anything, is refused: an
    override with no stated reason is not a logged override.
    """
    text = re.sub(r"[<>@\[\]`]", " ", raw or "")
    # Control and format characters (escape sequences, bidi overrides) are not text
    # an operator can read back; they would only change how the log line renders.
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < MIN_REASON_CHARS:
        raise OverrunReasonError(
            f"--allow-round-overrun needs a stated reason of at least {MIN_REASON_CHARS} "
            "characters; it is recorded on the approval"
        )
    return text[:MAX_REASON_CHARS]


def reviewed_heads(records: dict[str, list]) -> list[str]:
    """Distinct heads that received a lens verdict, from ``lens_records`` output.

    *records* is ``swarm_dispatch.lens_records(...)``: lens -> records, each with
    a ``.head``. Order is first appearance, lens by lens; it is for display only.
    """
    seen: list[str] = []
    for recs in records.values():
        for rec in recs:
            head = (getattr(rec, "head", "") or "").lower()
            if head and head not in seen:
                seen.append(head)
    return seen


def round_status(
    records: dict[str, list],
    current_head: str,
    *,
    override_reason: str = "",
    cap: int = ROUND_CAP,
) -> RoundStatus:
    """Where *current_head* stands against the cap, given the lens records."""
    current = (current_head or "").lower()
    heads = reviewed_heads(records)
    return RoundStatus(
        current_head=current,
        prior_heads=tuple(h for h in heads if h != current),
        cap=cap,
        current_has_verdict=current in heads,
        override_reason=override_reason,
    )
