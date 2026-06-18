"""
Unit tests for Apis domain routing — both the task-text inference used by the
SSE/A2A dispatch path and the PR-path inference used by Loxia per-domain review.

Run with: pytest execution/daemons/apis/test_routing.py -v
"""

from __future__ import annotations

from routing import (
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
        "docs/agents/monedula.md",   # agent metadata — must be ignored
        "lib/payment_profile.py",     # real finance code — must select monedula
    ]
    assert resolve_reviewers(paths) == ["monedula"]
