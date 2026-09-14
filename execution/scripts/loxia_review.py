#!/usr/bin/env python3
"""
Loxia — External PR review agent.

Loxia genus: crossbills. T4 invocable via GHA — reviews PRs against the
ateles mirror files and posts a structured review comment.

Promotion path:
  Phase 3/4: GHA + Claude API (this script, ~100 LOC)
  Phase 5+:  Promote to named T3 if Neotoma attribution or SSE is needed

Review checklist:
  - Changed files match declared scope (no scope creep)
  - No secrets or credentials in diff
  - Coding style consistent with surrounding code
  - CLAUDE.md / gitleaks allowlist updated if needed
  - Ruff/yamllint issues (surface, don't block)

Environment variables (set by GHA workflow):
  ANTHROPIC_API_KEY     Claude API key
  GITHUB_TOKEN          GHA token for posting PR comments
  LOXIA_REVIEW_TOKEN    Distinct reviewer token for native GitHub reviews
  LOXIA_EXPECTED_REVIEWER_LOGIN  Required login for the reviewer token
  LOXIA_NATIVE_FOUNDATION_REVIEW "true" to enable the bounded mechanism
  LOXIA_NATIVE_REVIEW_ONLY       "true" to skip non-foundation pull requests
  LOXIA_USE_GITHUB_PR_API        "true" to read PR files/diff from GitHub
  LOXIA_PR_NUMBER       PR number to review
  LOXIA_REPO            GitHub repo slug (owner/repo)
  LOXIA_DRY_RUN         "true" to print without posting
  LOXIA_HEAD_SHA        HEAD commit SHA of the PR
  NEOTOMA_BEARER_TOKEN  (optional) for filing Neotoma issues on findings
  NEOTOMA_BASE_URL      (optional) Neotoma API base URL
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Preferred: bill the operator's Anthropic Max subscription via an OAuth token
# (CLAUDE_CODE_OAUTH_TOKEN), same account the swarm's `claude --print` panelists
# use — no per-request metered spend. Falls back to the metered ANTHROPIC_API_KEY
# when no subscription token is present. NOTE: subscription OAuth tokens EXPIRE;
# in headless CI a lapsed token surfaces as an auth error, which the review-failure
# path below now makes VISIBLE (red check) rather than a silent false-green.
CLAUDE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
NATIVE_REVIEW_TOKEN = os.environ.get("LOXIA_REVIEW_TOKEN", "")
EXPECTED_REVIEWER_LOGIN = os.environ.get("LOXIA_EXPECTED_REVIEWER_LOGIN", "").strip()
NATIVE_FOUNDATION_REVIEW = (
    os.environ.get("LOXIA_NATIVE_FOUNDATION_REVIEW", "false").lower() == "true"
)
USE_GITHUB_PR_API = os.environ.get("LOXIA_USE_GITHUB_PR_API", "false").lower() == "true"
NATIVE_REVIEW_ONLY = (
    os.environ.get("LOXIA_NATIVE_REVIEW_ONLY", "false").lower() == "true"
)
POST_REVIEW_COMMENT = (
    os.environ.get("LOXIA_POST_REVIEW_COMMENT", "true").lower() == "true"
)
PR_NUMBER = os.environ.get("LOXIA_PR_NUMBER", "")
REPO = os.environ.get("LOXIA_REPO", "")
DRY_RUN = os.environ.get("LOXIA_DRY_RUN", "false").lower() == "true"
HEAD_SHA = os.environ.get("LOXIA_HEAD_SHA", "")

NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")

CLAUDE_MODEL = "claude-opus-4-8"
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
GITHUB_API_URL = "https://api.github.com"

MAX_DIFF_CHARS = 40_000  # truncate large diffs to stay within context

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_VERDICT_RE = re.compile(
    r"^\s*\*{0,2}Verdict\*{0,2}\s*:\s*"
    r"(APPROVE|REQUEST_CHANGES|COMMENT)\b",
    re.IGNORECASE | re.MULTILINE,
)

_FOUNDATION_EXACT_PATHS = {
    ".github/workflows/foundation-checks.yml",
    "execution/scripts/link_vocabulary_terms.py",
    "execution/scripts/render_reading_projection.py",
}

NATIVE_REASON_MESSAGES = {
    "review_credential_missing": (
        "Loxia refused to review because its distinct reviewer credential is "
        "not configured."
    ),
    "expected_reviewer_missing": (
        "Loxia refused to review because the expected reviewer login is not configured."
    ),
    "head_unresolved": (
        "Loxia refused to review because the pull request head could not be "
        "bound to a full commit SHA."
    ),
    "reviewer_identity_unreadable": (
        "Loxia could not verify which GitHub account owns the review credential."
    ),
    "wrong_reviewer_identity": (
        "Loxia refused to approve because the review credential belongs to an "
        "unexpected GitHub account."
    ),
    "pr_head_unreadable": (
        "Loxia could not verify the pull request's current head commit."
    ),
    "head_changed": (
        "Loxia refused to approve because the pull request head changed during "
        "the review."
    ),
    "self_approval_rejected": (
        "GitHub rejected this approval because the reviewer and author are the "
        "same account."
    ),
    "review_post_failed": (
        "Loxia produced a verdict but could not post it as a GitHub review."
    ),
    "review_readback_failed": (
        "Loxia posted a review but could not verify its identity, state, and "
        "commit through GitHub."
    ),
    "review_decision_readback_failed": (
        "Loxia could not verify GitHub's aggregate review decision."
    ),
    "review_decision_not_approved": (
        "Loxia posted an approval, but GitHub still reports the pull request as "
        "not approved without another current-head blocking review."
    ),
}


class NativeReviewError(RuntimeError):
    """A native GitHub review did not bind to the intended identity/head.

    ``reason_class`` is safe to show in a public failure comment. Detailed
    transport errors stay in the workflow log, where they cannot be mistaken
    for a review verdict or copied into durable public artifacts.
    """

    def __init__(self, reason_class: str, detail: str = "") -> None:
        self.reason_class = reason_class
        self.detail = detail
        super().__init__(f"{reason_class}: {detail}" if detail else reason_class)


def is_foundation_pr(changed_files: list[str]) -> bool:
    """Whether every changed path belongs to the bounded foundation surface."""
    if not changed_files:
        return False
    for path in changed_files:
        if path.startswith("docs/foundation/"):
            continue
        if path in _FOUNDATION_EXACT_PATHS:
            continue
        if path.startswith("execution/scripts/check_foundation_") and path.endswith(
            ".py"
        ):
            continue
        if path.startswith("execution/daemons/apis/test_foundation") and path.endswith(
            ".py"
        ):
            continue
        return False
    return True


def parse_review_verdict(review: str) -> str:
    """Map Loxia's structured verdict to a conservative native review event."""
    match = _VERDICT_RE.search(review or "")
    return match.group(1).upper() if match else "COMMENT"


def format_native_review_failure(reason_class: str, *, head_sha: str) -> str:
    """Format the stable three-part failure contract for PR comments."""
    happened = NATIVE_REASON_MESSAGES.get(
        reason_class,
        "Loxia could not complete the native GitHub review.",
    )
    commit = head_sha[:12] if _FULL_SHA_RE.fullmatch(head_sha or "") else "unknown"
    return (
        f"**What happened:** {happened} "
        f"Reason class: `{reason_class}`.\n\n"
        f"**Mergeability:** This pull request is NOT approved for commit "
        f"`{commit}`.\n\n"
        "**Next action:** Check the failed workflow run, correct the named "
        "identity, head, or GitHub API condition, and rerun Loxia against the "
        "current head."
    )


# ── Domain routing ─────────────────────────────────────────────────────────────
#
# Reuse Apis's single source of truth (execution/daemons/apis/routing.py) for
# path → domain-owning-agent mapping rather than forking the patterns. Loaded by
# file path because the repo has no package __init__ files. Best-effort: if the
# module can't be loaded, Loxia still runs as the baseline reviewer.


def _load_resolve_reviewers():
    """Return routing.resolve_reviewers, or a no-op fallback on import failure."""
    routing_path = (
        Path(__file__).resolve().parents[1] / "daemons" / "apis" / "routing.py"
    )
    try:
        spec = importlib.util.spec_from_file_location("apis_routing", routing_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no spec for {routing_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.resolve_reviewers
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[loxia] routing module unavailable ({exc}) — baseline review only")
        return lambda _paths: []


resolve_reviewers = _load_resolve_reviewers()


# ── Git diff ──────────────────────────────────────────────────────────────────


def get_pr_diff() -> str:
    """
    Get the diff for the current PR by comparing HEAD to merge-base with main.
    Falls back to `git diff HEAD~1` if merge-base fails.
    """
    if USE_GITHUB_PR_API:
        request = urllib.request.Request(
            f"{GITHUB_API_URL}/repos/{REPO}/pulls/{PR_NUMBER}",
            headers={
                **_github_headers(),
                "Accept": "application/vnd.github.v3.diff",
            },
            method="GET",
        )
        request.add_header("User-Agent", "ateles-neotoma-sync/1.0")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace")[:MAX_DIFF_CHARS]
        except urllib.error.HTTPError as exc:
            raise NativeReviewError(
                "pr_diff_unreadable", f"GitHub HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise NativeReviewError("pr_diff_unreadable", type(exc).__name__) from exc

    try:
        base = subprocess.check_output(
            ["git", "merge-base", "origin/main", "HEAD"],
            text=True,
        ).strip()
        diff = subprocess.check_output(
            ["git", "diff", base, "HEAD"],
            text=True,
        )
        return diff[:MAX_DIFF_CHARS]
    except subprocess.CalledProcessError:
        pass

    try:
        diff = subprocess.check_output(["git", "diff", "HEAD~1"], text=True)
        return diff[:MAX_DIFF_CHARS]
    except subprocess.CalledProcessError:
        return "(could not retrieve diff)"


def get_changed_files() -> list[str]:
    """Return list of files changed in this PR."""
    if USE_GITHUB_PR_API:
        names: list[str] = []
        page = 1
        while True:
            suffix = "" if page == 1 else f"&page={page}"
            files = _github_json(
                "GET",
                f"{GITHUB_API_URL}/repos/{REPO}/pulls/{PR_NUMBER}"
                f"/files?per_page=100{suffix}",
                reason_class="pr_files_unreadable",
            )
            if not isinstance(files, list):
                raise NativeReviewError("pr_files_unreadable")
            names.extend(
                item.get("filename", "")
                for item in files
                if isinstance(item, dict) and item.get("filename")
            )
            if len(files) < 100:
                break
            page += 1
        return names

    try:
        base = subprocess.check_output(
            ["git", "merge-base", "origin/main", "HEAD"],
            text=True,
        ).strip()
        out = subprocess.check_output(
            ["git", "diff", "--name-only", base, "HEAD"],
            text=True,
        )
        return [f.strip() for f in out.splitlines() if f.strip()]
    except subprocess.CalledProcessError:
        return []


# ── Claude API ────────────────────────────────────────────────────────────────


class ClaudeReviewError(RuntimeError):
    """Raised when Claude could not produce a real review (auth/credit/network/
    empty response). Callers must treat this as a FAILED review — never post it
    as if it were a verdict and never let the job exit green on it."""


def call_claude(prompt: str) -> str:
    """Get a review from Claude for `prompt`; return the text response.

    Prefers the operator's Max subscription via the `claude --print` CLI (same
    path the swarm's panelists use — it manages auth and rate budgeting for the
    subscription tier). Verified in CI: a raw /v1/messages call with the
    subscription OAuth token authenticates but is persistently 429 rate-limited,
    so the CLI is the correct transport. Falls back to a direct API call only
    when the CLI is unavailable AND a metered ANTHROPIC_API_KEY is present.

    Raises ClaudeReviewError on any failure to obtain a real review (no
    credential, CLI/API error, timeout, or empty response). Deliberate: never
    return an error string as if it were the review (that was the false-green
    bug — a failed review must fail the check, not pass it).
    """
    if CLAUDE_OAUTH_TOKEN:
        return _call_claude_cli(prompt)
    if ANTHROPIC_API_KEY:
        return _call_claude_api(prompt)
    raise ClaudeReviewError(
        "no Claude credential set (need CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY)"
    )


def _call_claude_cli(prompt: str) -> str:
    """Run `claude --print` with the prompt on stdin (subscription-authed via
    CLAUDE_CODE_OAUTH_TOKEN in the env). Mirrors the daemon panelist pattern."""
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    if shutil.which(claude_bin) is None:
        # CLI missing but we were told to use the subscription — fail visibly
        # rather than silently degrade to a metered key the operator opted out of.
        raise ClaudeReviewError(
            f"'{claude_bin}' CLI not found on PATH — cannot use the Max "
            f"subscription; install the Claude Code CLI in the runner."
        )
    try:
        proc = subprocess.run(
            [claude_bin, "--print", "--model", CLAUDE_MODEL],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
            env=os.environ,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeReviewError("claude --print timed out") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise ClaudeReviewError(f"claude --print exited {proc.returncode}: {err[:400]}")
    text = (proc.stdout or "").strip()
    if not text:
        raise ClaudeReviewError("claude --print returned an empty review")
    return text


def _call_claude_api(prompt: str) -> str:
    """Direct /v1/messages call with the metered ANTHROPIC_API_KEY (fallback)."""
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }

    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    req.add_header("User-Agent", "ateles-neotoma-sync/1.0")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Surface the API error body. A bare "HTTP Error 400: Bad Request" hides
        # the actual cause (e.g. the invalid_request_error / credit-exhausted
        # message), which made an earlier 400 undiagnosable from the posted
        # review comment alone.
        try:
            detail = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            detail = ""
        raise ClaudeReviewError(
            f"Claude API HTTP {exc.code} — {detail or exc.reason}"
        ) from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ClaudeReviewError(f"Claude API call failed: {exc}") from exc

    try:
        text = data["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ClaudeReviewError(
            f"Claude response missing content: {json.dumps(data)[:300]}"
        ) from exc
    if not text or not text.strip():
        raise ClaudeReviewError("Claude returned an empty review")
    return text


# ── GitHub comment ─────────────────────────────────────────────────────────────


def review_comment_marker(reviewer: "Reviewer") -> str:
    """Stable per-reviewer marker so a re-run updates that reviewer's prior
    comment in place instead of stacking a new one per push. Matches the
    `## {display} Review {emoji}` heading the prompt scaffold emits, so each
    of Loxia/Monedula/Gorilla owns exactly one live comment per PR."""
    return f"## {reviewer.display} Review {reviewer.emoji}"


def _github_headers(token: str | None = None) -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN if token is None else token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }


def _github_json(
    method: str,
    url: str,
    payload: dict | None = None,
    *,
    reason_class: str,
    token: str | None = None,
) -> dict | list:
    """Call GitHub and return decoded JSON or a classified binding error."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=_github_headers(token),
        method=method,
    )
    request.add_header("User-Agent", "ateles-neotoma-sync/1.0")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise NativeReviewError(reason_class, f"GitHub HTTP {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise NativeReviewError(reason_class, type(exc).__name__) from exc


def _repo_parts() -> tuple[str, str]:
    try:
        owner, name = REPO.split("/", 1)
    except ValueError as exc:
        raise NativeReviewError("invalid_repository") from exc
    if not owner or not name:
        raise NativeReviewError("invalid_repository")
    return owner, name


def _review_state_for_event(event: str) -> str:
    return {
        "APPROVE": "APPROVED",
        "REQUEST_CHANGES": "CHANGES_REQUESTED",
        "COMMENT": "COMMENTED",
    }[event]


def preflight_native_review(expected_head: str) -> None:
    """Prove reviewer identity, current head, and non-self authorship."""

    if not NATIVE_REVIEW_TOKEN:
        raise NativeReviewError("review_credential_missing")
    if not EXPECTED_REVIEWER_LOGIN:
        raise NativeReviewError("expected_reviewer_missing")
    if not _FULL_SHA_RE.fullmatch(expected_head or ""):
        raise NativeReviewError("head_unresolved")

    actor = _github_json(
        "GET",
        f"{GITHUB_API_URL}/user",
        reason_class="reviewer_identity_unreadable",
        token=NATIVE_REVIEW_TOKEN,
    )
    if not isinstance(actor, dict) or actor.get("login") != EXPECTED_REVIEWER_LOGIN:
        raise NativeReviewError("wrong_reviewer_identity")

    pr_url = f"{GITHUB_API_URL}/repos/{REPO}/pulls/{PR_NUMBER}"
    pr = _github_json(
        "GET",
        pr_url,
        reason_class="pr_head_unreadable",
        token=NATIVE_REVIEW_TOKEN,
    )
    current_head = (pr.get("head") or {}).get("sha") if isinstance(pr, dict) else None
    if current_head != expected_head:
        raise NativeReviewError("head_changed")
    author = (pr.get("user") or {}).get("login") if isinstance(pr, dict) else None
    if author == EXPECTED_REVIEWER_LOGIN:
        raise NativeReviewError("self_approval_rejected")


def post_native_review(review: str, *, expected_head: str) -> dict:
    """Post and read back a second-principal review bound to one full head SHA.

    This is intentionally a callable mechanism, not activation policy. The
    workflow must opt in with an expected reviewer login and its authorized
    token after the foundation governance exception is approved. Preflight is
    repeated immediately before the write to fence model-runtime head changes.
    """
    preflight_native_review(expected_head)
    pr_url = f"{GITHUB_API_URL}/repos/{REPO}/pulls/{PR_NUMBER}"

    event = parse_review_verdict(review)
    posted = _github_json(
        "POST",
        f"{pr_url}/reviews",
        {"body": review, "event": event, "commit_id": expected_head},
        reason_class="review_post_failed",
        token=NATIVE_REVIEW_TOKEN,
    )
    review_id = posted.get("id") if isinstance(posted, dict) else None
    if not isinstance(review_id, int):
        raise NativeReviewError("review_post_failed")

    readback = _github_json(
        "GET",
        f"{pr_url}/reviews/{review_id}",
        reason_class="review_readback_failed",
        token=NATIVE_REVIEW_TOKEN,
    )
    expected_state = _review_state_for_event(event)
    if not (
        isinstance(readback, dict)
        and readback.get("id") == review_id
        and (readback.get("user") or {}).get("login") == EXPECTED_REVIEWER_LOGIN
        and readback.get("commit_id") == expected_head
        and readback.get("state") == expected_state
    ):
        raise NativeReviewError("review_readback_failed")

    owner, name = _repo_parts()
    graph = _github_json(
        "POST",
        f"{GITHUB_API_URL}/graphql",
        {
            "query": (
                "query($owner:String!,$name:String!,$number:Int!){"
                "repository(owner:$owner,name:$name){pullRequest(number:$number){"
                "headRefOid reviewDecision reviews(last:100){nodes{"
                "author{login} state commit{oid} submittedAt}}}}}"
            ),
            "variables": {
                "owner": owner,
                "name": name,
                "number": int(PR_NUMBER),
            },
        },
        reason_class="review_decision_readback_failed",
        token=NATIVE_REVIEW_TOKEN,
    )
    try:
        state = graph["data"]["repository"]["pullRequest"]
    except (KeyError, TypeError) as exc:
        raise NativeReviewError("review_decision_readback_failed") from exc
    if state.get("headRefOid") != expected_head:
        raise NativeReviewError("head_changed")
    if event == "APPROVE" and state.get("reviewDecision") != "APPROVED":
        nodes = (state.get("reviews") or {}).get("nodes") or []
        latest_by_actor: dict[str, dict] = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            login = ((node.get("author") or {}).get("login") or "").strip()
            submitted_at = str(node.get("submittedAt") or "")
            previous = latest_by_actor.get(login)
            if login and (
                previous is None
                or submitted_at >= str(previous.get("submittedAt") or "")
            ):
                latest_by_actor[login] = node
        other_current_head_blocker = any(
            login != EXPECTED_REVIEWER_LOGIN
            and node.get("state") == "CHANGES_REQUESTED"
            and ((node.get("commit") or {}).get("oid")) == expected_head
            for login, node in latest_by_actor.items()
        )
        if not other_current_head_blocker:
            raise NativeReviewError("review_decision_not_approved")

    # The REST read-back above proves the review itself. This final fetch makes
    # the race fence explicit even when GraphQL data was served concurrently.
    final_pr = _github_json(
        "GET",
        pr_url,
        reason_class="pr_head_unreadable",
        token=NATIVE_REVIEW_TOKEN,
    )
    final_head = (
        ((final_pr.get("head") or {}).get("sha"))
        if isinstance(final_pr, dict)
        else None
    )
    if final_head != expected_head:
        raise NativeReviewError("head_changed")
    return {
        "review_id": review_id,
        "reviewer": EXPECTED_REVIEWER_LOGIN,
        "state": expected_state,
        "commit_id": expected_head,
        "review_decision": state.get("reviewDecision"),
    }


def find_existing_review_comment(marker: str) -> int | None:
    """Return the id of the most recent review comment carrying `marker` on
    this PR, if any.

    Matched by marker rather than author so it works whether the comment was
    posted by github-actions[bot] or a machine account, and so each reviewer
    only ever matches its own comment."""
    url = f"{GITHUB_API_URL}/repos/{REPO}/issues/{PR_NUMBER}/comments?per_page=100"
    req = urllib.request.Request(url, headers=_github_headers(), method="GET")
    req.add_header("User-Agent", "ateles-neotoma-sync/1.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            comments = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(
            f"[loxia] Could not list comments (will POST fresh): {exc}", file=sys.stderr
        )
        return None
    matches = [c for c in comments if marker in (c.get("body") or "")]
    return matches[-1]["id"] if matches else None


def post_github_comment(body: str, marker: str | None = None) -> None:
    """Upsert a review comment on the PR: when `marker` is supplied and a prior
    comment carries it, update that comment in place; otherwise post fresh."""
    if not GITHUB_TOKEN or not PR_NUMBER or not REPO:
        print("[loxia] Cannot post comment — missing GITHUB_TOKEN/PR_NUMBER/REPO")
        return

    existing_id = find_existing_review_comment(marker) if marker else None
    if existing_id is not None:
        url = f"{GITHUB_API_URL}/repos/{REPO}/issues/comments/{existing_id}"
        method, action = "PATCH", "updated"
    else:
        url = f"{GITHUB_API_URL}/repos/{REPO}/issues/{PR_NUMBER}/comments"
        method, action = "POST", "posted"

    req = urllib.request.Request(
        url,
        data=json.dumps({"body": body}).encode(),
        headers=_github_headers(),
        method=method,
    )
    req.add_header("User-Agent", "ateles-neotoma-sync/1.0")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"[loxia] Comment {action}: {result.get('html_url', '(no url)')}")
    except urllib.error.URLError as exc:
        print(f"[loxia] Failed to {action[:-1]} comment: {exc}", file=sys.stderr)


# ── Neotoma issue filing ───────────────────────────────────────────────────────


def file_neotoma_issue(title: str, body: str, agent: str = "loxia") -> None:
    """
    File a Neotoma issue for a significant finding, attributed to the reviewing
    agent. audience=agent, severity=medium. Best-effort; never blocks the review.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return

    payload = {
        "entity_type": "issue",
        "canonical_name": f"issue:{agent}:pr{PR_NUMBER}:{HEAD_SHA[:8]}",
        "snapshot": {
            "title": title,
            "body": body,
            "audience": "agent",
            "severity": "medium",
            "kind": "code_review",
            "source": f"{agent}:pr{PR_NUMBER}",
            "reviewer": agent,
            "repository": REPO,
        },
    }

    req = urllib.request.Request(
        f"{NEOTOMA_BASE_URL}/observations",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    req.add_header("User-Agent", "ateles-neotoma-sync/1.0")

    try:
        with urllib.request.urlopen(req, timeout=15):
            print(f"[loxia] Neotoma issue filed: {title}")
    except urllib.error.URLError as exc:
        print(f"[loxia] Failed to file Neotoma issue: {exc}", file=sys.stderr)


# ── Reviewers ──────────────────────────────────────────────────────────────────
#
# Loxia is the universal baseline reviewer, run on every PR. Domain reviewers are
# appended when the PR touches their domain (routing.resolve_reviewers). Each
# reviewer contributes its own checklist + findings legend; everything else (the
# diff scaffold, output structure, attribution footer) is shared.


@dataclass(frozen=True)
class Reviewer:
    skill: str  # routing key returned by resolve_reviewers, e.g. "monedula"
    display: str  # heading name, e.g. "Loxia"
    emoji: str
    persona: str  # one-line role description injected into the prompt intro
    checklist: str  # numbered "assess each item" block
    findings: str  # the "### Findings" legend bullets


PROMPT_SCAFFOLD = """\
You are {persona}. Review the following pull request diff for the ateles \
repository (a public mirror of a Neotoma-canonical agent swarm) and produce a \
structured review comment.

Repository: {repo}
PR number: #{pr_number}
Changed files: {changed_files}

Diff (truncated to {max_diff_chars} chars if large):
```diff
{diff}
```

Review checklist — assess each item:
{checklist}

Output format — use this exact structure:
## {display} Review {emoji}

**Verdict**: APPROVE | REQUEST_CHANGES | COMMENT

### Summary
<1–3 sentences>

### Findings
<bullet list — only include non-empty categories>
{findings}

### Recommendations
<optional — only if REQUEST_CHANGES>

---
*{display} automated review · commit {head_sha}*
"""


LOXIA = Reviewer(
    skill="loxia",
    display="Loxia",
    emoji="🪶",
    persona="Loxia, a generalist PR review agent",
    checklist="""\
1. **Scope** — do changed files match a coherent, focused purpose? Flag scope creep.
2. **Secrets** — any API keys, tokens, passwords, IBANs, or personal data in the diff?
3. **gitleaks** — if new daemon/script files added that reference env var names \
(BEARER_TOKEN, API_KEY, etc.), is .gitleaks.toml allowlist updated?
4. **Linting** — obvious ruff / yamllint / shellcheck issues visible in the diff?
5. **Pattern consistency** — new daemons should follow the T3 startup pattern \
(AgentLoader → AAuthSigner → Notifier → work loop). New scripts should use httpx \
or stdlib urllib (not requests). Flag deviations.
6. **CLAUDE.md** — if new public files added, does CLAUDE.md need updating?""",
    findings="""\
- 🔴 **Secrets**: <finding or "none detected">
- 🟡 **Scope**: <finding or "focused">
- 🟡 **gitleaks**: <finding or "allowlist updated / not needed">
- 🟢 **Linting**: <finding or "no issues visible">
- 🟢 **Pattern**: <finding or "follows T3 pattern">
- 🟢 **Docs**: <finding or "no doc updates needed">""",
)


MONEDULA = Reviewer(
    skill="monedula",
    display="Monedula",
    emoji="🪙",
    persona=(
        "Monedula, the finance-domain agent, reviewing only the "
        "finance/payment-relevant parts of this PR"
    ),
    checklist="""\
1. **Hardcoded payee data** — any literal IBAN, account number, wallet address, \
amount, or contact detail in the diff? These MUST be read from env or parquet, \
never hardcoded. Flag every literal.
2. **Payment profiles** — new/changed payment logic should resolve payee + \
account data from Neotoma payment_profile entities or parquet/env, not inline \
constants.
3. **Yoga payments** — must NEVER set a memo / OP_RETURN. Flag any `memo=` on a \
yoga payment path.
4. **Yoga / therapy tasks** — must NEVER be marked completed; only `due_date` is \
updated. Flag any status→done transition on those task types.
5. **Idempotency / safety** — payment execution paths should be idempotent and \
guard against double-send. Flag missing idempotency keys or retry-without-guard.""",
    findings="""\
- 🔴 **Hardcoded payee data**: <finding or "none — reads from env/parquet">
- 🔴 **Yoga memo / OP_RETURN**: <finding or "no memo on yoga path">
- 🟡 **Payment profiles**: <finding or "resolved from entities/parquet">
- 🟡 **Yoga/therapy completion**: <finding or "due_date-only, never completed">
- 🟢 **Idempotency**: <finding or "guarded">""",
)


GORILLA = Reviewer(
    skill="gorilla",
    display="Gorilla",
    emoji="🦍",
    persona=(
        "Gorilla, the health & fitness agent, reviewing only the "
        "health/fitness-relevant parts of this PR"
    ),
    checklist="""\
1. **Special-category health data** — any literal biomarker, body metric, \
medical condition, medication, or other RGPD Art. 9 health datum committed in \
the diff? These MUST come from parquet / the operator's Neotoma data, never \
inlined. Flag every literal.
2. **Data minimization** — per CLAUDE.md people-data rules, durable records keep \
only what serves the relationship/analysis; incidental sensitive disclosures are \
summarized, not transcribed verbatim. Flag over-capture.
3. **Grounded in own data** — health/fitness logic reads from the operator's \
logged Neotoma data, not fabricated, hardcoded, or assumed values.
4. **Units & correctness** — workout/fitness math (weights, reps, sets, dates, \
kg/lb units) consistent and sane. Flag unit mismatches or off-by-one date logic.
5. **No medical overreach** — stays within logged-data analysis; no diagnostic \
or prescriptive medical claims beyond Gorilla's grounded scope.""",
    findings="""\
- 🔴 **Health data (Art. 9)**: <finding or "none — reads from parquet/Neotoma">
- 🟡 **Data minimization**: <finding or "minimal / no over-capture">
- 🟡 **Grounded in own data**: <finding or "reads logged data, not hardcoded">
- 🟢 **Units & correctness**: <finding or "consistent">
- 🟢 **Medical scope**: <finding or "within grounded scope">""",
)


# Domain skill → Reviewer. Extend this map as domain agents are onboarded; a
# skill resolve_reviewers() returns without an entry here is skipped (logged).
DOMAIN_REVIEWERS: dict[str, Reviewer] = {
    "monedula": MONEDULA,
    "gorilla": GORILLA,
}


def select_reviewers(changed_files: list[str]) -> list[Reviewer]:
    """Loxia (baseline) plus any registered domain reviewer whose domain the PR
    touches. Order-stable: Loxia first, then domains in routing order."""
    reviewers: list[Reviewer] = [LOXIA]
    for skill in resolve_reviewers(changed_files):
        reviewer = DOMAIN_REVIEWERS.get(skill)
        if reviewer:
            reviewers.append(reviewer)
        else:
            print(
                f"[loxia] domain '{skill}' has no registered reviewer yet — "
                "skipping (Loxia still covers it)"
            )
    return reviewers


def should_post_native_review(reviewer: Reviewer, changed_files: list[str]) -> bool:
    """Whether the opt-in foundation approval mechanism applies to this run."""
    return (
        NATIVE_FOUNDATION_REVIEW
        and reviewer.skill == LOXIA.skill
        and is_foundation_pr(changed_files)
    )


def build_prompt(reviewer: Reviewer, diff: str, changed_files: list[str]) -> str:
    return PROMPT_SCAFFOLD.format(
        persona=reviewer.persona,
        display=reviewer.display,
        emoji=reviewer.emoji,
        checklist=reviewer.checklist,
        findings=reviewer.findings,
        repo=REPO,
        pr_number=PR_NUMBER,
        changed_files=", ".join(changed_files) if changed_files else "(none)",
        max_diff_chars=MAX_DIFF_CHARS,
        diff=diff,
        head_sha=HEAD_SHA[:12] if HEAD_SHA else "unknown",
    )


# ── Main ──────────────────────────────────────────────────────────────────────


def _report_native_review_failure(reviewer: Reviewer, exc: NativeReviewError) -> None:
    """Publish only the stable reason class for a failed native review."""
    print(
        f"[{reviewer.skill}] NATIVE REVIEW FAILED: {exc.reason_class}",
        file=sys.stderr,
    )
    failure_body = (
        f"{review_comment_marker(reviewer)}\n\n"
        "⚠️ **Native review did not land.**\n\n"
        f"{format_native_review_failure(exc.reason_class, head_sha=HEAD_SHA)}"
    )
    post_github_comment(failure_body, marker=review_comment_marker(reviewer))


def run_reviewer(
    reviewer: Reviewer,
    diff: str,
    changed_files: list[str],
    *,
    native_review: bool = False,
) -> bool:
    """Build the prompt, call Claude, and (unless dry-run) post the comment and
    file a Neotoma issue on REQUEST_CHANGES — all attributed to this reviewer.

    Returns True if a real review was produced, False if it failed. A failure
    posts a clearly-marked "review could not run" comment (so the PR shows the
    reviewer is broken, not silently absent) and the caller exits non-zero.
    """
    if native_review and not DRY_RUN:
        try:
            preflight_native_review(HEAD_SHA)
        except NativeReviewError as exc:
            _report_native_review_failure(reviewer, exc)
            return False

    prompt = build_prompt(reviewer, diff, changed_files)
    print(f"[{reviewer.skill}] Calling Claude ({CLAUDE_MODEL})...")
    try:
        review = call_claude(prompt)
    except ClaudeReviewError as exc:
        print(f"[{reviewer.skill}] REVIEW FAILED: {exc}", file=sys.stderr)
        if not DRY_RUN:
            failure_body = (
                f"{review_comment_marker(reviewer)}\n\n"
                f"⚠️ **Review could not run** — {reviewer.display} did not "
                f"produce a verdict for commit `{HEAD_SHA[:12] or 'unknown'}`.\n\n"
                f"Reason: `{exc}`\n\n"
                f"This is NOT an approval. The check fails so the missing review "
                f"is visible; re-run once the cause is resolved."
            )
            post_github_comment(failure_body, marker=review_comment_marker(reviewer))
        return False

    print("\n" + "=" * 60)
    print(review)
    print("=" * 60 + "\n")

    if DRY_RUN:
        print(f"[{reviewer.skill}] DRY RUN — not posting comment or filing issue")
        return True

    if POST_REVIEW_COMMENT:
        post_github_comment(review, marker=review_comment_marker(reviewer))

    if native_review:
        try:
            result = post_native_review(review, expected_head=HEAD_SHA)
        except NativeReviewError as exc:
            _report_native_review_failure(reviewer, exc)
            return False
        print(
            f"[{reviewer.skill}] Native review {result['review_id']} read back "
            f"as {result['state']} on {result['commit_id'][:12]}"
        )

    if parse_review_verdict(review) == "REQUEST_CHANGES":
        file_neotoma_issue(
            title=f"{reviewer.display}: PR #{PR_NUMBER} requests changes",
            body=(
                f"{reviewer.display} automated review found issues in PR "
                f"#{PR_NUMBER} ({REPO}).\n\nHead SHA: {HEAD_SHA}\n\n"
                f"Review:\n\n{review}"
            ),
            agent=reviewer.skill,
        )
    return True


def main() -> None:
    if not PR_NUMBER:
        print("[loxia] LOXIA_PR_NUMBER not set — nothing to review", file=sys.stderr)
        sys.exit(1)

    changed_files: list[str] | None = None
    if NATIVE_REVIEW_ONLY:
        changed_files = get_changed_files()
        if not is_foundation_pr(changed_files):
            print("[loxia] PR is outside the bounded foundation surface — skipping")
            return

    # Fail loud, not silent: a review job that can't actually call Claude must
    # not exit green — that gives a false "reviewed" signal on the PR. Accept
    # EITHER the Max subscription OAuth token (preferred) or the metered key. If
    # neither is available (e.g. forks without secret access), set
    # LOXIA_ALLOW_NO_KEY=true to downgrade to a skip that still exits 0.
    if not CLAUDE_OAUTH_TOKEN and not ANTHROPIC_API_KEY:
        msg = (
            "[loxia] No Claude credential set — cannot perform a real review. "
            "Set CLAUDE_CODE_OAUTH_TOKEN (Max subscription, preferred) or the "
            "ANTHROPIC_API_KEY repo secret. Failing so the missing review is "
            "visible rather than a false green."
        )
        if os.environ.get("LOXIA_ALLOW_NO_KEY", "false").lower() == "true":
            print(msg + " (LOXIA_ALLOW_NO_KEY=true — exiting 0)", file=sys.stderr)
            sys.exit(0)
        print(msg, file=sys.stderr)
        sys.exit(1)

    print(f"[loxia] Reviewing PR #{PR_NUMBER} in {REPO} (dry_run={DRY_RUN})")

    diff = get_pr_diff()
    if changed_files is None:
        changed_files = get_changed_files()

    reviewers = select_reviewers(changed_files)
    print(
        f"[loxia] Reviewers: {', '.join(r.display for r in reviewers)} "
        f"({len(changed_files)} changed files)"
    )

    # Track whether every selected reviewer produced a real review. If any
    # failed to (auth/credit/network/empty response), exit non-zero so the
    # GHA check is red/failed rather than a false green — a review that
    # reviewed nothing must never look merge-ready.
    failed = [
        reviewer.display
        for reviewer in reviewers
        if not run_reviewer(
            reviewer,
            diff,
            changed_files,
            native_review=should_post_native_review(reviewer, changed_files),
        )
    ]

    if failed:
        print(
            f"[loxia] {len(failed)} reviewer(s) failed to produce a review: "
            f"{', '.join(failed)} — failing the job.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
