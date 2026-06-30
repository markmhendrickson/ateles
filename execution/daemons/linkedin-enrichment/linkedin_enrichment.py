#!/usr/bin/env python3
"""
linkedin-enrichment — Ateles standing LinkedIn contact-enrichment daemon.

Task: ent_3757c3fa27e81711778a6565. Keeps intro-relevant contacts' LinkedIn data
current, because Neotoma contact records are stale proxies (2010–14 titles) while
live LinkedIn is the ground truth intros turn on.

PIPELINE (one enrichment unit):
  1. queue.build_two_way_address_ranking() — Gmail two-way correspondents,
     ranked by most-recent reciprocal interaction (operator sent AND received).
  2. queue.match_contacts() — map to Neotoma `contact` entities, skip already-
     enriched (enrich-once), take up to the day's remaining cap.
  3. For each contact, IF state.can_visit():
       a. SCRAPE SEAM — the agent layer (Chrome MCP) loads the profile and
          returns a ProfileCapture. The daemon itself NEVER calls Chrome; a
          standalone process cannot reach mcp__Claude_in_Chrome__*. See
          `scrape_seam` below for the contract + fail-safe protocol.
       b. state.record_visit() — bump the hard daily counter + dedupe marker.
       c. enrich.build_write() + apply via Neotoma correct() — append a dated
          line to `notes` (never overwrite), refresh title/company/location.
  4. SILENT — no notifications on success. Blockers (challenge / cap / auth) do
     notify, because they need the operator.

LOAD-BEARING GUARDRAILS:
  * HARD 10/day cap (state.py), persisted, no burst-catchup.
  * FAIL-SAFE: on ANY LinkedIn challenge / checkpoint / captcha / unusual-
    activity prompt the scrape seam MUST raise LinkedInChallenge; the daemon
    STOPS the run, surfaces a blocker, and never auto-solves. The operator's
    PERSONAL account is at risk.
  * RGPD-minimized capture (enrich.py); internal-only; no external publication.
  * Operator-agnostic: operator email, timezone, cap all read from env.

AUTONOMY: checkpoint-gated. This module exposes `run_once(scrape)` for both the
supervised first run (real Chrome scrape passed in by the agent) and a
dry-run / test mode (stub scrape). It does NOT auto-start scraping on import or
as a launchd loop until the operator validates the first run.

Environment variables:
  NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN  Neotoma API
  GWS_OPERATOR_EMAIL                       operator's Gmail address (queue)
  LINKEDIN_ENRICH_TZ                       local tz for day rollover (Europe/Madrid)
  LINKEDIN_ENRICH_DAILY_CAP                hard cap (clamped ≤ 10)
  LINKEDIN_ENRICH_SCAN_WINDOW              Gmail date filter (default newer_than:3y)
  LINKEDIN_ENRICH_DRY_RUN                  "1" → preview writes, persist nothing
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

from enrich import EnrichmentWrite, ProfileCapture, build_write  # noqa: E402
from enrichment_queue import QueueItem, build_two_way_address_ranking, match_contacts  # noqa: E402
from state import EnrichmentState  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("linkedin-enrichment")

DAEMON_NAME = "linkedin-enrichment"
DRY_RUN = os.environ.get("LINKEDIN_ENRICH_DRY_RUN", "0") == "1"
SCAN_WINDOW = os.environ.get("LINKEDIN_ENRICH_SCAN_WINDOW", "newer_than:3y")


# ── Fail-safe signal ───────────────────────────────────────────────────────────


class LinkedInChallenge(Exception):
    """Raised by the scrape seam when LinkedIn shows ANY challenge / checkpoint /
    captcha / unusual-activity prompt. The daemon treats this as a HARD STOP for
    the whole run — never retried, never auto-solved."""


# ── Scrape seam ────────────────────────────────────────────────────────────────


class ScrapeFn(Protocol):
    """The contract the agent (Chrome MCP) layer must satisfy.

    Given a QueueItem, return a ProfileCapture (title / employer / location /
    recent_activity / resolved linkedin_url). MUST raise LinkedInChallenge on any
    LinkedIn anti-automation prompt — see fail-safe guardrail. MAY return an
    all-None ProfileCapture if the profile loaded cleanly but exposed no public
    professional data (that still counts as a visit and enriches a 'no data'
    note, so the contact is not re-queued forever)."""

    def __call__(self, item: QueueItem) -> ProfileCapture: ...


def stub_scrape(item: QueueItem) -> ProfileCapture:
    """Dry-run / test scrape: performs NO network access. Returns empty capture.
    The real scrape is injected by the supervised agent layer."""
    log.info("[dry-run] would scrape %s <%s>", item.name, item.email)
    return ProfileCapture()


# ── Neotoma adapters (injected; transport-agnostic) ────────────────────────────


def _http_retrieve_by_identifier(identifier: str, entity_type: str, by: str) -> dict:
    """Look up a contact by email via the Neotoma HTTP API. Used when the daemon
    runs standalone. In an MCP session the agent passes its own retrieve fn."""
    import httpx

    base = os.environ.get("NEOTOMA_BASE_URL", "").rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not base:
        return {"entities": []}
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.post(
            f"{base}/retrieve_entity_by_identifier",
            headers=headers,
            json={"identifier": identifier, "entity_type": entity_type, "by": by},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("retrieve_by_identifier(%s) failed: %s", identifier, exc)
        return {"entities": []}


def _http_apply_write(write: EnrichmentWrite) -> bool:
    """Apply an EnrichmentWrite via Neotoma correct() over HTTP. One correction
    per non-empty field. Returns True if all corrections succeeded."""
    import httpx

    base = os.environ.get("NEOTOMA_BASE_URL", "").rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not base:
        log.warning("No NEOTOMA_BASE_URL — cannot apply write for %s", write.contact_entity_id)
        return False
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    today = date.today().isoformat()
    fields = {
        "notes": write.notes,
        "title": write.title,
        "company": write.company,
        "location": write.location,
        "linkedin_url": write.linkedin_url,
    }
    ok = True
    for field_name, value in fields.items():
        if value is None:
            continue
        key = f"linkedin-enrich-{write.contact_entity_id}-{field_name}-{today}"
        try:
            resp = httpx.post(
                f"{base}/correct",
                headers=headers,
                json={
                    "entity_id": write.contact_entity_id,
                    "entity_type": "contact",
                    "field": field_name,
                    "value": value,
                    "idempotency_key": key,
                },
                timeout=20,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("correct(%s.%s) failed: %s", write.contact_entity_id, field_name, exc)
            ok = False
    return ok


# ── Core run ───────────────────────────────────────────────────────────────────


def run_once(
    scrape: ScrapeFn | None = None,
    *,
    retrieve_by_identifier: Callable = _http_retrieve_by_identifier,
    apply_write: Callable[[EnrichmentWrite], bool] = _http_apply_write,
    notify: Callable[[str], None] | None = None,
    max_items: int | None = None,
) -> dict:
    """Run one enrichment pass, bounded by the daily cap.

    scrape: the Chrome-MCP-backed scrape fn (supervised). Defaults to stub_scrape
            (no network) so an accidental run never touches LinkedIn.
    Returns a summary dict {visited, written, remaining, stopped_reason}.
    """
    scrape = scrape or stub_scrape
    state = EnrichmentState.load()
    remaining = state.remaining_today()
    if max_items is not None:
        remaining = min(remaining, max_items)

    summary = {"visited": 0, "written": 0, "remaining_after": state.remaining_today(), "stopped_reason": None}

    if remaining <= 0:
        log.info("Daily cap reached (%d/%d for %s) — nothing to do", state.count, state.cap, state.day)
        summary["stopped_reason"] = "cap_reached"
        return summary

    ranked = build_two_way_address_ranking(scan_window=SCAN_WINDOW)
    if not ranked:
        summary["stopped_reason"] = "empty_queue"
        return summary

    items = match_contacts(
        ranked,
        retrieve_by_identifier,
        already_enriched=state.already_enriched,
        limit=remaining,
    )
    if not items:
        summary["stopped_reason"] = "no_enrichable_contacts"
        return summary

    for item in items:
        if not state.can_visit():
            summary["stopped_reason"] = "cap_reached"
            break
        try:
            capture = scrape(item)
        except LinkedInChallenge as exc:
            msg = (
                f"{DAEMON_NAME}: STOPPED — LinkedIn challenge/checkpoint hit while "
                f"enriching {item.name}. Not pushed through, not auto-solved. "
                f"Operator action needed. ({exc})"
            )
            log.warning(msg)
            if notify:
                notify(msg)
            summary["stopped_reason"] = "linkedin_challenge"
            break  # HARD STOP for the whole run — fail-safe.

        # A successful profile load — count it against the cap even if empty.
        state.record_visit(item.contact_entity_id)
        summary["visited"] += 1

        write = build_write(
            contact_entity_id=item.contact_entity_id,
            existing_notes=_existing_notes(item, retrieve_by_identifier),
            existing_linkedin_url=item.linkedin_url,
            capture=capture,
            enriched_on=date.today().isoformat(),
        )

        if DRY_RUN:
            log.info("[dry-run] would write notes for %s:\n  %s", item.name, write.notes.splitlines()[-1])
        else:
            if apply_write(write):
                summary["written"] += 1
            else:
                log.warning("write-back failed for %s", item.name)

    summary["remaining_after"] = state.remaining_today()
    log.info("Run complete: %s", summary)
    return summary


def _existing_notes(item: QueueItem, retrieve_by_identifier: Callable) -> str | None:
    """Re-fetch the contact's current notes immediately before append, so we
    never clobber notes another writer added between queue-build and write."""
    try:
        res = retrieve_by_identifier(identifier=item.email, entity_type="contact", by="email")
        ents = (res or {}).get("entities", [])
        if ents:
            snap = (ents[0].get("snapshot", {}) or {}).get("snapshot", {}) or ents[0].get("snapshot", {})
            return snap.get("notes")
    except Exception:
        pass
    return None


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="LinkedIn contact-enrichment daemon")
    parser.add_argument(
        "--preview-queue",
        action="store_true",
        help="Build and print the enrichment queue without scraping or writing.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Cap items this run (still bounded by the hard daily cap).",
    )
    args = parser.parse_args()

    if args.preview_queue:
        state = EnrichmentState.load()
        log.info("Cap state: %d/%d used for %s", state.count, state.cap, state.day)
        ranked = build_two_way_address_ranking(scan_window=SCAN_WINDOW)
        items = match_contacts(
            ranked,
            _http_retrieve_by_identifier,
            already_enriched=state.already_enriched,
            limit=state.remaining_today() if args.max is None else min(state.remaining_today(), args.max),
        )
        print(f"\nEnrichment queue ({len(items)} contacts, most-recent reciprocal first):")
        for i, it in enumerate(items, 1):
            url = it.linkedin_url or "(search needed)"
            print(f"  {i}. {it.name} <{it.email}>  last touch {it.rank_date}  {url}")
        return 0

    # Default run uses the stub scrape — never touches LinkedIn. The supervised
    # first run is driven by the agent layer calling run_once(real_scrape).
    log.warning(
        "Running with STUB scrape (no Chrome / no LinkedIn). For the supervised "
        "first run, drive run_once() from the agent layer with a Chrome-MCP scrape fn."
    )
    summary = run_once(max_items=args.max)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
