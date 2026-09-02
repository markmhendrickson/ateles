"""
Unit tests for Apis domain routing — both the task-text inference used by the
SSE/A2A dispatch path and the PR-path inference used by Loxia per-domain review.

Run with: pytest execution/daemons/apis/test_routing.py -v
"""

from __future__ import annotations

from routing import (
    canonical_assignee,
    infer_domains_from_paths,
    infer_tags_from_text,
    resolve_reviewers,
    resolve_skill,
)

# ── Task-text routing (existing behavior — regression guard) ──────────────────


def test_assigned_to_wins_over_tags() -> None:
    assert resolve_skill(["health"], assigned_to="monedula") == "monedula"


def test_tag_fallback_when_assignee_unset() -> None:
    assert resolve_skill(["health"]) == "gorilla"


def test_apis_self_assignment_falls_back_to_tags() -> None:
    assert resolve_skill(["finance"], assigned_to="apis") == "monedula"


def test_text_inference_picks_finance() -> None:
    assert "finance" in infer_tags_from_text("Pay the rent invoice")


# ── PR-path → domain inference (new — Loxia per-domain routing) ───────────────


def test_finance_path_routes_to_monedula() -> None:
    paths = ["execution/daemons/monedula/handlers/wise_transfer.py"]
    assert infer_domains_from_paths(paths) == ["finance"]
    assert resolve_reviewers(paths) == ["monedula"]


def test_payment_keyword_in_path_routes_to_finance() -> None:
    assert resolve_reviewers(["lib/payment_profile.py"]) == ["monedula"]


def test_health_path_routes_to_gorilla() -> None:
    assert resolve_reviewers(["execution/daemons/gorilla/workout.py"]) == ["gorilla"]


def test_non_domain_path_has_no_specialist_reviewer() -> None:
    # Generalist/baseline-only paths must NOT pull in a domain reviewer; Loxia
    # covers them. resolve_reviewers returns [] (caller still runs Loxia).
    assert resolve_reviewers([".github/workflows/loxia-pr-review.yml"]) == []
    assert resolve_reviewers(["docs/pr_review_routing.md"]) == []


def test_multiple_domains_deduplicated_and_order_stable() -> None:
    paths = [
        "execution/daemons/monedula/monedula.py",  # finance
        "execution/daemons/gorilla/workout.py",  # health
        "lib/invoice_writer.py",  # finance again
    ]
    assert infer_domains_from_paths(paths) == ["finance", "health"]
    assert resolve_reviewers(paths) == ["monedula", "gorilla"]


def test_empty_changeset_returns_no_reviewers() -> None:
    assert resolve_reviewers([]) == []


# ── Agent-metadata path exclusion (PR #107 regression guard) ─────────────────
# docs/agents/<agent>.md and .claude/skills/<agent>/SKILL.md are generated
# mirrors/documentation — they carry no domain-code signal. A PR that only
# touches these files must not summon domain specialists; Loxia covers it.


def test_agent_doc_paths_do_not_summon_specialist() -> None:
    """Regression for PR #107 — mass agent-doc regeneration must not invite
    Monedula, Gorilla, or Fringilla as reviewers."""
    paths = [
        "docs/agents/monedula.md",
        "docs/agents/gorilla.md",
        "docs/agents/fringilla.md",
    ]
    assert infer_domains_from_paths(paths) == []
    assert resolve_reviewers(paths) == []


def test_skill_mirror_paths_do_not_summon_specialist() -> None:
    assert resolve_reviewers([".claude/skills/lanius/SKILL.md"]) == []


def test_mixed_agent_doc_and_real_code_selects_specialist() -> None:
    """When a PR touches both a doc mirror AND real domain code, the specialist
    IS selected — because of the code file, not the doc file."""
    paths = [
        "docs/agents/monedula.md",  # agent metadata — must be ignored
        "lib/payment_profile.py",  # real finance code — must select monedula
    ]
    assert resolve_reviewers(paths) == ["monedula"]


# ── #597: routing coverage for the 12 unreachable T4 agents ──────────────────
#
# Every one of these agents is fully built (complete prompt_markdown + rendered
# .claude/skills/<name>/SKILL.md mirror, loadable by skill_runner) and was
# unreachable purely because no tag named it. See ateles#597.
#
# NOTE: these routes are INERT until Apis's task-event subscription is restored
# (ateles#589) — a merged routing table does not by itself make dispatch work.

import pytest  # noqa: E402

from routing import ASSIGNED_TO_ROUTES, DOMAIN_ROUTES  # noqa: E402

# The 13 work descriptions that resolved to NO owner (or the WRONG owner) on
# main, each with the agent that owns that role on the swarm roster.
PREVIOUSLY_UNOWNED: list[tuple[str, str]] = [
    ("Prepare tax return materials for 2025", "picus"),
    ("Draft a blog post about memory systems", "corvus"),
    ("Rewrite the headline and positioning on the landing page", "manucode"),
    ("Choose a brand colour palette", "aythya"),
    ("Plan the launch campaign for the new product", "ciconia"),
    ("Audit the README and onboarding path for new developers", "regulus"),
    ("Synthesize ICP from customer interviews", "hirundo"),
    ("Cut the v0.22 release", "struthio"),
    ("Run a session audit for compliance last week", "robin"),
    ("Escalate a constitution policy question", "columba"),
    ("Book a dentist appointment", "nucifraga"),
    ("Deliver the anchor customer milestone", "ploceus"),
    ("Update the CRM contact records", "sturnus"),
]


@pytest.mark.parametrize("text,expected", PREVIOUSLY_UNOWNED)
def test_previously_unowned_work_now_resolves(text: str, expected: str) -> None:
    tags = infer_tags_from_text(text)
    assert resolve_skill(tags) == expected, f"{text!r} -> {tags}"


def test_all_twelve_new_tags_are_routed() -> None:
    """Every tag #597 asked for exists and names its roster owner."""
    expected = {
        "tax": "picus",
        "content": "corvus",
        "copy": "manucode",
        "design": "aythya",
        "gtm": "ciconia",
        "devrel": "regulus",
        "customer_intel": "hirundo",
        "release": "struthio",
        "compliance": "robin",
        "policy": "columba",
        "personal": "nucifraga",
        "anchor_delivery": "ploceus",
        "crm": "sturnus",
    }
    for tag, owner in expected.items():
        assert DOMAIN_ROUTES.get(tag) == owner, tag


def test_every_domain_owner_is_addressable_by_name() -> None:
    """An agent that owns a domain must also be reachable via assigned_to.

    This is the drift guard: a tag added to DOMAIN_ROUTES without a matching
    ASSIGNED_TO_ROUTES entry would leave the owner reachable by inference but
    not by an explicit assignment from Sylvia/Turdus.
    """
    for tag, skill in DOMAIN_ROUTES.items():
        assert ASSIGNED_TO_ROUTES.get(skill) == skill, f"{tag} -> {skill}"
        assert resolve_skill([], assigned_to=skill) == skill


def test_specialist_tags_precede_generalist_tags() -> None:
    """Specialist domains must sort ahead of the cicada-owned generalists.

    resolve_skill takes the FIRST matching tag, so a specialist declared after
    `product`/`comms`/`agents` would be swallowed by the generalist.
    """
    order = list(DOMAIN_ROUTES)
    generalists = [t for t in order if DOMAIN_ROUTES[t] == "cicada"]
    specialists = [t for t in order if DOMAIN_ROUTES[t] != "cicada"]
    assert max(order.index(t) for t in specialists) < min(
        order.index(t) for t in generalists
    )


# ── Existing routing must not regress ────────────────────────────────────────

EXISTING_ROUTES: list[tuple[str, str]] = [
    ("Fix the deploy pipeline bug", "cicada"),
    ("Pay the rent invoice", "monedula"),
    ("Transfer this month's wage", "monedula"),
    ("Log my workout at the gym", "gorilla"),
    ("Quarterly review of fixed costs", "fringilla"),
    ("Reconcile the portfolio statements", "fringilla"),
    ("Free up disk space on the host", "cicada"),
    ("Add an entity schema migration to neotoma", "cicada"),
    ("Draft a reply email to the landlord", "cicada"),
]


@pytest.mark.parametrize("text,expected", EXISTING_ROUTES)
def test_existing_routes_do_not_regress(text: str, expected: str) -> None:
    tags = infer_tags_from_text(text)
    assert resolve_skill(tags) == expected, f"{text!r} -> {tags}"


# ── Over-broad patterns: what each tag must NOT claim ────────────────────────
#
# A tag that over-matches is a NEW silent misroute. `\baudit\b` routing "Audit
# the README" to fringilla (the financial analyst) is the defect #597 named:
# it reads as covered and would produce a confident answer from the wrong
# specialist. Every entry below is a description that must NOT carry the tag.

MUST_NOT_MATCH: list[tuple[str, str]] = [
    # the #597 headline defect
    ("Audit the README and onboarding path for new developers", "finance_analysis"),
    ("Security audit of the auth flow", "finance_analysis"),
    ("Audit accessibility of the dashboard", "finance_analysis"),
    ("Run a session audit for compliance last week", "finance_analysis"),
    ("Audit the routing test coverage", "finance_analysis"),
    # finance vs ordinary English
    ("Pay attention to the flaky test", "finance"),
    ("Update the payload parser", "finance"),
    # agents vs human intermediaries
    ("Email our insurance agent about the claim", "agents"),
    ("Talk to the real estate agent", "agents"),
    # health vs docs / ML
    ("Write training documentation for new hires", "health"),
    ("Model training pipeline is slow", "health"),
    # the second #597 misroute
    ("Install certificate for Social Security", "comms"),
    # new tags must not over-claim
    ("Copy the config file to the new host", "copy"),
    ("Design the database schema", "design"),
    ("Design the retry backoff algorithm", "design"),
    ("Release the deposit back to the tenant", "release"),
    ("Release the hold on the booking", "release"),
    ("Update the job post description", "content"),
    ("Fix the postgres connection", "content"),
    ("Fix the tax calculation rounding bug in the invoice module", "personal"),
    ("Set the retention policy on the log bucket", "policy"),
    ("Plan the office migration", "neotoma"),
    ("Build a relationship with the anchor customer", "ops"),
]


@pytest.mark.parametrize("text,forbidden", MUST_NOT_MATCH)
def test_tags_do_not_overmatch(text: str, forbidden: str) -> None:
    tags = infer_tags_from_text(text)
    assert forbidden not in tags, f"{text!r} over-matched {forbidden}: {tags}"


# ── The body argument (ateles#607 qa lens) ──────────────────────────────────
#
# Both production callers pass a BODY — apis.py `_infer_tags_from_text(title,
# body)` and a2a_executor.py `infer_tags_from_text(title, body)` — while every
# corpus above passes a title alone. That gap hid a guard wide enough to fire on
# an incidental body word:
#
#   ("Reconcile the portfolio statements", "Numbers are in the shared docs
#    folder.")                                  -> [] (route lost entirely)
#   ("Reconcile the Q3 portfolio", "Cross-check against the invoice test
#    data.")                                    -> ['finance'] -> MONEDULA
#
# The second is the worse half: a reconciliation task reaching the payment
# EXECUTOR — a brand-new silent misroute created by the fix for silent
# misroutes. Guards are now bound to the ambiguous word they qualify, and these
# cases pin that a body cannot steal or destroy a route the title earned.

BODY_MUST_NOT_CHANGE_ROUTE: list[tuple[str, str, str]] = [
    (
        "Reconcile the portfolio statements",
        "Numbers are in the shared docs folder.",
        "fringilla",
    ),
    (
        "Reconcile the Q3 portfolio",
        "Cross-check against the invoice test data.",
        "fringilla",
    ),
    (
        "Quarterly review of fixed costs",
        "See the onboarding docs and the accessibility notes for context.",
        "fringilla",
    ),
    (
        "Pay the rent invoice",
        "The tests for this live in the docs folder.",
        "monedula",
    ),
]


@pytest.mark.parametrize("title,body,expected", BODY_MUST_NOT_CHANGE_ROUTE)
def test_body_context_does_not_steal_the_title_route(
    title: str, body: str, expected: str
) -> None:
    """An incidental word in the body must not suppress the title's domain."""
    tags = infer_tags_from_text(title, body)
    assert resolve_skill(tags) == expected, f"{title!r} + {body!r} -> {tags}"


@pytest.mark.parametrize("text,expected", EXISTING_ROUTES + PREVIOUSLY_UNOWNED)
def test_routes_hold_when_the_body_carries_the_text(text: str, expected: str) -> None:
    """The same corpus through the two-argument production signature.

    Every caller passes a body; a corpus that only ever passes a title cannot
    catch a defect in how the two are combined.
    """
    assert resolve_skill(infer_tags_from_text(text, "")) == expected
    assert resolve_skill(infer_tags_from_text("", text)) == expected


@pytest.mark.parametrize("text,forbidden", MUST_NOT_MATCH)
def test_over_broad_guards_hold_from_the_body_too(text: str, forbidden: str) -> None:
    """A guard that only works on titles is not a guard."""
    assert forbidden not in infer_tags_from_text("", text), text


def test_unmatched_task_surfaces_as_unowned() -> None:
    """No route is better than a wrong route.

    resolve_skill returns None rather than falling through to a loose match, and
    apis.dispatch_task turns that None into BLOCKED + a BLOCKER page — visibly
    unowned. A misrouted task looks handled and never surfaces.
    """
    for text in (
        "Water the plants on the terrace",
        "Ponder the meaning of the universe",
    ):
        assert resolve_skill(infer_tags_from_text(text)) is None, text


# ── assigned_to canonicalization (ateles#682) ────────────────────────────────
#
# Stored tasks carry the same owner written several ways. These lock the
# reduction to the one form ASSIGNED_TO_ROUTES is keyed by, and — more
# importantly — lock the two behaviours a wrong reduction would silently break:
# an AAuth-suffixed owner must still route, and a sentinel must read as absence.


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Absence, spelled every way it appears in stored data.
        (None, None),
        ("", None),
        ("   ", None),
        ("unassigned", None),
        ("Unassigned", None),
        ("N/A", None),
        # Mechanical variation on a real owner.
        ("cicada", "cicada"),
        ("Cicada", "cicada"),
        ("  CICADA  ", "cicada"),
        # AAuth subject forms — the local part IS the agent name.
        ("corvus@ateles-swarm", "corvus"),
        ("corvus@ateles", "corvus"),
        ("Corvus@Ateles-Swarm", "corvus"),
        # Not an agent: reduced in form, never guessed at or dropped.
        ("Mark", "mark"),
        ("neotoma coding agent", "neotoma coding agent"),
    ],
)
def test_canonical_assignee_reduces_form_without_inventing_owners(
    raw: str | None, expected: str | None
) -> None:
    assert canonical_assignee(raw) == expected


@pytest.mark.parametrize(
    "raw", ["corvus@ateles-swarm", "corvus@ateles", "Corvus", " corvus "]
)
def test_aauth_and_case_variants_route_like_the_bare_name(raw: str) -> None:
    """Every spelling of an owner must reach that owner's skill.

    Before canonicalization `corvus@ateles-swarm` resolved to None and fell
    through to tag inference — an owner the creating agent had explicitly named
    was silently ignored.
    """
    assert resolve_skill([], assigned_to=raw) == resolve_skill([], assigned_to="corvus")


def test_sentinel_assignee_is_absence_not_an_owner() -> None:
    """"unassigned" must behave exactly like an empty field.

    It is worse than absence when treated as a value: it is truthy, so it
    satisfies a `bool(assigned_to)` owner check while naming nobody. Tag
    inference must still get its turn, exactly as it does for an empty field.
    """
    assert resolve_skill([], assigned_to="unassigned") is None
    assert resolve_skill(["finance"], assigned_to="unassigned") == resolve_skill(
        ["finance"], assigned_to=None
    )


def test_unknown_assignee_still_falls_through_to_tags() -> None:
    """A human name is not an agent — it must not block tag inference."""
    assert resolve_skill(["finance"], assigned_to="Mark") == resolve_skill(
        ["finance"], assigned_to=None
    )
