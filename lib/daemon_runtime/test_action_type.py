"""
Tests for content-based action_type inference (ateles#682).

The regression these lock down: before this module, blast radius was inferred
from WHICH AGENT would handle a task. Cicada owns six of nine routing domains,
so every engineering-flavoured task — including tasks whose only output was a
written report — inherited "open_or_merge_pr" and scored HIGH.

The fixtures below are drawn from the 27 tasks Apis actually gated on
2026-09-01/02, so these are not hypotheticals: each `test_real_gated_*` asserts
what the gate SHOULD have said about a task it really saw.
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime.action_type import (
    HIGH_BLAST_ACTION_TYPES,
    KNOWN_ACTION_TYPES,
    LOW_BLAST_ACTION_TYPES,
    infer_action_type,
    normalize_action_type,
)


# ── The core defect ──────────────────────────────────────────────────────────


def test_report_only_task_is_not_high_blast():
    """The task that says it writes nothing must not classify as a PR."""
    action = infer_action_type(
        "Add a read-only reconciler comparing issue gate_status "
        "against participation_record",
        "Report through the existing daemon reporting path. Write NOTHING - "
        "this job's only output is a divergence report.",
    )
    assert action == "compute_only_analysis"
    assert action in LOW_BLAST_ACTION_TYPES


def test_pure_reporting_task_is_low_blast():
    action = infer_action_type(
        "Extend the workflow drift check to compare declared gate sequence",
        "PURE REPORTING. No behaviour change, nothing gated on the result.",
    )
    assert action in LOW_BLAST_ACTION_TYPES


def test_analysis_that_also_opens_a_pr_is_high_blast():
    """High-blast signals win outright — this is the no-weakening invariant."""
    action = infer_action_type(
        "Analyze the failure and open a PR with the fix",
        "Produce a report of the findings, then open a pull request.",
    )
    assert action == "open_or_merge_pr"
    assert action in HIGH_BLAST_ACTION_TYPES


# ── Real tasks Apis gated, and what it should have said ──────────────────────


@pytest.mark.parametrize(
    "title, body, expected_high",
    [
        # THE control case from the brief: this one must STILL gate. It is the
        # config migration that makes daemons read Neotoma at startup, and its
        # own body says a PR is open for it.
        (
            "Migrate swarm configuration from env files to Neotoma entities",
            "Moves ~250 config variables into daemon_configuration entities. "
            "PR #643 open. Resolution: env var -> Neotoma entity -> local "
            "cache -> declared default -> loud failure.",
            True,
        ),
        # Report-only work that was wrongly gated.
        (
            "Add a read-only reconciler comparing gate_status",
            "Write NOTHING - this job's only output is a divergence report.",
            False,
        ),
        (
            "Extend the workflow drift check to compare gate sequence",
            "PURE REPORTING. No behaviour change, nothing gated on the result.",
            False,
        ),
        # Genuinely consequential work that must keep gating.
        (
            "Backfill the 129 stuck participation_record rows",
            "Two idempotent backfill sweeps that delete the orphaned rows and "
            "rewrite the edges.",
            True,
        ),
    ],
)
def test_real_gated_tasks(title, body, expected_high):
    action = infer_action_type(title, body)
    if expected_high:
        assert action in HIGH_BLAST_ACTION_TYPES, (
            f"{title!r} must remain high blast; inferred {action!r}"
        )
    else:
        assert action in LOW_BLAST_ACTION_TYPES, (
            f"{title!r} should be low blast; inferred {action!r}"
        )


# ── High-blast coverage ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Pay the invoice from the landlord", "payment"),
        ("Transfer funds to the contractor", "payment"),
        ("Send a payment to the yoga instructor", "payment"),
        ("Permanently delete the orphaned entities", "delete_entity_or_data"),
        ("Delete all records older than a year", "delete_entity_or_data"),
        ("Send the email to the client about the delay", "send_external_comms"),
        ("Reply to the customer thread", "send_external_comms"),
        ("Publish the post to the website", "publish"),
        ("Cut a release and tag it", "publish"),
        ("Open a PR with the fix", "open_or_merge_pr"),
        ("Merge the pull request once CI is green", "open_or_merge_pr"),
        ("Push to main after review", "open_or_merge_pr"),
        ("Close the github issue once verified", "external_api_write"),
    ],
)
def test_high_blast_signals(text, expected):
    assert infer_action_type(text) == expected
    assert expected in HIGH_BLAST_ACTION_TYPES


# ── Low-blast coverage ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Investigate the timeout and report the findings", "compute_only_analysis"),
        ("Read-only audit of the config surface", "compute_only_analysis"),
        ("Produce a report on test coverage", "compute_only_analysis"),
        ("Do not modify anything; assess the drift", "compute_only_analysis"),
        ("Retrieve the entities for the plan", "neotoma_read"),
        ("Draft a reply to the scheduling request", "draft"),
        ("Prepare a draft but do not send", "draft"),
    ],
)
def test_low_blast_signals(text, expected):
    assert infer_action_type(text) == expected
    assert expected in LOW_BLAST_ACTION_TYPES


# ── Conservatism: silence is never a licence ─────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "Fix the thing",
        "Update the component",
        "Look into the flakiness",
        "Improve performance",
        "Refactor the module",
    ],
)
def test_unclassifiable_text_returns_none(text):
    """No decisive signal → None, so the caller keeps its conservative default.

    This is the invariant that stops the fix from weakening the gate: an
    ambiguous task does NOT get quietly labelled low blast.
    """
    assert infer_action_type(text) is None


def test_none_and_empty_inputs():
    assert infer_action_type(None) is None
    assert infer_action_type(None, None) is None
    assert infer_action_type("", "") is None


def test_body_is_searched_not_just_title():
    assert infer_action_type("Housekeeping", "Then open a pull request") == (
        "open_or_merge_pr"
    )


# ── normalize_action_type ────────────────────────────────────────────────────


def test_normalize_accepts_known_values():
    for value in KNOWN_ACTION_TYPES:
        assert normalize_action_type(value) == value
        assert normalize_action_type(value.upper()) == value
        assert normalize_action_type(f"  {value}  ") == value


@pytest.mark.parametrize(
    "bogus",
    ["open_pull_request", "analysis", "readonly", "PR", "", "   ", None, 42, []],
)
def test_normalize_rejects_unknown_values(bogus):
    """A typo must not pass through to the policy's LOW default.

    `blast_radius_for` returns blast_radius_default (LOW) for any action type
    it does not recognize, so passing a typo through would earn auto-execution.
    Discarding it instead routes to the per-agent fallback.
    """
    assert normalize_action_type(bogus) is None


def test_vocabulary_partitions_cleanly():
    assert not (LOW_BLAST_ACTION_TYPES & HIGH_BLAST_ACTION_TYPES)
    assert KNOWN_ACTION_TYPES == LOW_BLAST_ACTION_TYPES | HIGH_BLAST_ACTION_TYPES
