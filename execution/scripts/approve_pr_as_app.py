#!/usr/bin/env python3
"""
execution/scripts/approve_pr_as_app.py — approve a PR as the swarm's GitHub
App, but ONLY when every required review lens has cleared the PR's CURRENT
head and every required status check is green.

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
        [--lenses pm,security] [--apply]

`--lenses` ADDS to the derived floor; it can never remove a lens the panel
logic would itself require for this diff/issue. Without --apply this is
always a dry run: it prints the derived-lens rationale, the per-lens table,
and the check-run table, and does nothing else, whether every gate passes or
not. With --apply, and ONLY if every lens and every required check passes, it
submits a formal APPROVE review as the App. On any failure it exits non-zero
and submits nothing.

Env:
    GITHUB_TOKEN / ATELES_AGENT_PAT       read-only GitHub calls (PR, comments, checks)
    ATELES_REVIEWER_APP_ID                reviewer App id (required for --apply)
    ATELES_REVIEWER_APP_PRIVATE_KEY(_PATH) reviewer App private key (required for --apply)
    ATELES_REVIEWER_APP_INSTALLATION_ID   optional; resolved via API if unset

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

from review_panel import LENSES, Lens, select_panel  # noqa: E402
from swarm_dispatch import (  # noqa: E402
    EXPECTATION_MARKER,
    SwarmDispatcher,
    _mint_reviewer_app_installation_token,
    _normalise_full_sha,
    _normalise_github_review_id,
    _reviewer_app_private_key_pem,
    compose_lens_review_marker,
    lens_own_verdict,
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
# enforcement for that check. Provisioning the runner is tracked by the
# Neotoma task ent_45626c1604fefc32118ffd62; remove the entry when it lands.
# Labels are compared case-insensitively, as GitHub matches them.
KNOWN_UNPROVISIONED_RUNNER_LABEL_SETS: frozenset[frozenset[str]] = frozenset(
    {
        # .github/workflows/canonical-rule-inventory.yml, job "canonical rule inventory"
        frozenset({"self-hosted", "macos", "canonical-rule-inventory"}),
    }
)

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


def _github_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("ATELES_AGENT_PAT", "")
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
    ) -> None:
        self.lens = lens
        self.agent = agent
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
    resp = await client.get(f"{GITHUB_API}/repos/{repo}/pulls/{pr}", headers=_github_headers())
    resp.raise_for_status()
    return resp.json() or {}


async def _fetch_issue_comments(client: httpx.AsyncClient, repo: str, pr: int) -> list[dict]:
    """All issue-API comments on the PR (GitHub serves PR comments there)."""
    out: list[dict] = []
    page = 1
    while True:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{pr}/comments",
            headers=_github_headers(),
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
    comments: list[dict], *, marker: str
) -> dict | None:
    """The LATEST comment carrying *marker* verbatim in its body, or None.

    Latest, not first: a lens can post more than once for the same head (a
    correction after its own comment), and the most recent word is the one
    that should govern. Comments are returned by GitHub in creation order, so
    scanning in reverse gives the latest.
    """
    for c in reversed(comments):
        if marker in (c.get("body") or ""):
            return c
    return None


class RequiredLens:
    """One lens in the derived floor, with why it is required."""

    def __init__(self, lens: str, reason: str) -> None:
        self.lens = lens
        self.reason = reason


async def _changed_files(client: httpx.AsyncClient, repo: str, pr: int) -> list[str]:
    """Filenames touched by the PR — same endpoint `swarm_dispatch._changed_files` reads."""
    out: list[str] = []
    page = 1
    while True:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr}/files",
            headers=_github_headers(),
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
            headers=_github_headers(),
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
) -> LensOutcome:
    agent = LENS_AGENTS.get(lens, "")
    if not agent:
        return LensOutcome(
            lens, agent="", head_matched=False, verdict=None, passed=False,
            reason=f"unknown lens (not in LENS_AGENTS: {sorted(LENS_AGENTS)})",
        )

    marker = compose_lens_review_marker(lens, head_sha)
    comment = _latest_matching_comment(comments, marker=marker)
    if comment is None:
        return LensOutcome(
            lens, agent=agent, head_matched=False, verdict=None, passed=False,
            reason=f"no comment carries {marker!r} — lens has not reviewed the current head",
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
    headers = _github_headers()
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
            f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}", headers=_github_headers()
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
                headers=_github_headers(),
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
        headers=_github_headers(),
    )
    status_resp.raise_for_status()
    status_payload = status_resp.json() or {}
    combined_state = status_payload.get("state", "")
    legacy_status_count = status_payload.get("total_count", 0) or 0

    checks_resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/commits/{head_sha}/check-runs",
        headers={**_github_headers(), "Accept": "application/vnd.github+json"},
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
    for outcome in lens_outcomes:
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


def _print_derived_lenses(required: list[RequiredLens], extra: list[str]) -> None:
    print()
    print("required lenses (derived from review_panel.select_panel for this diff/issue):")
    for r in required:
        print(f"  - {r.lens}: {r.reason}")
    if extra:
        print(f"added by --lenses (on top of the derived floor): {', '.join(extra)}")
    print()


def _print_table(lens_outcomes: list[LensOutcome], check_outcomes: list[CheckOutcome]) -> None:
    print(f"{'lens':<10} {'verdict':<14} {'head match':<11} {'pass/fail':<10} reason")
    print("-" * 80)
    for o in lens_outcomes:
        verdict_s = o.verdict or "(none)"
        head_s = "yes" if o.head_matched else "no"
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
    client: httpx.AsyncClient, *, repo: str, pr: int, pr_body: str, extra_lenses: list[str]
) -> tuple[list[str], list[RequiredLens]]:
    """The lens set the tool will require: the derived floor plus `--lenses`
    additions. `--lenses` can only ADD — it is unioned onto the derived
    floor, never used to shrink it, so a caller can never accidentally (or
    deliberately) drop a lens `review_panel.select_panel` itself would
    require for this diff/issue."""
    required = await derive_required_lenses(client, repo=repo, pr=pr, pr_body=pr_body)
    floor = [r.lens for r in required]
    added = [lens for lens in extra_lenses if lens not in floor]
    return floor + added, required


async def run(repo: str, pr: int, extra_lenses: list[str], *, apply: bool) -> int:
    async with httpx.AsyncClient(timeout=30) as client:
        pr_data = await _fetch_pr(client, repo, pr)
        head_sha = _normalise_full_sha(str((pr_data.get("head") or {}).get("sha") or ""))
        if not head_sha:
            print(f"refusing: could not resolve a full 40-char head SHA for {repo}#{pr}")
            return 1
        pr_body = pr_data.get("body") or ""

        try:
            lenses, required = await resolve_lenses(
                client, repo=repo, pr=pr, pr_body=pr_body, extra_lenses=extra_lenses
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
        _print_derived_lenses(required, added)

        comments = await _fetch_issue_comments(client, repo, pr)

        lens_outcomes: list[LensOutcome] = []
        for lens in lenses:
            outcome = await evaluate_lens(
                client, repo=repo, pr=pr, head_sha=head_sha, comments=comments, lens=lens
            )
            lens_outcomes.append(outcome)

        base_ref = str((pr_data.get("base") or {}).get("ref") or "")
        check_outcomes, checks_green = await evaluate_checks(
            client, repo=repo, head_sha=head_sha, base_ref=base_ref
        )

        _print_table(lens_outcomes, check_outcomes)

        all_lenses_pass = all(o.passed for o in lens_outcomes)
        overall_pass = all_lenses_pass and checks_green

        print(f"head_sha={head_sha}")
        print(f"lenses: {'ALL PASS' if all_lenses_pass else 'FAIL'}")
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
    parser = argparse.ArgumentParser(
        description=(
            "Approve a PR as the swarm's GitHub App, only when every required "
            "review lens has cleared the PR's current head. The required-lens "
            "floor is derived from review_panel.select_panel for this PR's "
            "diff and linked issue; --lenses can only ADD to that floor."
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
        "--apply",
        action="store_true",
        help="submit the APPROVE review as the App if every lens and check passes (default: dry run)",
    )
    args = parser.parse_args()

    extra_lenses = [x.strip() for x in args.lenses.split(",") if x.strip()]

    return asyncio.run(run(args.repo, args.pr, extra_lenses, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
