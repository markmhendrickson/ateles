"""
A PR cleared on content but held for an absent lens must not sit forever.

## The failure

neotoma#2153, 2026-08-19. The panel reviewed the current head: pm APPROVE,
arch SIGNED_OFF, ux COMMENT, **Blocking: 0**. Vanellus withheld merge for one
reason — the security lens produced no verdict that round:

    security: NOT RECEIVED — this lens was declared as having reviewed but no
    verdict text or PR comment from a security panelist was found. security_gates
    CI is green, but that is an automated gate, not the security lens's judgment
    call, and does not substitute for it.

That hold is correct and must stay. The defect is that NOTHING re-runs the
absent lens, and every existing recovery path misses this state:

* `lenses_missing_comments` reposts a verdict that WAS captured but whose
  comment failed to post — it reads `captured[lens]`, so it cannot help when
  the lens produced nothing.
* `resume_stalled_reviews` (#415) requires ZERO reviews; its docstring treats a
  REQUEST_CHANGES as a working pipeline. #2153 had four reviews.
* `resume_deferred_reviews` keys on a `review-deferred-until:` marker the
  missing lens never posted.
* An author push is the only remaining trigger — and there is nothing to push,
  because the code is already clear.

So the PR needs a human to notice. Same shape as #414/#416: the recovery path
keys on an artifact the failure prevented from existing. Here it is the lens
verdict itself.

Run: pytest execution/daemons/apis/test_missing_lens_review_recovery.py -v
"""

from __future__ import annotations

import swarm_dispatch as sd


# ---------------------------------------------------------------------------
# Pure parsers — the detection half
# ---------------------------------------------------------------------------

REAL_2153 = """<!-- vanellus-aggregation -->
**COMMENT**

- **pm (Pavo):** **APPROVE** — no blocking findings.
- **arch (Waxwing):** **SIGNED_OFF** — no blocking structural findings.
- **ux (Accipiter):** **COMMENT** — no blocking UX defects.
- **security:** **NOT RECEIVED** — this lens was declared as having reviewed but
  no verdict text or PR comment from a security panelist was found.

**Blocking: 0** confirmed findings from pm/arch/ux. However, merge is withheld
because the security lens verdict is missing for this round.
"""


def test_detects_the_real_2153_hold():
    assert sd.parse_blocking_count(REAL_2153) == 0
    assert sd.parse_not_received_lenses(REAL_2153) == ["security"]
    assert sd.is_missing_lens_candidate(REAL_2153) is True


def test_blocking_findings_are_not_a_missing_lens_candidate():
    """A real blocker is the blocker's problem — re-running a lens won't clear it."""
    body = "**security:** NOT RECEIVED\n**Blocking: 2** findings from arch."
    assert sd.is_missing_lens_candidate(body) is False


def test_a_fully_reported_clean_panel_is_not_a_candidate():
    body = "All four lenses reported.\n**Blocking: 0**"
    assert sd.parse_not_received_lenses(body) == []
    assert sd.is_missing_lens_candidate(body) is False


def test_missing_blocking_count_is_not_a_candidate():
    """No `Blocking:` line means the aggregation shape is unknown — do not guess."""
    body = "**security:** NOT RECEIVED"
    assert sd.parse_blocking_count(body) is None
    assert sd.is_missing_lens_candidate(body) is False


def test_multiple_absent_lenses_are_all_returned_in_order():
    body = "**qa:** NOT RECEIVED\n**security:** NOT RECEIVED\n**Blocking: 0**"
    assert sd.parse_not_received_lenses(body) == ["qa", "security"]


def test_deduplicates_a_lens_named_twice():
    body = (
        "**security:** NOT RECEIVED\n"
        "the security lens verdict is missing for this round\n"
        "**Blocking: 0**"
    )
    assert sd.parse_not_received_lenses(body) == ["security"]


def test_prose_form_alone_is_enough():
    """Vanellus does not always use the structured form."""
    body = "Blocking: 0. Merge withheld — the security lens verdict is missing."
    assert sd.parse_not_received_lenses(body) == ["security"]
    assert sd.is_missing_lens_candidate(body) is True


def test_stopwords_are_not_mistaken_for_lens_names():
    """`no lens is missing` must not yield a lens called `no`."""
    body = "Blocking: 0. Every declared lens reported; no lens is missing."
    assert sd.parse_not_received_lenses(body) == []


def test_ci_green_is_never_treated_as_a_verdict():
    """The invariant the hold exists to protect.

    An aggregation that names CI as green while a lens is absent is STILL a
    candidate — the automated gate must never stand in for the lens.
    """
    body = (
        "**security:** NOT RECEIVED. `security_gates` CI check is green "
        "(SUCCESS), but that is an automated gate, not the lens's judgment.\n"
        "**Blocking: 0**"
    )
    assert sd.is_missing_lens_candidate(body) is True


def test_an_unknown_lens_name_is_never_returned():
    """The parser must not invent a lens the panel cannot dispatch.

    A fabricated name would send the sweep looking for an agent that does not
    exist — and the whole point of this recovery path is to re-run a REAL lens.
    Bounding to the known set means a malformed aggregation yields nothing
    rather than a plausible-looking wrong answer.
    """
    body = "**wombat:** NOT RECEIVED\n**Blocking: 0**"
    assert sd.parse_not_received_lenses(body) == []
    assert sd.is_missing_lens_candidate(body) is False


def test_every_known_lens_name_parses():
    """Guards the bound itself: if a lens is added to the panel and not here,
    its absence becomes undetectable — the failure this file exists to catch."""
    for lens in ("pm", "ux", "arch", "qa", "security", "legal"):
        body = f"**{lens}:** NOT RECEIVED\n**Blocking: 0**"
        assert sd.parse_not_received_lenses(body) == [lens], lens
