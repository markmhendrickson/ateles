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
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("apis.review_panel")

# Provider the security lens prefers, so its adversarial pass does not run on
# the same model that authored the code (Cicada/Gryllus default to `claude`).
# Deployments that do not have a second provider authenticated can set this to
# "" to disable the preference outright rather than eat a per-review fallback.
_DEFAULT_SECURITY_LENS_PROVIDER = "codex"


def _security_lens_provider() -> str:
    """Resolve the security lens's preferred harness provider."""
    raw = os.environ.get(
        "ATELES_SECURITY_LENS_PROVIDER", _DEFAULT_SECURITY_LENS_PROVIDER
    )
    return raw.strip().lower()


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
    # Harness provider this lens PREFERS (claude/codex/cursor — genuinely
    # different models). A preference, never a hard pin: `run_skill(provider=)`
    # narrows the candidate list to exactly one, so a pinned provider that is
    # cooling or unavailable yields no candidates and the lens does not run at
    # all. For a security lens that failure mode is the one thing worse than
    # same-model review — the review silently does not happen. The dispatcher
    # resolves this to a pin only when the provider is actually available, and
    # otherwise falls back to normal routing with the divergence logged.
    preferred_provider: str = ""


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
        agent="waxwing",
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
        agent="falco",
        lens="security",
        gate="",
        checks=(
            "ADVERSARIAL SECURITY REVIEW — your job is to REFUTE this change, "
            "not to confirm it. Do not ask 'is this adequate?'; ask 'what is "
            "the path that still fails open?' Assume the author's sweep of the "
            "vulnerable class is INCOMPLETE until you have proven otherwise, "
            "and treat a fix that is correct on the paths it touches as "
            "unfinished until you have looked for the paths it did not. "
            "Specifically: (1) ENUMERATE EVERY SINK of the pattern this change "
            "addresses — grep the whole repo for the vulnerable call, not just "
            "the files in the diff — and name the ones the fix did NOT cover, "
            "including exported entry points that sit beside a guarded sibling; "
            "(2) attack the guard's INPUT DOMAIN: alternate encodings and "
            "normalizations of a value the guard rejects in its canonical form "
            "(IPv4-mapped and IPv6-compressed addresses, percent-encoding, "
            "unicode and case folding, trailing dots, redirects, DNS names that "
            "resolve to a blocked address), and say which of them reach the "
            "sink; (3) find every branch where an error, an unset env var, a "
            "parse failure, or an unknown value yields ALLOW rather than DENY — "
            "name each fail-open default explicitly; (4) check that the change "
            "does not narrow an existing protection anywhere else. Report a "
            "break you demonstrated as CONFIRMED and block on it; report a "
            "break you can argue but not demonstrate as PLAUSIBLE and do not "
            "block. A review that finds nothing must state which sinks and "
            "which encodings you actually checked — 'looks fine' is not a "
            "security review."
        ),
        diff_patterns=(
            # Path-level security surfaces, kept in lock-step with the CONCERNS
            # matcher in neotoma's scripts/security/classify_diff.js (which
            # emits sensitive=true from these same paths). Replicated as regexes
            # rather than shelling out to the classifier: the panel reviews any
            # repo, runs from the Apis daemon checkout with no guarantee that
            # the reviewed repo's node_modules or scripts are present, and a
            # subprocess that fails would silently drop the security lens.
            r"(^|/)src/actions\.ts$",
            r"(^|/)services/root_landing/",
            r"(^|/)middleware/",
            r"(^|/)services/auth/",
            r"(^|/)services/aauth/",
            r"(^|/)services/subscriptions/",
            r"(^|/)services/sync/",
            r"(^|/)services/issues/gh_auth\.ts$",
            r"(^|/)services/entity_submission/",
            r"(^|/)access_policy\.ts$",
            r"(^|/)local_auth\.ts$",
            r"(^|/)sandbox_mode\.ts$",
            r"(^|/)inspector_mount\.ts$",
            r"openapi\.ya?ml$",
            r"(^|/)scripts/security/",
            r"protected_routes_manifest\.json$",
            # Generic, repo-agnostic security surfaces so the lens is not
            # neotoma-only: the panel also reviews ateles and future repos.
            r"(^|/)auth/",
            r"(^|/)security/",
            r"\.env",
            r"(^|/)(hooks|guards?)/",
            r"token|credential|secret|password|crypto|signature|sanitiz|escape",
            r"ssrf|xss|csrf|injection|traversal",
            r"(^|/)net/",
            r"webhook",
        ),
        issue_patterns=(
            r"\b(security|vulnerabilit|ssrf|xss|csrf|injection|traversal|"
            r"auth|authz|authentication|authorization|token|credential|secret|"
            r"bypass|escalation|exploit|cve|ghsa|hardening|sanitiz)\b",
        ),
        # Prefer a different model than the one that most often authors the
        # code under review (Cicada/Gryllus default to the claude provider), so
        # the adversarial pass does not inherit the author's priors — the
        # documented same-priors blind spot. Overridable per deployment.
        preferred_provider=_security_lens_provider(),
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
    pending_gates: set[str] | None = None,
) -> list[Lens]:
    """
    Pick the review panel for a PR.

    `gate_contributors` are agent names that filed a gate plan_contribution
    (or review_expectation) on the parent issue. Relevance filter per
    neotoma#1640 — not all-agents-always. Dropped lenses are logged so the
    cap never silently truncates.

    `pending_gates` are gate names still unsigned on the parent issue (from
    Lanius's `GATE_PENDING:` line). A lens that OWNS a pending gate is always
    relevant AND is prioritized ahead of other blocking lenses when the panel
    is capped — otherwise a carried-over gate could never clear, because the
    only agent that can re-evaluate it (its owning lens) would be dropped by
    the cap and never re-invoked (ateles#230 panel-assembly gap: PR #1944's
    arch gate stuck `pending` while pm/ux/legal/qa re-ran without arch).
    """
    pending = pending_gates or set()
    selected: list[Lens] = []
    for lens in LENSES:
        relevant = (
            lens.always
            or lens.agent in gate_contributors
            or _matches_diff(lens, changed_files)
            or (lens.gate != "" and lens.gate in pending)
        )
        if lens.forward_looking:
            # Size threshold is an additional opt-in path, not an override
            # (Loxia review on PR #87): a forward-looking lens that
            # pre-registered expectations must keep its panel seat even on
            # small diffs.
            relevant = relevant or len(changed_files) >= lens.min_changed_files
        if relevant:
            selected.append(lens)

    # Priority order under the cap: lenses owning a still-pending gate first
    # (they MUST re-run to clear it), then other blocking lenses, then
    # forward-looking. Registry order is preserved within each tier.
    gate_owners = [
        item
        for item in selected
        if not item.forward_looking and item.gate != "" and item.gate in pending
    ]
    # The security lens owns no gate, so registry order alone would let the cap
    # drop it exactly when it matters most: a BROAD security-touching PR pulls
    # in arch/ux/legal too, and with the default cap of 4 the security lens —
    # last in registry order — is the first thing dropped. A lens that is
    # silently absent from the highest-risk reviews is worse than no lens, so
    # once its trigger has fired it ranks alongside pending-gate owners rather
    # than behind every other blocking lens (ateles#425).
    security = [
        item
        for item in selected
        if not item.forward_looking
        and item.lens == "security"
        and item not in gate_owners
    ]
    other_blocking = [
        item
        for item in selected
        if not item.forward_looking
        and item not in gate_owners
        and item not in security
    ]
    forward = [item for item in selected if item.forward_looking]
    panel = (gate_owners + security + other_blocking + forward)[:max_panel]
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


def resolve_lens_provider(
    lens: Lens, available_providers: set[str] | None = None
) -> str | None:
    """Resolve the harness provider to pin for `lens`, or None for normal routing.

    Model diversity on the security lens is a PREFERENCE, deliberately not a
    hard pin. `run_skill(provider=...)` narrows the candidate list to exactly
    that one adapter, so pinning a provider that is unauthenticated, missing its
    binary, or cooling down after a capacity failure produces zero candidates —
    and the lens does not run at all. Silently skipping the security review is a
    strictly worse outcome than running it on the same model that wrote the
    code, so an unavailable preference degrades to normal weighted routing and
    says so in the log rather than failing closed.

    `available_providers` is the set the caller knows to be usable right now
    (the dispatcher passes the live binary/headroom view). When omitted, the
    preference is returned unvalidated — callers that cannot check availability
    are trusted to handle a failed run.
    """
    preferred = (lens.preferred_provider or "").strip().lower()
    if not preferred:
        return None
    # Re-read the env at dispatch time: the registry is built at import, so a
    # deployment that sets ATELES_SECURITY_LENS_PROVIDER after module load (or
    # in a test) would otherwise keep the value frozen at import time.
    if lens.lens == "security":
        preferred = _security_lens_provider()
        if not preferred:
            return None
    if available_providers is not None and preferred not in available_providers:
        log.warning(
            f"[apis] {lens.lens} lens prefers provider '{preferred}' but it is "
            "unavailable — falling back to normal routing. The review still "
            "runs, without model diversity."
        )
        return None
    return preferred


def _matches_diff(lens: Lens, changed_files: list[str]) -> bool:
    return any(
        re.search(pattern, path)
        for pattern in lens.diff_patterns
        for path in changed_files
    )
