#!/usr/bin/env python3
"""Tests for the load-bearing logic of the linkedin-enrichment daemon.

Covers the guardrails that protect the operator's account and data:
  * hard daily cap + no burst-catchup across day rollover,
  * enrich-once dedupe,
  * notes append never overwrites + same-day idempotency,
  * two-way reciprocity (one-way and non-human excluded),
  * RGPD Art. 9 scrub on recent-activity,
  * fail-safe HARD STOP on LinkedInChallenge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DIR))

import enrich  # noqa: E402
import linkedin_enrichment as daemon  # noqa: E402
import enrichment_queue as q  # noqa: E402
from enrich import ProfileCapture  # noqa: E402
from state import EnrichmentState  # noqa: E402


# ── state / cap ────────────────────────────────────────────────────────────────


def _state(tmp_path, **kw) -> EnrichmentState:
    return EnrichmentState(path=tmp_path / "s.json", **kw)


def test_cap_blocks_after_limit(tmp_path):
    st = _state(tmp_path, cap=2)
    assert st.can_visit()
    st.record_visit("ent_a")
    st.record_visit("ent_b")
    assert not st.can_visit()
    with pytest.raises(RuntimeError):
        st.record_visit("ent_c")


def test_no_burst_catchup_on_rollover(tmp_path):
    st = _state(tmp_path, cap=10, day="2020-01-01", count=7)
    # remaining_today rolls the (stale) day → resets to 0, NOT 10-7+something.
    assert st.remaining_today() == 10
    assert st.count == 0
    assert st.day != "2020-01-01"


def test_cap_clamped_to_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_ENRICH_DAILY_CAP", "500")
    st = EnrichmentState.load(path=tmp_path / "s.json")
    assert st.cap == 10  # ABSOLUTE_MAX_CAP


def test_enriched_dedupe_persists(tmp_path):
    p = tmp_path / "s.json"
    st = EnrichmentState(path=p, cap=5)
    st.record_visit("ent_x")
    again = EnrichmentState.load(path=p)
    assert again.already_enriched("ent_x")
    assert not again.already_enriched("ent_y")


# ── enrich / notes ─────────────────────────────────────────────────────────────


def test_append_notes_preserves_existing():
    existing = "Met at conf 2019.\nIntro candidate for LocalGlobe."
    line = "LinkedIn enrichment 2026-06-30: Head of Ops at Theodóre · Barcelona"
    out = enrich.append_notes(existing, line)
    assert "Met at conf 2019." in out
    assert "Intro candidate for LocalGlobe." in out
    assert out.endswith(line)


def test_append_notes_same_day_replaces():
    existing = "LinkedIn enrichment 2026-06-30: old capture"
    line = "LinkedIn enrichment 2026-06-30: new capture"
    out = enrich.append_notes(existing, line)
    assert out == line  # single line, replaced not duplicated


def test_append_notes_empty_existing():
    line = "LinkedIn enrichment 2026-06-30: x"
    assert enrich.append_notes(None, line) == line
    assert enrich.append_notes("", line) == line


def test_art9_scrub_drops_sensitive():
    assert enrich.scrub_sensitive("posted about retail automation") == "posted about retail automation"
    assert enrich.scrub_sensitive("shared a cancer diagnosis update") is None
    assert enrich.scrub_sensitive("celebrating a new church role") is None
    assert enrich.scrub_sensitive(None) is None


def test_format_note_no_data():
    line = enrich.format_enrichment_note(ProfileCapture(), "2026-06-30")
    assert line == "LinkedIn enrichment 2026-06-30: (no public professional data found)"


def test_build_write_reuses_existing_url():
    w = enrich.build_write(
        contact_entity_id="ent_z",
        existing_notes="prior",
        existing_linkedin_url="https://linkedin.com/in/known",
        capture=ProfileCapture(title="CTO", employer="Acme", linkedin_url="https://linkedin.com/in/other"),
        enriched_on="2026-06-30",
    )
    assert w.linkedin_url == "https://linkedin.com/in/known"  # on-file wins
    assert w.title == "CTO" and w.company == "Acme"
    assert w.notes.startswith("prior")


# ── queue / reciprocity ────────────────────────────────────────────────────────


def test_non_human_excluded():
    # Synthetic addresses on RFC 2606 reserved example domains; the exclusion
    # logic keys on the local-part / domain fragment, not a real mailbox.
    assert q._is_non_human("no-reply@example.com")
    assert q._is_non_human("newsletter@example.org")
    assert q._is_non_human("notifications@example.net")
    assert not q._is_non_human("person@example.com")


def test_match_contacts_skips_one_way(monkeypatch):
    # two-way ranking already excludes one-way upstream; here verify match_contacts
    # skips already-enriched and missing-contact addresses.
    ranked = [("person@example.com", "2026-06-20"), ("ghost@example.org", "2026-06-19")]

    def fake_retrieve(identifier, entity_type, by):
        if identifier == "person@example.com":
            return {"entities": [{"id": "ent_person", "snapshot": {"snapshot": {"name": "Test Person"}}}]}
        return {"entities": []}  # ghost has no Neotoma contact

    items = q.match_contacts(ranked, fake_retrieve, already_enriched=lambda e: False, limit=10)
    assert len(items) == 1
    assert items[0].contact_entity_id == "ent_person"
    assert items[0].name == "Test Person"


def test_match_contacts_respects_already_enriched():
    ranked = [("foo@example.com", "2026-06-20")]
    retrieve = lambda **kw: {"entities": [{"id": "ent_a", "snapshot": {"snapshot": {}}}]}  # noqa: E731
    items = q.match_contacts(ranked, retrieve, already_enriched=lambda e: e == "ent_a", limit=10)
    assert items == []


# ── fail-safe ──────────────────────────────────────────────────────────────────


def test_challenge_hard_stops_run(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.EnrichmentState, "load", classmethod(lambda cls: _state(tmp_path, cap=10)))
    monkeypatch.setattr(
        daemon,
        "build_two_way_address_ranking",
        lambda scan_window=None: [("foo@example.com", "2026-06-20"), ("bar@example.com", "2026-06-19")],
    )
    monkeypatch.setattr(
        daemon,
        "match_contacts",
        lambda *a, **k: [
            q.QueueItem("ent_a", "foo@example.com", "A", None, "2026-06-20"),
            q.QueueItem("ent_b", "bar@example.com", "B", None, "2026-06-19"),
        ],
    )

    notes = []

    def scrape(item):
        raise daemon.LinkedInChallenge("captcha")

    summary = daemon.run_once(
        scrape=scrape,
        retrieve_by_identifier=lambda **kw: {"entities": []},
        apply_write=lambda w: True,
        notify=notes.append,
    )
    assert summary["stopped_reason"] == "linkedin_challenge"
    assert summary["visited"] == 0  # stopped before any visit counted
    assert notes and "STOPPED" in notes[0]


def test_run_respects_cap_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(
        daemon.EnrichmentState, "load", classmethod(lambda cls: _state(tmp_path, cap=10, count=10))
    )
    summary = daemon.run_once(scrape=lambda i: ProfileCapture())
    assert summary["stopped_reason"] == "cap_reached"
    assert summary["visited"] == 0
