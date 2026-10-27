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
also spent one. There is no override: past the cap the unit is split or cut in
scope and continues in a fresh PR.

Pure: no network, no daemon, no Neotoma.
"""

from __future__ import annotations

from dataclasses import dataclass

ROUND_CAP = 3
CAP_REACHED = "round cap reached: split or cut scope"


@dataclass(frozen=True)
class RoundStatus:
    """Where the PR's review rounds stand against the cap."""

    current_head: str
    prior_heads: tuple[str, ...]  # distinct OTHER heads that got a verdict
    cap: int = ROUND_CAP
    current_has_verdict: bool = False

    @property
    def round_number(self) -> int:
        """The round the current head is (or would be)."""
        return len(self.prior_heads) + 1

    @property
    def capped(self) -> bool:
        return len(self.prior_heads) >= self.cap

    @property
    def refused(self) -> bool:
        """The approval must be refused: the head is past the cap."""
        return self.capped

    def describe(self) -> str:
        """Plain-words status line for the tool's output."""
        seen = ", ".join(h[:7] for h in self.prior_heads) or "none"
        if not self.capped:
            return (
                f"review rounds: this head is round {self.round_number} of {self.cap} "
                f"(earlier reviewed heads: {seen})."
            )
        return (
            f"{CAP_REACHED}. {len(self.prior_heads)} earlier heads already received lens verdicts "
            f"({seen}); this head would be round {self.round_number}, past the cap of {self.cap}. "
            "Refusing to approve this verdict set. Split the unit or cut its scope and open a fresh PR."
        )


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
    )
