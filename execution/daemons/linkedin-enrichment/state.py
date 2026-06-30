#!/usr/bin/env python3
"""
linkedin-enrichment / state — persisted pacing + daily-cap state.

The load-bearing guardrail of this daemon is the HARD daily cap on LinkedIn
profile visits. The risk is the operator's PERSONAL LinkedIn account
(restriction / ban), so the counter must:

  * persist across process restarts (a crash must NOT reset the day's tally to 0),
  * roll over only at a real calendar-day boundary (operator's local timezone),
  * NEVER burst-catchup — a missed day does not let the next day exceed the cap,
  * gate every single profile visit, not just batches.

State lives in a JSON file in the daemon dir (gitignored), mirroring the
turdus / cotinga convention. There is exactly one writer (this daemon), so a
plain read-modify-write with an atomic replace is sufficient; no locking.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("linkedin-enrichment.state")

# Default hard cap. Overridable via env for testing, but the daemon refuses to
# raise it above a sane ceiling (see clamp in EnrichmentState.load).
DEFAULT_DAILY_CAP = 10
ABSOLUTE_MAX_CAP = 10  # the cap is a ceiling, not a tunable — never exceed this.

# Operator's local timezone governs when "today" rolls over. Per CLAUDE.md the
# operator is in Europe/Madrid; sourced from env so the daemon stays portable.
_LOCAL_TZ_NAME = os.environ.get("LINKEDIN_ENRICH_TZ", "Europe/Madrid")

_STATE_FILE = Path(__file__).parent / ".linkedin_enrichment_state.json"


def _local_today() -> str:
    """ISO date string for 'today' in the operator's local timezone."""
    try:
        tz = ZoneInfo(_LOCAL_TZ_NAME)
    except Exception:  # pragma: no cover - bad tz name → fall back to UTC date
        log.warning("Invalid LINKEDIN_ENRICH_TZ=%r, falling back to UTC", _LOCAL_TZ_NAME)
        return date.today().isoformat()
    return datetime.now(tz).date().isoformat()


@dataclass
class EnrichmentState:
    """In-memory view of the persisted daemon state.

    Fields:
      cap            — hard per-day ceiling on profile visits.
      day            — ISO date (local tz) the counter applies to.
      count          — profiles visited so far on `day`.
      enriched_ids   — set of contact entity_ids ever enriched (once-only).
      last_visit_at  — ISO timestamp of the most recent profile visit (pacing).
      path           — backing file path.
    """

    cap: int = DEFAULT_DAILY_CAP
    day: str = field(default_factory=_local_today)
    count: int = 0
    enriched_ids: set[str] = field(default_factory=set)
    last_visit_at: str | None = None
    path: Path = _STATE_FILE

    # ── persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path = _STATE_FILE) -> "EnrichmentState":
        cap_env = os.environ.get("LINKEDIN_ENRICH_DAILY_CAP")
        cap = DEFAULT_DAILY_CAP
        if cap_env:
            try:
                cap = int(cap_env)
            except ValueError:
                log.warning("Bad LINKEDIN_ENRICH_DAILY_CAP=%r, using default", cap_env)
        # The cap is a ceiling, never a way to scrape faster. Clamp hard.
        cap = max(0, min(cap, ABSOLUTE_MAX_CAP))

        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read state (%s); starting fresh", exc)
                raw = {}
        else:
            raw = {}

        st = cls(
            cap=cap,
            day=raw.get("day", _local_today()),
            count=int(raw.get("count", 0)),
            enriched_ids=set(raw.get("enriched_ids", [])),
            last_visit_at=raw.get("last_visit_at"),
            path=path,
        )
        # Roll over the counter if the persisted day is stale. No burst-catchup:
        # rollover always resets to 0 regardless of how many days were skipped.
        st._maybe_roll_day()
        return st

    def save(self) -> None:
        payload = {
            "day": self.day,
            "count": self.count,
            "cap": self.cap,
            "enriched_ids": sorted(self.enriched_ids),
            "last_visit_at": self.last_visit_at,
        }
        try:
            # Atomic write: tmp file in same dir + os.replace.
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tmp_state_")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("Failed to persist state: %s", exc)

    # ── cap logic ────────────────────────────────────────────────────────────

    def _maybe_roll_day(self) -> None:
        today = _local_today()
        if self.day != today:
            log.info(
                "Day rollover %s → %s; resetting count %d → 0 (no burst-catchup)",
                self.day,
                today,
                self.count,
            )
            self.day = today
            self.count = 0

    def remaining_today(self) -> int:
        """How many more visits are allowed today. Always rolls the day first."""
        self._maybe_roll_day()
        return max(0, self.cap - self.count)

    def can_visit(self) -> bool:
        return self.remaining_today() > 0

    def record_visit(self, contact_entity_id: str, *, visited_at: str | None = None) -> None:
        """Mark one profile visit: bump the daily counter, persist the dedupe
        marker, and stamp the pacing timestamp. Must be called exactly once per
        actual LinkedIn profile load, AFTER it succeeds.

        Raises RuntimeError if the cap is already exhausted — callers must check
        can_visit() first; this is a defense-in-depth backstop.
        """
        self._maybe_roll_day()
        if self.count >= self.cap:
            raise RuntimeError(
                f"daily cap {self.cap} already reached for {self.day}; refusing visit"
            )
        self.count += 1
        self.enriched_ids.add(contact_entity_id)
        self.last_visit_at = visited_at or datetime.now(ZoneInfo("UTC")).isoformat()
        self.save()

    def already_enriched(self, contact_entity_id: str) -> bool:
        """True if this contact has been enriched before (enrich-once rule)."""
        return contact_entity_id in self.enriched_ids
