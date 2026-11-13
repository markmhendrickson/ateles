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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import review_scope

log = logging.getLogger("apis.review_panel")

# Provider the security lens prefers, so its adversarial pass does not run on
# the same model that authored the code (Cicada defaults to `claude`).
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
            "does not narrow an existing protection anywhere else. Every concern "
            "must end in one of three states before your verdict. A break you "
            "demonstrated, or traced through a fully closed code path with "
            "named inputs, is CONFIRMED: block on it. A concern you checked "
            "and refuted is dropped from the findings and listed with what "
            "you checked. Only a concern you tried and truly cannot settle "
            "with what you have is PLAUSIBLE: mark it non-blocking, give a "
            "COMMENT verdict, and say what you tried and what would settle it. "
            "A review that finds nothing must state which sinks and which "
            "encodings you actually checked — 'looks fine' is not a security "
            "review."
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
            # Ateles' own security surfaces. Without these the lens fires on
            # NOTHING in the repo it lives in: the patterns above this block
            # mirror neotoma's classify_diff.js CONCERNS matcher, which
            # enumerates neotoma's tree (src/services/auth/, src/middleware/,
            # ...), and Ateles has no such paths. Measured after ateles#426
            # merged: five consecutive real PRs — including #426 itself, which
            # edits the dispatch path — seated pm/qa and never security.
            #
            # Deliberately NOT `execution/daemons/` wholesale. That would seat
            # the security lens on every daemon PR, spending the panel cap and
            # the reviewer's attention on routine changes until the lens is
            # ignored. These name the surfaces where an Ateles defect is
            # actually a security defect: identity and signing, capability
            # grants, money movement, and the dispatch path that decides which
            # agent runs with which token.
            r"(^|/)aauth",  # AAuth signing / HTTP-signature identity
            r"grant_checker",  # capability-grant enforcement
            r"neotoma_signed|signed_fetch",  # signed outbound requests
            r"(^|/)secrets_",  # secrets materialize / publish tooling
            r"(^|/)monedula/",  # payment execution
            r"(^|/)mcp/",  # MCP servers: they hold bearer tokens and build
            # Authorization headers, so a change here is an auth-surface change
            #
            # NOT matched here, deliberately: swarm_dispatch.py, skill_runner.py,
            # harness_router.py and the gateways. They ARE token-routing code,
            # but they are also the swarm's busiest files — most edits are
            # routing, ordering, or marker bookkeeping. Matching them by path
            # seated the lens on 76% of the last 30 merged PRs, which spends the
            # panel cap on routine changes until the lens is noise. They are
            # covered by the CONTENT patterns above (token|credential|secret|
            # signature, auth/, webhook) when an edit actually touches those
            # concerns, and reachability-based selection (neotoma#2174) is the
            # real fix for catching them when it does not.
        ),
        issue_patterns=(
            r"\b(security|vulnerabilit|ssrf|xss|csrf|injection|traversal|"
            r"auth|authz|authentication|authorization|token|credential|secret|"
            r"bypass|escalation|exploit|cve|ghsa|hardening|sanitiz)\b",
        ),
        # Prefer a different model than the one that most often authors the
        # code under review (Cicada defaults to the claude provider), so
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
    max_panel: int = 6,
    pending_gates: set[str] | None = None,
    required_lenses: set[str] | frozenset[str] | None = None,
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

    `required_lenses` are the lens labels the panel-gated approval tool will
    require for this PR at this head, chosen by changed path from the repo's
    `.github/review_scope.json` (`review_scope.path_panel_for_pr`, the same
    reader and decision the tool uses). The panel is the UNION of those and
    the lenses picked here, and a required lens is never dropped by the cap:
    it ranks with the pending-gate owners, and the cap only trims the rest. A
    panel without one of them could only ever produce an approval the tool
    refuses.
    """
    pending = pending_gates or set()
    required = {name.strip().lower() for name in (required_lenses or ())}
    selected: list[Lens] = []
    for lens in LENSES:
        relevant = (
            lens.always
            or lens.agent in gate_contributors
            or _matches_diff(lens, changed_files)
            or (lens.gate != "" and lens.gate in pending)
            or lens.lens in required
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
    # (they MUST re-run to clear it), then security, then always-on lenses,
    # other blocking lenses, and forward-looking lenses. This keeps qa/pm in a
    # constrained broad-diff panel rather than letting registry order evict
    # them before their mandatory review runs.
    gate_owners = [
        item
        for item in selected
        if (not item.forward_looking and item.gate != "" and item.gate in pending)
        or item.lens in required
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
    always_on = [
        item
        for item in selected
        if not item.forward_looking
        and item.always
        and item not in gate_owners
        and item not in security
    ]
    other_blocking = [
        item
        for item in selected
        if not item.forward_looking
        and item not in gate_owners
        and item not in security
        and item not in always_on
    ]
    forward = [item for item in selected if item.forward_looking]
    ranked = gate_owners + security + always_on + other_blocking + forward
    panel = ranked[:max_panel]
    # A lens the approval tool requires is never capped away: a panel without
    # it cannot produce an approvable review.
    panel += [item for item in ranked[max_panel:] if item.lens in required]
    dropped = [item.lens for item in selected if item not in panel]
    if dropped:
        log.info(f"[apis] review panel capped at {max_panel}; dropped: {dropped}")
    return panel


# Lenses the bootstrap approval gate never requires, even when `select_panel`
# derives them into a PR's floor for its diff (ateles#1317, operator ruling
# 2026-09-26: Corvus/content is excluded from bootstrap review). The approval
# tool's `BOOTSTRAP_EXCLUDED_LENSES` is this constant.
APPROVAL_EXCLUDED_LENSES: frozenset[str] = frozenset({"content"})

# The `review_expectation` pre-registration marker an issue comment carries
# (`swarm_dispatch.EXPECTATION_MARKER`, which posts it). Read here so the
# approval tool and Apis recover the same gate contributors the same way.
_EXPECTATION_MARKER_RE = re.compile(
    r"\*\*review_expectation \((?P<lens>[\w-]+)\)\*\* — what "
    r"(?P<agent>\w+) will verify",
    re.I,
)


def approval_floor(
    gate_contributors: set[str], changed_files: list[str], max_panel: int
) -> list[Lens]:
    """The approval tool's diff-derived required-lens FLOOR for a PR.

    `select_panel` over the PR's changed files and the agents that
    pre-registered on its parent issue, at `max_panel`, with every always-on
    lens unioned back in by name (defense in depth if `select_panel` ever drops
    one). Always-on lenses first, then the rest in `select_panel`'s order. The
    one implementation `approve_pr_as_app.derive_required_lenses` and Apis
    (`approval_required_lenses`) both use, so the two cannot disagree on it.
    """
    panel = select_panel(
        gate_contributors=gate_contributors,
        changed_files=changed_files,
        max_panel=max_panel,
    )
    by_name: dict[str, Lens] = {lens.lens: lens for lens in LENSES if lens.always}
    for lens in panel:
        by_name.setdefault(lens.lens, lens)
    return list(by_name.values())


def approval_required_lenses(
    decision: "review_scope.PanelDecision",
    *,
    gate_contributors: set[str],
    changed_files: list[str],
    max_panel: int,
) -> frozenset[str]:
    """Every lens `approve_pr_as_app.py --panel path` requires for a PR whose
    path decision is *decision*, with no `--lenses` additions.

    A full-panel decision requires the diff-derived floor (`approval_floor`,
    less `APPROVAL_EXCLUDED_LENSES`) AND the five-lens bootstrap roster: a
    `LICENSE` or `package.json` change adds `legal` through the floor's own
    patterns, and a pre-registered contributor adds its lens. A one-lens
    decision requires that lens alone (the floor is not added on the reduced
    route). Apis seats exactly this set, so a panel it finishes is one the tool
    can approve.
    """
    if not decision.full:
        return frozenset({decision.lens})
    floor = {
        lens.lens
        for lens in approval_floor(gate_contributors, changed_files, max_panel)
        if lens.lens not in APPROVAL_EXCLUDED_LENSES
    }
    return frozenset(floor | review_scope.FULL_PANEL_LENSES)


def every_requirable_lens() -> frozenset[str]:
    """Every lens the approval tool could require for some PR: each blocking
    lens in the registry, less `APPROVAL_EXCLUDED_LENSES`. The fail-closed
    answer when a PR's own required set cannot be read."""
    return frozenset(
        lens.lens
        for lens in LENSES
        if not lens.forward_looking and lens.lens not in APPROVAL_EXCLUDED_LENSES
    )


def gate_contributors_from_comments(bodies: Iterable[str]) -> set[str]:
    """Agents that pre-registered a `review_expectation` in *bodies*."""
    out: set[str] = set()
    for body in bodies:
        match = _EXPECTATION_MARKER_RE.search(body or "")
        if match:
            out.add(match.group("agent").lower())
    return out


async def fetch_gate_contributors(
    client: object,
    *,
    api_base: str,
    repo: str,
    issue_number: int,
    headers: Mapping[str, str],
) -> set[str]:
    """Agents that pre-registered on *issue_number*, over every comment page.
    Raises on an HTTP error: an unread contributor could be a required lens."""
    out: set[str] = set()
    page = 1
    while True:
        resp = await client.get(  # type: ignore[attr-defined]
            f"{api_base}/repos/{repo}/issues/{issue_number}/comments",
            headers=dict(headers),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        rows = resp.json() or []
        out |= gate_contributors_from_comments(c.get("body") or "" for c in rows)
        if len(rows) < 100:
            break
        page += 1
    return out


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


def lens_by_name(name: str) -> "Lens | None":
    """The panel lens whose label matches *name*, or None.

    ateles#431. The missing-lens sweep knows only the label Vanellus printed in
    its aggregation ("security"), and needs the Lens to resolve the agent and
    provider. Returning None for an unknown label keeps the sweep from
    inventing a dispatch target.
    """
    wanted = (name or "").strip().lower()
    for lens in LENSES:
        if lens.lens.lower() == wanted:
            return lens
    return None


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
    # Re-read the env BEFORE the empty check, not after: the registry is built
    # at import, so the frozen field can be "" (preference disabled at import)
    # while the env now names a provider. Checking the frozen value first would
    # early-return and silently ignore a late enable — the disable-then-enable
    # direction of the same staleness this re-read exists to fix.
    if lens.lens == "security":
        preferred = _security_lens_provider()
    else:
        preferred = (lens.preferred_provider or "").strip().lower()
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
