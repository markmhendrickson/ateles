"""Tests for the Loxia canary gate (loxia-pr-review.yml `gate` job).

Bootstrap mode restricts the swarm review panel to the `swarm-canary` lane
(agent_policy ent_d0f1a840e549b3b299f62397, ateles#1269); Loxia re-reviewing
every PR push duplicated that work and burned the same Claude subscription
quota bootstrap needed. These tests drive the actual gate logic — the same
`main()` the GHA workflow invokes — through env vars and a stubbed
`urlopen` for the parent-issue lookup, so no network or real GitHub token is
needed.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loxia_canary_gate as gate  # noqa: E402


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _base_env(monkeypatch, tmp_path, **overrides):
    out_path = tmp_path / "gh_output"
    out_path.write_text("")
    env = {
        "CANARY_LABEL": "swarm-canary",
        "EVENT_NAME": "pull_request",
        "REPO": "markmhendrickson/ateles",
        "PR_LABELS_JSON": "[]",
        "PR_BODY": "",
        "GITHUB_TOKEN": "",
        "GITHUB_OUTPUT": str(out_path),
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return out_path


def _run_review_value(out_path: Path) -> str:
    text = out_path.read_text()
    for line in text.splitlines():
        if line.startswith("run_review="):
            return line.split("=", 1)[1]
    raise AssertionError(f"run_review not written to GITHUB_OUTPUT: {text!r}")


def test_pr_carries_canary_label_directly(monkeypatch, tmp_path):
    out_path = _base_env(
        monkeypatch, tmp_path, PR_LABELS_JSON=json.dumps([{"name": "swarm-canary"}])
    )
    assert gate.main() == 0
    assert _run_review_value(out_path) == "true"


def test_pr_with_unrelated_label_and_no_parent_link_denies(monkeypatch, tmp_path):
    out_path = _base_env(
        monkeypatch,
        tmp_path,
        PR_LABELS_JSON=json.dumps([{"name": "bug"}]),
        PR_BODY="No link here.",
    )
    assert gate.main() == 0
    assert _run_review_value(out_path) == "false"


def test_parent_issue_carries_canary_label(monkeypatch, tmp_path):
    out_path = _base_env(monkeypatch, tmp_path, PR_BODY="Closes #42")
    monkeypatch.setattr(
        gate.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResp(
            json.dumps({"labels": [{"name": "swarm-canary"}]}).encode()
        ),
    )
    assert gate.main() == 0
    assert _run_review_value(out_path) == "true"


def test_parent_issue_without_canary_label_denies(monkeypatch, tmp_path):
    out_path = _base_env(monkeypatch, tmp_path, PR_BODY="Part of #99")
    monkeypatch.setattr(
        gate.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResp(json.dumps({"labels": [{"name": "bug"}]}).encode()),
    )
    assert gate.main() == 0
    assert _run_review_value(out_path) == "false"


def test_unreadable_parent_issue_lookup_denies_not_raises(monkeypatch, tmp_path):
    out_path = _base_env(monkeypatch, tmp_path, PR_BODY="Fixes #7")

    def _boom(*a, **k):
        raise TimeoutError("network unavailable")

    monkeypatch.setattr(gate.urllib.request, "urlopen", _boom)
    assert gate.main() == 0
    assert _run_review_value(out_path) == "false"


def test_empty_canary_label_fails_closed(monkeypatch, tmp_path):
    # An unconfigured gate must NOT mean "review everything" — that is the
    # exact duplication this gate exists to stop.
    out_path = _base_env(
        monkeypatch,
        tmp_path,
        CANARY_LABEL="",
        PR_LABELS_JSON=json.dumps([{"name": "swarm-canary"}]),
    )
    assert gate.main() == 0
    assert _run_review_value(out_path) == "false"


def test_non_pull_request_event_denies(monkeypatch, tmp_path):
    out_path = _base_env(
        monkeypatch,
        tmp_path,
        EVENT_NAME="push",
        PR_LABELS_JSON=json.dumps([{"name": "swarm-canary"}]),
    )
    assert gate.main() == 0
    assert _run_review_value(out_path) == "false"


def test_malformed_pr_labels_json_denies_not_raises(monkeypatch, tmp_path):
    out_path = _base_env(monkeypatch, tmp_path, PR_LABELS_JSON="not json")
    assert gate.main() == 0
    assert _run_review_value(out_path) == "false"


@pytest.mark.parametrize("repo_qualifier", ["markmhendrickson/ateles", ""])
def test_parent_link_cross_repo_qualifier_ignored_when_same_repo(
    monkeypatch, tmp_path, repo_qualifier
):
    # A bare #N (no owner/repo qualifier) always resolves against REPO.
    out_path = _base_env(
        monkeypatch, tmp_path, PR_BODY="Refs #5", REPO="markmhendrickson/ateles"
    )
    monkeypatch.setattr(
        gate.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResp(
            json.dumps({"labels": [{"name": "swarm-canary"}]}).encode()
        ),
    )
    assert gate.main() == 0
    assert _run_review_value(out_path) == "true"
