"""Tests for the GitHub webhook gateway (ateles#80)."""

import hashlib
import hmac
import json

from github_gateway import parse_github_event, verify_github_signature

TEST_HMAC_KEY = "dummy-hmac-fixture-key"


def _sign(body: bytes, secret: str = TEST_HMAC_KEY) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _issue_payload(action="opened", **issue_extra):
    issue = {
        "number": 80,
        "title": "Wire webhooks",
        "body": "the trigger layer",
        "html_url": "https://github.com/o/r/issues/80",
        "user": {"login": "markmhendrickson"},
        "labels": [{"name": "enhancement"}],
        **issue_extra,
    }
    return {
        "action": action,
        "repository": {"full_name": "o/r"},
        "issue": issue,
    }


def _pr_payload(action="opened"):
    return {
        "action": action,
        "repository": {"full_name": "o/r"},
        "pull_request": {
            "number": 12,
            "title": "Fix the thing",
            "body": "closes #80",
            "html_url": "https://github.com/o/r/pull/12",
            "user": {"login": "ateles-agent"},
            "labels": [],
            "head": {"ref": "feat/x"},
            "base": {"ref": "main"},
        },
    }


# ── Signature verification ─────────────────────────────────────────────────


def test_valid_signature_accepted():
    body = json.dumps(_issue_payload()).encode()
    assert verify_github_signature(TEST_HMAC_KEY, body, _sign(body))


def test_wrong_secret_rejected():
    body = b"{}"
    assert not verify_github_signature(TEST_HMAC_KEY, body, _sign(body, "other-secret"))


def test_tampered_body_rejected():
    sig = _sign(b'{"a":1}')
    assert not verify_github_signature(TEST_HMAC_KEY, b'{"a":2}', sig)


def test_malformed_header_rejected():
    assert not verify_github_signature(TEST_HMAC_KEY, b"{}", "")
    assert not verify_github_signature(TEST_HMAC_KEY, b"{}", "sha1=deadbeef")


# ── Event parsing ──────────────────────────────────────────────────────────


def test_issue_opened_parses():
    t = parse_github_event("issues", _issue_payload(), "d-1")
    assert t is not None
    assert t.kind == "issue_opened"
    assert t.repository == "o/r"
    assert t.number == 80
    assert t.labels == ["enhancement"]
    assert not t.is_pr


def test_issue_closed_ignored():
    assert parse_github_event("issues", _issue_payload(action="closed")) is None


def test_pr_surfaced_as_issue_ignored():
    payload = _issue_payload(pull_request={"url": "..."})
    assert parse_github_event("issues", payload) is None


def test_pr_opened_parses():
    t = parse_github_event("pull_request", _pr_payload(), "d-2")
    assert t is not None
    assert t.kind == "pr_opened"
    assert t.is_pr
    assert t.head_ref == "feat/x"
    assert t.base_ref == "main"


def test_pr_synchronize_parses():
    t = parse_github_event("pull_request", _pr_payload(action="synchronize"))
    assert t is not None
    assert t.kind == "pr_synchronize"


def test_pr_closed_ignored():
    assert parse_github_event("pull_request", _pr_payload(action="closed")) is None


def test_unknown_event_ignored():
    assert parse_github_event("workflow_run", {"action": "completed"}) is None


def test_null_body_normalizes_to_empty_string():
    payload = _issue_payload(body=None)
    t = parse_github_event("issues", payload)
    assert t is not None
    assert t.body == ""
