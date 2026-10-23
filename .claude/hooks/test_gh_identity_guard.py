#!/usr/bin/env python3
"""Exercise gh_identity_guard.py against block/allow cases.

The hazard: `GH_TOKEN=""` (empty but SET) is not "no token" to `gh` — it
silently falls back to the ambient keyring identity instead of erroring,
which is how a PR landed under the operator's own account instead of the
intended agent's. This hook catches the literal empty-token-prefix shape on
a mutating `gh` call at PreToolUse, before the network round-trip;
execution/scripts/verify_gh_identity.py is the authoritative live check for
everywhere else (daemons, scripts) that a shell-command pattern can't see.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = str(Path(__file__).with_name("gh_identity_guard.py"))

BLOCK = [
    ("empty GH_TOKEN, double-quoted, pr create", 'GH_TOKEN="" gh pr create --title x --body-file b.md'),
    ("empty GH_TOKEN, single-quoted, pr create", "GH_TOKEN='' gh pr create --title x"),
    ("GH_TOKEN= with nothing after, pr create", "GH_TOKEN= gh pr create --title x"),
    ("empty GITHUB_TOKEN, pr merge", 'GITHUB_TOKEN="" gh pr merge 123 --squash'),
    ("empty GH_TOKEN, issue create", 'GH_TOKEN="" gh issue create --title x --body y'),
    ("empty GH_TOKEN, issue comment", 'GH_TOKEN="" gh issue comment 5 --body hi'),
    ("empty GH_TOKEN, release create", 'GH_TOKEN="" gh release create v1.0.0'),
    ("empty GH_TOKEN, api POST via -X", 'GH_TOKEN="" gh api repos/o/r/issues -X POST -f title=x'),
    ("empty GH_TOKEN, api POST via --method", 'GH_TOKEN="" gh api repos/o/r/issues --method POST -f title=x'),
    ("empty GH_TOKEN with env prefix form", 'env GH_TOKEN="" gh pr create --title x'),
    ("hidden after innocuous first segment", 'echo staging && GH_TOKEN="" gh pr create --title x'),
    ("hidden after cd", 'cd /tmp && GH_TOKEN="" gh pr create --title x'),
    ("multiple segments, violation last", 'echo a; echo b; GH_TOKEN="" gh pr merge 1'),
    ("newline-separated segments", 'echo a\nGH_TOKEN="" gh pr create --title x'),
]

ALLOW = [
    ("valid-looking token, pr create", "GH_TOKEN=ghp_realvaluehere1234567890 gh pr create --title x"),
    ("valid-looking token, pr merge", "GH_TOKEN=ghp_realvaluehere1234567890 gh pr merge 1 --squash"),
    ("empty token but read-only pr view", 'GH_TOKEN="" gh pr view 123'),
    ("empty token but read-only pr list", 'GH_TOKEN="" gh pr list'),
    ("empty token but read-only issue view", 'GH_TOKEN="" gh issue view 5'),
    ("empty token but api GET (no mutating verb)", 'GH_TOKEN="" gh api repos/o/r/issues'),
    ("empty token but auth status", 'GH_TOKEN="" gh auth status'),
    ("empty token, no gh at all", 'GH_TOKEN="" echo hello'),
    ("no gh mentioned", "echo hello world"),
    ("commit message documenting the hazard", 'git commit -m "fix: block GH_TOKEN=\\"\\" gh pr create"'),
    ("real token present, unrelated later segment", "GH_TOKEN=ghp_x gh pr create --title x && echo done"),
]


def run(command, tool="Bash"):
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    p = subprocess.run(
        [sys.executable, HOOK],
        input=payload,
        capture_output=True,
        text=True,
    )
    return p.returncode


@pytest.mark.parametrize("label,cmd", BLOCK, ids=[label for label, _ in BLOCK])
def test_block_empty_token_mutating_gh(label, cmd):
    assert run(cmd) == 2, label


@pytest.mark.parametrize("label,cmd", ALLOW, ids=[label for label, _ in ALLOW])
def test_allow_safe_or_read_only(label, cmd):
    assert run(cmd) == 0, label


def test_non_bash_tool_is_ignored():
    assert run('GH_TOKEN="" gh pr create --title x', tool="Edit") == 0


def test_malformed_json_fail_open():
    p = subprocess.run(
        [sys.executable, HOOK], input="not json", capture_output=True, text=True
    )
    assert p.returncode == 0


def test_empty_stdin_fail_open():
    p = subprocess.run(
        [sys.executable, HOOK], input="", capture_output=True, text=True
    )
    assert p.returncode == 0


def test_missing_tool_input_fail_open():
    p = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_name": "Bash"}),
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
