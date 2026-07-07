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
        raise ClaudeReviewError(
            f"claude --print exited {proc.returncode}: {err[:400]}"
        )
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


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }


def find_existing_review_comment(marker: str) -> int | None:
    """Return the id of the most recent review comment carrying `marker` on
    this PR, if any.

    Matched by marker rather than author so it works whether the comment was
    posted by github-actions[bot] or a machine account, and so each reviewer
    only ever matches its own comment."""
    url = (
        f"{GITHUB_API_URL}/repos/{REPO}/issues/{PR_NUMBER}/comments"
        "?per_page=100"
    )
    req = urllib.request.Request(url, headers=_github_headers(), method="GET")
    req.add_header("User-Agent", "ateles-neotoma-sync/1.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            comments = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"[loxia] Could not list comments (will POST fresh): {exc}",
              file=sys.stderr)
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


def run_reviewer(reviewer: Reviewer, diff: str, changed_files: list[str]) -> bool:
    """Build the prompt, call Claude, and (unless dry-run) post the comment and
    file a Neotoma issue on REQUEST_CHANGES — all attributed to this reviewer.

    Returns True if a real review was produced, False if it failed. A failure
    posts a clearly-marked "review could not run" comment (so the PR shows the
    reviewer is broken, not silently absent) and the caller exits non-zero.
    """
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

    post_github_comment(review, marker=review_comment_marker(reviewer))

    if "REQUEST_CHANGES" in review:
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
        if not run_reviewer(reviewer, diff, changed_files)
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

# ci: re-trigger marker 9a0a859
