"""Tests for the review → skill learning loop (ateles#82)."""

from review_learning import classify_finding, parse_findings, propose_skill_updates

SAMPLE_REVIEW = """\
Review through the arch lens.

[BLOCKING] tenant-isolation: entity lookup in `server/tools/fetch.py` is not \
scoped to the authenticated user
This violates change_guardrails_rules.mdc — every entity lookup MUST be \
owner-scoped before acting.

[BLOCKING] idempotency: mutating op lacks idempotency_key
The new store call in `server/tools/fetch.py` retries unsafely. This is \
specific to the retry wrapper introduced here.

[NON-BLOCKING] naming: consider `fetch_workflow` over `get_wf`
Minor readability point.
"""


def test_parse_extracts_all_findings():
    findings = parse_findings(SAMPLE_REVIEW, lens="arch")
    assert len(findings) == 3
    assert [f.blocking for f in findings] == [True, True, False]
    assert findings[0].category == "tenant-isolation"
    assert findings[0].lens == "arch"
    assert "server/tools/fetch.py" in findings[0].files


def test_rule_citation_marks_systemic():
    findings = parse_findings(SAMPLE_REVIEW)
    assert findings[0].rule_citations  # cites change_guardrails .mdc
    assert classify_finding(findings[0]) == "systemic"


def test_no_citation_is_one_off():
    findings = parse_findings(SAMPLE_REVIEW)
    assert not findings[1].rule_citations
    assert classify_finding(findings[1]) == "one_off"


def test_recurrence_promotes_to_systemic():
    findings = parse_findings(SAMPLE_REVIEW)
    assert classify_finding(findings[1], category_recurrence=2) == "systemic"


def test_proposals_only_for_systemic_blocking_findings():
    proposals = propose_skill_updates([("arch", SAMPLE_REVIEW)], pr_ref="o/r#12")
    assert len(proposals) == 1
    p = proposals[0]
    assert p["entity_type"] == "proposed_skill_update"
    assert p["finding_category"] == "tenant-isolation"
    assert p["owning_agent"] == "gryllus"
    assert p["status"] == "proposed"
    assert p["approval_required"] is True
    assert p["source_pr"] == "o/r#12"


def test_content_lens_findings_route_to_corvus():
    review = "[BLOCKING] missing-content-angle: per the advisory, ship a story\ndetail"
    proposals = propose_skill_updates([("content", review)], pr_ref="o/r#13")
    assert len(proposals) == 1
    assert proposals[0]["owning_agent"] == "corvus"


def test_duplicate_categories_dedupe_across_lenses():
    proposals = propose_skill_updates(
        [("arch", SAMPLE_REVIEW), ("qa", SAMPLE_REVIEW)], pr_ref="o/r#12"
    )
    assert len(proposals) == 1


def test_category_history_threshold():
    # Two prior occurrences of "idempotency" promote the uncited finding too.
    proposals = propose_skill_updates(
        [("arch", SAMPLE_REVIEW)],
        pr_ref="o/r#12",
        category_history={"idempotency": 2},
    )
    cats = {p["finding_category"] for p in proposals}
    assert cats == {"tenant-isolation", "idempotency"}


def test_non_blocking_never_proposes():
    review = "[NON-BLOCKING] naming: per change_guardrails, prefer snake_case\nd"
    assert propose_skill_updates([("arch", review)], pr_ref="o/r#12") == []
