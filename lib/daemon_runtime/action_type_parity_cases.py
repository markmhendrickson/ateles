"""Shared fixtures: one task, two surfaces, one verdict (ateles#682).

The dispatcher's gate (`apis.dispatch_task`) and the Ateles MCP `route_task`
preview both classify blast radius. Each is tested against THIS table in its own
natural call shape, so a change to either that makes them disagree fails in CI
instead of being discovered when an operator is told "low" by the preview and
held by the gate.

Provenance: the resolver reads no `source` or other provenance field, so NO case
below depends on it; the MCP preview taking no `source` argument cannot make it
differ from the dispatcher on any case here. What the preview cannot see is the
task's body and description fields (it is given one description string); every
case below puts its text in the title/description the preview does receive.

Data only, stdlib only, so the MCP server's test environment can import it.
Each case: (id, task description, declared action_type or None, agent the
description routes to under the standard roster, expected action_type,
expected source, expected blast radius under PARITY_POLICY or None when no
action type resolves).
"""

from __future__ import annotations

# Sets deliberately smaller than the live policy's; every value used below is in
# one of them. Blast for an unclassified declared value is "never".
PARITY_POLICY = {
    "high_blast_action_types": ["open_or_merge_pr", "payment", "git_push"],
    "low_blast_action_types": [
        "local_edit",
        "draft",
        "neotoma_read",
        "compute_only_analysis",
    ],
}

PARITY_CASES = [
    (
        "report_only_language_does_not_lower_the_prior",
        "Review the code path. Write NOTHING - this job's only output is a divergence report.",
        None, "cicada", "open_or_merge_pr", "agent_prior", "high",
    ),
    (
        "explicit_pr_is_high",
        "implement the change",
        "open_or_merge_pr", "cicada", "open_or_merge_pr", "declared", "high",
    ),
    (
        "explicit_low_stays_low_under_cicada",
        "implement the local cleanup",
        "local_edit", "cicada", "local_edit", "declared", "low",
    ),
    (
        "no_signal_keeps_the_agent_prior",
        "implement a new feature",
        None, "cicada", "open_or_merge_pr", "agent_prior", "high",
    ),
    (
        "pr_text_under_cicada_is_high",
        "implement the change and open a PR for it. Write nothing else.",
        None, "cicada", "open_or_merge_pr", "agent_prior", "high",
    ),
    (
        "specialist_prior_is_unchanged",
        "pay the yoga invoice",
        None, "monedula", "payment", "agent_prior", "high",
    ),
    (
        "declared_typo_is_never_not_discarded",
        "implement the change",
        "open_pull_request", "cicada", "open_pull_request", "declared", "never",
    ),
    (
        "declared_operator_only_is_never",
        "rotate the credential, code change",
        "operator_only", "cicada", "operator_only", "declared", "never",
    ),
    (
        "no_agent_prior_and_no_signal_resolves_nothing",
        "something completely unrelated to any keyword",
        None, "apis", None, "none", None,
    ),
    (
        "text_escalates_an_agent_with_no_prior",
        "something completely unrelated, but open a PR for it",
        None, "apis", "open_or_merge_pr", "content", "high",
    ),
    (
        "declared_low_with_outward_text_is_escalated",
        "implement the local cleanup, then push it",
        "local_edit", "cicada", "git_push", "declared_escalated", "high",
    ),
]
