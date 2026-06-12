"""
execution/daemons/apis/review_panel.py — multi-perspective review panel.

Implements neotoma#1640: instead of one generic reviewer, a PR gets a panel
of agents each reviewing through its own lens. The same lens registry drives
the shift-left review contract (ateles#81): at issue time the relevant agents
pre-register what they will check at PR time (`review_expectation`), and at
PR time each panelist reviews against its own pre-registered expectations.

Panel = {gate contributors on the parent issue}
      ∪ {lenses whose diff-surface patterns match the changed files}
      ∪ {forward-looking downstream lenses (Corvus) on non-trivial PRs},
capped at `max_panel` with blocking lenses prioritized over forward-looking.

The Claude GHA reviewer stays as the always-on correctness/security baseline;
this panel adds domain + forward-looking layers on top (per the issue's
recommendation), so nothing here replaces CI review.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("apis.review_panel")


@dataclass(frozen=True)
class Lens:
    """One reviewer perspective in the panel."""

    agent: str  # T4 skill name (must exist in .claude/skills/<agent>/)
    lens: str  # short label, used as `review:<lens>`
    gate: str  # gate this agent owns on the issue pipeline ("" if none)
    checks: str  # what this lens verifies — seeds review_expectation comments
    diff_patterns: tuple[str, ...] = ()  # changed-file regexes that pull it in
    issue_patterns: tuple[str, ...] = ()  # issue-text regexes for pre-registration
    always: bool = False  # serves on every panel / pre-registers on every issue
    forward_looking: bool = False  # non-blocking; output routes to own queue
    min_changed_files: int = 0  # skip when the diff is smaller than this


# Lens registry. Order = priority when the panel is capped.
LENSES: tuple[Lens, ...] = (
    Lens(
        agent="pavo",
        lens="pm",
        gate="pm",
        checks=(
            "Change matches the scoped intent and acceptance criteria the pm "
            "gate signed off; no unrequested scope creep; user-visible "
            "behavior matches the issue."
        ),
        always=True,
    ),
    Lens(
        agent="bombycilla",
        lens="arch",
        gate="arch",
        checks=(
            "Contract-first: OpenAPI + contract_mappings updated before "
            "handlers; layering respected; schema declared for new response "
            "fields; tenant isolation on every entity lookup; idempotency_key "
            "on mutating ops."
        ),
        diff_patterns=(
            r"openapi\.ya?ml",
            r"(^|/)api/",
            r"(^|/)schemas?/",
            r"(^|/)migrations?/",
            r"contract_mappings",
            r"(^|/)server/",
        ),
        issue_patterns=(
            r"\b(api|endpoint|schema|entity|mcp tool|migration|contract)\b",
        ),
    ),
    Lens(
        agent="accipiter",
        lens="ux",
        gate="ux",
        checks=(
            "Agent/developer experience of the new surface: discoverable "
            "naming, error messages with actionable hints, docs/examples for "
            "the new surface."
        ),
        diff_patterns=(r"(^|/)docs/", r"(^|/)cli/", r"SKILL\.md$", r"README"),
        issue_patterns=(r"\b(ux|cli|developer experience|dx|docs?|onboarding)\b",),
    ),
    Lens(
        agent="buteo",
        lens="legal",
        gate="legal",
        checks=(
            "Licensing of new dependencies; data-handling on public-effect "
            "surfaces; guest-token / credential exposure scope; PII leaving "
            "the store."
        ),
        diff_patterns=(
            r"package\.json$",
            r"requirements.*\.txt$",
            r"pyproject\.toml$",
            r"(^|/)auth/",
            r"\.env",
            r"LICENSE",
        ),
        issue_patterns=(
            r"\b(license|licensing|public|auth|token|credential|pii|privacy)\b",
        ),
    ),
    Lens(
        agent="phoenicurus",
        lens="qa",
        gate="qa",
        checks=(
            "Test coverage adequacy for the change: regression test for any "
            "fixed bug, edge cases for new branches, contract tests for new "
            "endpoints."
        ),
        always=True,
    ),
    Lens(
        agent="corvus",
        lens="content",
        gate="",
        checks=(
            "Is this PR shippable as a content/dogfooding story? If yes, "
            "draft the angle as a task in your queue — do not block the PR."
        ),
        forward_looking=True,
        min_changed_files=5,  # only non-trivial PRs spawn content review
    ),
)


def select_panel(
    gate_contributors: set[str],
    changed_files: list[str],
    max_panel: int = 4,
) -> list[Lens]:
    """
    Pick the review panel for a PR.

    `gate_contributors` are agent names that filed a gate plan_contribution
    (or review_expectation) on the parent issue. Relevance filter per
    neotoma#1640 — not all-agents-always. Dropped lenses are logged so the
    cap never silently truncates.
    """
    selected: list[Lens] = []
    for lens in LENSES:
        relevant = (
            lens.always
            or lens.agent in gate_contributors
            or _matches_diff(lens, changed_files)
        )
        if lens.forward_looking:
            # Size threshold is an additional opt-in path, not an override
            # (Loxia review on PR #87): a forward-looking lens that
            # pre-registered expectations must keep its panel seat even on
            # small diffs.
            relevant = relevant or len(changed_files) >= lens.min_changed_files
        if relevant:
            selected.append(lens)

    blocking = [item for item in selected if not item.forward_looking]
    forward = [item for item in selected if item.forward_looking]
    panel = (blocking + forward)[:max_panel]
    dropped = [item.lens for item in selected if item not in panel]
    if dropped:
        log.info(f"[apis] review panel capped at {max_panel}; dropped: {dropped}")
    return panel


def select_expectation_agents(
    title: str, body: str, labels: list[str]
) -> list[Lens]:
    """
    Pick which lenses pre-register review expectations on a new issue
    (ateles#81). Always-on lenses pre-register on every issue; others only
    when their issue_patterns match — same relevance principle as the panel,
    and expectations are capped to a tight checklist by the dispatch prompt.
    """
    text = f"{title}\n{body}\n{' '.join(labels)}"
    out: list[Lens] = []
    for lens in LENSES:
        if lens.forward_looking:
            continue  # downstream lenses react to PRs, not issues
        if lens.always or any(
            re.search(p, text, re.I) for p in lens.issue_patterns
        ):
            out.append(lens)
    return out


def _matches_diff(lens: Lens, changed_files: list[str]) -> bool:
    return any(
        re.search(pattern, path)
        for pattern in lens.diff_patterns
        for path in changed_files
    )
