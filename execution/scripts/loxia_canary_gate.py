#!/usr/bin/env python3
"""
Loxia canary gate — decides whether the Loxia PR review job should run.

Bootstrap mode (agent_policy ent_d0f1a840e549b3b299f62397) runs software work
through the operator/lens review path; only the `swarm-canary` lane still
goes through the swarm pipeline end to end. Loxia re-reviewing every PR push
duplicated that bootstrap review and burned the same Claude subscription
quota bootstrap needed, so this gate restricts Loxia to the canary lane:

  - the PR itself carries the canary label, or
  - the PR's linked parent issue (via a closing/parentage keyword in the PR
    body, e.g. "Closes #123", "Part of #123") carries it.

Matching reuses `lib/daemon_runtime/label_gate.py` — the SAME module Apis
and Anthus import for their own ATELES_SWARM_REQUIRE_LABEL gate (ateles#1269)
— so this workflow cannot drift into a different notion of "labelled" than
the daemons that actually dispatch swarm work.

`label_gate.py` is loaded by FILE PATH (`importlib.util.spec_from_file_location`),
never via `from lib.daemon_runtime import label_gate` / `import lib.daemon_runtime`.
A normal package import runs `lib/daemon_runtime/__init__.py`, which
unconditionally imports `agent_loader`, `sse_client`, `aauth_signer`, and
`grant_checker` — and `agent_loader` does `import httpx` at module level. The
gate job's runner installs nothing (by design — it should stay cheap and run
on every PR push, canary or not), so a package import crashes it with
`ModuleNotFoundError: No module named 'httpx'` (caught live on ateles#1315's
own PR, run 36242051053). `label_gate.py` itself has no such imports — stdlib
only (`json`, `os`, `re`, `collections.abc`, `typing`) — so loading it
standalone, bypassing `__init__.py` entirely, needs nothing extra installed.

Environment variables (set by loxia-pr-review.yml `gate` job):
  CANARY_LABEL      the label name that admits the canary lane
  EVENT_NAME        github.event_name (expected: "pull_request")
  PR_LABELS_JSON    JSON array of the PR's current labels (GitHub shape)
  PR_BODY           the PR body text, for parent-issue resolution
  REPO              "owner/repo"
  GITHUB_TOKEN      only used if a parent-issue lookup is needed

Writes `run_review=true` or `run_review=false` to $GITHUB_OUTPUT. Never
raises past main() — an unreadable input denies (no review), matching
label_gate's own "malformed label data denies, never raises" rule, since a
crash here must not accidentally make the gate step fail red for an
unrelated reason.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_LABEL_GATE_PATH = _REPO_ROOT / "lib" / "daemon_runtime" / "label_gate.py"


def _load_label_gate() -> ModuleType:
    """Load label_gate.py by file path, bypassing lib/daemon_runtime/__init__.py.

    A normal package import (`from lib.daemon_runtime import label_gate`)
    runs the package `__init__.py` first, which unconditionally imports
    `agent_loader` -> `httpx`. This job installs nothing beyond the stdlib on
    purpose, so that import crashes with `ModuleNotFoundError: No module
    named 'httpx'` (ateles#1315 CI run 36242051053). `label_gate.py` itself
    is stdlib-only, so loading it directly — never touching `__init__.py` —
    needs nothing extra installed.
    """
    spec = importlib.util.spec_from_file_location(
        "loxia_canary_gate_label_gate", _LABEL_GATE_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for {_LABEL_GATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


label_gate = _load_label_gate()

GITHUB_API_URL = "https://api.github.com"


def _write_output(run_review: bool) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    line = f"run_review={'true' if run_review else 'false'}\n"
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    print(f"run_review={'true' if run_review else 'false'}")


def _fetch_issue_labels(repo: str, issue_number: int, token: str) -> list[str]:
    """Label names on issue `issue_number` in `repo`, or [] if unreadable."""
    url = f"{GITHUB_API_URL}/repos/{repo}/issues/{issue_number}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
    ):
        return []
    return label_gate.label_names(payload)


def main() -> int:
    canary_label = label_gate.required_label(
        {"ATELES_SWARM_REQUIRE_LABEL": os.environ.get("CANARY_LABEL", "")}
    )
    if not canary_label:
        # No canary label configured at all — fail closed (no review), since
        # an empty gate here would silently mean "review everything again",
        # the exact duplication this gate exists to stop.
        print("CANARY_LABEL is empty — denying (fail closed).", file=sys.stderr)
        _write_output(False)
        return 0

    event_name = os.environ.get("EVENT_NAME", "")
    if event_name != "pull_request":
        print(f"Unexpected EVENT_NAME={event_name!r} — denying.", file=sys.stderr)
        _write_output(False)
        return 0

    try:
        pr_labels_raw = json.loads(os.environ.get("PR_LABELS_JSON") or "[]")
    except json.JSONDecodeError:
        pr_labels_raw = []
    pr_labels = label_gate.label_names({"labels": pr_labels_raw})

    if label_gate.carries_label(canary_label, pr_labels):
        print(f"PR carries {canary_label!r} directly — running review.")
        _write_output(True)
        return 0

    repo = os.environ.get("REPO", "")
    pr_body = os.environ.get("PR_BODY") or ""
    parent = label_gate.parent_issue_number(pr_body, repository=repo)
    if parent is None:
        print("No canary label on the PR and no parent issue link found — denying.")
        _write_output(False)
        return 0

    token = os.environ.get("GITHUB_TOKEN", "")
    parent_labels = _fetch_issue_labels(repo, parent, token)
    if label_gate.carries_label(canary_label, parent_labels):
        print(f"Parent issue #{parent} carries {canary_label!r} — running review.")
        _write_output(True)
        return 0

    print(
        f"Neither the PR nor parent issue #{parent} carries {canary_label!r} — denying."
    )
    _write_output(False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
