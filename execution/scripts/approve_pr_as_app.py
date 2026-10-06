#!/usr/bin/env python3
"""
execution/scripts/approve_pr_as_app.py — approve a PR as the swarm's GitHub
App, but ONLY when every required review lens has cleared the PR's CURRENT
head (or, for a lens the fix did not touch, an earlier head the approval
review names) and every required status check is green.

Carried sign-offs (ateles#1368, ruling `rereview_only_blockers_and_touched_areas`):
a lens with no verdict on the current head clears on its verdict at an EARLIER
head only when it signed off there, never blocked, and the fix's own delta
since that head touches none of its areas (`review_carry`: every file belongs
to every lens unless on a short docs/tests/release-notes allowlist; an empty or
unreadable delta carries nothing). The approval review names every carried lens
with the head its verdict came from. `--no-carry` requires every lens on the
current head itself.

Bootstrap mode (agent_policy `ent_d0f1a840e549b3b299f62397`): the operator
has ruled that sessions, not the operator, approve merges, under the App's
identity, with the lens panel's verdicts as the gate. This tool is the
mechanism: it never trusts its own judgement about whether a lens cleared —
it reads the SAME parser the dispatcher uses (`swarm_dispatch.lens_own_verdict`
/ `sign_off_is_warranted`), and it refuses to approve on anything it cannot
verify.

Design basis: docs/foundation/principles.md#1-a-mechanism-that-does-not-bind-is-not-a-control
The approval this tool submits is a binding control only because it can be
produced ONLY from a lens verdict comment marked for the PR's CURRENT head
(`compose_lens_review_marker`) — a verdict comment left on an old head, a
missing lens, a `[BLOCKING]` finding, or a red required check each refuse
outright rather than approving with a caveat. The required-lens FLOOR is
likewise derived, not typed by the caller (round-1 pm/arch/security review on
PR #1266): `derive_required_lenses` calls `review_panel.select_panel` — the
same function `swarm_dispatch.py`'s dispatcher calls to assemble a PR's
review panel — against THIS PR's changed files and linked issue, so the tool
can never silently require less than the pipeline itself would review with.

ateles#1293 (2026-09-26): this tool approved a PR with "lenses: ALL PASS"
while Waxwing (arch) — a lens `select_panel` did NOT seat for that diff —
had a live REQUEST_CHANGES with a `[BLOCKING]` finding on the SAME head,
posted ten minutes earlier. The required-lens floor decides who MUST clear;
it was silently read as deciding who is even LOOKED AT. Fix, two parts:
  - `find_non_required_blocks` reads every lens in `LENS_AGENTS` — not only
    the ones in the resolved `lenses` list — for a comment on the PR's
    CURRENT head, and reports any of them that actively objects (REQUEST_CHANGES,
    BLOCKED, or `[BLOCKING]`; an unreadable verdict also counts, fail closed),
    with the same `lens_own_verdict` fixed-position parser `evaluate_lens`
    already uses for required lenses. A plain non-blocking `COMMENT` from a
    non-required lens is NOT an objection (ateles#1394). `run()` refuses
    the approval if this ever finds anything, independent of whether every
    required lens itself passed.
  - `--panel {required,all}` (`all` is the default while bootstrap mode is
    on, see `_panel_all_default`): `all` folds every one of the five lenses
    in the bootstrap roster into the required set up front, so the gap
    above can never open at all — a lens has to clear or it is a
    required-lens FAILURE, not merely a "non-required block" the second
    layer has to catch. `required` is the pre-#1293 behaviour (derived
    floor only). The non-required-block check above stays on regardless of
    `--panel`, as defense in depth for when `required` is explicitly chosen.

Whose comments count: a lens verdict is read only from a comment a swarm
identity wrote (`lens_authors`, taken from configuration — never a login typed
into this file). The head marker is text any account can post on a public
repository, so a marker from any other account neither clears nor objects; an
empty or unreadable identity set reads every lens as having no verdict
(docs/foundation/principles.md#5-fail-closed-on-the-field-that-carries-the-safety-meaning).

Reuse, not rebuild:
  - Lens verdict parsing: `swarm_dispatch.lens_own_verdict` /
    `swarm_dispatch.sign_off_is_warranted` — the security-reviewed fixed-
    position parser (ateles#1181, PR #1248). This script does not parse a
    verdict itself; it hands the comment body to these functions.
  - App identity: `swarm_dispatch._mint_reviewer_app_installation_token` /
    `swarm_dispatch._reviewer_app_private_key_pem` — the same JWT-then-
    installation-token exchange `_emit_formal_review` already uses to submit
    binding reviews as `ateles-agents[bot]` (PR #1239: key read from
    ATELES_REVIEWER_APP_PRIVATE_KEY or ATELES_REVIEWER_APP_PRIVATE_KEY_PATH).
  - Required-lens selection: `review_panel.select_panel` / `LENSES` — the
    same panel-assembly logic (`diff_patterns` on changed files,
    `issue_patterns` + pre-registered `review_expectation` comments on the
    linked issue, `always`-on lenses) the dispatcher uses, imported and
    called, not copied.
  - Pre-submit re-verification + readback: mirrors
    `swarm_dispatch._emit_formal_review`'s binding-review contract exactly —
    re-fetch the PR immediately before posting and refuse unless it is still
    OPEN, unmerged, and at the verified head; pin `commit_id` to that head;
    refuse a self-approval; and after posting, read the created review back
    and confirm id/commit/state/author before treating it as landed (round-1
    Falco finding on PR #1266: this tool's own POST response was previously
    trusted as proof, with no independent read-back — exactly the failure
    shape `docs/foundation/principles.md#1` names).

Unschedulable non-required checks (operator ruling 2026-09-25): until a
self-hosted runner exists for a check, a check that branch protection on the
base branch does NOT require, and that is still queued with no online runner
carrying all of its job's labels, is reported as "not run (no runner)" in the
check table and named in the approval body instead of failing the gate. It
binds again automatically once a matching runner is online. Fail-closed rules:
unreadable protection makes every check required; an unreadable job or a
GitHub-hosted label keeps the check blocking; an unreadable runner list keeps
it blocking unless its exact label set is on
`KNOWN_UNPROVISIONED_RUNNER_LABEL_SETS`; a completed check with a failing
conclusion always blocks. See `_unscheduled_non_required_reason`.

Usage:
    python3 execution/scripts/approve_pr_as_app.py --repo <owner/name> --pr <n> \\
        [--lenses pm,security] [--panel {required,all}] [--no-carry] [--apply]

`--lenses` ADDS to the derived floor; it can never remove a lens the panel
logic would itself require for this diff/issue. `--panel all` (the default
while bootstrap mode is on — see `_panel_all_default` and
ATELES_APPROVE_PANEL_REQUIRED_ONLY below) additionally requires the five
lenses in `BOOTSTRAP_PANEL_LENSES` (pm, arch, ux, qa, security — the
bootstrap roster; NEITHER `legal` NOR `content`/Corvus is in this set)
to have signed off on the current head; `legal` joins the floor only when
`review_panel.select_panel` itself derives it for the diff, and
`content`/Corvus is excluded from bootstrap mode outright (operator
ruling 2026-09-26, agent_policy `ent_d0f1a840e549b3b299f62397`) — it is
never required under `--panel all`, and `resolve_lenses` actively strips
it even if `select_panel` would otherwise have derived it into the floor
for a non-trivial diff (see `resolve_lenses`). `--panel required` uses the
diff-derived floor alone, as before ateles#1293's fix — `content` CAN
still appear there, since that floor mirrors the normal non-bootstrap
pipeline exactly. Either way, a live REQUEST_CHANGES/`[BLOCKING]`
verdict from ANY lens on the current head refuses the approval, whether or
not that lens is in the required set. Without --apply this is always a dry
run: it prints the derived-lens rationale, the per-lens table, the non-
required-block list (if any), and the check-run table, and does nothing
else, whether every gate passes or not. With --apply, and ONLY if every
required lens passes, no lens of any kind carries a live block, and every
required check passes, it submits a formal APPROVE review as the App. On
any failure it exits non-zero and submits nothing.

Env:
    (App installation token)              read-only GitHub calls (PR, comments, checks);
    GITHUB_TOKEN / ATELES_AGENT_PAT       fallback only while the App is unconfigured
    ATELES_REVIEWER_APP_ID                reviewer App id (required for --apply)
    ATELES_REVIEWER_APP_PRIVATE_KEY(_PATH) reviewer App private key (required for --apply)
    ATELES_REVIEWER_APP_INSTALLATION_ID   optional; resolved via API if unset
    ATELES_APPROVE_PANEL_REQUIRED_ONLY    set to "1" to opt OUT of the bootstrap-
                                           mode --panel all default (use the
                                           diff-derived floor alone); --panel on
                                           the command line always overrides this

This script never prints, logs, or commits the App private key or any token.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402

from lib import github_app_token as _github_app_token  # noqa: E402

import review_carry  # noqa: E402
import lens_authors  # noqa: E402
from review_panel import LENSES, Lens, select_panel  # noqa: E402
from swarm_dispatch import (  # noqa: E402
    EXPECTATION_MARKER,
    SwarmDispatcher,
    _mint_reviewer_app_installation_token,
    _normalise_full_sha,
    _normalise_github_review_id,
    _reviewer_app_private_key_pem,
    body_has_blocking_findings,
    compose_lens_review_marker,
    lens_comment_authors,
    lens_explicit_objection,
    lens_own_verdict,
    lens_records,
    output_has_blocking_verdict,
    sign_off_is_warranted,
)

# lens short-name -> owning agent, mirroring review_panel.LENSES. Imported by
# value rather than by reference to LENSES because LENSES also carries
# diff_patterns/issue_patterns/provider preferences this tool has no use for;
# the agent name is the only field `lens_own_verdict` needs
# (skill_runner.SWARM_GITHUB_CONTRACT's attribution header names the AGENT,
# not the lens short-name). Kept in lock-step with review_panel.py by
# `test_approve_pr_as_app.py::test_lens_agents_match_review_panel_registry`.
LENS_AGENTS: dict[str, str] = {
    "pm": "pavo",
    "arch": "waxwing",
    "ux": "accipiter",
    "legal": "buteo",
    "qa": "phoenicurus",
    "security": "falco",
    "content": "corvus",
}

# The five lenses bootstrap mode's own panel actually dispatches (agent_policy
# `ent_d0f1a840e549b3b299f62397`, "Software work runs in bootstrap mode":
# "Pavo pm, Waxwing arch, Falco security, Phoenicurus qa, Accipiter ux when
# UI changes").
#
# NOT `list(LENS_AGENTS)` — that is a documented incident, not a typo. PR
# #1303 round 1 shipped `--panel all` unioning in literally every entry of
# `LENS_AGENTS`, seven lenses including `legal`/Buteo. `legal` is not one of
# `review_panel.LENSES`'s always-on lenses (only `pm`/`qa` carry
# `always=True`); it is diff/issue-pattern-gated on licensing, `auth/`,
# `LICENSE`, and PII/privacy surfaces. Bootstrap mode's own roster never
# dispatches Buteo for an ordinary diff, so requiring it unconditionally
# meant `--apply` could never pass under the tool's own stated default —
# Waxwing (arch), Pavo (pm), Phoenicurus (qa) and Accipiter (ux) each
# independently confirmed this reading #1303's round-1 head and withheld
# sign-off. `legal` still joins the required set for a diff that actually
# needs it, exactly as before: `derive_required_lenses` calls
# `review_panel.select_panel`, which pulls `legal` in via its own
# `diff_patterns`/`issue_patterns` (e.g. neotoma#2513, a `package.json`/
# licensing diff) regardless of what `--panel` is set to. `--panel all`
# only widens the FLOOR to this bootstrap roster; it never narrows what
# `select_panel` would otherwise require.
#
# `content`/Corvus is likewise NOT in this set, but for a different reason
# than `legal`: operator ruling 2026-09-26 excludes Corvus from bootstrap
# review dispatch and the bootstrap approval gate OUTRIGHT — unlike
# `legal`, `content` must never join the bootstrap floor even when this
# PR's own diff would otherwise cause `review_panel.select_panel` to
# select it (its `forward_looking=True`/`min_changed_files=5` opt-in path
# on a >=5-file diff). `resolve_lenses` enforces this by stripping
# `content` out of the diff-derived floor whenever `panel_all` is set —
# see its docstring. `content` remains fully available to the normal
# non-bootstrap pipeline (`swarm_dispatch.py`'s canary-lane dispatch),
# which calls `review_panel.select_panel` directly and never goes through
# this tool or `BOOTSTRAP_PANEL_LENSES`.
BOOTSTRAP_PANEL_LENSES: frozenset[str] = frozenset(
    {"pm", "arch", "ux", "qa", "security"}
)

# Lenses bootstrap mode excludes outright, even when review_panel.select_panel
# would otherwise derive them into a PR's floor for this diff (ateles#1317,
# operator ruling 2026-09-26). Currently just Corvus/content — see the
# BOOTSTRAP_PANEL_LENSES comment above. Kept as a separate named constant
# (rather than inlined into resolve_lenses) so a future bootstrap-only
# exclusion has an obvious place to be added, and so tests can pin the exact
# set the way TestBootstrapPanelLensesConstant already pins the roster.
BOOTSTRAP_EXCLUDED_LENSES: frozenset[str] = frozenset({"content"})

_FAILING_CHECK_CONCLUSIONS = ("failure", "timed_out", "cancelled", "action_required")

# ── Unschedulable non-required checks (operator ruling, 2026-09-25) ─────────
#
# Until a self-hosted runner exists for a check, a check that is NOT required
# by branch protection on the base branch and that CANNOT be scheduled (it is
# still queued/pending and no online runner in the repo has every label its
# job needs) is reported as "not run (no runner)" instead of failing the gate.
# Once a matching runner is online the check binds again automatically, since
# the runner lookup then finds it schedulable. Every rule below fails closed:
# anything this tool cannot read keeps the check blocking.
NOT_RUN_NO_RUNNER = "not run (no runner)"

# Check-run statuses that mean "waiting to be scheduled". `in_progress` means a
# runner already picked the job up; `waiting` is an environment approval, not
# a missing runner; neither qualifies.
_UNSCHEDULED_STATUSES = frozenset({"queued", "pending"})

# Known-unprovisioned self-hosted runner label sets. Consulted ONLY when the
# repo's runner list cannot be read (`GET /actions/runners` needs admin, which
# the read token usually lacks): a job whose runs-on label set EXACTLY equals
# an entry here is treated as unschedulable. Removing an entry restores
# enforcement for that check. Labels are compared case-insensitively, as
# GitHub matches them.
#
# Empty since ateles#1333: its one entry was the canonical rule inventory's
# self-hosted job, and that workflow was removed — complete cross-store
# measurement is now a private milestone audit run locally
# (docs/runbooks/rule_inventory_audit.md), so no PR-gating job needs a
# self-hosted runner. Do not re-add an entry to excuse a new self-hosted PR
# check; `test_public_rule_sources.py` fails on such a job.
KNOWN_UNPROVISIONED_RUNNER_LABEL_SETS: frozenset[frozenset[str]] = frozenset()

# GitHub-hosted runner labels never qualify: a job on a hosted runner that sits
# queued is a GitHub capacity problem, not a missing runner. A qualifying job
# must carry `self-hosted` and none of these hosted image labels.
_GITHUB_HOSTED_LABEL_RE = re.compile(
    r"^(ubuntu|windows|macos)-(latest|\d[\w.-]*)$", re.I
)

GITHUB_API = "https://api.github.com"

_EXPECTATION_MARKER_RE = re.compile(
    rf"\*\*{EXPECTATION_MARKER} \((?P<lens>[\w-]+)\)\*\* — what "
    r"(?P<agent>\w+) will verify",
    re.I,
)


def _github_headers(repo: str | None = None) -> dict[str, str]:
    """Headers for this tool's READ-ONLY GitHub calls.

    The swarm App's short-lived installation token when the App is configured
    (it already is wherever --apply works), else the migration-window env
    fallback. Replacing the long-lived agent PAT here is step 1 of
    credential_rotation_split_by_issuer; the fallback names go when the PATs
    are revoked.
    """
    token = _github_app_token.read_token(
        repo, fallback_env=("GITHUB_TOKEN", "ATELES_AGENT_PAT")
    )
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class LensOutcome:
    """One lens's evaluation against the PR's current head."""

    def __init__(
        self,
        lens: str,
        *,
        agent: str,
        head_matched: bool,
        verdict: str | None,
        passed: bool,
        comment_url: str = "",
        reason: str = "",
        carried_from: str = "",
    ) -> None:
        self.lens = lens
        self.agent = agent
        # Full head SHA of an EARLIER round whose sign-off stands in for this
        # head's; "" when the lens reviewed the current head itself.
        self.carried_from = carried_from
        self.head_matched = head_matched
        self.verdict = verdict
        self.passed = passed
        self.comment_url = comment_url
        self.reason = reason


class CheckOutcome:
    def __init__(
        self, name: str, state: str, passed: bool, *, not_run: bool = False, reason: str = ""
    ) -> None:
        self.name = name
        self.state = state
        self.passed = passed
        # True only for a non-required check that could not be scheduled (see
        # NOT_RUN_NO_RUNNER); `reason` then says why it did not bind.
        self.not_run = not_run
        self.reason = reason


async def _fetch_pr(client: httpx.AsyncClient, repo: str, pr: int) -> dict:
    resp = await client.get(f"{GITHUB_API}/repos/{repo}/pulls/{pr}", headers=_github_headers(repo))
    resp.raise_for_status()
    return resp.json() or {}


async def _fetch_issue_comments(client: httpx.AsyncClient, repo: str, pr: int) -> list[dict]:
    """All issue-API comments on the PR (GitHub serves PR comments there)."""
    out: list[dict] = []
    page = 1
    while True:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{pr}/comments",
            headers=_github_headers(repo),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        rows = resp.json() or []
        out.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    return out


def _latest_matching_comment(
    comments: list[dict], *, marker: str, authors: frozenset[str]
) -> dict | None:
    """The LATEST comment a swarm identity wrote that carries *marker*, or None.

    Latest, not first: a lens can post more than once for the same head (a
    correction after its own comment), and the most recent word is the one
    that should govern. Comments are returned by GitHub in creation order, so
    scanning in reverse gives the latest.

    Only a comment whose author is in *authors* (`lens_authors`) counts: the
    marker is text any account can post. `authors` is required, with no
    default, so a caller cannot read lens comments without deciding whose
    they are; an empty set matches nothing.
    """
    for c in reversed(comments):
        if marker in (c.get("body") or "") and lens_authors.is_swarm_comment(c, authors):
            return c
    return None


def _matching_comments(
    comments: list[dict], *, marker: str, authors: frozenset[str]
) -> list[dict]:
    """EVERY swarm-written comment carrying *marker*, in creation order.

    Same author rule as `_latest_matching_comment`.
    """
    return [
        c
        for c in comments
        if marker in (c.get("body") or "") and lens_authors.is_swarm_comment(c, authors)
    ]


class RequiredLens:
    """One lens in the derived floor, with why it is required."""

    def __init__(self, lens: str, reason: str) -> None:
        self.lens = lens
        self.reason = reason


class NonRequiredBlock:
    """A lens the derived floor did NOT require, but which posted a
    REQUEST_CHANGES / [BLOCKING] verdict on the PR's CURRENT head anyway.

    ateles#1293 (2026-09-26): this tool approved a PR whose required floor
    was only {pm, qa}, while Waxwing (arch) — not required for that diff —
    had posted a live REQUEST_CHANGES with a [BLOCKING] finding on the same
    head ten minutes earlier. The tool never looked at arch's comment at
    all, because arch was not in the derived floor. A live blocking verdict
    from ANY lens must refuse the approval, whether or not the panel-
    assembly logic (`review_panel.select_panel`) happened to seat that lens
    for this diff — the floor decides who MUST clear, not who is allowed to
    object.
    """

    def __init__(
        self,
        lens: str,
        *,
        agent: str,
        verdict: str | None,
        comment_url: str,
        reason: str = "",
    ) -> None:
        self.lens = lens
        self.agent = agent
        self.verdict = verdict
        self.comment_url = comment_url
        # Why this lens blocks, precisely: the objecting verdict token,
        # "unreadable verdict", "[BLOCKING] finding", or
        # "earlier objection not retired by a clearing verdict".
        self.reason = reason


async def _changed_files(client: httpx.AsyncClient, repo: str, pr: int) -> list[str]:
    """Filenames touched by the PR — same endpoint `swarm_dispatch._changed_files` reads."""
    out: list[str] = []
    page = 1
    while True:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr}/files",
            headers=_github_headers(repo),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        rows = resp.json() or []
        out.extend(f.get("filename", "") for f in rows)
        if len(rows) < 100:
            break
        page += 1
    return out


async def _preregistered_gate_contributors(
    client: httpx.AsyncClient, repo: str, issue_number: int
) -> set[str]:
    """Agents that pre-registered a review_expectation on the parent issue.

    Same marker and same regex `swarm_dispatch._preregistered_expectations`
    reads (`EXPECTATION_MARKER`, imported, not retyped) — reproduced here
    rather than calling the dispatcher method directly because that method is
    an instance method requiring a live `SwarmDispatcher` (Notifier, gws
    config, …) this standalone tool has no reason to construct.
    """
    out: set[str] = set()
    page = 1
    while True:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{issue_number}/comments",
            headers=_github_headers(repo),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        rows = resp.json() or []
        for c in rows:
            m = _EXPECTATION_MARKER_RE.search(c.get("body") or "")
            if m:
                out.add(m.group("agent").lower())
        if len(rows) < 100:
            break
        page += 1
    return out


def _why_lens_selected(lens: Lens, *, changed_files: list[str], gate_contributors: set[str]) -> str:
    """Human-readable reason `select_panel` would have included *lens*.

    Descriptive only — `select_panel` itself already decided inclusion; this
    just explains which of its OR'd conditions fired, for the dry-run table.
    """
    if lens.always:
        return "always-on lens"
    if lens.agent in gate_contributors:
        return f"{lens.agent} pre-registered a review_expectation on the linked issue"
    matched_patterns = [
        pattern
        for pattern in lens.diff_patterns
        for path in changed_files
        if re.search(pattern, path)
    ]
    if matched_patterns:
        return f"diff matches {matched_patterns[0]!r}"
    if lens.gate:
        return f"owns a gate still pending on the linked issue ({lens.gate})"
    return "selected by the panel (reason not otherwise categorized)"


def _panel_all_default() -> bool:
    """Whether `--panel` defaults to `all` rather than `required`.

    Bootstrap mode (agent_policy `ent_d0f1a840e549b3b299f62397`, CLAUDE.md
    "Software work runs in bootstrap mode"): while the Ateles foundation is
    not yet in place, the session is the routine approver in place of the
    operator, so the gate this tool enforces has to be the stricter one —
    every lens signed off, not merely the ones `review_panel.select_panel`
    would have seated for this diff. There is no existing flag or env var
    representing bootstrap mode in code (it lives only as a Neotoma
    agent_policy + CLAUDE.md mirror), so this tool defines its own:
    `ATELES_APPROVE_PANEL_REQUIRED_ONLY=1` opts OUT of the bootstrap-mode
    default and restores the pre-existing derived-floor-only behaviour —
    set it only once bootstrap mode has ended (the operator judges that, per
    the policy's own "Exit" clause) or for a deliberate one-off run outside
    it. `--panel` on the command line always overrides this default either
    way.
    """
    return os.environ.get("ATELES_APPROVE_PANEL_REQUIRED_ONLY", "") != "1"


def _always_required_lenses() -> list[Lens]:
    """Every lens `review_panel.LENSES` marks `always=True` — taken from the
    live registry, never a hard-coded name list, so a future lens the
    registry marks always-on is covered automatically. Currently `pm` and
    `qa`, but this function must never assume that."""
    return [lens for lens in LENSES if lens.always]


def _validate_panel_max(raw: str) -> int:
    """Parse and validate `APIS_PANEL_MAX`, or raise a clear config-error.

    round-2 Falco finding on PR #1266: `select_panel(..., max_panel=0)`
    returns `[]` — even the `always=True` lenses — because it slices the
    assembled panel with Python's own `list[:max_panel]` semantics, and a
    NEGATIVE `max_panel` is worse: `list[:-1]` silently drops from the END
    rather than emptying the list, so `APIS_PANEL_MAX=-1` drops exactly one
    always-on lens (whichever sorts last) instead of refusing outright —
    `all(o.passed for o in [])` and `all(o.passed for o in <n-1 lenses>)` are
    both silent, wrong "pass"es, just of different sizes. A value below the
    number of always-required lenses can NEVER produce a panel that includes
    all of them (`select_panel` slices AFTER assembling gate-owners +
    security + always-on + other-blocking + forward-looking, so nothing this
    tool's own union step does afterward can recover a lens `select_panel`
    truncated away — see the union step below, which is defense in depth for
    a DIFFERENT failure mode: `select_panel` itself changing behaviour, not
    this one). So this is validated as a configuration ERROR, not silently
    capped or silently worked around.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"APIS_PANEL_MAX={raw!r} is not a valid integer — refusing rather than "
            "silently falling back to a default"
        ) from None
    floor_count = len(_always_required_lenses())
    if value < floor_count:
        raise RuntimeError(
            f"APIS_PANEL_MAX={value} is below the number of always-required lenses "
            f"({floor_count}: {', '.join(sorted(lens.lens for lens in _always_required_lenses()))}) "
            "— a panel this small can never include every always-on lens, so this is a "
            "configuration error, not a value to silently cap or work around"
        )
    return value


async def derive_required_lenses(
    client: httpx.AsyncClient, *, repo: str, pr: int, pr_body: str
) -> list[RequiredLens]:
    """The required-lens FLOOR for this PR: `review_panel.select_panel`'s own
    panel-assembly logic, applied to this PR's changed files and linked
    issue — never a hand-typed default.

    This is the same function `swarm_dispatch.py`'s dispatcher calls
    (`select_panel(gate_contributors=..., changed_files=..., ...)`) to
    assemble a PR's review panel, imported and called here rather than
    reimplemented, so this tool's idea of "required" cannot silently diverge
    from the pipeline's (round-1 pm/arch finding on PR #1266: the previous
    `DEFAULT_LENSES` was a hand-typed tuple that already disagreed with
    `PRE_IMPL_GATES` on `ux`). `max_panel` is validated (see
    `_validate_panel_max`) rather than passed through raw — a misconfigured
    or malicious `APIS_PANEL_MAX` must be a refusal, never a silently
    emptied or truncated panel (round-2 Falco finding: `APIS_PANEL_MAX=0`
    made `select_panel` return `[]`, even dropping the always-on lenses, so
    `all(o.passed for o in [])` vacuously passed with zero lenses reviewed).

    Defense in depth beyond validating the input: the always-required lenses
    (`_always_required_lenses()`, taken from the registry) are UNIONED into
    the result after calling `select_panel`, never trusted to have survived
    the cap alone. This guards a different failure mode than the input
    validation above — `select_panel`'s own internal logic changing in a way
    that drops an always-on lens even for a valid `max_panel` — so both
    layers stay even though a validated `max_panel` should already make this
    redundant today.
    """
    panel_max = _validate_panel_max(os.environ.get("APIS_PANEL_MAX", "6"))

    changed_files = await _changed_files(client, repo, pr)
    parent_issue = SwarmDispatcher._parent_issue_number(pr_body, repo)

    gate_contributors: set[str] = set()
    if parent_issue is not None:
        gate_contributors = await _preregistered_gate_contributors(client, repo, parent_issue)

    panel = select_panel(
        gate_contributors=gate_contributors,
        changed_files=changed_files,
        max_panel=panel_max,
    )

    # Defense in depth: union the always-on floor back in by lens name, in
    # case select_panel's own behaviour ever changes. Order: always-on
    # lenses first (they are the true floor), then whatever select_panel
    # additionally selected, deduplicated by lens name.
    by_name: dict[str, Lens] = {lens.lens: lens for lens in _always_required_lenses()}
    for lens in panel:
        by_name.setdefault(lens.lens, lens)

    return [
        RequiredLens(
            lens.lens,
            _why_lens_selected(
                lens, changed_files=changed_files, gate_contributors=gate_contributors
            ),
        )
        for lens in by_name.values()
    ]


async def evaluate_lens(
    client: httpx.AsyncClient,
    *,
    repo: str,
    pr: int,
    head_sha: str,
    comments: list[dict],
    lens: str,
    authors: frozenset[str],
    diff_derived: bool = True,
) -> LensOutcome:
    """Evaluate one required lens's verdict on `head_sha`.

    `diff_derived` says whether *this specific PR's diff/issue* actually
    pulled `lens` into the required floor (`derive_required_lenses` /
    `review_panel.select_panel`), as opposed to it being required only
    because `--panel all`/`--lenses` widened the floor past what this diff
    needs. Accipiter's ux review on PR #1303 round 1: the "no comment
    carries {marker!r}" reason string read identically for "this lens is
    just running slow" and "this lens is not part of the bootstrap roster
    for this diff and will never comment" — a user reading the per-lens
    table had no way to tell "wait" from "this will never resolve." This
    distinguishes the two and says what action actually clears the row.
    """
    agent = LENS_AGENTS.get(lens, "")
    if not agent:
        return LensOutcome(
            lens, agent="", head_matched=False, verdict=None, passed=False,
            reason=f"unknown lens (not in LENS_AGENTS: {sorted(LENS_AGENTS)})",
        )

    marker = compose_lens_review_marker(lens, head_sha)
    comment = _latest_matching_comment(comments, marker=marker, authors=authors)
    if comment is None:
        if diff_derived:
            reason = (
                f"no comment carries {marker!r} yet — {lens} ({agent}) is "
                "part of this diff's required floor and has not reviewed "
                "the current head. Wait for it to review, or dispatch it "
                "yourself and re-run once it has posted."
            )
        else:
            reason = (
                f"no comment carries {marker!r}, and never will on its own — "
                f"{lens} ({agent}) is required only because --panel all/"
                "--lenses widened the floor past what this diff needs; "
                "nothing in the normal pipeline dispatches this lens for "
                "this PR. To clear this row: dispatch the lens yourself and "
                "have it post a review, or drop it from the required set "
                "(--panel required, or omit it from --lenses)."
            )
        if not authors:
            # The real cause is identity, not a slow lens: say so instead of
            # telling the operator to wait for a review that may already exist.
            reason = (
                "no swarm lens-comment identity could be resolved, so no comment "
                f"is read as a verdict, including {lens} ({agent})'s. Set "
                f"{lens_authors.ENV_AUTHORS} to the account(s) the swarm posts as, "
                "then re-run."
            )
        return LensOutcome(
            lens, agent=agent, head_matched=False, verdict=None, passed=False,
            reason=reason,
        )

    body = comment.get("body") or ""
    verdict = lens_own_verdict(body, lens_agent=agent)
    passed = sign_off_is_warranted(body, lens_agent=agent)
    reason = ""
    if not passed:
        if verdict is None:
            reason = "no readable verdict at the fixed header/verdict position"
        elif verdict not in {"signed_off", "approve"}:
            reason = f"verdict is {verdict!r}, not a clearing verdict"
        else:
            reason = "a blocking verdict token or a [BLOCKING] finding appears in the body"

    return LensOutcome(
        lens,
        agent=agent,
        head_matched=True,
        verdict=verdict,
        passed=passed,
        comment_url=comment.get("html_url", ""),
        reason=reason,
    )


async def carry_earlier_signoffs(
    client: httpx.AsyncClient,
    *,
    repo: str,
    base_ref: str,
    head_sha: str,
    comments: list[dict],
    lens_outcomes: list[LensOutcome],
    authors: frozenset[str],
) -> list[LensOutcome]:
    """Replace a failing lens outcome with a carried sign-off where that is safe.

    Operator ruling `rereview_only_blockers_and_touched_areas` (2026-09-29). A
    lens with no verdict on the current head clears on an EARLIER head's verdict
    only when all three hold: it signed off there (its latest verdict, no
    blocking verdict at that head), it did not block, and the fix's own delta
    since that head (`review_delta.interdiff`) touches none of its areas
    (`review_carry.lenses_touched`, fail-closed: an unmapped file touches every
    lens). An unreadable delta carries nothing. A lens that reviewed the
    current head is never carried, whatever it said.
    """
    # Only a swarm identity's comments are verdicts, earlier heads included.
    records = lens_records(lens_authors.scope_lens_comments(comments, authors))
    pending = [o for o in lens_outcomes if not o.passed and not o.head_matched]
    if not pending:
        return lens_outcomes
    heads = review_carry.candidate_heads([o.lens for o in pending], records, head_sha)
    deltas = {
        old: await review_carry.fetch_interdiff(
            client,
            repo=repo,
            base_ref=base_ref,
            old_head=old,
            new_head=head_sha,
            headers=_github_headers(repo),
        )
        for old in heads
    }
    selection = review_carry.select_rerun(
        [o.lens for o in pending], records, head_sha, deltas
    )
    out: list[LensOutcome] = []
    for outcome in lens_outcomes:
        carried = selection.carried.get(outcome.lens)
        if carried is None or outcome not in pending:
            if outcome in pending and outcome.lens in selection.reasons:
                # Keep WHY it could not be carried next to why it failed, so a
                # refusal reads "the fix touched ux's area", not just "no verdict".
                outcome.reason = (
                    f"{outcome.reason} Not carried from an earlier head: "
                    f"{selection.reasons[outcome.lens]}."
                )
            out.append(outcome)
            continue
        out.append(
            LensOutcome(
                outcome.lens,
                agent=outcome.agent,
                head_matched=False,
                verdict=carried.verdict or None,
                passed=True,
                comment_url=carried.url,
                reason=(
                    f"carried from {carried.head[:7]}: the fix since then touched "
                    "none of its areas"
                ),
                carried_from=carried.head,
            )
        )
    return out


_OBJECTING_VERDICTS = frozenset({"request_changes", "blocked"})
EARLIER_OBJECTION_REASON = "earlier objection not retired by a clearing verdict"


def _objection_reason(body: str, *, lens_agent: str) -> str | None:
    """Why one comment from a lens OUTSIDE the required set objects, or None.

    Read with the same functions `evaluate_lens` uses for a required lens
    (`sign_off_is_warranted`, `lens_own_verdict`, the blocking-token and
    `[BLOCKING]` vetoes), so the two kinds of lens differ only in what
    absence of a clearing verdict means: for a required lens it fails the
    gate; for a non-required lens it does not.

    Not an objection (None), and only these two shapes:
      - a clearing verdict (`sign_off_is_warranted`: `APPROVE` / `SIGNED_OFF`
        at the fixed position, no blocking token, no `[BLOCKING]` finding);
      - an explicit, parsed `COMMENT` at the fixed position with no blocking
        token and no `[BLOCKING]` finding.
    Everything else objects: `REQUEST_CHANGES`, `BLOCKED`, a blocking token or
    `[BLOCKING]` finding anywhere in the body, AND any reply whose verdict
    cannot be read (missing, misplaced, unrecognised, a second verdict line).
    Unknown is not clear (`docs/foundation/principles.md#5-fail-closed-on-the-
    field-that-carries-the-safety-meaning`).
    """
    if sign_off_is_warranted(body, lens_agent=lens_agent):
        return None
    verdict = lens_own_verdict(body, lens_agent=lens_agent)
    if verdict in _OBJECTING_VERDICTS:
        return verdict.upper()
    if body_has_blocking_findings(body):
        return "[BLOCKING] finding"
    if output_has_blocking_verdict(body):
        return "blocking verdict token in body"
    if verdict is None:
        return "unreadable verdict"
    if verdict == "comment":
        return None
    return f"verdict {verdict!r} is not a clearing verdict"


def _non_required_lens_block(
    bodies: list[str], *, lens_agent: str
) -> tuple[str, str | None] | None:
    """(reason, verdict) when a non-required lens blocks on this head, else None.

    `bodies` is every comment the lens posted for the CURRENT head, in order.
    The lens blocks when its LATEST comment objects (or cannot be read), or
    when an EARLIER one objected and no comment after that objection is a
    clearing verdict: only an explicit clearing verdict (`APPROVE` /
    `SIGNED_OFF`) retires an objection; a later plain `COMMENT` does not.
    """
    if not bodies:
        return None
    reasons = [_objection_reason(b, lens_agent=lens_agent) for b in bodies]
    if reasons[-1] is not None:
        return reasons[-1], lens_own_verdict(bodies[-1], lens_agent=lens_agent)
    last_objection = max((i for i, r in enumerate(reasons) if r is not None), default=None)
    if last_objection is None:
        return None
    for later in bodies[last_objection + 1 :]:
        if sign_off_is_warranted(later, lens_agent=lens_agent):
            return None
    return EARLIER_OBJECTION_REASON, lens_own_verdict(bodies[-1], lens_agent=lens_agent)


def find_non_required_blocks(
    *,
    comments: list[dict],
    head_sha: str,
    required_lenses: set[str],
    authors: frozenset[str],
) -> list[NonRequiredBlock]:
    """Every lens NOT in `required_lenses` that actively objects on the
    CURRENT head (`compose_lens_review_marker`): its latest comment is
    REQUEST_CHANGES, BLOCKED, carries a blocking token / `[BLOCKING]` finding
    anywhere in the body, or has a verdict that cannot be read (fail closed);
    or an earlier comment on this head objected and no later comment is a
    clearing verdict (`APPROVE` / `SIGNED_OFF`).

    Reads every lens registered in `LENS_AGENTS` — not only the derived
    floor — because ateles#1293 was approved with 'lenses: ALL PASS' while a
    non-required lens's live blocking verdict on the SAME head sat unread.
    Parsed with `swarm_dispatch.lens_own_verdict`, the identical fixed-
    position parser `evaluate_lens` already uses for required lenses, so a
    non-required lens cannot be held to a looser or stricter reading than a
    required one (`_objection_reason`). A lens with no comment on this head,
    a lens whose comments read as a clearing verdict, and a lens that only
    ever posted an explicit `COMMENT` with no blocking token (ateles#1394:
    the automatic pipeline's content lens) are not blocks — this function
    reports ONLY lenses that actively object, never absence or a plain
    non-blocking comment. A `COMMENT` posted AFTER an objection does not
    retire it. A comment edited in place shows its current body, which is read
    as the lens's updated verdict.

    Only comments written by a swarm identity (`authors`) are read: a
    lens-marked comment from any other account neither objects nor clears.
    """
    blocks: list[NonRequiredBlock] = []
    for lens, agent in LENS_AGENTS.items():
        if lens in required_lenses:
            continue
        marker = compose_lens_review_marker(lens, head_sha)
        matching = _matching_comments(comments, marker=marker, authors=authors)
        if not matching:
            continue
        found = _non_required_lens_block(
            [c.get("body") or "" for c in matching], lens_agent=agent
        )
        if found is None:
            continue
        reason, verdict = found
        blocks.append(
            NonRequiredBlock(
                lens,
                agent=agent,
                verdict=verdict,
                comment_url=matching[-1].get("html_url", ""),
                reason=reason,
            )
        )
    return blocks


class UnadmittedObjection:
    """An objecting lens comment at the current head from an account that is
    not an admitted swarm identity. It can only HOLD approval: a clearing
    verdict from such an account is never read."""

    def __init__(
        self, lens: str, *, agent: str, author: str, why: str, comment_url: str
    ) -> None:
        self.lens = lens
        self.agent = agent
        self.author = author
        self.why = why
        self.comment_url = comment_url


def find_unadmitted_objections(
    *, comments: list[dict], head_sha: str, authors: frozenset[str]
) -> list[UnadmittedObjection]:
    """Every lens-marked comment at the CURRENT head that objects explicitly
    (`REQUEST_CHANGES` / `BLOCKED` / `[BLOCKING]` / a blocking token) and was
    written by an account that is NOT in `authors`.

    The author filter keeps such a comment from clearing anything; it must not
    also let it vanish when it is real. A swarm account missing from the
    configured set would otherwise have a genuine objection read as no
    objection at all. Reads every lens in `LENS_AGENTS`, required or not. An
    unreadable verdict is not an objection here (only a plain one holds).
    """
    out: list[UnadmittedObjection] = []
    for lens, agent in LENS_AGENTS.items():
        marker = compose_lens_review_marker(lens, head_sha)
        for c in comments:
            if marker not in (c.get("body") or ""):
                continue
            if lens_authors.is_swarm_comment(c, authors):
                continue
            why = lens_explicit_objection(c.get("body") or "", lens_agent=agent)
            if why:
                out.append(
                    UnadmittedObjection(
                        lens,
                        agent=agent,
                        author=lens_authors.comment_author(c) or "an unreadable author",
                        why=why,
                        comment_url=c.get("html_url", ""),
                    )
                )
    return out


_IGNORED_LIST_CAP = 5


def _print_ignored(ignored: list[dict], authors: frozenset[str]) -> None:
    """Say which lens-marked comments were not read, and why, in terms of what
    the operator can do about it (capped like the dispatcher's log)."""
    if not ignored:
        return
    if not authors:
        print(
            f"{len(ignored)} lens-marked comment(s) were NOT read because no swarm "
            f"identity could be resolved (they may be the swarm's own). Set "
            f"{lens_authors.ENV_AUTHORS} to the account(s) the swarm posts as, then re-run:"
        )
    else:
        print(
            f"ignored {len(ignored)} lens-marked comment(s) not written by a "
            "configured swarm identity:"
        )
    for c in ignored[:_IGNORED_LIST_CAP]:
        print(
            f"  - {lens_authors.claimed_marker(c.get('body')) or 'a lens marker'} "
            f"by {lens_authors.comment_author(c) or 'an unreadable author'}: "
            f"{c.get('html_url') or c.get('id')}"
        )
    if len(ignored) > _IGNORED_LIST_CAP:
        print(f"  (+{len(ignored) - _IGNORED_LIST_CAP} more)")
    if authors:
        print(
            f"  If one of these accounts is the swarm's, add it to "
            f"{lens_authors.ENV_AUTHORS} and re-run."
        )


def _contexts_from_required_status_checks(payload: object) -> set[str] | None:
    """Context names from a classic-protection `required_status_checks` object
    (`contexts` plus `checks[].context`), or None if the shape is unreadable."""
    if not isinstance(payload, dict):
        return None
    contexts = payload.get("contexts")
    checks = payload.get("checks")
    if contexts is None and checks is None:
        return None
    out: set[str] = set()
    for c in contexts or []:
        if not isinstance(c, str):
            return None
        out.add(c)
    for c in checks or []:
        if not isinstance(c, dict) or not isinstance(c.get("context"), str):
            return None
        out.add(c["context"])
    return out


async def fetch_required_check_contexts(
    client: httpx.AsyncClient, *, repo: str, base_ref: str
) -> set[str] | None:
    """Check contexts branch protection REQUIRES on *base_ref*, or None when
    that cannot be established — and None means every check is treated as
    required (fail closed).

    Two sources, unioned, because either can require a check:
      - classic branch protection: the protection API
        (`branches/{base}/protection/required_status_checks`, admin only),
        falling back to the `protection` summary `GET branches/{base}` serves
        to read-only tokens — the dedicated endpoint answers 404 both for
        "unprotected" and for "no admin access", so its 404 alone decides
        nothing;
      - repository rulesets: `rules/branches/{base}`, `required_status_checks`
        rules.
    Any read that fails or returns an unexpected shape returns None.
    """
    if not base_ref:
        return None
    headers = _github_headers(repo)
    try:
        required: set[str] = set()

        prot = await client.get(
            f"{GITHUB_API}/repos/{repo}/branches/{base_ref}/protection/required_status_checks",
            headers=headers,
        )
        if prot.status_code == 200:
            classic = _contexts_from_required_status_checks(prot.json())
            if classic is None:
                return None
        else:
            branch = await client.get(
                f"{GITHUB_API}/repos/{repo}/branches/{base_ref}", headers=headers
            )
            if branch.status_code != 200:
                return None
            data = branch.json() or {}
            protected = data.get("protected")
            if protected is False:
                classic = set()
            elif protected is True:
                protection = data.get("protection")
                if not isinstance(protection, dict):
                    return None
                classic = _contexts_from_required_status_checks(
                    protection.get("required_status_checks")
                )
                if classic is None:
                    return None
            else:
                return None
        required |= classic

        page = 1
        while True:
            rules_resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/rules/branches/{base_ref}",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            if rules_resp.status_code != 200:
                return None
            rules = rules_resp.json()
            if not isinstance(rules, list):
                return None
            for rule in rules:
                if not isinstance(rule, dict):
                    return None
                if rule.get("type") != "required_status_checks":
                    continue
                params = rule.get("parameters") or {}
                for c in params.get("required_status_checks") or []:
                    if not isinstance(c, dict) or not isinstance(c.get("context"), str):
                        return None
                    required.add(c["context"])
            if len(rules) < 100:
                break
            page += 1
        return required
    except Exception:
        return None


async def _fetch_job_labels(
    client: httpx.AsyncClient, *, repo: str, job_id: object
) -> list[str] | None:
    """The runs-on labels of an Actions job (a check-run's id IS its job id),
    or None when unreadable. None keeps the check blocking."""
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        return None
    try:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}", headers=_github_headers(repo)
        )
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        # The job itself must also still be waiting for a runner.
        if str(payload.get("status") or "") not in _UNSCHEDULED_STATUSES:
            return None
        labels = payload.get("labels")
        if not isinstance(labels, list) or not labels or not all(
            isinstance(x, str) and x.strip() for x in labels
        ):
            return None
        return [x.strip() for x in labels]
    except Exception:
        return None


async def fetch_online_runner_label_sets(
    client: httpx.AsyncClient, *, repo: str
) -> list[frozenset[str]] | None:
    """Casefolded label sets of every ONLINE self-hosted runner in *repo*, or
    None when the runner list cannot be read (it needs admin)."""
    out: list[frozenset[str]] = []
    try:
        page = 1
        while True:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/actions/runners",
                headers=_github_headers(repo),
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("runners"), list):
                return None
            runners = payload["runners"]
            for runner in runners:
                if not isinstance(runner, dict):
                    return None
                if runner.get("status") != "online":
                    continue
                names = [
                    lbl.get("name") for lbl in (runner.get("labels") or []) if isinstance(lbl, dict)
                ]
                out.append(frozenset(n.casefold() for n in names if isinstance(n, str)))
            if len(runners) < 100:
                break
            page += 1
        return out
    except Exception:
        return None


class _SchedulingContext:
    """Lazily reads, once per evaluation, what deciding "not run (no runner)"
    needs: the base branch's required contexts and the repo's online runners.
    Nothing is fetched unless a check is actually waiting to be scheduled, so
    an all-completed head costs no extra API calls."""

    _UNSET = object()

    def __init__(self, client: httpx.AsyncClient, *, repo: str, base_ref: str) -> None:
        self.client = client
        self.repo = repo
        self.base_ref = base_ref
        self._required: object = self._UNSET
        self._runners: object = self._UNSET

    async def required_contexts(self) -> set[str] | None:
        if self._required is self._UNSET:
            self._required = await fetch_required_check_contexts(
                self.client, repo=self.repo, base_ref=self.base_ref
            )
        return self._required  # type: ignore[return-value]

    async def online_runner_label_sets(self) -> list[frozenset[str]] | None:
        if self._runners is self._UNSET:
            self._runners = await fetch_online_runner_label_sets(self.client, repo=self.repo)
        return self._runners  # type: ignore[return-value]


async def _unscheduled_non_required_reason(
    ctx: _SchedulingContext, run: dict
) -> str | None:
    """Why *run* is a non-required check no runner can schedule, or None if it
    does not qualify (and so keeps its ordinary pending/failing reading).

    Qualifies only when ALL hold — each is a fail-closed step:
      1. the check-run is still waiting to be scheduled (queued/pending) —
         a completed run, including any failure conclusion, never qualifies;
      2. branch protection on the base branch was read and does NOT require
         this check's name (unreadable protection ⇒ every check required);
      3. the job's runs-on labels were read, include `self-hosted`, and name
         no GitHub-hosted image;
      4. either the runner list was read and no ONLINE runner carries every
         one of the job's labels, or — only if the runner list is unreadable —
         the job's exact label set is on KNOWN_UNPROVISIONED_RUNNER_LABEL_SETS.
    """
    if str(run.get("status") or "") not in _UNSCHEDULED_STATUSES:
        return None
    if run.get("conclusion"):
        return None
    name = (run.get("name") or "").strip()
    if not name:
        return None

    required = await ctx.required_contexts()
    if required is None:
        return None
    if name.casefold() in {r.strip().casefold() for r in required}:
        return None

    labels = await _fetch_job_labels(ctx.client, repo=ctx.repo, job_id=run.get("id"))
    if labels is None:
        return None
    wanted = frozenset(lbl.casefold() for lbl in labels)
    if "self-hosted" not in wanted:
        return None
    if any(_GITHUB_HOSTED_LABEL_RE.match(lbl) for lbl in wanted):
        return None

    label_text = ", ".join(labels)
    not_required = f"not required by branch protection on `{ctx.base_ref}`"
    runners = await ctx.online_runner_label_sets()
    if runners is None:
        if wanted not in KNOWN_UNPROVISIONED_RUNNER_LABEL_SETS:
            return None
        return (
            f"{not_required}; runner list unreadable, and its runner labels "
            f"[{label_text}] are on the known-unprovisioned allowlist"
        )
    if any(wanted <= runner for runner in runners):
        return None
    return f"{not_required}; no online runner has all of its labels [{label_text}]"


async def evaluate_checks(
    client: httpx.AsyncClient, *, repo: str, head_sha: str, base_ref: str = ""
) -> tuple[list[CheckOutcome], bool]:
    """Every required-looking check-run on *head_sha*; True iff ALL are green.

    Mirrors `swarm_dispatch._required_ci_state`'s green/failing/pending
    reading of the combined status + check-runs, but reports every run
    individually rather than collapsing to one state string, since the
    operator-facing table names each check — with one deliberate correction:
    GitHub's combined-status endpoint answers `state="pending"` whenever
    `total_count==0`, i.e. when a repo has posted NO legacy commit statuses
    at all (this repo uses the Checks API exclusively; ateles-tests.yml is a
    check-run, not a status). Treating that as a live "pending" would refuse
    every PR in a Checks-API-only repo regardless of how green its check-runs
    are — confirmed against PR #1255, whose commits/.../status returns
    state=pending, total_count=0 while every check-run is completed+success.
    So `total_count==0` is read as "no legacy statuses to consider", and the
    check-runs alone decide `all_green`. Still fails closed: zero check-runs
    AND zero legacy statuses (`total_count==0`) is treated as NOT green, since
    that means nothing has run yet, not that nothing needs to.

    Operator ruling 2026-09-25: a check-run that is still queued, is not
    required by branch protection on *base_ref*, and that no online runner
    can schedule is reported as NOT_RUN_NO_RUNNER and does not fail the gate
    (see `_unscheduled_non_required_reason` for the fail-closed conditions).
    An empty *base_ref* disables this, so every pending check blocks. If every
    check-run on the head is "not run", that is still no signal: not green.
    """
    status_resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/commits/{head_sha}/status",
        headers=_github_headers(repo),
    )
    status_resp.raise_for_status()
    status_payload = status_resp.json() or {}
    combined_state = status_payload.get("state", "")
    legacy_status_count = status_payload.get("total_count", 0) or 0

    checks_resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/commits/{head_sha}/check-runs",
        headers={**_github_headers(repo), "Accept": "application/vnd.github+json"},
    )
    checks_resp.raise_for_status()
    runs = (checks_resp.json() or {}).get("check_runs", [])

    outcomes: list[CheckOutcome] = []
    legacy_statuses_green = legacy_status_count == 0 or combined_state in ("success", "")
    all_green = legacy_statuses_green
    sched = _SchedulingContext(client, repo=repo, base_ref=base_ref)
    for run in runs:
        name = (run.get("name") or "").strip()
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            not_run_reason = (
                await _unscheduled_non_required_reason(sched, run) if base_ref else None
            )
            if not_run_reason:
                outcomes.append(
                    CheckOutcome(
                        name, NOT_RUN_NO_RUNNER, True, not_run=True, reason=not_run_reason
                    )
                )
                continue
            state = f"pending ({status})"
            passed = False
        elif conclusion in _FAILING_CHECK_CONCLUSIONS:
            state = f"failing ({conclusion})"
            passed = False
        elif conclusion in ("success", "neutral", "skipped"):
            state = conclusion
            passed = True
        else:
            state = f"unrecognized ({conclusion})"
            passed = False
        outcomes.append(CheckOutcome(name, state, passed))
        all_green = all_green and passed

    ran = [o for o in outcomes if not o.not_run]
    if not ran and legacy_status_count == 0:
        # No signal of any kind — nothing has run yet (or every check-run is
        # "not run"). Fail closed rather than approving a head CI has not
        # touched.
        all_green = False

    return outcomes, all_green


async def submit_app_approval(
    client: httpx.AsyncClient,
    *,
    repo: str,
    pr: int,
    head_sha: str,
    lens_outcomes: list[LensOutcome],
    check_outcomes: list[CheckOutcome] | None = None,
) -> dict:
    """Mint an App installation token and submit a formal APPROVE review.

    Mirrors `swarm_dispatch._emit_formal_review`'s binding-review contract
    (round-1 Falco finding on PR #1266: this function previously trusted a
    single early head fetch and the POST's own response body as proof of
    success, with no re-verification and no read-back — exactly the TOCTOU
    gap `_emit_formal_review` already closes for the dispatcher's own binding
    reviews). Every step below has a namesake in that function:

      1. re-fetch the PR immediately before posting; refuse unless it is
         still OPEN, not merged, and its head still equals *head_sha* —
         closes the window between the caller's earlier verification (lens
         comments, checks) and this POST, which is unbounded I/O away;
      2. pin `commit_id` to *head_sha* in the review POST (unchanged — this
         was already correct);
      3. refuse a self-approval: the App must not be approving a PR it
         authored;
      4. after posting, READ THE REVIEW BACK by id and confirm
         `commit_id == head_sha`, `state == "APPROVED"`, and the reviewer is
         the App's own bot identity and not the PR author — only that
         readback, never the POST response alone, is treated as proof the
         approval landed as intended.

    Raises on any failure; the caller treats a raised exception as
    "submitted nothing" per the --apply contract.
    """
    app_id = (os.environ.get("ATELES_REVIEWER_APP_ID") or "").strip()
    if not app_id or not _reviewer_app_private_key_pem():
        raise RuntimeError(
            "ATELES_REVIEWER_APP_ID and ATELES_REVIEWER_APP_PRIVATE_KEY"
            "(_PATH) must both be set to approve as the App"
        )

    token = await _mint_reviewer_app_installation_token(repo, client)
    if not token:
        raise RuntimeError(
            "could not mint a reviewer App installation token — verify the "
            "App's private key is valid PEM and the App is installed on this repo"
        )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }

    # 1. Re-verify immediately before submitting: still open, unmerged, and
    # at the verified head. A push landing during the earlier lens/check
    # evaluation (multiple paginated GitHub calls, unbounded network time)
    # must not be approved silently for a head the panel no longer covers.
    pr_resp = await client.get(f"{GITHUB_API}/repos/{repo}/pulls/{pr}", headers=headers)
    if pr_resp.status_code >= 400:
        raise RuntimeError(
            f"pre-submit PR re-fetch failed: HTTP {pr_resp.status_code} — refusing to approve"
        )
    pr_data = pr_resp.json() or {}
    pr_state = str(pr_data.get("state") or "").lower()
    if pr_state != "open" or pr_data.get("merged_at"):
        raise RuntimeError(
            f"PR is no longer open (state={pr_state!r}, merged_at={pr_data.get('merged_at')!r}) "
            "— refusing to approve a closed/merged PR"
        )
    live_head = _normalise_full_sha(str((pr_data.get("head") or {}).get("sha") or ""))
    if live_head != head_sha:
        raise RuntimeError(
            f"head moved between verification and submission (verified={head_sha}, "
            f"live={live_head or 'unreadable'}) — refusing to approve a head the panel "
            "did not clear"
        )
    pr_author = str((pr_data.get("user") or {}).get("login") or "")
    if not pr_author:
        raise RuntimeError("could not resolve PR author from the pre-submit re-fetch — refusing")

    body_lines = [
        "Approved by the swarm App — every required review lens cleared the "
        f"PR's current head `{head_sha}`.",
        "",
    ]
    carried_outcomes = [o for o in lens_outcomes if o.carried_from]
    if carried_outcomes:
        body_lines[0] = (
            "Approved by the swarm App — every required review lens cleared, "
            f"{len(lens_outcomes) - len(carried_outcomes)} on the PR's current head "
            f"`{head_sha}` and {len(carried_outcomes)} carried forward from an "
            "earlier head (listed below)."
        )
    for outcome in lens_outcomes:
        if outcome.carried_from:
            body_lines.append(
                f"- **{outcome.lens}** ({outcome.agent}): CARRIED FORWARD, "
                f"`{outcome.verdict}` at head `{outcome.carried_from}` — "
                f"{outcome.comment_url}. The fix since that head touched none of "
                "this lens's areas, so it was not re-run."
            )
            continue
        body_lines.append(
            f"- **{outcome.lens}** ({outcome.agent}): `{outcome.verdict}` — {outcome.comment_url}"
        )
    not_run = [c for c in (check_outcomes or []) if c.not_run]
    if not_run:
        body_lines.append("")
    for c in not_run:
        body_lines.append(f"- check `{c.name}` did not bind — {NOT_RUN_NO_RUNNER}: {c.reason}")
    body_lines.append("")
    body_lines.append(f"head_sha={head_sha}")
    body = "\n".join(body_lines)

    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr}/reviews"
    # 2. commit_id pinned to the verified (and just re-confirmed) head.
    resp = await client.post(
        url,
        json={"event": "APPROVE", "body": body[:65000], "commit_id": head_sha},
        headers=headers,
    )
    # 3. Self-approval refusal: GitHub's own 422 for "can not approve your
    # own pull request" is the authoritative signal (mirrors
    # `_emit_formal_review`'s same-login handling) — there is no reliable
    # pre-check for an App-token principal, since Apps cannot call
    # `GET /user` to learn their own login ahead of time.
    if resp.status_code == 422:
        lower = (resp.text or "").casefold()
        if "own pull request" in lower or "your own pull" in lower:
            raise RuntimeError(
                f"refusing: the App would be approving a PR it authored (author={pr_author})"
            )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"review submission failed: HTTP {resp.status_code}: {(resp.text or '')[:240]}"
        )
    posted = resp.json() or {}
    review_id = _normalise_github_review_id(posted.get("id"))
    if not review_id:
        raise RuntimeError("review submission returned no usable review id — cannot read back")

    # 4. Read the created review back. The POST's own response body is NOT
    # treated as proof — only this independent GET is.
    readback_resp = await client.get(f"{url}/{review_id}", headers=headers)
    if readback_resp.status_code >= 400:
        raise RuntimeError(
            f"review read-back failed: HTTP {readback_resp.status_code} — approval NOT "
            "confirmed landed"
        )
    readback = readback_resp.json() or {}
    actual_review_id = _normalise_github_review_id(readback.get("id"))
    user = readback.get("user") or {}
    actual_login = str(user.get("login") or "")
    user_type = str(user.get("type") or "")
    actual_commit = _normalise_full_sha(str(readback.get("commit_id") or ""))
    actual_state = str(readback.get("state") or "").upper()

    readback_ok = (
        actual_review_id == review_id
        and actual_commit == head_sha
        and actual_state == "APPROVED"
        and bool(actual_login)
        and actual_login.casefold() != pr_author.casefold()
        and user_type.casefold() == "bot"
    )
    if not readback_ok:
        raise RuntimeError(
            "review read-back did not match expected state — approval NOT confirmed landed "
            f"(expected_head={head_sha} expected_state=APPROVED, "
            f"got commit={actual_commit or 'unreadable'} state={actual_state or 'unreadable'} "
            f"login={actual_login or 'unreadable'} user_type={user_type or 'unreadable'})"
        )

    return readback


def _print_derived_lenses(
    required: list[RequiredLens], extra: list[str], *, excluded: list[str] | None = None
) -> None:
    print()
    print("required lenses (derived from review_panel.select_panel for this diff/issue):")
    for r in required:
        print(f"  - {r.lens}: {r.reason}")
    if extra:
        print(f"added on top of the derived floor (--lenses and/or --panel all): {', '.join(extra)}")
    if excluded:
        print(
            "excluded from bootstrap mode outright (operator ruling 2026-09-26, "
            f"BOOTSTRAP_EXCLUDED_LENSES) — never evaluated or required despite "
            f"select_panel deriving it for this diff: {', '.join(excluded)}"
        )
    print()


def _print_table(lens_outcomes: list[LensOutcome], check_outcomes: list[CheckOutcome]) -> None:
    print(f"{'lens':<10} {'verdict':<14} {'head match':<11} {'pass/fail':<10} reason")
    print("-" * 80)
    for o in lens_outcomes:
        verdict_s = o.verdict or "(none)"
        head_s = "carried" if o.carried_from else ("yes" if o.head_matched else "no")
        pf = "PASS" if o.passed else "FAIL"
        print(f"{o.lens:<10} {verdict_s:<14} {head_s:<11} {pf:<10} {o.reason}")
    print()
    if not check_outcomes:
        print("checks: none reported for this head")
    else:
        print(f"{'check':<40} {'state':<20} pass/fail")
        print("-" * 80)
        for c in check_outcomes:
            pf = "NOT RUN" if c.not_run else ("PASS" if c.passed else "FAIL")
            print(f"{c.name:<40} {c.state:<20} {pf}")
        for c in check_outcomes:
            if c.not_run:
                print(f"  {c.name}: did not bind — {c.reason}")
    print()


async def resolve_lenses(
    client: httpx.AsyncClient,
    *,
    repo: str,
    pr: int,
    pr_body: str,
    extra_lenses: list[str],
    panel_all: bool = False,
) -> tuple[list[str], list[RequiredLens], list[str]]:
    """The lens set the tool will require: the derived floor plus `--lenses`
    additions, and — with `panel_all` — the bootstrap-mode panel
    (`BOOTSTRAP_PANEL_LENSES`) rather than only the derived floor.

    `--lenses` can only ADD — it is unioned onto the derived floor, never
    used to shrink it, so a caller can never accidentally (or deliberately)
    drop a lens `review_panel.select_panel` itself would require for this
    diff/issue. `panel_all` is a separate, stronger union: bootstrap mode
    (agent_policy `ent_d0f1a840e549b3b299f62397`) requires the five lenses
    the bootstrap panel actually dispatches — pm, arch, ux, qa, security —
    to have signed off on the current head, regardless of what this diff's
    panel-assembly logic would have seated. This is `BOOTSTRAP_PANEL_LENSES`,
    NOT `list(LENS_AGENTS)`: the latter also contains `legal`, which
    bootstrap mode's own roster never dispatches for an ordinary diff
    (round-1 review on this PR: requiring it unconditionally made `--apply`
    unable to ever pass). `legal` still joins the floor for a diff that
    genuinely needs it — via `derive_required_lenses` calling
    `review_panel.select_panel`, unaffected by `panel_all` — this only
    changes what the BOOTSTRAP DEFAULT widens the floor to.

    `content`/Corvus is handled differently from `legal`: it is not merely
    absent from `BOOTSTRAP_PANEL_LENSES` (so `panel_all` never ADDS it) — it
    is actively EXCLUDED (`BOOTSTRAP_EXCLUDED_LENSES`) from the diff-derived
    floor itself whenever `panel_all` is set, even though
    `derive_required_lenses` calls the SAME `review_panel.select_panel` that
    can independently select `content` as a forward-looking lens for a
    >=5-file diff (`Lens.min_changed_files=5`). Operator ruling 2026-09-26
    (ateles#1317): while bootstrap mode is active, Corvus/content must never
    be required, including by a diff that would normally pull it in. This is
    a bootstrap-only exclusion — `derive_required_lenses` itself is
    unaffected, so `--panel required` (the non-bootstrap-default path) can
    still surface `content` in `required` exactly as `review_panel.py`
    intends for the normal swarm pipeline (e.g. the swarm-canary lane, which
    never calls this tool at all and so never sees this filter).

    The reasons for the derived floor are still computed and reported
    (`required`) even for an excluded lens, so the dry-run table still
    explains WHY `select_panel` would have seated it; the panel_all
    additions are reported as additions, exactly like `--lenses`.

    Returns a third element, `actually_excluded`: the lenses this call
    stripped for being in `BOOTSTRAP_EXCLUDED_LENSES` — from the
    diff-derived floor, from `extra_lenses`, or both — so the caller can
    report an explicit `--lenses content`-style request was dropped rather
    than silently discarding it (round-2 self-review finding: an operator
    passing `--lenses content` under `--panel all` previously got no
    indication their addition was ignored).
    """
    required = await derive_required_lenses(client, repo=repo, pr=pr, pr_body=pr_body)
    excluded = BOOTSTRAP_EXCLUDED_LENSES if panel_all else frozenset()
    floor = [r.lens for r in required if r.lens not in excluded]
    all_lenses = sorted(BOOTSTRAP_PANEL_LENSES) if panel_all else []
    added = [
        lens for lens in (*extra_lenses, *all_lenses) if lens not in floor and lens not in excluded
    ]
    # De-duplicate `added` while preserving first-seen order (extra_lenses
    # before the panel_all union), since `--lenses` and panel_all can name
    # the same lens.
    seen: set[str] = set()
    deduped_added = []
    for lens in added:
        if lens not in seen:
            seen.add(lens)
            deduped_added.append(lens)

    actually_excluded = sorted(
        {r.lens for r in required if r.lens in excluded}
        | {lens for lens in extra_lenses if lens in excluded}
    )
    return floor + deduped_added, required, actually_excluded


async def run(
    repo: str,
    pr: int,
    extra_lenses: list[str],
    *,
    apply: bool,
    panel_all: bool = False,
    carry: bool = True,
) -> int:
    async with httpx.AsyncClient(timeout=30) as client:
        pr_data = await _fetch_pr(client, repo, pr)
        head_sha = _normalise_full_sha(str((pr_data.get("head") or {}).get("sha") or ""))
        if not head_sha:
            print(f"refusing: could not resolve a full 40-char head SHA for {repo}#{pr}")
            return 1
        pr_body = pr_data.get("body") or ""

        try:
            lenses, required, excluded = await resolve_lenses(
                client,
                repo=repo,
                pr=pr,
                pr_body=pr_body,
                extra_lenses=extra_lenses,
                panel_all=panel_all,
            )
        except Exception as exc:
            print(f"refusing: could not derive the required-lens set: {exc}")
            return 1

        # Hard, explicit, first-line-of-defense refusal (round-2 Falco
        # finding on PR #1266): whatever the reason a required-lens set ever
        # comes back empty — a bug in `select_panel`, a future code path
        # this tool has not anticipated, a `--lenses` computation error —
        # `all(o.passed for o in [])` is vacuously True over an empty list,
        # so an empty lens set must never be allowed to reach the pass/fail
        # computation at all. This check runs BEFORE any lens or check is
        # evaluated, and does not rely on `_validate_panel_max` or the
        # always-on union in `derive_required_lenses` alone — a second,
        # independent layer for the one invariant that must never fail
        # silently.
        if not lenses:
            print(
                "refusing: the required-lens set resolved to EMPTY — approving with zero "
                "lenses reviewed is never valid, regardless of cause"
            )
            return 1

        floor_names = {r.lens for r in required}
        added = [lens for lens in lenses if lens not in floor_names]
        _print_derived_lenses(required, added, excluded=excluded)

        comments = await _fetch_issue_comments(client, repo, pr)

        # Whose comments are lens verdicts. The head marker is text any account
        # can post on a public repo, so a lens comment counts only when a swarm
        # identity wrote it (`lens_authors`; fails closed). Resolved once for
        # the whole run so every read below agrees.
        try:
            authors = await asyncio.to_thread(lens_comment_authors)
        except Exception as exc:  # noqa: BLE001 — unreadable identities admit nobody
            print(f"lens comment identities unreadable ({exc.__class__.__name__})")
            authors = frozenset()
        _print_ignored(lens_authors.ignored_lens_comments(comments, authors), authors)

        lens_outcomes: list[LensOutcome] = []
        for lens in lenses:
            outcome = await evaluate_lens(
                client,
                repo=repo,
                pr=pr,
                head_sha=head_sha,
                comments=comments,
                lens=lens,
                authors=authors,
                diff_derived=lens in floor_names,
            )
            lens_outcomes.append(outcome)

        # ateles#1293: a lens outside the required floor can still have
        # posted a live blocking verdict on this SAME head. Read EVERY lens
        # registered in LENS_AGENTS, not only the ones `lenses` required,
        # and refuse if any of them blocks — naming the lens and linking
        # its comment. With panel_all this set is always empty (every lens
        # is already in `lenses`), but the check stays unconditional so it
        # is defense in depth rather than something panel_all could
        # silently make redundant and later regress without a failing test.
        non_required_blocks = find_non_required_blocks(
            comments=comments,
            head_sha=head_sha,
            required_lenses=set(lenses),
            authors=authors,
        )

        # A real objection from an account the configuration does not admit still
        # HOLDS approval (it can only delay; a clearing verdict from it is never
        # read), so a swarm account missing from the setting cannot make an
        # objection vanish.
        unadmitted = find_unadmitted_objections(
            comments=comments, head_sha=head_sha, authors=authors
        )

        base_ref = str((pr_data.get("base") or {}).get("ref") or "")
        if carry:
            lens_outcomes = await carry_earlier_signoffs(
                client,
                repo=repo,
                base_ref=base_ref,
                head_sha=head_sha,
                comments=comments,
                lens_outcomes=lens_outcomes,
                authors=authors,
            )
        check_outcomes, checks_green = await evaluate_checks(
            client, repo=repo, head_sha=head_sha, base_ref=base_ref
        )

        _print_table(lens_outcomes, check_outcomes)

        if non_required_blocks:
            print("non-required lenses with a LIVE BLOCKING verdict on this head:")
            for b in non_required_blocks:
                print(f"  - {b.lens} ({b.agent}): {b.reason or b.verdict} — {b.comment_url}")
            print()

        if unadmitted:
            print(
                "objecting lens comments from accounts that are not configured "
                "swarm identities (held: they can only delay approval, and a "
                "clearing verdict from them is never read):"
            )
            for u in unadmitted:
                print(
                    f"  - {u.lens} ({u.agent}) by {u.author}: {u.why} — {u.comment_url}"
                )
            print(
                f"  If the account is the swarm's, add it to "
                f"{lens_authors.ENV_AUTHORS} and re-run; otherwise resolve the "
                "objection or remove the comment."
            )
            print()

        all_lenses_pass = all(o.passed for o in lens_outcomes)
        no_non_required_blocks = not non_required_blocks
        overall_pass = (
            all_lenses_pass
            and no_non_required_blocks
            and not unadmitted
            and checks_green
        )

        print(f"head_sha={head_sha}")
        print(f"lenses: {'ALL PASS' if all_lenses_pass else 'FAIL'}")
        print(
            "non-required lens blocks: "
            f"{'NONE' if no_non_required_blocks else 'BLOCKED'}"
        )
        print(f"non-swarm objections: {'HELD' if unadmitted else 'NONE'}")
        print(f"checks: {'GREEN' if checks_green else 'NOT GREEN'}")
        print(f"overall: {'PASS' if overall_pass else 'FAIL'}")

        if not apply:
            print()
            print("dry run — no review submitted. Pass --apply to submit if the gate is clear.")
            return 0 if overall_pass else 1

        if not overall_pass:
            print()
            print("refusing to approve: gate is not clear. Submitting nothing.")
            return 1

        try:
            review = await submit_app_approval(
                client,
                repo=repo,
                pr=pr,
                head_sha=head_sha,
                lens_outcomes=lens_outcomes,
                check_outcomes=check_outcomes,
            )
        except Exception as exc:
            print()
            print(f"approval FAILED: {exc}")
            return 1

        print()
        print(f"APPROVED as the App — review id={review.get('id')} state={review.get('state')}")
        return 0


def main() -> int:
    panel_all_default = _panel_all_default()
    parser = argparse.ArgumentParser(
        description=(
            "Approve a PR as the swarm's GitHub App, only when every required "
            "review lens has cleared the PR's current head (or, for a lens the "
            "fix since its earlier sign-off did not touch, that earlier head, "
            "named in the approval; --no-carry turns this off), AND no lens of any "
            "kind carries a live REQUEST_CHANGES/[BLOCKING] verdict on that "
            "head. The required-lens floor is derived from "
            "review_panel.select_panel for this PR's diff and linked issue; "
            "--lenses can only ADD to that floor. --panel all additionally "
            "requires the five lenses bootstrap mode actually dispatches "
            "(pm, arch, ux, qa, security — BOOTSTRAP_PANEL_LENSES; legal is "
            "NOT in this set and joins the floor only when select_panel "
            "derives it for the diff; content/Corvus is excluded from "
            "bootstrap mode outright, per operator ruling, and is stripped "
            "from the floor even if select_panel would have derived it for "
            "this diff) to have signed off — the bootstrap-mode default "
            "(agent_policy ent_d0f1a840e549b3b299f62397); pass --panel "
            "required to use the diff-derived floor alone (where content "
            "can still appear), or set ATELES_APPROVE_PANEL_REQUIRED_ONLY=1 "
            "to change the default."
        )
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--lenses",
        default="",
        help=(
            "comma-separated lenses to ADD on top of the derived floor "
            "(never removes a lens the panel logic requires for this PR)"
        ),
    )
    parser.add_argument(
        "--panel",
        choices=("required", "all"),
        default="all" if panel_all_default else "required",
        help=(
            "'all' (default while bootstrap mode is on, see "
            "ATELES_APPROVE_PANEL_REQUIRED_ONLY) requires every one of the "
            "five bootstrap-roster review lenses to have signed off on the "
            "current head, and excludes content/Corvus outright; 'required' "
            "uses only the diff-derived floor from review_panel.select_panel "
            "(plus --lenses additions, where content can still appear). "
            "Either way, a live blocking verdict from ANY lens — required "
            "or not — always refuses the approval. A lens outside the "
            "required set blocks only when it objects (REQUEST_CHANGES, "
            "BLOCKED, a [BLOCKING] finding) or its verdict cannot be read; "
            "a plain COMMENT from it does not, but a later COMMENT never retires "
            "an earlier objection on the same head; only a clearing verdict "
            "does."
        ),
    )
    parser.add_argument(
        "--no-carry",
        action="store_true",
        help=(
            "require every lens to have cleared the CURRENT head itself; do not "
            "accept an earlier head's sign-off for a lens the fix did not touch"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="submit the APPROVE review as the App if every lens and check passes (default: dry run)",
    )
    args = parser.parse_args()

    extra_lenses = [x.strip() for x in args.lenses.split(",") if x.strip()]

    return asyncio.run(
        run(
            args.repo,
            args.pr,
            extra_lenses,
            apply=args.apply,
            panel_all=(args.panel == "all"),
            # Only named when the operator opts out; carrying is the default.
            **({"carry": False} if args.no_carry else {}),
        )
    )


if __name__ == "__main__":
    sys.exit(main())
