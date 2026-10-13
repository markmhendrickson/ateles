"""A hard stop on metered spend.

Only real, provider-reported cost counts (``cost_usd`` on a result line); there
is no estimating from a price list. The guard refuses to START a run when the
total already spent, plus a reserve for every run in flight, would pass the cap.
A run's reserve is the most expensive finished run of the same candidate (zero
before any run has finished), and each run is also given a per-run limit equal
to what is left, so a first run cannot overshoot unnoticed either.

A metered candidate whose finished run reported no cost trips the guard at once:
continuing would spend money the guard can no longer see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

METERED_KINDS = frozenset({"openrouter"})


@dataclass
class SpendGuard:
    cap_usd: float
    spent_usd: float = 0.0
    max_run_cost: dict[str, float] = field(default_factory=dict)
    in_flight: dict[str, int] = field(default_factory=dict)
    tripped: str = ""

    @classmethod
    def from_results(cls, cap_usd: float, results_path: Path) -> "SpendGuard":
        guard = cls(cap_usd=cap_usd)
        if results_path.exists():
            for line in results_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cost = rec.get("cost_usd")
                if isinstance(cost, (int, float)):
                    guard.spent_usd += float(cost)
                    cand = str(rec.get("candidate"))
                    guard.max_run_cost[cand] = max(
                        guard.max_run_cost.get(cand, 0.0), float(cost)
                    )
        return guard

    def reserve(self, candidate: str) -> tuple[bool, str, float | None]:
        """Ask to start a run. Returns ``(allowed, reason, per_run_limit)``."""
        if self.tripped:
            return False, self.tripped, None
        projected = self.max_run_cost.get(candidate, 0.0)
        reserved = sum(
            self.max_run_cost.get(c, 0.0) * n for c, n in self.in_flight.items()
        )
        if self.spent_usd >= self.cap_usd:
            return (
                False,
                f"spend cap reached: ${self.spent_usd:.4f} of ${self.cap_usd:.2f}",
                None,
            )
        if self.spent_usd + reserved + projected > self.cap_usd:
            return (
                False,
                f"next run could pass the cap: spent ${self.spent_usd:.4f}, "
                f"reserve ${reserved + projected:.4f}, cap ${self.cap_usd:.2f}",
                None,
            )
        self.in_flight[candidate] = self.in_flight.get(candidate, 0) + 1
        return True, "", max(self.cap_usd - self.spent_usd - reserved, 0.0)

    def finish(self, candidate: str, kind: str, cost_usd: float | None) -> None:
        """Record a finished run (always call after ``reserve`` allowed it)."""
        self.in_flight[candidate] = max(self.in_flight.get(candidate, 1) - 1, 0)
        if isinstance(cost_usd, (int, float)):
            self.spent_usd += float(cost_usd)
            self.max_run_cost[candidate] = max(
                self.max_run_cost.get(candidate, 0.0), float(cost_usd)
            )
        elif kind in METERED_KINDS:
            self.tripped = (
                f"candidate {candidate!r} is metered but its last run reported no cost; "
                "stopping rather than spend unseen money"
            )
