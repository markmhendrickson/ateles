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
outright rather than approving with a caveat.

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

Usage:
    python3 execution/scripts/approve_pr_as_app.py --repo <owner/name> --pr <n> \\
        [--lenses pm,arch,qa,security] [--apply]

Without --apply this is always a dry run: it prints the per-lens table and
the check-run table and does nothing else, whether every gate passes or not.
With --apply, and ONLY if every lens and every required check passes, it
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
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402

from swarm_dispatch import (  # noqa: E402
    _mint_reviewer_app_installation_token,
    _normalise_full_sha,
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

DEFAULT_LENSES: tuple[str, ...] = ("pm", "arch", "qa", "security")

_FAILING_CHECK_CONCLUSIONS = ("failure", "timed_out", "cancelled", "action_required")

GITHUB_API = "https://api.github.com"


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
    def __init__(self, name: str, state: str, passed: bool) -> None:
        self.name = name
        self.state = state
        self.passed = passed


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


async def evaluate_checks(
    client: httpx.AsyncClient, *, repo: str, head_sha: str
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
    for run in runs:
        name = (run.get("name") or "").strip()
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
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

    if not runs and legacy_status_count == 0:
        # No signal of any kind — nothing has run yet. Fail closed rather
        # than approving a head CI has not touched.
        all_green = False

    return outcomes, all_green


async def submit_app_approval(
    client: httpx.AsyncClient,
    *,
    repo: str,
    pr: int,
    head_sha: str,
    lens_outcomes: list[LensOutcome],
) -> dict:
    """Mint an App installation token and submit a formal APPROVE review.

    Reuses `swarm_dispatch._mint_reviewer_app_installation_token` — the same
    JWT-then-installation-token exchange `_emit_formal_review` uses for
    binding reviews. Raises on any failure; the caller treats a raised
    exception as "submitted nothing" per the --apply contract.
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

    body_lines = [
        "Approved by the swarm App — every required review lens cleared the "
        f"PR's current head `{head_sha}`.",
        "",
    ]
    for outcome in lens_outcomes:
        body_lines.append(
            f"- **{outcome.lens}** ({outcome.agent}): `{outcome.verdict}` — {outcome.comment_url}"
        )
    body_lines.append("")
    body_lines.append(f"head_sha={head_sha}")
    body = "\n".join(body_lines)

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }
    resp = await client.post(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr}/reviews",
        json={"event": "APPROVE", "body": body[:65000], "commit_id": head_sha},
        headers=headers,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"review submission failed: HTTP {resp.status_code}: {(resp.text or '')[:240]}")
    return resp.json() or {}


def _print_table(lens_outcomes: list[LensOutcome], check_outcomes: list[CheckOutcome]) -> None:
    print()
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
            pf = "PASS" if c.passed else "FAIL"
            print(f"{c.name:<40} {c.state:<20} {pf}")
    print()


async def run(repo: str, pr: int, lenses: list[str], *, apply: bool) -> int:
    async with httpx.AsyncClient(timeout=30) as client:
        pr_data = await _fetch_pr(client, repo, pr)
        head_sha = _normalise_full_sha(str((pr_data.get("head") or {}).get("sha") or ""))
        if not head_sha:
            print(f"refusing: could not resolve a full 40-char head SHA for {repo}#{pr}")
            return 1

        comments = await _fetch_issue_comments(client, repo, pr)

        lens_outcomes: list[LensOutcome] = []
        for lens in lenses:
            outcome = await evaluate_lens(
                client, repo=repo, pr=pr, head_sha=head_sha, comments=comments, lens=lens
            )
            lens_outcomes.append(outcome)

        check_outcomes, checks_green = await evaluate_checks(client, repo=repo, head_sha=head_sha)

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
                client, repo=repo, pr=pr, head_sha=head_sha, lens_outcomes=lens_outcomes
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
            "review lens has cleared the PR's current head."
        )
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--lenses",
        default=",".join(DEFAULT_LENSES),
        help=f"comma-separated required lenses (default: {','.join(DEFAULT_LENSES)})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="submit the APPROVE review as the App if every lens and check passes (default: dry run)",
    )
    args = parser.parse_args()

    lenses = [x.strip() for x in args.lenses.split(",") if x.strip()]
    if not lenses:
        print("refusing: --lenses resolved to an empty list")
        return 1

    return asyncio.run(run(args.repo, args.pr, lenses, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
