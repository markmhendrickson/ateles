"""
EFFECT-level tests for Monedula's Telegram consent gate (#554).

Companion to `test_gate_channel_failure.py`, which stops at `_parse_reply` and
documents the pre-fix defect (channel failure and decline collapse to the same
`set()`). This file exercises the fix: the structured `TelegramPollResult` /
`telegram_poll_approval` poll layer, `main()`'s branch on channel failure vs.
decline vs. approval, the dead-gate streak counter, the Neotoma escalation
writer, and `payment_approved` non-authority.

Layers crossed that the parser file cannot reach: the poll layer that turns a
409/timeout into a structured outcome; `main()`, where that outcome decides
whether money moves and whether the process exits 0 or 1; and the enforcement
loop past `monedula.py:if handler.name not in approved: continue`, which only
a partial approval reaches.

Run with: pytest execution/daemons/monedula/test_telegram_gate.py -v
"""

from __future__ import annotations

import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import monedula  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


class _FakeClock:
    """A monotonic clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _RecordingHandler:
    """Payment handler double that records being asked to pay.

    `execute()` is real and recording on purpose. A double that simply lacks
    the attribute turns a fail-open regression into an `AttributeError` — the
    suite goes red for the wrong reason, and the assertion that should have
    caught it is never written.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.execute_calls: list = []
        self.profile = types.SimpleNamespace(
            one_off=False,
            due_date="",
            calendar_keywords=[name],
            label=name,
        )

    def matches(self, events):
        return [{"trigger": "calendar", "handler": self.name}]

    def preview(self, match):
        return f"preview:{self.name}"

    def execute(self, match):
        self.execute_calls.append(match)
        return {"status": "unapproved payment executed"}


@pytest.fixture
def sent_messages(tmp_path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Neutralise every outbound effect of `main()` except Telegram sends."""
    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / ".monedula_last_run")
    monkeypatch.setattr(
        monedula, "GATE_HEALTH_FILE", tmp_path / ".monedula_gate_health"
    )
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        monedula, "fetch_due_payment_tasks", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])

    sent: list[str] = []
    monkeypatch.setattr(
        monedula, "telegram_send", lambda msg: sent.append(msg), raising=False
    )
    return sent


@pytest.fixture
def escalation_posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict, str]]:
    """Capture escalation POSTs instead of hitting Neotoma."""
    posts: list[tuple[dict, str]] = []

    def _fake_post(entity: dict, idempotency_key: str) -> bool:
        posts.append((entity, idempotency_key))
        return True

    monkeypatch.setattr(monedula, "_post_escalation_entity", _fake_post)
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
    return posts


def _install_handlers(monkeypatch, handlers):
    fake_mod = types.ModuleType("handlers")
    fake_mod.load_handlers = lambda strandings=None: handlers
    monkeypatch.setitem(sys.modules, "handlers", fake_mod)


# ── 1. Poll layer: telegram_poll_approval structured outcomes ────────────────


def test_http_409_short_circuits_as_channel_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 returns `channel_error` immediately — no retry-to-deadline.

    Distinct from the legacy `telegram_long_poll_once`, which does retry to
    the deadline (pinned in `test_http_409_returns_none_from_poll_layer`
    below): retrying cannot win a single-consumer lock held elsewhere
    (Cyphorhinus, per the #554 investigation), so `telegram_poll_approval`
    gives up the instant it sees 409 rather than burning the full timeout.
    """
    monkeypatch.setattr(monedula, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(monedula, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(monedula, "TELEGRAM_ALLOWED_USER_ID", "67890")

    clock = _FakeClock()
    monkeypatch.setattr(monedula.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(monedula.time, "sleep", clock.sleep)

    attempts: list[str] = []

    def _conflict(url: str, timeout: float | None = None):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 409, "Conflict", hdrs=None, fp=None)

    monkeypatch.setattr(monedula.urllib.request, "urlopen", _conflict)

    result = monedula.telegram_poll_approval(timeout_sec=120)

    assert result.kind == "channel_error"
    assert result.error_code == 409
    assert len(attempts) == 1, "409 must short-circuit, not retry to the deadline"
    assert clock.now < 120, "must not burn the full timeout on an unwinnable lock"


def test_timeout_with_no_error_is_channel_error_not_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll that runs to its deadline with no message is `timeout`.

    Per #554's engineering decision, timeout is a channel failure, not proof
    of decline: the operator may never have seen the prompt at all.
    """
    monkeypatch.setattr(monedula, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(monedula, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(monedula, "TELEGRAM_ALLOWED_USER_ID", "67890")

    clock = _FakeClock()
    monkeypatch.setattr(monedula.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(monedula.time, "sleep", clock.sleep)

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": []}).encode()

    def _blocking_getupdates(url: str, timeout: float | None = None):
        # A real Telegram long-poll blocks server-side for ~`timeout` seconds
        # before returning an empty result. Advance the fake clock to match,
        # or the poll loop spins with no result and no sleep between calls.
        clock.sleep(timeout or 0)
        return _FakeResponse()

    monkeypatch.setattr(monedula.urllib.request, "urlopen", _blocking_getupdates)

    result = monedula.telegram_poll_approval(timeout_sec=10)

    assert result.kind == "timeout"
    assert result.text is None


def test_missing_credentials_is_channel_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monedula, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(monedula, "TELEGRAM_CHAT_ID", "")

    result = monedula.telegram_poll_approval(timeout_sec=10)

    assert result.kind == "channel_error"


def test_ok_false_payload_is_channel_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monedula, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(monedula, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(monedula, "TELEGRAM_ALLOWED_USER_ID", "67890")

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": False, "description": "revoked"}).encode()

    monkeypatch.setattr(
        monedula.urllib.request, "urlopen", lambda url, timeout=None: _FakeResponse()
    )

    result = monedula.telegram_poll_approval(timeout_sec=10)

    assert result.kind == "channel_error"


def test_genuine_reply_is_returned_as_reply_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monedula, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(monedula, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(monedula, "TELEGRAM_ALLOWED_USER_ID", "67890")

    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {
                    "text": "attended yoga",
                    "from": {"id": 67890},
                    "chat": {"id": 12345},
                },
            }
        ],
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        monedula.urllib.request, "urlopen", lambda url, timeout=None: _FakeResponse()
    )

    result = monedula.telegram_poll_approval(timeout_sec=10)

    assert result.kind == "reply"
    assert result.text == "attended yoga"


def test_legacy_telegram_long_poll_once_still_retries_409_to_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`telegram_long_poll_once` (unchanged, callers other than `main()` may
    still use it) keeps its original retry-to-deadline behaviour on 409 —
    only the new `telegram_poll_approval` short-circuits.
    """
    monkeypatch.setattr(monedula, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(monedula, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(monedula, "TELEGRAM_ALLOWED_USER_ID", "67890")

    clock = _FakeClock()
    monkeypatch.setattr(monedula.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(monedula.time, "sleep", clock.sleep)

    attempts: list[str] = []

    def _conflict(url: str, timeout: float | None = None):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 409, "Conflict", hdrs=None, fp=None)

    monkeypatch.setattr(monedula.urllib.request, "urlopen", _conflict)

    assert monedula.telegram_long_poll_once(timeout_sec=10) is None
    assert len(attempts) > 1, "a 409 must be retried, not given up on immediately"
    assert clock.now >= 10, "the loop must terminate on its deadline, not early"


# ── 2. main(): channel failure blocks payment, escalates, exits non-zero ─────


def test_channel_error_409_blocks_payment_and_escalates(
    monkeypatch: pytest.MonkeyPatch,
    sent_messages: list[str],
    escalation_posts: list[tuple[dict, str]],
) -> None:
    handler = _RecordingHandler("therapy")
    _install_handlers(monkeypatch, [handler])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(
            kind="channel_error", error_code=409, error_detail="HTTPError 409: Conflict"
        ),
    )

    ok = monedula.main()

    # (a) no execute
    assert handler.execute_calls == [], "a channel failure must not execute a payment"
    # (d) main() signals failure — the entrypoint maps False to exit 1
    assert ok is False
    # (b) escalation POSTed with the right shape
    assert len(escalation_posts) == 1
    entity, idem_key = escalation_posts[0]
    assert entity["entity_type"] == "escalation"
    assert "monedula_consent_channel_failure" in entity["tags"]
    assert entity["source_agent"] == "monedula@ateles-swarm"
    assert entity["status"] == "open"
    # (e) log must not reuse the decline string — check the actual message sent
    assert not any(m.startswith("⏭️ Monedula: skipped all payments") for m in sent_messages)
    assert any("consent channel failed" in m.lower() for m in sent_messages)


def test_timeout_blocks_payment_and_escalates(
    monkeypatch: pytest.MonkeyPatch,
    sent_messages: list[str],
    escalation_posts: list[tuple[dict, str]],
) -> None:
    handler = _RecordingHandler("yoga")
    _install_handlers(monkeypatch, [handler])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="timeout"),
    )

    ok = monedula.main()

    assert handler.execute_calls == []
    assert ok is False
    assert len(escalation_posts) == 1
    assert "monedula_consent_channel_failure" in escalation_posts[0][0]["tags"]


def test_neotoma_store_failure_still_blocks_and_notifies(
    monkeypatch: pytest.MonkeyPatch, sent_messages: list[str]
) -> None:
    """Escalation write failing must not un-block payment or hide the failure."""
    handler = _RecordingHandler("therapy")
    _install_handlers(monkeypatch, [handler])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="timeout"),
    )
    monkeypatch.setattr(monedula, "_post_escalation_entity", lambda *a, **k: False)

    notified: list[tuple] = []
    monkeypatch.setattr(
        monedula, "_notify", lambda msg, priority="info": notified.append((msg, priority))
    )

    ok = monedula.main()

    assert handler.execute_calls == []
    assert ok is False
    assert any(p == "blocker" for _, p in notified)


# ── 3. main(): explicit decline stays clean — no escalation, exit 0 ──────────


def test_explicit_decline_skips_payment_no_escalation_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    sent_messages: list[str],
    escalation_posts: list[tuple[dict, str]],
) -> None:
    handler = _RecordingHandler("therapy")
    _install_handlers(monkeypatch, [handler])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="reply", text="no"),
    )

    ok = monedula.main()

    assert handler.execute_calls == []
    assert ok is True, "an explicit decline is a clean run, not a failure"
    assert escalation_posts == [], "a decline must never escalate"
    yesterday_str = monedula._yesterday().isoformat()
    assert f"⏭️ Monedula: skipped all payments for {yesterday_str}." in sent_messages


def test_bare_yes_reply_is_decline_not_channel_failure(
    monkeypatch: pytest.MonkeyPatch,
    sent_messages: list[str],
    escalation_posts: list[tuple[dict, str]],
) -> None:
    """Regression lock: a real (if rejected) reply must never be reclassified
    as a channel failure just because it fails to approve anything."""
    handler = _RecordingHandler("therapy")
    _install_handlers(monkeypatch, [handler])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="reply", text="yes"),
    )

    ok = monedula.main()

    assert handler.execute_calls == []
    assert ok is True
    assert escalation_posts == []


# ── 4. main(): approval still executes ────────────────────────────────────────


def test_attended_yoga_executes_yoga_only(
    monkeypatch: pytest.MonkeyPatch, sent_messages: list[str]
) -> None:
    yoga = _RecordingHandler("yoga")
    therapy = _RecordingHandler("therapy")
    _install_handlers(monkeypatch, [yoga, therapy])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="reply", text="attended yoga"),
    )

    ok = monedula.main()

    assert len(yoga.execute_calls) == 1
    assert therapy.execute_calls == []
    assert ok is True


def test_attended_all_executes_all_pending(
    monkeypatch: pytest.MonkeyPatch, sent_messages: list[str]
) -> None:
    yoga = _RecordingHandler("yoga")
    therapy = _RecordingHandler("therapy")
    _install_handlers(monkeypatch, [yoga, therapy])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="reply", text="attended all"),
    )

    monedula.main()

    assert len(yoga.execute_calls) == 1
    assert len(therapy.execute_calls) == 1


def test_partial_approval_executes_only_the_named_handler(
    monkeypatch: pytest.MonkeyPatch, sent_messages: list[str]
) -> None:
    """Pins the per-handler enforcement itself (`if handler.name not in
    approved: continue`). Every other test in this suite leaves `approved`
    empty and returns before the loop, so this is the sole assertion that
    goes red if that guard is deleted.
    """
    yoga = _RecordingHandler("yoga")
    therapy = _RecordingHandler("therapy")
    _install_handlers(monkeypatch, [yoga, therapy])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="reply", text="attended yoga"),
    )

    monedula.main()

    assert len(yoga.execute_calls) == 1, "the approved handler must be paid"
    assert therapy.execute_calls == [], (
        "an unapproved handler must not be paid on a partial approval"
    )
    yesterday_str = monedula._yesterday().isoformat()
    assert any(
        m.startswith(f"📋 Monedula results for {yesterday_str}:") for m in sent_messages
    ), "a partial approval must still confirm what was paid"


# ── 5. Dead-gate detector ─────────────────────────────────────────────────────


def test_dead_gate_fires_on_nth_consecutive_channel_failure(
    monkeypatch: pytest.MonkeyPatch,
    sent_messages: list[str],
    escalation_posts: list[tuple[dict, str]],
) -> None:
    monkeypatch.setattr(monedula, "MONEDULA_DEAD_GATE_THRESHOLD", 3)
    assert monedula._load_gate_health() == {"consecutive_channel_failures": 0}, (
        "sent_messages fixture must isolate GATE_HEALTH_FILE per test"
    )
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="timeout"),
    )

    for _ in range(3):
        _install_handlers(monkeypatch, [_RecordingHandler("therapy")])
        monedula.main()

    dead_gate_posts = [
        e for e, _ in escalation_posts if "monedula_dead_consent_gate" in e["tags"]
    ]
    assert len(dead_gate_posts) == 1, (
        "the 3rd consecutive failure must fire a distinct dead-gate escalation"
    )
    assert dead_gate_posts[0]["severity"] == "critical"


def test_any_reply_resets_gate_failure_streak(
    monkeypatch: pytest.MonkeyPatch,
    sent_messages: list[str],
    escalation_posts: list[tuple[dict, str]],
) -> None:
    monkeypatch.setattr(monedula, "MONEDULA_DEAD_GATE_THRESHOLD", 3)
    assert monedula._load_gate_health() == {"consecutive_channel_failures": 0}, (
        "sent_messages fixture must isolate GATE_HEALTH_FILE per test"
    )

    # Two failures.
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="timeout"),
    )
    for _ in range(2):
        _install_handlers(monkeypatch, [_RecordingHandler("therapy")])
        monedula.main()

    # A decline reply — still not an approval, but a real reply.
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="reply", text="no"),
    )
    _install_handlers(monkeypatch, [_RecordingHandler("therapy")])
    monedula.main()

    # One more failure — must NOT be the 3rd of a continuous streak.
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="timeout"),
    )
    _install_handlers(monkeypatch, [_RecordingHandler("therapy")])
    monedula.main()

    dead_gate_posts = [
        e for e, _ in escalation_posts if "monedula_dead_consent_gate" in e["tags"]
    ]
    assert dead_gate_posts == [], "a reply must reset the streak, not just pause it"


def test_dead_gate_not_incremented_when_no_payments_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Routine 'nothing due' must never look like a dead gate."""
    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / ".monedula_last_run")
    gate_health_file = tmp_path / ".monedula_gate_health"
    monkeypatch.setattr(monedula, "GATE_HEALTH_FILE", gate_health_file)
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(monedula, "fetch_due_payment_tasks", lambda *a, **k: [])
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **k: None)
    poll_called = []
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: poll_called.append(1),
    )
    _install_handlers(monkeypatch, [])  # no handlers triggered at all

    monedula.main()

    assert poll_called == [], "no pending payment means no poll and no streak bump"
    assert not gate_health_file.exists()


# ── 6. Escalation contract ────────────────────────────────────────────────────


def test_escalation_post_shape_has_no_payment_data(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict] = []

    def _fake_urlopen(req, timeout=None):
        posted.append(json.loads(req.data))

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        return _Resp()

    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
    monkeypatch.setattr(monedula.urllib.request, "urlopen", _fake_urlopen)

    result = monedula.TelegramPollResult(kind="channel_error", error_code=409)
    ok = monedula._emit_consent_escalation(
        result,
        pending_handler_names=["yoga", "therapy"],
        yesterday_str="2026-09-08",
        gate_failure_streak=1,
    )

    assert ok is True
    assert len(posted) == 1
    body = posted[0]["entities"][0]
    assert body["entity_type"] == "escalation"
    assert body["source_agent"] == "monedula@ateles-swarm"
    assert body["status"] == "open"
    # No IBANs, amounts, payee names, addresses — only handler labels already
    # public in this repo (yoga/therapy).
    forbidden_markers = ("IBAN", "wallet", "amount", "€", "$")
    haystack = json.dumps(body)
    for marker in forbidden_markers:
        assert marker not in haystack, f"escalation body must not contain {marker!r}"


def test_escalation_idempotency_key_stable_per_run_date_and_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[str] = []
    monkeypatch.setattr(
        monedula,
        "_post_escalation_entity",
        lambda entity, idem_key: posts.append(idem_key) or True,
    )

    result = monedula.TelegramPollResult(kind="timeout")
    monedula._emit_consent_escalation(
        result,
        pending_handler_names=["therapy"],
        yesterday_str="2026-09-08",
        gate_failure_streak=1,
    )
    monedula._emit_consent_escalation(
        result,
        pending_handler_names=["therapy"],
        yesterday_str="2026-09-08",
        gate_failure_streak=1,
    )

    assert len(posts) == 2
    assert posts[0] == posts[1], "same date + same escalation type must reuse one key"


# ── 7. payment_approved is vestigial and must never authorize payment ────────


def test_payment_approved_true_does_not_authorize_execute(
    monkeypatch: pytest.MonkeyPatch, sent_messages: list[str]
) -> None:
    """Guards against silent reinstatement of the deleted autoexec path.

    `payment_approved` was the field an unmerged branch (`origin/feat/
    monedula-task-autoexecute`, PR #249) used to execute payments straight
    from a mutable Neotoma task field, with no interactive confirmation. That
    path never merged to `main` and must not come back implicitly: the only
    thing that may authorize a payment is a real Telegram attendance reply.
    """
    handler = _RecordingHandler("therapy")
    # Simulate a linked task carrying payment_approved=True — nothing in
    # monedula.py reads this field, so a handler double is sufficient; the
    # point is that its presence changes nothing about the poll outcome.
    handler.profile.payment_approved = True  # type: ignore[attr-defined]
    _install_handlers(monkeypatch, [handler])
    monkeypatch.setattr(
        monedula,
        "telegram_poll_approval",
        lambda *a, **k: monedula.TelegramPollResult(kind="reply", text="no"),
    )

    monedula.main()

    assert handler.execute_calls == [], (
        "payment_approved=True must not authorize execution when the "
        "operator did not confirm attendance over Telegram"
    )


def test_payment_approved_never_referenced_to_gate_execute() -> None:
    """Static guard: `monedula.py` must never read `payment_approved` to
    decide whether to call `handler.execute`. Reinstating that check would
    resurrect the unreviewed autoexec path the operator explicitly rejected
    on #554 (`origin/feat/monedula-task-autoexecute`, PR #249, never merged).
    """
    source = (Path(__file__).parent / "monedula.py").read_text()
    assert "payment_approved" not in source, (
        "monedula.py must not reference payment_approved — the canonical "
        "gate is the Telegram attendance reply only"
    )
