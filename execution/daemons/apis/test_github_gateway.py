"""Tests for the GitHub webhook gateway (ateles#80)."""

import asyncio
import hashlib
import hmac
import json

from aiohttp.test_utils import TestClient, TestServer

from github_gateway import make_app, parse_github_event, verify_github_signature

TEST_HMAC_KEY = "dummy-hmac-fixture-key"


def _post_webhook(secret: str, body: bytes, headers: dict) -> int:
    async def run() -> int:
        async def handler(trigger):
            pass

        app = make_app(secret, handler)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/github/webhook", data=body, headers=headers)
            return resp.status

    return asyncio.run(run())


def test_unset_secret_rejects_delivery_fail_closed():
    # Loxia review on PR #87: an unset secret must reject deliveries (503),
    # never accept unsigned ones — the gateway sits behind a public tunnel.
    status = _post_webhook("", b"{}", {"X-GitHub-Event": "ping"})
    assert status == 503


def test_signed_ping_accepted():
    body = b"{}"
    status = _post_webhook(
        TEST_HMAC_KEY,
        body,
        {"X-GitHub-Event": "ping", "X-Hub-Signature-256": _sign(body)},
    )
    assert status == 200


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


# ── pull_request_review parsing (approval loop) ─────────────────────────────


def _review_payload(action="submitted", state="approved", reviewer="markmhendrickson"):
    return {
        "action": action,
        "repository": {"full_name": "o/r"},
        "review": {"state": state, "user": {"login": reviewer}},
        "pull_request": {
            "number": 12,
            "title": "Fix the thing",
            "body": "closes #80",
            "html_url": "https://github.com/o/r/pull/12",
            "user": {"login": "ateles-agent"},
            "head": {"ref": "feat/x"},
            "base": {"ref": "main"},
        },
    }


def test_pr_review_approved_parses():
    t = parse_github_event("pull_request_review", _review_payload(), "d-r1")
    assert t is not None
    assert t.kind == "pr_review"
    assert t.number == 12
    assert t.review_state == "approved"
    assert t.review_author == "markmhendrickson"
    # pr_review must NOT be treated as a pipeline-firing PR event.
    assert t.is_pr is False


def test_pr_review_state_lowercased():
    # GitHub sends "APPROVED"/"CHANGES_REQUESTED" in some payloads.
    t = parse_github_event("pull_request_review", _review_payload(state="APPROVED"))
    assert t is not None
    assert t.review_state == "approved"


def test_pr_review_changes_requested_parses_but_not_approved():
    t = parse_github_event(
        "pull_request_review", _review_payload(state="changes_requested")
    )
    assert t is not None
    assert t.kind == "pr_review"
    assert t.review_state == "changes_requested"


def test_pr_review_dismissed_action_ignored():
    # Only "submitted" carries a fresh verdict; "dismissed"/"edited" are ignored.
    assert (
        parse_github_event("pull_request_review", _review_payload(action="dismissed"))
        is None
    )


def test_pr_closed_ignored():
    assert parse_github_event("pull_request", _pr_payload(action="closed")) is None


def test_unknown_event_ignored():
    assert parse_github_event("workflow_run", {"action": "completed"}) is None


def test_null_body_normalizes_to_empty_string():
    payload = _issue_payload(body=None)
    t = parse_github_event("issues", payload)
    assert t is not None
    assert t.body == ""


# ── issue_comment events (ateles#112) ─────────────────────────────────────


def _issue_comment_payload(
    action="created",
    on_pr=True,
    comment_body="/confirm-gates-clear",
    comment_author="markmhendrickson",
):
    """Build an issue_comment webhook payload.

    GitHub sends PR comments as issue_comment with issue.pull_request present.
    """
    issue: dict = {
        "number": 80,
        "title": "Wire webhooks",
        "body": "the trigger layer",
        "html_url": "https://github.com/o/r/issues/80",
        "user": {"login": "contributor"},
        "labels": [{"name": "enhancement"}],
    }
    if on_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/o/r/pulls/80"}
    comment = {
        "id": 12345,
        "user": {"login": comment_author},
        "body": comment_body,
        "html_url": "https://github.com/o/r/issues/80#issuecomment-12345",
    }
    return {
        "action": action,
        "repository": {"full_name": "o/r"},
        "issue": issue,
        "comment": comment,
    }


def test_issue_comment_on_pr_parses():
    """A PR comment (issue_comment with pull_request field) should parse as issue_comment."""
    t = parse_github_event("issue_comment", _issue_comment_payload(on_pr=True), "d-3")
    assert t is not None
    assert t.kind == "issue_comment"
    assert t.repository == "o/r"
    assert t.number == 80
    assert t.comment_on_pr is True
    assert t.comment_author == "markmhendrickson"
    assert t.comment_body == "/confirm-gates-clear"
    assert t.comment_id == 12345
    assert not t.is_pr


def test_issue_comment_on_plain_issue_parses():
    """A comment on a plain issue (no pull_request field) also parses."""
    t = parse_github_event("issue_comment", _issue_comment_payload(on_pr=False), "d-4")
    assert t is not None
    assert t.kind == "issue_comment"
    assert t.comment_on_pr is False


def test_issue_comment_deleted_ignored():
    """Only 'created' action is routed; 'deleted'/'edited' are ignored."""
    payload = _issue_comment_payload(action="deleted")
    assert parse_github_event("issue_comment", payload) is None


def test_issue_comment_edited_ignored():
    payload = _issue_comment_payload(action="edited")
    assert parse_github_event("issue_comment", payload) is None


def test_existing_events_unaffected_by_issue_comment_addition():
    """issue_comment addition must not break existing issue/PR event parsing."""
    issue_t = parse_github_event("issues", _issue_payload(), "d-5")
    assert issue_t is not None and issue_t.kind == "issue_opened"

    pr_t = parse_github_event("pull_request", _pr_payload(), "d-6")
    assert pr_t is not None and pr_t.kind == "pr_opened"


def test_issue_comment_payload_does_not_set_is_pr():
    """issue_comment triggers are NOT PR triggers (is_pr must be False)."""
    t = parse_github_event("issue_comment", _issue_comment_payload(), "d-7")
    assert t is not None
    assert not t.is_pr


# ── /approve-email internal route (approval loop) ───────────────────────────


def _post_approve_email(app_secret, header_secret, body_obj):
    """POST to /approve-email; return (status, captured_trigger_or_None)."""
    captured = {}

    async def run():
        async def handler(trigger):
            captured["trigger"] = trigger

        app = make_app(TEST_HMAC_KEY, handler, approve_email_secret=app_secret)
        headers = {}
        if header_secret is not None:
            headers["X-Approve-Secret"] = header_secret
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/approve-email", data=json.dumps(body_obj), headers=headers
            )
            # Give the fire-and-forget handler task a tick to run.
            await asyncio.sleep(0)
            return resp.status

    status = asyncio.run(run())
    return status, captured.get("trigger")


def test_approve_email_unset_secret_fails_closed():
    status, trig = _post_approve_email(
        "", "anything", {"repository": "o/r", "pr_number": 5}
    )
    assert status == 503
    assert trig is None


def test_approve_email_wrong_secret_rejected():
    status, trig = _post_approve_email(
        "right", "wrong", {"repository": "o/r", "pr_number": 5}
    )
    assert status == 401
    assert trig is None


def test_approve_email_missing_fields_rejected():
    status, _ = _post_approve_email("s", "s", {"repository": "o/r"})
    assert status == 400
    status2, _ = _post_approve_email("s", "s", {"pr_number": 5})
    assert status2 == 400


def test_approve_email_accepted_builds_operator_trigger():
    status, trig = _post_approve_email(
        "s", "s", {"repository": "o/r", "pr_number": 5, "sender": "op@x"}
    )
    assert status == 200
    assert trig is not None
    assert trig.kind == "email_approve"
    assert trig.repository == "o/r"
    assert trig.number == 5
    assert trig.review_state == "approved"
    # Attributed to the operator login so the shared handler's guard passes.
    assert trig.review_author == "markmhendrickson"


# ── check_suite parsing (CI-driven loop closure, ateles#197) ────────────────


def _check_suite_payload(action="completed", conclusion="success",
                         head_sha="deadbeef", prs=((7, "PR title"),)):
    return {
        "action": action,
        "repository": {"full_name": "o/r"},
        "check_suite": {
            "head_sha": head_sha,
            "conclusion": conclusion,
            "pull_requests": [{"number": n, "title": t} for n, t in prs],
        },
    }


def test_check_suite_completed_parses_to_ci_status():
    t = parse_github_event("check_suite", _check_suite_payload(), "d-ci")
    assert t is not None
    assert t.kind == "ci_status"
    assert t.repository == "o/r"
    assert t.number == 7
    assert t.ci_head_sha == "deadbeef"
    assert t.ci_conclusion == "success"
    assert t.ci_pr_numbers == [7]


def test_check_suite_failure_conclusion_lowercased():
    t = parse_github_event("check_suite", _check_suite_payload(conclusion="FAILURE"))
    assert t is not None
    assert t.ci_conclusion == "failure"


def test_check_suite_non_completed_action_ignored():
    # Only the terminal "completed" action drives the pipeline; requested/etc. no-op.
    assert parse_github_event("check_suite", _check_suite_payload(action="requested")) is None


def test_check_suite_with_no_prs_parses_with_zero_number():
    t = parse_github_event("check_suite", _check_suite_payload(prs=()))
    assert t is not None
    assert t.kind == "ci_status"
    assert t.number == 0
    assert t.ci_pr_numbers == []


def test_status_event_not_handled():
    # `status` events fire per-context and would multiply per head; only
    # check_suite:completed is ingested.
    assert parse_github_event("status", {"repository": {"full_name": "o/r"}}) is None
