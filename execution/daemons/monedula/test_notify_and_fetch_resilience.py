"""
Monedula: lossless operator alerts, and honest handling of a Neotoma outage.

Two incidents, one theme: the operator must never be told something false, and
an alert must never be silently dropped.

A. ``_notify`` dropped every alert when the module-level notifier was None, and
   the notifier was built once at import, so one failure at daemon start
   silenced all payment-approval notices until a restart.
B. On 7 and 13 Nov 2026 the hosted Neotoma timed out; the daemon loaded no
   profiles and emailed "STRANDED ... (all profiles)" plus "Escalations filed in
   Neotoma", but the escalation write had failed, so the record the email named
   did not exist.

Every test here was red against origin/main. Synthetic data only.

Run with: pytest execution/daemons/monedula/test_notify_and_fetch_resilience.py -v
"""

from __future__ import annotations

import io
import json
import logging
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import monedula  # noqa: E402
import strandings as st  # noqa: E402
from handlers.payment_profile import load_profiles_from_neotoma  # noqa: E402

HOSTED = "https://neotoma.example.invalid"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", HOSTED)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok-placeholder")
    monkeypatch.setattr(st.time, "sleep", lambda *_: None)
    monkeypatch.setattr(monedula, "_notify_undeliverable", False, raising=False)


# ── A. notifier is lazy, retried, and failures are loud ─────────────────────


class _FakeNotifier:
    def __init__(self, transport=True, raises=False):
        self.sent: list = []
        self._transport = transport
        self._raises = raises

    def has_transport(self, email_eligible=True):
        return self._transport

    def send(self, message, **kw):
        if self._raises:
            raise RuntimeError("apprise exploded")
        self.sent.append((message, kw))
        return True

    def clear_dedupe(self, key):
        pass


def test_notifier_missing_at_start_is_rebuilt_and_delivers_on_a_later_call(
    monkeypatch, caplog
):
    import lib.notify as ln

    built = []
    fake = _FakeNotifier()

    def from_neotoma(*a, **k):
        built.append(1)
        if len(built) == 1:
            raise ConnectionError("neotoma unreachable at start")
        return fake

    monkeypatch.setattr(ln.Notifier, "from_neotoma", staticmethod(from_neotoma))
    monkeypatch.setattr(monedula, "_notifier", None)

    with caplog.at_level(logging.ERROR):
        monedula._notify("first alert", priority="blocker")
    assert fake.sent == []
    assert any("could not be delivered" in r.message for r in caplog.records)

    monedula._notify("second alert", priority="blocker")
    assert [m for m, _ in fake.sent] == ["second alert"], (
        "a notifier that failed to build must be retried, not dropped forever"
    )


def test_notifier_without_a_transport_logs_error_and_marks_health(
    monkeypatch, caplog
):
    fake = _FakeNotifier(transport=False)
    monkeypatch.setattr(monedula, "_notifier", fake)
    with caplog.at_level(logging.ERROR):
        monedula._notify("payment held", priority="blocker")
    assert fake.sent == [], "nothing may be claimed as sent without a transport"
    assert any("no working transport" in r.message for r in caplog.records)
    assert monedula._notify_undeliverable is True


def test_a_failed_send_logs_error_and_marks_health(monkeypatch, caplog):
    monkeypatch.setattr(monedula, "_notifier", _FakeNotifier(raises=True))
    with caplog.at_level(logging.ERROR):
        monedula._notify("payment rejected", priority="blocker")
    assert any(
        r.levelno == logging.ERROR and "RuntimeError" in r.message
        for r in caplog.records
    )
    assert monedula._notify_undeliverable is True


def test_info_priority_failure_is_not_a_health_failure(monkeypatch):
    # INFO is dropped by the Notifier by design; losing it loses nothing.
    monkeypatch.setattr(monedula, "_notifier", _FakeNotifier(transport=False))
    monedula._notify("monedula started", priority="info")
    assert monedula._notify_undeliverable is False


def test_health_streak_is_persisted_and_reset(monkeypatch, tmp_path):
    path = tmp_path / "health.json"
    monkeypatch.setattr(monedula, "_notify_undeliverable", True)
    assert monedula._record_notify_health(path=path) == 1
    assert monedula._record_notify_health(path=path) == 2
    assert json.loads(path.read_text())["consecutive_notify_failures"] == 2
    monkeypatch.setattr(monedula, "_notify_undeliverable", False, raising=False)
    assert monedula._record_notify_health(path=path) == 0
    assert json.loads(path.read_text())["consecutive_notify_failures"] == 0


def test_real_notifier_reports_whether_it_has_a_transport(monkeypatch):
    pytest.importorskip("apprise")
    from lib.notify import Notifier

    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ATELES_NOTIFY_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    assert Notifier(rubric={}).has_transport() is False
    assert (
        Notifier(
            rubric={}, telegram_bot_token="123:abc", telegram_chat_id="1"
        ).has_transport()
        is True
    )


def test_real_notifier_email_route_counts_as_a_transport(monkeypatch):
    from lib.notify import Notifier

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "ops@example.invalid")
    assert Notifier(rubric={}).has_transport() is True


# ── B. profile fetch: cause, retry, escalation threshold, read-back ─────────


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _fetch_stranding():
    return st.Stranding(
        entity_id="",
        label="(all profiles)",
        reason=st.REASON_FETCH_FAILED,
        detail="Neotoma unreachable",
    )


def test_fetch_failure_names_the_cause_not_a_stranded_profile(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    found: list = []
    assert load_profiles_from_neotoma(found) == []
    detail = found[0].detail
    assert "Neotoma unreachable" in detail
    assert "no payments attempted" in detail
    assert "retrying next tick" in detail


def test_fetch_retries_a_transient_failure_once_then_loads(monkeypatch):
    calls = []

    def flaky(req, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return _Resp(json.dumps({"entities": []}).encode())

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    found: list = []
    load_profiles_from_neotoma(found)
    assert len(calls) == 2, "one bounded retry"
    assert found == [], "a recovered fetch is not a stranding"
    assert calls[0] == st.NEOTOMA_TIMEOUT_SEC == 30


def test_fetch_retry_is_bounded(monkeypatch):
    calls = []

    def down(req, timeout=None):
        calls.append(1)
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", down)
    found: list = []
    load_profiles_from_neotoma(found)
    assert len(calls) == st.NEOTOMA_ATTEMPTS == 2
    assert [s.reason for s in found] == [st.REASON_FETCH_FAILED]


def test_fetch_does_not_retry_an_auth_rejection(monkeypatch):
    calls = []

    def forbidden(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(HOSTED, 403, "Forbidden", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    load_profiles_from_neotoma([])
    assert len(calls) == 1


def _record(monkeypatch):
    posted: list = []
    monkeypatch.setattr(
        "strandings._post_escalation", lambda e, k: posted.append((e, k)) or True
    )
    return posted


def test_one_failed_fetch_tick_is_not_escalated_or_emailed(monkeypatch, tmp_path):
    posted = _record(monkeypatch)
    notes: list = []
    fresh = st.escalate(
        [_fetch_stranding()],
        notify=lambda m, **k: notes.append(m),
        state_file=tmp_path / "s.json",
        now=1000.0,
    )
    assert fresh == [] and posted == [] and notes == []


def test_a_fetch_failure_that_clears_never_escalates(monkeypatch, tmp_path):
    posted = _record(monkeypatch)
    f = tmp_path / "s.json"
    for _ in range(st.FETCH_FAILURE_ESCALATE_AFTER_TICKS - 1):
        st.escalate([_fetch_stranding()], state_file=f, now=1000.0)
    st.escalate([], state_file=f, now=1100.0)  # recovered
    for _ in range(st.FETCH_FAILURE_ESCALATE_AFTER_TICKS - 1):
        st.escalate([_fetch_stranding()], state_file=f, now=1200.0)
    assert posted == [], "the streak must reset when a tick succeeds"


def test_a_fetch_failure_that_persists_escalates_once_and_names_the_cause(
    monkeypatch, tmp_path
):
    posted = _record(monkeypatch)
    notes: list = []
    f = tmp_path / "s.json"
    n = st.FETCH_FAILURE_ESCALATE_AFTER_TICKS
    for i in range(n + 2):
        st.escalate(
            [_fetch_stranding()],
            notify=lambda m, **k: notes.append(m),
            state_file=f,
            now=1000.0 + i * 900,
        )
    assert len(posted) == 1, "escalated once, then deduplicated"
    assert len(notes) == 1
    assert "Neotoma has been unreachable for about 45 minutes" in notes[0]
    assert "no payments attempted" in notes[0]
    assert "retrying next tick" in notes[0]
    assert "No action needed unless this repeats" in notes[0]
    assert "STRANDED" not in notes[0], "an outage is not a stranded profile"


def test_a_transient_fetch_failure_leaves_existing_strandings_state_alone(
    monkeypatch, tmp_path
):
    _record(monkeypatch)
    f = tmp_path / "s.json"
    other = st.Stranding("ent_a", "A", st.REASON_UNREACHABLE, "d")
    st.escalate([other], state_file=f, now=1000.0)
    before = json.loads(f.read_text())
    st.escalate([_fetch_stranding()], state_file=f, now=1900.0)
    assert json.loads(f.read_text()) == before


def test_alert_reports_a_failed_escalation_write_and_retries_it(monkeypatch, tmp_path):
    monkeypatch.setattr("strandings._post_escalation", lambda e, k: False)
    notes: list = []
    f = tmp_path / "s.json"
    s = st.Stranding("ent_a", "A", st.REASON_UNREACHABLE, "d")
    st.escalate([s], notify=lambda m, **k: notes.append(m), state_file=f, now=1000.0)
    assert "FAILED" in notes[0]
    assert "no record was created" in notes[0]
    assert "filed in Neotoma" not in notes[0]
    # The failed write must not start the 24h quiet period: next tick retries.
    posted: list = []
    monkeypatch.setattr(
        "strandings._post_escalation", lambda e, k: posted.append(k) or True
    )
    st.escalate([s], state_file=f, now=1900.0)
    assert len(posted) == 1


def test_alert_after_a_confirmed_write_says_so(monkeypatch, tmp_path):
    _record(monkeypatch)
    notes: list = []
    s = st.Stranding("ent_a", "A", st.REASON_UNREACHABLE, "d")
    st.escalate(
        [s],
        notify=lambda m, **k: notes.append(m),
        state_file=tmp_path / "s.json",
        now=1000.0,
    )
    assert "written to Neotoma and read back" in notes[0]


def _urlopen_script(monkeypatch, store_body, read_body=None, read_exc=None):
    seen: list = []

    def fake(req, timeout=None):
        seen.append((req.get_method(), req.full_url))
        if req.get_method() == "POST":
            return _Resp(json.dumps(store_body).encode())
        if read_exc:
            raise read_exc
        return _Resp(json.dumps(read_body).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return seen


def _entity():
    return st.build_escalation_entity(
        st.Stranding("ent_a", "A", st.REASON_UNREACHABLE, "d"),
        observed_at="2026-11-14T00:00:00Z",
    )


def test_escalation_write_is_confirmed_by_reading_the_record_back(monkeypatch):
    e = _entity()
    seen = _urlopen_script(
        monkeypatch,
        {"entities": [{"entity_id": "ent_new"}]},
        {"snapshot": {"title": e["title"]}},
    )
    assert st._post_escalation(e, "k") == st.OUTCOME_CONFIRMED
    assert ("GET", f"{HOSTED}/entities/ent_new") in seen


def test_a_2xx_store_with_no_readable_record_is_not_success(monkeypatch):
    e = _entity()
    _urlopen_script(
        monkeypatch,
        {"entities": [{"entity_id": "ent_new"}]},
        read_exc=urllib.error.URLError("timed out"),
    )
    assert st._post_escalation(e, "k") == st.OUTCOME_UNCONFIRMED


def test_a_store_that_returns_no_entity_id_is_not_success(monkeypatch):
    _urlopen_script(monkeypatch, {"ok": True}, {})
    assert st._post_escalation(_entity(), "k") == st.OUTCOME_UNCONFIRMED


def test_a_record_that_reads_back_differently_is_not_success(monkeypatch):
    _urlopen_script(
        monkeypatch,
        {"entities": [{"entity_id": "ent_new"}]},
        {"snapshot": {"title": "something else"}},
    )
    assert st._post_escalation(_entity(), "k") == st.OUTCOME_UNCONFIRMED


# ── Round 2 ─────────────────────────────────────────────────────────────────


class _FakeNeotoma:
    """Enforces Neotoma's idempotency contract: a reused key must carry the
    same content, else 400. ``fail_reads`` makes GETs time out."""

    def __init__(self):
        self.keys: dict = {}
        self.entities: dict = {}
        self.fail_reads = False
        self.posts = 0

    def __call__(self, req, timeout=None):
        if req.get_method() == "POST":
            self.posts += 1
            payload = json.loads(req.data)
            key = payload["idempotency_key"]
            content = json.dumps(payload["entities"], sort_keys=True)
            if key in self.keys and self.keys[key] != content:
                raise urllib.error.HTTPError(
                    req.full_url, 400, "ERR_IDEMPOTENCY_MISMATCH", {}, io.BytesIO(b"")
                )
            self.keys[key] = content
            ent = payload["entities"][0]
            self.entities["ent_1"] = ent
            return _Resp(json.dumps({"entities": [{"entity_id": "ent_1"}]}).encode())
        if self.fail_reads:
            raise TimeoutError("read timed out")
        return _Resp(
            json.dumps({"snapshot": {"title": self.entities["ent_1"]["title"]}}).encode()
        )


def test_same_day_retry_after_a_landed_write_with_failed_readback_is_not_a_false_no_record(
    monkeypatch, tmp_path
):
    neo = _FakeNeotoma()
    monkeypatch.setattr("urllib.request.urlopen", neo)
    notes: list = []
    f = tmp_path / "s.json"
    s = st.Stranding("ent_a", "A", st.REASON_UNREACHABLE, "d")

    neo.fail_reads = True  # write 1 lands, its read-back fails
    st.escalate([s], notify=lambda m, **k: notes.append(m), state_file=f, now=1000.0)
    assert "CONFIRMED" in notes[0] and "may or may not exist" in notes[0]
    assert "no record was created" not in notes[0]

    neo.fail_reads = False  # tick 2, later the same day: retried, different body
    notes.clear()
    # Same day, different time => different body. A reused key would 400.
    fresh = st.escalate(
        [s], notify=lambda m, **k: notes.append(m), state_file=f, now=1900.0
    )
    assert len(fresh) == 1
    assert "no record was created" not in "".join(notes)
    assert "written to Neotoma and read back" in json.dumps(
        st._alert_text(fresh, {s.key: st.OUTCOME_CONFIRMED}, 0)
    )
    # And the retry really was confirmed (state now records it as escalated).
    assert s.key in json.loads(f.read_text())


def test_three_outcomes_not_stored_unconfirmed_confirmed(monkeypatch):
    e = _entity()
    # not stored: Neotoma answered the write with a 4xx
    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(HOSTED, 400, "bad", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    assert st._post_escalation(e, "k") == st.OUTCOME_NOT_STORED

    # unconfirmed: the write timed out (it may have committed)
    def timeout(req, timeout=None):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", timeout)
    assert st._post_escalation(e, "k") == st.OUTCOME_UNCONFIRMED

    # unconfirmed: a 5xx after retries
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(HOSTED, 503, "down", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert st._post_escalation(e, "k") == st.OUTCOME_UNCONFIRMED


@pytest.mark.parametrize(
    "outcome,present,absent",
    [
        ("confirmed", "written to Neotoma and read back", "FAILED"),
        ("not_stored", "no record was created", "may or may not exist"),
        ("unconfirmed", "may or may not exist", "no record was created"),
    ],
)
def test_alert_wording_per_outcome(outcome, present, absent):
    s = st.Stranding("ent_a", "A", st.REASON_UNREACHABLE, "d")
    text = st._alert_text([s], {s.key: outcome}, 0)
    assert present in text
    assert absent not in text


def test_after_one_failed_write_the_rest_are_deferred_not_attempted(
    monkeypatch, tmp_path
):
    calls: list = []
    monkeypatch.setattr(
        "strandings._post_escalation",
        lambda e, k: calls.append(k) or st.OUTCOME_UNCONFIRMED,
    )
    notes: list = []
    a = st.Stranding("ent_a", "A", st.REASON_UNREACHABLE, "d")
    b = st.Stranding("ent_b", "B", st.REASON_UNREACHABLE, "d")
    st.escalate(
        [a, b],
        notify=lambda m, **k: notes.append(m),
        state_file=tmp_path / "s.json",
        now=1000.0,
    )
    assert len(calls) == 1
    assert "not attempted this tick" in notes[0]


def test_http_rejection_is_its_own_reason_with_status_and_remedy(monkeypatch):
    def forbidden(req, timeout=None):
        raise urllib.error.HTTPError(HOSTED, 403, "Forbidden", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    found: list = []
    load_profiles_from_neotoma(found)
    assert [s.reason for s in found] == [st.REASON_FETCH_REJECTED]
    assert "HTTP 403" in found[0].detail
    assert "NEOTOMA_BEARER_TOKEN" in found[0].detail


def test_unreadable_body_is_a_rejection_not_an_outage(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _Resp(b"<html>nope")
    )
    found: list = []
    load_profiles_from_neotoma(found)
    assert [s.reason for s in found] == [st.REASON_FETCH_REJECTED]


def test_server_error_after_retries_is_still_an_outage(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(HOSTED, 503, "down", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    found: list = []
    load_profiles_from_neotoma(found)
    assert [s.reason for s in found] == [st.REASON_FETCH_FAILED]


def test_a_rejection_escalates_on_the_first_tick_and_keeps_its_remedy(
    monkeypatch, tmp_path
):
    posted = _record(monkeypatch)
    notes: list = []
    rej = st.Stranding(
        "",
        "(all profiles)",
        st.REASON_FETCH_REJECTED,
        "Neotoma rejected the payment profile fetch with HTTP 403. "
        "Check NEOTOMA_BEARER_TOKEN and the request User-Agent.",
    )
    st.escalate(
        [rej],
        notify=lambda m, **k: notes.append(m),
        state_file=tmp_path / "s.json",
        now=1000.0,
    )
    assert len(posted) == 1
    assert "HTTP 403" in notes[0]
    assert "Check NEOTOMA_BEARER_TOKEN" in notes[0]
    assert "will not clear by itself" in notes[0]
    assert "unreachable" not in notes[0]
    assert "Neotoma rejected" in posted[0][0]["title"]


def test_a_rejected_fetch_still_counts_as_profile_load_degraded():
    rej = st.Stranding("", "(all profiles)", st.REASON_FETCH_REJECTED, "d")
    assert monedula._profile_load_degraded([], [rej]) is True


def test_read_back_url_quotes_the_entity_id(monkeypatch):
    e = _entity()
    seen = _urlopen_script(
        monkeypatch,
        {"entities": [{"entity_id": "a/../b"}]},
        {"snapshot": {"title": e["title"]}},
    )
    st._post_escalation(e, "k")
    assert ("GET", f"{HOSTED}/entities/a%2F..%2Fb") in seen


def test_has_transport_ignores_email_for_a_non_eligible_alert(monkeypatch):
    from lib.notify import Notifier

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "ops@example.invalid")
    n = Notifier(rubric={})
    assert n.has_transport(email_eligible=True) is True
    assert n.has_transport(email_eligible=False) is False


class _FalseNotifier(_FakeNotifier):
    def __init__(self, open_keys=()):
        super().__init__()
        self._open = set(open_keys)

    def is_dedupe_open(self, key):
        return key in self._open

    def send(self, message, **kw):
        return False


def test_a_blocker_the_notifier_reports_undelivered_marks_health(monkeypatch, caplog):
    monkeypatch.setattr(monedula, "_notifier", _FalseNotifier())
    with caplog.at_level(logging.ERROR):
        monedula._notify("payment held", priority="blocker")
    assert monedula._notify_undeliverable is True
    assert any("not delivered by the notifier" in r.message for r in caplog.records)


def test_a_suppressed_repeat_or_a_held_warn_is_not_a_delivery_failure(monkeypatch):
    monkeypatch.setattr(monedula, "_notifier", _FalseNotifier(open_keys={"k"}))
    monedula._notify("again", priority="blocker", dedupe_key="k")
    monedula._notify("held for silence", priority="warn")
    assert monedula._notify_undeliverable is False


def test_failed_alert_log_never_contains_the_message_text(monkeypatch, caplog):
    monkeypatch.setattr(monedula, "_notifier", _FakeNotifier(raises=True))
    with caplog.at_level(logging.ERROR):
        monedula._notify("STRANDED SecretPayeeLabel", priority="blocker")
    assert "SecretPayeeLabel" not in caplog.text


def _entry(monkeypatch, *, clean, undeliverable):
    order: list = []
    monkeypatch.setattr(monedula, "main", lambda: clean)

    def fake_notify(message, priority="info", **kw):
        order.append(("notify", message))
        if undeliverable and message == "monedula run complete":
            monkeypatch.setattr(monedula, "_notify_undeliverable", True)

    monkeypatch.setattr(monedula, "_notify", fake_notify)
    monkeypatch.setattr(
        monedula, "_record_notify_health", lambda **k: order.append(("health", None))
    )
    return order


def test_clean_run_exits_zero(monkeypatch):
    _entry(monkeypatch, clean=True, undeliverable=False)
    assert monedula.run_entrypoint() == 0


def test_clean_run_with_an_undeliverable_alert_exits_one(monkeypatch):
    order = _entry(monkeypatch, clean=True, undeliverable=True)
    assert monedula.run_entrypoint() == 1
    # The streak is recorded AFTER the final alert was attempted.
    assert order.index(("notify", "monedula run complete")) < order.index(
        ("health", None)
    )


def test_unclean_run_exits_one(monkeypatch):
    _entry(monkeypatch, clean=False, undeliverable=False)
    assert monedula.run_entrypoint() == 1
