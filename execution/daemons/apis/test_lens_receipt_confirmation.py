"""A lens receipt clears only after an admitted current-head verdict is read back."""

from __future__ import annotations

import pytest

import fly_runner
import lens_receipts
import swarm_dispatch as sd
from lib import github_app_token as gat

REPO = "owner/repo"
NUMBER = 7
HEAD = "a" * 40
KEY = lens_receipts.lens_key(REPO, NUMBER, HEAD, "qa")
AUTHOR = "swarm-lens-account"


class _Response:
    def __init__(self, comments):
        self.comments = comments

    def raise_for_status(self):
        pass

    def json(self):
        return self.comments


class _Client:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        page = params["page"]
        self.requested.append(page)
        return _Response(self.pages.get(page, []))


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, message, **kwargs):
        self.sent.append((message, kwargs))
        return True


def _dispatcher(monkeypatch, pages=None):
    notifier = _Notifier()
    dispatcher = sd.SwarmDispatcher(notifier)
    client = _Client(pages or {})
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(dispatcher, "_github_headers", lambda repository: {})
    monkeypatch.setattr(sd, "lens_comment_authors", lambda: frozenset({AUTHOR}))
    return dispatcher, client, notifier


def _comment(author=AUTHOR, lens="qa"):
    return {
        "user": {"login": author},
        "body": f"<!-- review:{lens} commit={HEAD} -->\n**APPROVE**",
    }


def _full_pages(marker=None):
    pages = {page: [{"body": "ordinary comment"}] * 100 for page in range(1, 51)}
    if marker is not None:
        pages[50][-1] = marker
    return pages


def _awaiting(monkeypatch, tmp_path, *, run_id="run-1"):
    path = tmp_path / "ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(path))
    assert fly_runner.FlyConfig.from_env().ledger_path == path
    ledger = fly_runner.FlyLedger(path)
    outcome, reservation = lens_receipts.reserve(
        KEY, lens_receipts.meta_for(REPO, NUMBER, HEAD, "qa"), ledger=ledger
    )
    assert outcome == "reserved"
    assert lens_receipts.settle(
        reservation, lens_receipts.AWAITING, ledger=ledger, run_id=run_id
    )
    return reservation, path


@pytest.mark.asyncio
async def test_scan_reports_page_cap_and_preserves_old_list_contract(monkeypatch):
    pages = _full_pages()
    dispatcher, client, _ = _dispatcher(monkeypatch, pages)
    comments, complete = await dispatcher._issue_comments_scan(REPO, NUMBER, client)
    assert len(comments) == 5000
    assert complete is False
    assert client.requested == list(range(1, 51))
    assert await dispatcher._all_issue_comments(REPO, NUMBER, client) == comments

    short = {1: [{"body": "one"}]}
    dispatcher, client, _ = _dispatcher(monkeypatch, short)
    assert await dispatcher._issue_comments_scan(REPO, NUMBER, client) == (
        short[1], True
    )
    assert await dispatcher._all_issue_comments(REPO, NUMBER, client) == short[1]


@pytest.mark.asyncio
async def test_partial_scan_without_marker_is_unknown(monkeypatch):
    dispatcher, _, _ = _dispatcher(monkeypatch, _full_pages())
    assert await dispatcher._lens_verdict_state(REPO, NUMBER, "qa", HEAD) == "unknown"


@pytest.mark.asyncio
async def test_partial_scan_with_admitted_marker_is_confirmed(monkeypatch):
    dispatcher, _, _ = _dispatcher(monkeypatch, _full_pages(_comment()))
    assert await dispatcher._lens_verdict_state(REPO, NUMBER, "qa", HEAD) == "confirmed"


@pytest.mark.asyncio
async def test_complete_scan_without_marker_is_absent(monkeypatch):
    dispatcher, _, _ = _dispatcher(monkeypatch, {1: [{"body": "ordinary comment"}]})
    assert await dispatcher._lens_verdict_state(REPO, NUMBER, "qa", HEAD) == "absent"


@pytest.mark.asyncio
async def test_comment_read_error_is_unknown(monkeypatch):
    dispatcher, client, _ = _dispatcher(monkeypatch)

    async def fail(*args, **kwargs):
        raise OSError("comment API unavailable")

    monkeypatch.setattr(client, "get", fail)
    assert await dispatcher._lens_verdict_state(REPO, NUMBER, "qa", HEAD) == "unknown"


@pytest.mark.asyncio
async def test_a_lens_app_does_not_accept_shared_identity(monkeypatch):
    shared = _comment(lens="pm")
    dispatcher, _, _ = _dispatcher(monkeypatch, {1: [shared]})
    prefix = gat.identity_env_prefix("reviewer_pm")
    monkeypatch.setenv(f"{prefix}_ID", "1")
    monkeypatch.setenv(f"{prefix}_PRIVATE_KEY", "fake-key")
    monkeypatch.setenv(sd.lens_authors.ENV_AUTHORS, AUTHOR)
    monkeypatch.setattr(gat, "identity_bot_login", lambda identity: "pm-app[bot]")
    monkeypatch.setattr(sd, "lens_comment_authors", lambda: sd.lens_authors.resolve().authors)
    assert await dispatcher._lens_verdict_state(REPO, NUMBER, "pm", HEAD) == "absent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted", "verdict", "expected_held"),
    [(False, "confirmed", True), (True, "unknown", True), (True, "confirmed", False)],
)
async def test_settle_requires_both_persistence_and_read_back(
    monkeypatch, tmp_path, persisted, verdict, expected_held
):
    reservation, path = _awaiting(monkeypatch, tmp_path)
    dispatcher, _, notifier = _dispatcher(monkeypatch)

    async def verdict_state(*args):
        return verdict

    monkeypatch.setattr(dispatcher, "_lens_verdict_state", verdict_state)
    held = await dispatcher._settle_lens_receipts(
        REPO, NUMBER, HEAD, [("qa", reservation)], persisted=persisted
    )
    assert held == ({"qa"} if expected_held else set())
    entry = lens_receipts.held(KEY, ledger=fly_runner.FlyLedger(path))
    assert (entry is not None) == expected_held
    assert len(notifier.sent) == int(expected_held)
    if expected_held:
        message, kwargs = notifier.sent[0]
        assert KEY in message and "run-1" in message
        assert "merge is held" in message
        assert sd.build_delivery.FLY_RECEIPT_COMMAND in message
        assert kwargs["priority"] == sd.Priority.BLOCKER


@pytest.mark.asyncio
async def test_failed_release_keeps_receipt_and_notifies(monkeypatch, tmp_path):
    reservation, path = _awaiting(monkeypatch, tmp_path)
    dispatcher, _, notifier = _dispatcher(monkeypatch)

    async def confirmed(*args):
        return "confirmed"

    monkeypatch.setattr(dispatcher, "_lens_verdict_state", confirmed)
    monkeypatch.setattr(sd.lens_receipts, "release", lambda *args, **kwargs: False)
    assert await dispatcher._settle_lens_receipts(
        REPO, NUMBER, HEAD, [("qa", reservation)], persisted=True
    ) == {"qa"}
    assert lens_receipts.held(KEY, ledger=fly_runner.FlyLedger(path)) is not None
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_notification_claim_survives_dispatcher_restart(monkeypatch, tmp_path):
    _, path = _awaiting(monkeypatch, tmp_path, run_id=None)
    first, _, first_notifier = _dispatcher(monkeypatch)
    second, _, second_notifier = _dispatcher(monkeypatch)
    assert await first._notify_lens_receipt(KEY) is True
    assert await second._notify_lens_receipt(KEY) is False
    assert len(first_notifier.sent) + len(second_notifier.sent) == 1
    assert "not reported" in first_notifier.sent[0][0]
    assert "notified_at" in lens_receipts.held(KEY, ledger=fly_runner.FlyLedger(path))


@pytest.mark.asyncio
async def test_undelivered_notice_still_cannot_duplicate(monkeypatch, tmp_path, caplog):
    _, path = _awaiting(monkeypatch, tmp_path)
    dispatcher, _, notifier = _dispatcher(monkeypatch)
    monkeypatch.setattr(notifier, "send", lambda *args, **kwargs: False)
    assert await dispatcher._notify_lens_receipt(KEY) is True
    assert "BLOCKER notice was not delivered" in caplog.text
    assert await dispatcher._notify_lens_receipt(KEY) is False
    assert "notified_at" in lens_receipts.held(KEY, ledger=fly_runner.FlyLedger(path))
