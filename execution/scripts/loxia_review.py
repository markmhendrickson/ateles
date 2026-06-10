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

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
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


def call_claude(prompt: str) -> str:
    """Call the Claude API with a single user message; return the text response."""
    if not ANTHROPIC_API_KEY:
        return "(ANTHROPIC_API_KEY not set — skipping Claude review)"

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

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
        return f"(Claude API error: {exc})"


# ── GitHub comment ─────────────────────────────────────────────────────────────


def post_github_comment(body: str) -> None:
    """Post a review comment to the PR."""
    if not GITHUB_TOKEN or not PR_NUMBER or not REPO:
        print("[loxia] Cannot post comment — missing GITHUB_TOKEN/PR_NUMBER/REPO")
        return

    url = f"{GITHUB_API_URL}/repos/{REPO}/issues/{PR_NUMBER}/comments"
    payload = {"body": body}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"[loxia] Comment posted: {result.get('html_url', '(no url)')}")
    except urllib.error.URLError as exc:
        print(f"[loxia] Failed to post comment: {exc}", file=sys.stderr)


# ── Neotoma issue filing ───────────────────────────────────────────────────────


def file_neotoma_issue(title: str, body: str) -> None:
    """
    File a Neotoma issue for a significant finding.
    audience=agent, severity=medium. Best-effort; never blocks the review.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return

    payload = {
        "entity_type": "issue",
        "canonical_name": f"issue:loxia:pr{PR_NUMBER}:{HEAD_SHA[:8]}",
        "snapshot": {
            "title": title,
            "body": body,
            "audience": "agent",
            "severity": "medium",
            "kind": "code_review",
            "source": f"loxia:pr{PR_NUMBER}",
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

    try:
        with urllib.request.urlopen(req, timeout=15):
            print(f"[loxia] Neotoma issue filed: {title}")
    except urllib.error.URLError as exc:
        print(f"[loxia] Failed to file Neotoma issue: {exc}", file=sys.stderr)


# ── Review prompt ─────────────────────────────────────────────────────────────


REVIEW_PROMPT_TEMPLATE = """\
You are Loxia, a PR review agent for the ateles repository (a public mirror of \
a Neotoma-canonical agent swarm). Review the following pull request diff and \
produce a structured review comment.

Repository: {repo}
PR number: #{pr_number}
Changed files: {changed_files}

Diff (truncated to {max_diff_chars} chars if large):
```diff
{diff}
```

Review checklist — assess each item:
1. **Scope** — do changed files match a coherent, focused purpose? Flag scope creep.
2. **Secrets** — any API keys, tokens, passwords, IBANs, or personal data in the diff?
3. **gitleaks** — if new daemon/script files added that reference env var names \
(BEARER_TOKEN, API_KEY, etc.), is .gitleaks.toml allowlist updated?
4. **Linting** — obvious ruff / yamllint / shellcheck issues visible in the diff?
5. **Pattern consistency** — new daemons should follow the T3 startup pattern \
(AgentLoader → AAuthSigner → Notifier → work loop). New scripts should use httpx \
or stdlib urllib (not requests). Flag deviations.
6. **CLAUDE.md** — if new public files added, does CLAUDE.md need updating?

Output format — use this exact structure:
## Loxia Review 🪶

**Verdict**: APPROVE | REQUEST_CHANGES | COMMENT

### Summary
<1–3 sentences>

### Findings
<bullet list — only include non-empty categories>
- 🔴 **Secrets**: <finding or "none detected">
- 🟡 **Scope**: <finding or "focused">
- 🟡 **gitleaks**: <finding or "allowlist updated / not needed">
- 🟢 **Linting**: <finding or "no issues visible">
- 🟢 **Pattern**: <finding or "follows T3 pattern">
- 🟢 **Docs**: <finding or "no doc updates needed">

### Recommendations
<optional — only if REQUEST_CHANGES>

---
*Loxia automated review · commit {head_sha}*
"""


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    if not PR_NUMBER:
        print("[loxia] LOXIA_PR_NUMBER not set — nothing to review", file=sys.stderr)
        sys.exit(1)

    print(f"[loxia] Reviewing PR #{PR_NUMBER} in {REPO} (dry_run={DRY_RUN})")

    diff = get_pr_diff()
    changed_files = get_changed_files()

    prompt = REVIEW_PROMPT_TEMPLATE.format(
        repo=REPO,
        pr_number=PR_NUMBER,
        changed_files=", ".join(changed_files) if changed_files else "(none)",
        max_diff_chars=MAX_DIFF_CHARS,
        diff=diff,
        head_sha=HEAD_SHA[:12] if HEAD_SHA else "unknown",
    )

    print(f"[loxia] Calling Claude ({CLAUDE_MODEL})...")
    review = call_claude(prompt)

    print("\n" + "=" * 60)
    print(review)
    print("=" * 60 + "\n")

    if DRY_RUN:
        print("[loxia] DRY RUN — not posting comment or filing Neotoma issue")
        return

    post_github_comment(review)

    # File a Neotoma issue if the review contains REQUEST_CHANGES
    if "REQUEST_CHANGES" in review:
        file_neotoma_issue(
            title=f"Loxia: PR #{PR_NUMBER} requests changes",
            body=(
                f"Loxia automated review found issues in PR #{PR_NUMBER} "
                f"({REPO}).\n\nHead SHA: {HEAD_SHA}\n\nReview:\n\n{review}"
            ),
        )


if __name__ == "__main__":
    main()
