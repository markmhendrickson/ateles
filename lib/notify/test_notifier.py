"""Regression tests for lib/notify Priority routing.

Locks in the Priority.WARN fix: daemons (formica, neotoma-agent, apis a2a)
send WARN on their failure-reporting paths; before the enum member existed,
those paths raised AttributeError instead of notifying.
"""

import sys
import threading
import json
from itertools import product
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.notify import Notifier, Priority  # noqa: E402
from lib.notify import notifier as notifier_mod  # noqa: E402

NO_SILENCE = {"silence_start": "", "silence_end": "", "timezone": "Europe/Madrid"}
ALWAYS_SILENT = {
    "silence_start": "00:00",
    "silence_end": "23:59",
    "timezone": "Europe/Madrid",
}


def test_warn_member_exists():
    # The regression: daemons referenced Priority.WARN before it was defined.
    assert Priority.WARN.value == "warn"


def test_warn_send_does_not_raise():
    n = Notifier(rubric=NO_SILENCE)
    # Without apprise configured this returns False (logged only) — the point
    # is that the WARN path routes instead of raising AttributeError.
    n.send("dispatch failed", priority=Priority.WARN, handler="formica")


def test_warn_accepts_string_priority():
    n = Notifier(rubric=NO_SILENCE)
    n.send("dispatch failed", priority="warn", handler="formica")


def test_warn_queues_for_digest_in_silence_window():
    # WARN inside silence is still held (actionable) — but never as a digest email.
    n = Notifier(rubric=ALWAYS_SILENT)
    sent = n.send("dispatch failed", priority=Priority.WARN, handler="formica")
    assert sent is False
    assert any("dispatch failed" in m for m in n._digest_queue)
    assert all(m.lstrip().startswith("⚠") for m in n._digest_queue)


def test_all_daemon_used_priorities_route():
    n = Notifier(rubric=NO_SILENCE)
    for prio in Priority:
        n.send(f"smoke {prio.value}", priority=prio, handler="test")


# ── E6: email-primary transport (flag-gated) ─────────────────────────────────


def test_email_primary_off_by_default(monkeypatch):
    # Hermetic: another test module (execution/daemons/apis/apis.py) loads the
    # real .env via os.environ.setdefault at import, which can leak
    # ATELES_NOTIFY_EMAIL=1 into the process env and cross-contaminate this
    # default-behaviour assertion. Clear it so we test the code default.
    monkeypatch.delenv("ATELES_NOTIFY_EMAIL", raising=False)
    n = Notifier(rubric=NO_SILENCE)
    assert n._email_primary is False


def test_email_primary_delivers_via_gws(monkeypatch):
    n = Notifier(rubric=NO_SILENCE)
    n._email_primary = True
    n._operator_email = "op@test"
    n._swarm_email = "swarm@test"
    n._notify_to = "op@test"
    calls = {}

    class _P:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **k):
        calls["cmd"] = cmd
        return _P()

    monkeypatch.setattr("lib.notify.notifier.subprocess.run", fake_run)
    ok = n.send("blocker happened", priority=Priority.BLOCKER, handler="apis")
    assert ok is True
    assert calls["cmd"][:3] == ["gws", "gmail", "+send"]
    assert "op@test" in calls["cmd"] and "swarm@test" in calls["cmd"]


def test_notify_to_overrides_recipient(monkeypatch):
    """ATELES_NOTIFY_TO routes notifications to a distinct address so they
    arrive as received mail (inbox) rather than a self-addressed SENT copy."""
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "self@test")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "self+swarm@test")
    monkeypatch.setenv("ATELES_NOTIFY_TO", "alerts@test")
    n = Notifier(rubric=NO_SILENCE)
    assert n._notify_to == "alerts@test"
    calls = {}

    class _P:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("lib.notify.notifier.subprocess.run",
                        lambda cmd, **k: calls.setdefault("cmd", cmd) or _P())
    n.send("blocker", priority=Priority.BLOCKER, handler="apis")
    # Delivered TO the dedicated alert address, FROM the swarm alias.
    to_idx = calls["cmd"].index("--to")
    assert calls["cmd"][to_idx + 1] == "alerts@test"


def test_notify_to_defaults_to_operator_email(monkeypatch):
    """Unset ATELES_NOTIFY_TO → behaviour unchanged (defaults to OPERATOR_EMAIL)."""
    monkeypatch.delenv("ATELES_NOTIFY_TO", raising=False)
    monkeypatch.setenv("OPERATOR_EMAIL", "op@test")
    n = Notifier(rubric=NO_SILENCE)
    assert n._notify_to == "op@test"


def test_email_failure_falls_back_to_telegram(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = Notifier(rubric=NO_SILENCE)  # apprise unconfigured → Telegram returns False
    n._apprise = None  # hermetic: ignore process-env tokens from other modules
    n._email_primary = True
    n._operator_email = "op@test"
    n._notify_to = "op@test"

    class _P:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr("lib.notify.notifier.subprocess.run", lambda cmd, **k: _P())
    # Must not raise; email fails → falls through to (unconfigured) Telegram → False.
    assert n.send("blocker", priority=Priority.BLOCKER, handler="apis") is False


def test_email_skipped_when_no_operator_address(monkeypatch):
    n = Notifier(rubric=NO_SILENCE)
    n._email_primary = True
    n._operator_email = ""  # unset → email helper returns False immediately
    n._notify_to = ""  # no recipient at all
    called = {"n": 0}

    def fake_run(cmd, **k):
        called["n"] += 1

    monkeypatch.setattr("lib.notify.notifier.subprocess.run", fake_run)
    n.send("blocker", priority=Priority.BLOCKER, handler="apis")
    assert called["n"] == 0  # never shelled out without a recipient


# --------------------------------------------------------------------------
# Notifier.from_neotoma(telegram_topic_env=...) — per-daemon Telegram topic
# --------------------------------------------------------------------------
#
# The regression: Tyto's call site passes telegram_topic_env="TELEGRAM_TOPIC_TYTO"
# but the factory never accepted it, so every daemon startup died with
#   TypeError: from_neotoma() got an unexpected keyword argument
# and meeting transcription stayed down. These pin the factory's signature and
# the value actually reaching the Notifier, plus the unchanged no-arg default
# path that eight other daemons rely on.
#
# Hermetic: the Neotoma rubric fetch is patched out, so no network.


def _no_network_rubric(monkeypatch):
    """Stub the rubric fetch so from_neotoma() never touches the network."""
    monkeypatch.setattr(
        notifier_mod, "_load_rubric_from_neotoma", lambda: dict(NO_SILENCE)
    )


def test_from_neotoma_accepts_telegram_topic_env_without_typeerror(monkeypatch):
    # The exact crash Tyto hit on every startup.
    _no_network_rubric(monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOPIC_TYTO", "4242")

    n = Notifier.from_neotoma(telegram_topic_env="TELEGRAM_TOPIC_TYTO")

    assert isinstance(n, Notifier)


def test_from_neotoma_threads_topic_env_value_through_to_notifier(monkeypatch):
    # Accepting the kwarg is not enough — the value must reach the instance,
    # or alerts land in the shared default topic instead of the daemon's own.
    _no_network_rubric(monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOPIC_TYTO", "4242")

    n = Notifier.from_neotoma(telegram_topic_env="TELEGRAM_TOPIC_TYTO")

    assert n._topic_id == "4242"


def test_from_neotoma_no_arg_default_path_is_unchanged(monkeypatch):
    # Eight daemons call from_neotoma() with no argument; they must keep
    # falling back to TELEGRAM_TOPIC_MONEDULA exactly as before.
    _no_network_rubric(monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOPIC_MONEDULA", "999")

    n = Notifier.from_neotoma()

    assert n._topic_id == "999"


def test_from_neotoma_unset_topic_env_falls_back_to_default(monkeypatch):
    # An unset/empty named env must degrade to the constructor default rather
    # than blanking the topic — otherwise a misconfigured daemon silently
    # loses its routing.
    _no_network_rubric(monkeypatch)
    monkeypatch.delenv("TELEGRAM_TOPIC_TYTO", raising=False)
    monkeypatch.setenv("TELEGRAM_TOPIC_MONEDULA", "999")

    n = Notifier.from_neotoma(telegram_topic_env="TELEGRAM_TOPIC_TYTO")

    assert n._topic_id == "999"


# ── held-notice queue is persistent; routine digests are disabled ───────────
#
# Escalations held across silence must survive restart and drain as individual
# deliveries. Routine INFO must never grow the queue or produce a bulk
# "📋 Digest" email (standing_rule: do not send routine Ateles digest emails).


def _notifier(tmp_path, rubric, sent):
    n = Notifier(rubric=rubric)
    n._digest_path = tmp_path / "digest.json"
    n._dedupe_path = tmp_path / "dedupe.json"
    # Held items are only handed to _deliver when a route exists; stand in a
    # truthy Apprise, and keep email off regardless of the process env.
    n._apprise = object()
    n._email_primary = False
    n._deliver = lambda m, force=False, email_eligible=True: (
        sent.append(m),
        True,
    )[1]
    return n


def _keyed_notice(key, message="⚠ [apis] usage crossed"):
    return {"keys": [key], "message": message}


def test_queued_escalation_survives_restart_and_is_delivered(tmp_path):
    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.send("PR #570: 2 auto-fix rounds did not clear review",
           Priority.OPERATOR_DECISION, handler="apis")
    assert sent == []                       # held by the silence window
    assert len(n._digest_queue) == 1        # but persisted, not lost

    # A fresh process (daemon restart) must still see the queued escalation.
    n2 = _notifier(tmp_path, NO_SILENCE, sent)
    assert len(n2._digest_queue) == 1

    # Outside the silence window, the next send drains it individually —
    # never as a bulk "📋 Digest" message.
    n2.send("a later alert", Priority.OPERATOR_DECISION, handler="apis")
    assert any("570" in m for m in sent), sent
    assert all("📋 Digest" not in m for m in sent), sent
    assert n2._digest_queue == []


def test_failed_digest_delivery_keeps_items_queued(tmp_path):
    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.send("held escalation", Priority.OPERATOR_DECISION, handler="apis")
    assert len(n._digest_queue) == 1

    n._deliver = lambda m, force=False, email_eligible=True: False  # transport down
    assert n.flush_digest() is False
    # Must NOT be dropped on a failed send.
    assert len(n._digest_queue) == 1


def test_concurrent_held_writes_preserve_every_notification(tmp_path, monkeypatch):
    """Worker-thread completions must not overwrite each other's held-queue writes."""
    sent = []
    # Silence window so WARN holds rather than delivering immediately.
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    monkeypatch.setattr(n, "_maybe_flush_digest", lambda: None)
    messages = [f"completion-{index}" for index in range(32)]
    start = threading.Barrier(len(messages))

    def queue(message):
        start.wait()
        n.send(message, Priority.WARN, handler="tyto")

    with ThreadPoolExecutor(max_workers=len(messages)) as pool:
        list(pool.map(queue, messages))

    assert sorted(n._digest_queue) == sorted(
        f"⚠ [tyto] {message}" for message in messages
    )


def test_unreadable_queue_file_does_not_crash_send(tmp_path, monkeypatch):
    from lib.notify import held_view

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n._digest_path.write_text("{not json")
    assert n._digest_queue == []
    n.send("still works", Priority.BLOCKER, handler="apis")
    assert any("still works" in m for m in sent)
    assert held_view.summary(n._digest_path)["queue_unreadable"] == 1
    hook = _REPO_ROOT / ".claude" / "hooks" / "user_prompt_submit.py"
    run = notifier_mod.subprocess.run(
        [sys.executable, str(hook)], input='{"session_id":"synthetic-queue"}',
        text=True, capture_output=True,
        env={**os.environ, "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)},
        timeout=10,
    )
    assert run.returncode == 0 and "[held-queue]" in run.stdout


# ── standing_rule: no routine digest email; actionable delivery intact ──────


def test_info_does_not_grow_queue_or_emit_digest(tmp_path):
    """Routine INFO must not pile up for a later flush or mint a Digest email."""
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    for i in range(5):
        assert n.send(f"monedula started #{i}", Priority.INFO, handler="monedula") is False
    assert n._digest_queue == []
    assert sent == []
    assert not n._digest_path.exists()


def test_info_does_not_self_flush_as_digest_email(tmp_path, monkeypatch):
    """Even with email primary, INFO must never produce a Digest-shaped send."""
    sent_cmds = []
    n = Notifier(rubric=NO_SILENCE)
    n._digest_path = tmp_path / "digest.json"
    n._email_primary = True
    n._notify_to = "alerts@test"
    n._swarm_email = "swarm@test"
    n._apprise = None

    class _P:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **k):
        sent_cmds.append(cmd)
        return _P()

    monkeypatch.setattr("lib.notify.notifier.subprocess.run", fake_run)
    n.send("tyto auto-transcribing", Priority.INFO, handler="tyto")
    n.send("another info", Priority.INFO, handler="apis")
    assert sent_cmds == []
    assert n._digest_queue == []


def test_blocker_still_delivers_when_digest_disabled(tmp_path, monkeypatch):
    """Actionable failure notifications remain intact on the email channel."""
    calls = {}
    n = Notifier(rubric=NO_SILENCE)
    n._digest_path = tmp_path / "digest.json"
    n._email_primary = True
    n._notify_to = "alerts@test"
    n._swarm_email = "swarm@test"

    class _P:
        returncode = 0
        stderr = ""

    def _run(cmd, **kwargs):
        calls["cmd"] = cmd
        return _P()

    monkeypatch.setattr("lib.notify.notifier.subprocess.run", _run)
    assert n.send("daemon hard failure", Priority.BLOCKER, handler="apis") is True
    subject = calls["cmd"][calls["cmd"].index("--subject") + 1]
    assert subject.startswith("[Ateles]")
    assert "Digest" not in subject
    assert "daemon hard failure" in subject


def test_operator_decision_still_delivers_outside_silence(tmp_path, monkeypatch):
    calls = {}
    n = Notifier(rubric=NO_SILENCE)
    n._digest_path = tmp_path / "digest.json"
    n._email_primary = True
    n._notify_to = "alerts@test"

    class _P:
        returncode = 0
        stderr = ""

    def _run(cmd, **kwargs):
        calls["cmd"] = cmd
        return _P()

    monkeypatch.setattr("lib.notify.notifier.subprocess.run", _run)
    assert n.send("approve merge bypass?", Priority.OPERATOR_DECISION, handler="apis")
    body = calls["cmd"][calls["cmd"].index("--body") + 1]
    assert "approve merge bypass?" in body
    assert "📋 Digest" not in body


def test_classify_then_clear_preserves_actionable_drops_routine(tmp_path):
    """Legacy mixed queues: drop INFO residue; deliver actionable individually."""
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    # Simulate a pre-existing digest file accumulated under the old policy.
    n._digest_path.write_text(
        __import__("json").dumps(
            [
                "[monedula] monedula started",
                "[tyto] Auto-transcribing: clip.mp4",
                "⚠️ [apis] PR #570: auto-fix exhausted — needs operator",
                "[apis] Task created: something routine",
                "⚠ [formica] dispatch failed for issue #12",
            ]
        )
    )
    actionable, routine = n.classify_queue()
    assert len(actionable) == 2
    assert len(routine) == 3

    assert n.flush_digest() is True
    assert n._digest_queue == []
    assert any("570" in m for m in sent)
    assert any("dispatch failed" in m for m in sent)
    assert all("📋 Digest" not in m for m in sent)
    assert all("monedula started" not in m for m in sent)
    assert all("Auto-transcribing" not in m for m in sent)
    assert all("Task created" not in m for m in sent)


def test_flush_never_emits_bulk_digest_payload(tmp_path):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n._digest_path.write_text(
        __import__("json").dumps(["⚠️ held decision", "[info] noise"])
    )
    n.flush_digest()
    assert sent == ["⚠️ held decision"]
    # Belt-and-suspenders: the real _deliver refuses digest-shaped payloads
    # (do not use the _notifier mock — it always returns True).
    real = Notifier(rubric=NO_SILENCE)
    real._digest_path = tmp_path / "digest-refuse.json"
    real._email_primary = False
    real._apprise = None
    assert (
        Notifier._deliver(real, "📋 Digest (99 items)\n\n• noise", force=True) is False
    )


def test_hidden_queue_does_not_grow_from_info_then_self_flush(tmp_path):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n.send("noise a", Priority.INFO, handler="monedula")
    n.send("noise b", Priority.INFO, handler="tyto")
    # A later actionable send must not flush a pile of INFO as a digest.
    n.send("real blocker", Priority.BLOCKER, handler="apis")
    assert n._digest_queue == []
    assert any("real blocker" in m for m in sent)
    assert all("noise" not in m for m in sent)
    assert all("📋 Digest" not in m for m in sent)


# ── Repeat-condition dedupe journal (ateles#1127) ────────────────────────────
#
# Monedula's consent-channel-timeout alert sent 299 times over ~4 days at the
# launchd poll cadence (~17 min), one per tick, because nothing remembered it
# had already told the operator about the still-open condition. Same shape as
# ateles#1083 (22,878 retries of one deterministic Tyto error), generalized
# into the shared notifier so every daemon routing through Notifier gets it.


def test_dedupe_key_suppresses_repeat_sends_for_the_same_open_condition(tmp_path):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    first = n.send(
        "consent channel failed", Priority.BLOCKER, handler="monedula",
        dedupe_key="monedula:consent_channel_failed",
    )
    second = n.send(
        "consent channel failed", Priority.BLOCKER, handler="monedula",
        dedupe_key="monedula:consent_channel_failed",
    )
    third = n.send(
        "consent channel failed", Priority.BLOCKER, handler="monedula",
        dedupe_key="monedula:consent_channel_failed",
    )
    assert first is True
    assert second is False
    assert third is False
    assert sent == ["[monedula] consent channel failed"]


def test_dedupe_key_journal_survives_a_new_notifier_instance(tmp_path):
    """A restarted daemon must not re-notify a condition it already reported."""
    sent = []
    n1 = _notifier(tmp_path, NO_SILENCE, sent)
    n1.send(
        "consent channel failed", Priority.BLOCKER, handler="monedula",
        dedupe_key="monedula:consent_channel_failed",
    )
    n2 = _notifier(tmp_path, NO_SILENCE, sent)  # fresh instance, same journal path
    again = n2.send(
        "consent channel failed", Priority.BLOCKER, handler="monedula",
        dedupe_key="monedula:consent_channel_failed",
    )
    assert again is False
    assert sent == ["[monedula] consent channel failed"]


@pytest.mark.parametrize("mutation", [
    "pending", "delivered", "notified", "clear", "migrate", "release",
])
def test_each_journal_mutation_preserves_a_concurrent_instances_key(
    tmp_path, monkeypatch, mutation
):
    from . import neotoma_sink

    first = _notifier(tmp_path, NO_SILENCE, [])
    second = _notifier(tmp_path, NO_SILENCE, [])
    first._mark_dedupe_notified("target")
    monkeypatch.setattr(neotoma_sink, "resolve", lambda key: None)
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda old, new: True)
    entered = threading.Event()
    release = threading.Event()
    original = first._persist_dedupe_journal

    def paused_persist(journal):
        entered.set()  # first writer has read the journal, but has not written it
        assert release.wait(timeout=3)
        return original(journal)

    monkeypatch.setattr(first, "_persist_dedupe_journal", paused_persist)

    def mutate():
        if mutation == "pending":
            first._mark_dedupe_pending("target")
        elif mutation == "delivered":
            first._mark_dedupe_delivered("target")
        elif mutation == "notified":
            first._mark_dedupe_notified("new")
        elif mutation == "clear":
            first.clear_dedupe("target")
        elif mutation == "migrate":
            assert first.migrate_dedupe("target", "moved")
        else:
            first.release_dedupe("target")

    worker = threading.Thread(target=mutate)
    worker.start()
    try:
        assert entered.wait(timeout=3)
        other = threading.Thread(target=lambda: second._mark_dedupe_notified("other"))
        other.start()
        other.join(timeout=0.1)
    finally:
        release.set()
        worker.join(timeout=3)
    other.join(timeout=3)
    assert not worker.is_alive() and not other.is_alive()
    assert "other" in second._dedupe_journal


def test_retryable_alert_recovers_transport_refusal_before_acceptance(tmp_path):
    sent = []
    first = _notifier(tmp_path, NO_SILENCE, sent)
    key = "usage:weekly:50"

    def interrupted(*args, **kwargs):
        raise notifier_mod.TransportNotStarted("message construction failed")

    first._deliver = interrupted
    assert not first.send("usage crossed", Priority.WARN, handler="apis",
                          dedupe_key=key, retryable=True)
    assert first._dedupe_journal[key]["state"] == "pending"

    second = _notifier(tmp_path, NO_SILENCE, sent)
    assert second.send("usage crossed", Priority.WARN, handler="apis",
                       dedupe_key=key, retryable=True)
    assert second._dedupe_journal[key]["state"] == "delivered"
    assert not second.send("usage crossed", Priority.WARN, handler="apis",
                           dedupe_key=key, retryable=True)
    assert len(sent) == 1


def test_retryable_pending_has_one_live_owner_and_recovers_abandonment(tmp_path):
    import threading
    from . import held_view

    sent = []
    first = _notifier(tmp_path, NO_SILENCE, sent)
    second = _notifier(tmp_path, NO_SILENCE, sent)
    key = "usage:weekly:50"
    entered = threading.Event()
    release = threading.Event()

    def slow_transport(message, force=False, email_eligible=True):
        entered.set()
        assert release.wait(timeout=2)
        sent.append(message)
        return True

    first._deliver = slow_transport
    worker = threading.Thread(target=lambda: first.send(
        "usage crossed", Priority.WARN, handler="apis",
        dedupe_key=key, retryable=True,
    ))
    worker.start()
    try:
        assert entered.wait(timeout=2)
        assert second.is_dedupe_open(key)
        assert not second.send("usage crossed", Priority.WARN, handler="apis",
                               dedupe_key=key, retryable=True)
        assert sent == []
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert not second.send("usage crossed", Priority.WARN, handler="apis",
                           dedupe_key=key, retryable=True)
    assert len(sent) == 1

    # Model a process dying after it wrote pending: the OS releases its lock
    # without a delivered journal entry.
    second._mark_dedupe_pending(key, "apis")
    with held_view.file_lock(second._dedupe_owner_lock_path(key), fail_open=False) as held:
        assert held
        assert second.is_dedupe_open(key)
        assert not second.send("usage crossed", Priority.WARN, handler="apis",
                               dedupe_key=key, retryable=True)
    assert not second.is_dedupe_open(key)
    assert second.send("usage crossed", Priority.WARN, handler="apis",
                       dedupe_key=key, retryable=True)
    assert len(sent) == 2


def test_drain_between_queue_write_and_held_mark_settles_delivered(
    tmp_path, monkeypatch
):
    sent = []
    holder = _notifier(tmp_path, ALWAYS_SILENT, sent)
    holder.should_flush_digest = lambda: False
    drainer = _notifier(tmp_path, NO_SILENCE, sent)
    key = "usage:weekly:50"
    entered = threading.Event()
    release = threading.Event()
    original = holder._mark_dedupe_held

    def paused_mark(keys, handler, message):
        entered.set()  # queue lock is held; drain may now attempt its read
        assert release.wait(timeout=3)
        return original(keys, handler, message)

    monkeypatch.setattr(holder, "_mark_dedupe_held", paused_mark)
    sender = threading.Thread(target=lambda: holder.send(
        "usage crossed", Priority.WARN, handler="apis",
        dedupe_key=key, retryable=True,
    ))
    sender.start()
    try:
        assert entered.wait(timeout=3)
        drain = threading.Thread(target=drainer.flush_digest)
        drain.start()
        drain.join(timeout=0.1)
        assert drain.is_alive()
    finally:
        release.set()
        sender.join(timeout=3)
    drain.join(timeout=3)
    assert not sender.is_alive() and not drain.is_alive()
    assert sent == ["⚠ [apis] usage crossed"]
    assert drainer._digest_queue == []
    assert drainer._dedupe_journal[key]["state"] == "delivered"
    assert not drainer.send("usage crossed", Priority.WARN, handler="apis",
                            dedupe_key=key, retryable=True)
    assert len(sent) == 1


def test_held_check_takes_notification_lock_before_queue_lock(tmp_path, monkeypatch):
    """A usage check on a held key must not hold the queue lock while waiting
    for the notification lock: a send holding the notification lock would then
    time out on the queue lock and write the queue unlocked."""
    import contextlib

    monkeypatch.setattr(held_view, "_LOCK_WAIT_SECONDS", 0.3)
    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.should_flush_digest = lambda: False
    key = "usage:weekly:75"
    n.send("usage crossed 75", Priority.WARN, handler="apis",
           dedupe_key=key, retryable=True)
    assert n._dedupe_journal[key]["state"] == "held"
    n._persist_queue([], n._digest_path)  # the held notice is gone

    real = n._queue_lock
    guard = threading.Lock()
    inside = {"now": 0, "max": 0}
    checker_in_queue = threading.Event()
    sender_has_notification_lock = threading.Event()

    @contextlib.contextmanager
    def tracked():
        with real() as ok:
            with guard:
                inside["now"] += 1
                inside["max"] = max(inside["max"], inside["now"])
            try:
                if threading.current_thread().name == "usage-check" \
                        and not checker_in_queue.is_set():
                    checker_in_queue.set()
                    sender_has_notification_lock.wait(timeout=0.6)
                yield ok
            finally:
                with guard:
                    inside["now"] -= 1

    monkeypatch.setattr(n, "_queue_lock", tracked)
    result = {}
    checker = threading.Thread(
        name="usage-check", daemon=True,
        target=lambda: result.__setitem__("open", n.is_dedupe_open(key)),
    )

    def sender():
        assert checker_in_queue.wait(timeout=3)
        with n._notification_lock:
            sender_has_notification_lock.set()
            with n._queue_lock():
                pass

    other = threading.Thread(target=sender, daemon=True)
    checker.start()
    other.start()
    checker.join(timeout=5)
    other.join(timeout=5)
    assert not checker.is_alive() and not other.is_alive()
    assert inside["max"] == 1, "a queue writer entered while another held the queue lock"
    assert result["open"] is True  # missing held notice is quarantined


def test_clear_dedupe_lets_a_genuine_recurrence_notify_again(tmp_path):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    key = "monedula:consent_channel_failed"
    n.send("consent channel failed", Priority.BLOCKER, handler="monedula", dedupe_key=key)
    n.send("consent channel failed", Priority.BLOCKER, handler="monedula", dedupe_key=key)
    n.clear_dedupe(key)  # condition resolved (a real reply arrived)
    third = n.send(
        "consent channel failed", Priority.BLOCKER, handler="monedula", dedupe_key=key
    )
    assert third is True
    assert sent == [
        "[monedula] consent channel failed",
        "[monedula] consent channel failed",
    ]


def test_info_send_does_not_consume_the_dedupe_key(tmp_path):
    """INFO is dropped unconditionally, so it must not mark the key as reported.

    Marking on an INFO send would suppress every later send of that condition —
    including a BLOCKER — on the strength of a report the operator never got.
    """
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    key = "monedula:consent_channel_failed"

    info = n.send("consent channel failed", Priority.INFO, handler="monedula", dedupe_key=key)
    blocker = n.send("consent channel failed", Priority.BLOCKER, handler="monedula", dedupe_key=key)
    repeat = n.send("consent channel failed", Priority.BLOCKER, handler="monedula", dedupe_key=key)

    assert info is False, "INFO is never delivered"
    assert blocker is True, "the BLOCKER must still get through after an INFO send"
    assert repeat is False, "dedupe still holds once the condition has actually been reported"
    assert sent == ["[monedula] consent channel failed"]


def test_distinct_dedupe_keys_do_not_suppress_each_other(tmp_path):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    a = n.send("A failed", Priority.BLOCKER, handler="monedula", dedupe_key="cond:a")
    b = n.send("B failed", Priority.BLOCKER, handler="monedula", dedupe_key="cond:b")
    assert a is True
    assert b is True
    assert len(sent) == 2


def test_send_without_dedupe_key_is_unchanged_and_always_delivers(tmp_path):
    """No key → no journal interaction, no suppression — pre-existing behaviour."""
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    for _ in range(3):
        ok = n.send("routine blocker", Priority.BLOCKER, handler="apis")
        assert ok is True
    assert len(sent) == 3
    assert not n._dedupe_path.exists()


def test_unreadable_dedupe_journal_does_not_crash_send(tmp_path, monkeypatch):
    from . import held_view

    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(n._digest_path))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(n._dedupe_path))
    n._dedupe_path.write_text("not json")
    key = "monedula:consent_channel_failed"
    for _ in range(2):
        assert not n.send("consent channel failed", Priority.BLOCKER,
                          handler="monedula", dedupe_key=key)
    assert sent == []
    assert n._dedupe_path.read_text() == "not json"
    assert n._dedupe_path.with_name(n._dedupe_path.name + ".unreadable").read_text() == "not json"
    assert {item["keys"][0] for item in held_view.read_queue(n._digest_path)} == {
        key, "dedupe-journal-unreadable",
    }
    assert [row["key"] for row in held_view.read_recovery(n._digest_path)] == [
        "dedupe-journal-unreadable",
    ]
    hook = _REPO_ROOT / ".claude" / "hooks" / "user_prompt_submit.py"
    run = notifier_mod.subprocess.run(
        [sys.executable, str(hook)], input='{"session_id":"synthetic"}',
        text=True, capture_output=True,
        env={**os.environ, "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)},
        timeout=10,
    )
    assert run.returncode == 0
    assert "--clear-unreadable-journal" in run.stdout
    assert held_view.main(["--clear-unreadable-journal"]) == 0
    assert n._dedupe_journal[key]["state"] == "delivered_unconfirmed"
    assert not n.send("consent channel failed", Priority.BLOCKER,
                      handler="monedula", dedupe_key=key)
    assert not n.flush_digest()
    assert held_view.main(["--clear-unconfirmed", key]) == 0
    assert n.send("consent channel failed", Priority.BLOCKER,
                  handler="monedula", dedupe_key=key)
    assert sent == ["[monedula] consent channel failed"]


def test_unreadable_journal_keeps_keyless_delivery(tmp_path):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n._dedupe_path.write_text("not json")
    assert n.send("independent", Priority.BLOCKER, handler="apis")
    assert sent == ["[apis] independent"]


@pytest.mark.parametrize("queue_state", ["missing", "unreadable", "lock"])
def test_held_receipt_with_unverifiable_queue_is_quarantined(
    tmp_path, monkeypatch, queue_state
):
    from . import held_view

    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.should_flush_digest = lambda: False
    key = "usage:weekly:50"
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    if queue_state == "missing":
        n._digest_path.unlink()
    elif queue_state == "unreadable":
        n._digest_path.write_bytes(b"{synthetic broken queue")
    else:
        @contextmanager
        def unavailable():
            yield False

        monkeypatch.setattr(n, "_queue_lock", unavailable)
    n._in_silence_window = lambda: False
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    assert not [message for message in sent if "crossed" in message]
    assert any(row["key"] == key for row in held_view.read_recovery(n._digest_path))


def test_journal_recovery_never_reopens_delivered_or_uncertain_keys(tmp_path, monkeypatch):
    from . import held_view
    from datetime import datetime, timezone

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    delivered = "provider-usage-threshold:cursor:weekly:2026-11-18T00:00:00+00:00:50"
    uncertain = "provider-usage-threshold:cursor:weekly:2026-11-18T00:00:00+00:00:75"
    assert n.send("delivered", Priority.WARN, handler="apis", dedupe_key=delivered,
                  retryable=True)

    def lost(message, **kwargs):
        sent.append(message)
        raise OSError("synthetic lost result")

    n._deliver = lost
    assert not n.send("uncertain", Priority.WARN, handler="apis", dedupe_key=uncertain,
                      retryable=True)
    n._deliver = lambda message, **kwargs: sent.append(message) or True
    n._dedupe_path.write_bytes(b"{synthetic unreadable journal")
    assert not n.send("delivered", Priority.WARN, handler="apis", dedupe_key=delivered,
                      retryable=True)
    assert held_view.main(["--clear-unreadable-journal"]) == 0
    assert n._dedupe_journal[uncertain]["state"] == "delivered_unconfirmed"
    assert any(row["key"] == uncertain for row in held_view.read_recovery(n._digest_path))
    before = len(sent)
    for key in (delivered, uncertain, "provider-usage-threshold:cursor:weekly:no-reset:50"):
        assert not n.send("old window", Priority.WARN, handler="apis",
                          dedupe_key=key, retryable=True)
    assert len([message for message in sent[before:] if "old window" in message]) == 0
    assert n._dedupe_journal[delivered]["state"] == "delivered_unconfirmed"
    assert any(row["key"] == delivered for row in held_view.read_recovery(n._digest_path))
    assert held_view.main(["--clear-unconfirmed", uncertain]) == 0
    assert n.send("uncertain cleared", Priority.WARN, handler="apis",
                  dedupe_key=uncertain, retryable=True)
    future = (datetime.now(timezone.utc) + timedelta(days=8)).isoformat(timespec="seconds")
    later = f"provider-usage-threshold:cursor:weekly:{future}:50"
    assert not n.send("later", Priority.WARN, handler="apis", dedupe_key=later,
                      retryable=True)
    assert held_view.main(["--clear-unconfirmed", later]) == 0
    assert n.send("later", Priority.WARN, handler="apis", dedupe_key=later,
                  retryable=True)
    assert not n.send("later", Priority.WARN, handler="apis", dedupe_key=later,
                      retryable=True)
    assert len([message for message in sent if "later" in message]) == 1


@pytest.mark.parametrize("name,span", [
    ("monthly", timedelta(days=28)),
    ("monthly", timedelta(days=30)),
    ("monthly", timedelta(days=31)),
    ("weekly_all", timedelta(days=7)),
    ("five_hour", timedelta(hours=5)),
])
@pytest.mark.parametrize("after_end", [False, True])
def test_recovered_journal_releases_only_after_observed_window_end(
    tmp_path, monkeypatch, name, span, after_end
):
    from . import held_view

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    usage_path = tmp_path / "usage.json"
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(usage_path))
    epoch = datetime.now(timezone.utc)
    old_end = epoch + span
    usage_path.write_text(json.dumps({"cursor": {
        "observed_at": epoch.isoformat(),
        "windows": [{"name": name, "used_percent": 60,
                     "resets_at": old_end.isoformat()}],
    }}))
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n._dedupe_path.write_bytes(b"{synthetic unreadable journal")
    old = held_view.usage_key("cursor", name, old_end.isoformat(), 50)
    assert not n.send("old", Priority.WARN, handler="apis", dedupe_key=old,
                      retryable=True)
    assert held_view.main(["--clear-unreadable-journal"]) == 0
    marker = n._dedupe_journal[held_view.RECOVERED_AT]
    assert marker["windows"]["cursor"][name] == old_end.isoformat()
    n._now_local = lambda: old_end + (timedelta(seconds=1) if after_end
                                       else -timedelta(seconds=1))
    next_end = old_end + span
    new = held_view.usage_key("cursor", name, next_end.isoformat(), 50)
    accepted = n.send("new", Priority.WARN, handler="apis", dedupe_key=new,
                      retryable=True)
    assert accepted is after_end
    assert len([item for item in sent if item == "⚠ [apis] new"]) == int(after_end)
    if not after_end:
        assert n._dedupe_journal[new]["state"] == "delivered_unconfirmed"
        assert any(row["key"] == new for row in held_view.read_recovery(n._digest_path))
    else:
        assert not n.send("new", Priority.WARN, handler="apis", dedupe_key=new,
                          retryable=True)


def test_recovered_journal_without_window_end_needs_explicit_clear(tmp_path, monkeypatch):
    from . import held_view

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "missing-usage.json"))
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n._dedupe_path.write_bytes(b"{synthetic unreadable journal")
    key = held_view.usage_key("cursor", "monthly", "2027-01-01T00:00:00+00:00", 50)
    assert not n.send("uncertain", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    assert held_view.main(["--clear-unreadable-journal"]) == 0
    assert n._dedupe_journal[held_view.RECOVERED_AT]["windows"] == {}
    assert not n.send("unknown", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    assert held_view.main(["--clear-unconfirmed", key]) == 0
    assert n.send("cleared", Priority.WARN, handler="apis", dedupe_key=key,
                  retryable=True)
    assert sent.count("⚠ [apis] cleared") == 1


def test_unreadable_journal_preserves_first_and_latest_distinct_versions(tmp_path):
    n = _notifier(tmp_path, NO_SILENCE, [])
    first = b"{first synthetic broken"
    second = b"{second synthetic broken"
    third = b"{third synthetic broken"
    for raw in (first, first, second, third, third):
        n._dedupe_path.write_bytes(raw)
        assert not n.send("held", Priority.WARN, handler="apis", dedupe_key="old")
    copies = list(tmp_path.glob("dedupe.json.unreadable*"))
    assert len(copies) == 2
    assert (tmp_path / "dedupe.json.unreadable").read_bytes() == first
    assert (tmp_path / "dedupe.json.unreadable.latest").read_bytes() == third


def test_journal_recovery_salvages_parseable_siblings_from_torn_copy(tmp_path, monkeypatch):
    from . import held_view

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    n = _notifier(tmp_path, NO_SILENCE, [])
    good = {"state": "delivered", "notified_at": "2026-11-17T00:00:00+00:00",
            "handler": "apis"}
    n._dedupe_path.write_text(
        '{"first": ' + json.dumps(good) + ', "torn": {bad}, "last": '
        + json.dumps(good) + "}"
    )
    assert not n.send("held", Priority.WARN, handler="apis", dedupe_key="first")
    assert held_view.main(["--clear-unreadable-journal"]) == 0
    assert n._dedupe_journal["first"] == good
    assert n._dedupe_journal["last"] == good


@pytest.mark.parametrize("copy_bytes,expected_key", [
    (b'\xff{"known": {"state": "delivered", "notified_at": "2026-11-17T00:00:00+00:00", "handler": "apis"}}', "known"),
    (b'\xff{"known": {"state": "delivered", "notified_at": "2026-11-17T00:00:00+00:00", "handler": "apis"}, "torn": \xff}', "known"),
    (b'\xff\xfe\xfd', None),
])
def test_undecodable_journal_copy_reconciles_without_resending(
    tmp_path, monkeypatch, copy_bytes, expected_key
):
    from . import held_view

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n._dedupe_path.write_bytes(copy_bytes)
    assert not n.send("synthetic", Priority.WARN, handler="apis", dedupe_key="unknown",
                      retryable=True)
    assert held_view.main(["--clear-unreadable-journal"]) == 0
    journal = n._dedupe_journal
    assert held_view.RECOVERED_AT in journal
    if expected_key:
        assert journal[expected_key]["state"] == "delivered"
    assert not n.send("synthetic", Priority.WARN, handler="apis", dedupe_key="unknown",
                      retryable=True)
    assert sent == []


@pytest.mark.parametrize("journal", ["absent", "delivered", "malformed", "held"])
@pytest.mark.parametrize("queue", ["with_notice", "without_notice", "unreadable", "missing"])
@pytest.mark.parametrize("locked", [True, False])
def test_retryable_held_state_table(tmp_path, monkeypatch, journal, queue, locked):
    """Every journal/queue/lock cell is safe across two notifier instances."""
    from . import held_view, neotoma_sink

    monkeypatch.setattr(neotoma_sink, "record", lambda **kwargs: None)
    first = _notifier(tmp_path, ALWAYS_SILENT, [])
    second = _notifier(tmp_path, ALWAYS_SILENT, [])
    first.should_flush_digest = second.should_flush_digest = lambda: False
    key = "usage:weekly:50"
    notice = "⚠ [apis] usage crossed"
    entries = {
        "delivered": {"state": "delivered", "notified_at": "now", "handler": "apis"},
        "malformed": ["bad"],
        "held": {"state": "held", "held_message": notice, "handler": "apis"},
    }
    if journal != "absent":
        first._dedupe_path.write_text(json.dumps({key: entries[journal]}))
    if queue == "with_notice":
        first._digest_path.write_text(json.dumps([notice]))
    elif queue == "without_notice":
        first._digest_path.write_text("[]")
    elif queue == "unreadable":
        first._digest_path.write_bytes(b"broken queue bytes")

    if not locked:
        @contextmanager
        def unavailable():
            yield False
        monkeypatch.setattr(second, "_queue_lock", unavailable)

    assert second.send("usage crossed", Priority.WARN, handler="apis",
                       dedupe_key=key, retryable=True) is False
    entry = second._dedupe_journal.get(key)
    if queue == "unreadable":
        assert first._digest_path.read_bytes() == b"broken queue bytes"
    if queue == "with_notice" and isinstance(entry, dict) and entry.get("state") == "held":
        assert any(held_view.notice_text(item) == notice for item in held_view.read_queue(first._digest_path))
    if journal == "malformed":
        assert entry == ["bad"]
        assert any(row["key"] == key for row in held_view.read_recovery(first._digest_path))
    assert second.is_dedupe_open(key) in (True, False)


@pytest.mark.parametrize("entry", ["bad", ["bad"], 17, None,
                                    {"state": "held", "held_message": []},
                                    {"state": "held", "held_message": "⚠ [apis] usage crossed", "handler": 7},
                                    {"state": "pending", "pending_at": []},
                                    {"state": "delivered", "notified_at": 7},
                                    {"state": "delivered", "notified_at": "not-a-time"},
                                    {"state": "sending", "attempt_at": []},
                                    {"notified_at": "2026-11-17T00:00:00+00:00"}])
def test_malformed_journal_entry_is_quarantined_on_retryable_send(tmp_path, entry):
    from . import held_view
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    key = "provider-usage-threshold:cursor:50"
    n._dedupe_path.write_text(json.dumps({key: entry}))
    assert not n.send("usage crossed", Priority.WARN, handler="apis",
                      dedupe_key=key, retryable=True)
    assert not [message for message in sent if "usage crossed" in message]
    assert [row["key"] for row in held_view.read_recovery(n._digest_path)] == [key]
    if isinstance(entry, dict) and entry.get("state") == "sending":
        assert n._dedupe_journal[key]["state"] == "delivered_unconfirmed"
    else:
        assert n._dedupe_journal[key] == entry


def test_two_instances_retry_after_queue_lock_timeout(tmp_path, monkeypatch):
    from . import held_view

    first = _notifier(tmp_path, ALWAYS_SILENT, [])
    second = _notifier(tmp_path, ALWAYS_SILENT, [])
    first.should_flush_digest = second.should_flush_digest = lambda: False
    monkeypatch.setattr(held_view, "_LOCK_WAIT_SECONDS", 0.01)
    key = "usage:weekly:50"
    lock_path = held_view.queue_lock_path(first._digest_path)
    with held_view.file_lock(lock_path, fail_open=False) as acquired:
        assert acquired
        assert not second.send("usage crossed", Priority.WARN, handler="apis",
                               dedupe_key=key, retryable=True)
        assert not first._digest_path.exists()
        assert first._dedupe_journal.get(key, {}).get("state") != "held"
    assert not second.send("usage crossed", Priority.WARN, handler="apis",
                           dedupe_key=key, retryable=True)
    assert second._dedupe_journal[key]["state"] == "held"
    assert len(held_view.read_queue(first._digest_path)) == 1


def test_full_queue_preserves_existing_notice_and_key(tmp_path, monkeypatch):
    from . import held_view

    n = _notifier(tmp_path, ALWAYS_SILENT, [])
    n.should_flush_digest = lambda: False
    monkeypatch.setattr(notifier_mod, "HELD_QUEUE_MAX", 1)
    assert not n.send("first", Priority.WARN, handler="apis",
                      dedupe_key="first", retryable=True)
    assert n._dedupe_journal["first"]["state"] == "held"
    assert not n.send("second", Priority.WARN, handler="apis",
                      dedupe_key="second", retryable=True)
    assert held_view.read_queue(n._digest_path) == [_keyed_notice("first", "⚠ [apis] first")]
    assert n._dedupe_journal["first"]["state"] == "held"
    assert "second" not in n._dedupe_journal


@pytest.mark.parametrize("email", [False, True])
def test_failed_held_receipt_does_not_duplicate_on_retry(tmp_path, monkeypatch, email):
    from . import held_view

    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n._email_primary = email
    n._notify_to = "synthetic@example.test" if email else ""
    n.should_flush_digest = lambda: False
    original = n._mark_dedupe_held
    failures = [True]

    def fail_once(keys, handler, message):
        if failures.pop(0) if failures else False:
            return False
        return original(keys, handler, message)

    monkeypatch.setattr(n, "_mark_dedupe_held", fail_once)
    for _ in range(2):
        assert not n.send("usage crossed", Priority.WARN, handler="apis",
                          dedupe_key="usage:weekly:50", retryable=True)
    queued = held_view.read_queue(n._email_path if email else n._digest_path)
    assert queued == [_keyed_notice("usage:weekly:50")]
    assert n._dedupe_journal["usage:weekly:50"]["state"] == "held"
    n._in_silence_window = lambda: False
    if email:
        n._deliver_email = lambda message: sent.append(message) or True
    assert n.flush_digest()
    assert sent == ["⚠ [apis] usage crossed"]
    assert not n.send("usage crossed", Priority.WARN, handler="apis",
                      dedupe_key="usage:weekly:50", retryable=True)
    assert len(sent) == 1


def test_changed_reading_replaces_queued_notice_after_receipt_failure(tmp_path, monkeypatch):
    from . import held_view

    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.should_flush_digest = lambda: False
    key = "usage:weekly:50"
    original = n._mark_dedupe_held
    monkeypatch.setattr(n, "_mark_dedupe_held", lambda *args: False)
    assert not n.send("50%", Priority.WARN, handler="apis", dedupe_key=key, retryable=True)
    monkeypatch.setattr(n, "_mark_dedupe_held", original)
    assert not n.send("55%", Priority.WARN, handler="apis", dedupe_key=key, retryable=True)
    assert held_view.read_queue(n._digest_path) == [_keyed_notice(key, "⚠ [apis] 55%")]
    assert list(n._dedupe_journal) == [key]
    n._in_silence_window = lambda: False
    assert n.flush_digest()
    assert sent == ["⚠ [apis] 55%"]


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_successful_transport_with_failed_receipt_never_resends(
    tmp_path, monkeypatch, restart, held, caplog
):
    from lib.notify.notifier import SendResult
    from lib.notify import held_view

    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    key = "usage:weekly:50"
    original = n._mark_dedupe_delivered_many
    monkeypatch.setattr(n, "_mark_dedupe_delivered_many", lambda *args: False)
    if held:
        n._in_silence_window = lambda: True
        assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                          retryable=True)
        n._in_silence_window = lambda: False
        assert not n.flush_digest()
    else:
        assert n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True) is SendResult.DELIVERED_RECEIPT_UNKNOWN
    assert "receipt unknown" in caplog.text
    assert n._dedupe_journal[key]["state"] == "sending"
    assert held_view.read_recovery(n._digest_path)[0]["key"] == key
    if restart:
        n = _notifier(tmp_path, NO_SILENCE, sent)
        n._recover_sending_entries()
    else:
        monkeypatch.setattr(n, "_mark_dedupe_delivered_many", original)
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    assert len([message for message in sent if "crossed" in message]) == 1
    original_now = n._now_local
    n._now_local = lambda: original_now() + timedelta(seconds=6)
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    n.flush_digest()
    assert len([message for message in sent if "crossed" in message]) == 1
    assert len([message for message in sent if message.startswith("Unconfirmed alert")]) == 1
    assert n._dedupe_journal[key]["state"] == "sending"


@pytest.mark.parametrize("attempt", [None, "bad", 42, "2999-01-01T00:00:00+00:00"])
def test_unreadable_sending_attempt_is_visible_and_suppressing(tmp_path, attempt):
    from lib.notify import held_view
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    key = "usage:weekly:50"
    assert n._mark_dedupe_sending((key,), "apis")
    journal = n._dedupe_journal
    journal[key]["attempt_at"] = attempt
    n._persist_dedupe_journal(journal)
    assert n.is_dedupe_open(key)
    assert n._dedupe_journal[key]["state"] == "delivered_unconfirmed"
    assert held_view.read_recovery(n._digest_path)[0]["key"] == key
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    assert not any("crossed" in message for message in sent)


@pytest.mark.parametrize("failure", [None, "queue", "journal"])
def test_held_key_migration_moves_receipt_and_notice_together(tmp_path, monkeypatch, failure):
    from lib.notify import held_view, neotoma_sink
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.should_flush_digest = lambda: False
    old, new = "usage:weekly:old:50", "usage:weekly:new:50"
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=old,
                      retryable=True)
    original_queue = n._persist_queue
    original_journal = n._persist_dedupe_journal
    if failure == "queue":
        monkeypatch.setattr(n, "_persist_queue", lambda *args: False)
    if failure == "journal":
        monkeypatch.setattr(n, "_persist_dedupe_journal", lambda *args: False)
    assert n.migrate_dedupe(old, new) is (failure is None)
    monkeypatch.setattr(n, "_persist_queue", original_queue)
    monkeypatch.setattr(n, "_persist_dedupe_journal", original_journal)
    queue = held_view.read_queue(n._digest_path)
    assert len(queue) == 1
    expected = new if failure is None else old
    assert held_view.notice_identity(queue[0]) == (expected,)
    assert expected in n._dedupe_journal
    if failure is None:
        assert n._dedupe_journal[old] == {"state": "migrated", "migration_to": new}
    elif failure == "queue":
        assert not n.flush_digest()
        assert n.migrate_dedupe(old, new)
    n._in_silence_window = lambda: False
    assert n.flush_digest()
    assert len([message for message in sent if "crossed" in message]) == 1


@pytest.mark.parametrize("failure", ["queue", "journal", "sink"])
def test_failed_migration_keeps_sink_key_old_until_local_transition_commits(
    tmp_path, monkeypatch, failure
):
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("ATELES_NOTIFY_NEOTOMA_SINK", "shadow")
    monkeypatch.setenv("ATELES_NOTIFY_SINK_DIR", str(tmp_path / "sink"))
    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.should_flush_digest = lambda: False
    old, new = "usage:weekly:old:50", "usage:weekly:new:50"
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=old,
                      retryable=True)
    state_file = neotoma_sink.sink_dir() / "state-shadow-apis.json"

    def open_keys():
        state = json.loads(state_file.read_text())
        return {entry["key"] for entry in state["entries"].values()
                if entry.get("status") == "open"}

    assert open_keys() == {old}
    if failure == "queue":
        original = n._persist_queue
        monkeypatch.setattr(n, "_persist_queue", lambda *args: False)
    elif failure == "journal":
        original = n._persist_dedupe_journal
        monkeypatch.setattr(n, "_persist_dedupe_journal", lambda *args: False)
    else:
        original = neotoma_sink._state

        @contextmanager
        def broken_state(*args, **kwargs):
            raise OSError("synthetic sink failure")
            yield

        monkeypatch.setattr(neotoma_sink, "_state", broken_state)
    assert not n.migrate_dedupe(old, new)
    if failure == "queue":
        monkeypatch.setattr(n, "_persist_queue", original)
    elif failure == "journal":
        monkeypatch.setattr(n, "_persist_dedupe_journal", original)
    else:
        monkeypatch.setattr(neotoma_sink, "_state", original)
    assert open_keys() == {old}
    marker = [row for row in held_view.read_recovery(n._digest_path)
              if row.get("migration_to") == new]
    assert len(marker) == (0 if failure == "journal" else 1)
    if failure == "sink":
        assert n._dedupe_journal[old]["state"] == "migrating"
        assert new in n._dedupe_journal
        assert n.migrate_dedupe(old, new)
        assert open_keys() == {new}
        assert not [row for row in held_view.read_recovery(n._digest_path)
                    if row.get("migration_to") == new]
    else:
        assert old in n._dedupe_journal
        if failure == "queue":
            assert new in n._dedupe_journal
            assert n._dedupe_journal[old]["state"] == "migrating"
        assert n.migrate_dedupe(old, new)
        assert open_keys() == {new}


def test_malformed_sink_state_keeps_migration_marker_until_repaired(tmp_path, monkeypatch, capsys):
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("ATELES_NOTIFY_NEOTOMA_SINK", "shadow")
    monkeypatch.setenv("ATELES_NOTIFY_SINK_DIR", str(tmp_path / "sink"))
    n = _notifier(tmp_path, ALWAYS_SILENT, [])
    n.should_flush_digest = lambda: False
    old, new = "usage:weekly:old:50", "usage:weekly:new:50"
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=old,
                      retryable=True)
    state_file = neotoma_sink.sink_dir() / "state-shadow-apis.json"
    original = state_file.read_bytes()
    state_file.write_bytes(b"{synthetic malformed sink")
    assert not n.migrate_dedupe(old, new)
    assert state_file.read_bytes() == b"{synthetic malformed sink"
    assert any(row.get("migration_to") == new for row in held_view.read_recovery(n._digest_path))
    assert held_view.summary(n._digest_path)["sink_state_unreadable"] == 1
    held_view.main([])
    assert "notification sink state unreadable" in capsys.readouterr().out
    hook = _REPO_ROOT / ".claude" / "hooks" / "user_prompt_submit.py"
    run = notifier_mod.subprocess.run(
        [sys.executable, str(hook)], input='{"session_id":"synthetic-sink"}',
        text=True, capture_output=True,
        env={**os.environ, "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)},
        timeout=10,
    )
    assert run.returncode == 0 and "[notify-sink]" in run.stdout
    state_file.write_bytes(original)
    assert n.migrate_dedupe(old, new)
    state = json.loads(state_file.read_text())
    assert {entry["key"] for entry in state["entries"].values()
            if entry.get("status") == "open"} == {new}


@pytest.mark.parametrize("boundary", ["marker", "journal", "queue", "sink", "final"])
def test_uncertain_migration_crash_recovers_without_transport(tmp_path, monkeypatch, capsys, boundary):
    from lib.notify import held_view, neotoma_sink

    old, new = "usage:weekly:old:50", "usage:weekly:new:50"
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    monkeypatch.setattr(Notifier, "_try_deliver", lambda *args, **kwargs: False)
    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.should_flush_digest = lambda: False
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=old,
                      retryable=True)
    assert n._mark_dedupe_sending((old,), "apis")
    n._mark_delivery_uncertain((old,), "synthetic uncertain outcome")

    journal_write = n._persist_dedupe_journal
    queue_write = n._persist_queue
    sink_write = neotoma_sink.migrate_key
    writes = 0

    def crash_journal(doc):
        nonlocal writes
        result = journal_write(doc)
        writes += 1
        if (boundary, writes) in (("marker", 1), ("journal", 2), ("final", 3)):
            raise SystemExit("synthetic process death")
        return result

    def crash_queue(items, path=None):
        result = queue_write(items, path)
        if boundary == "queue":
            raise SystemExit("synthetic process death")
        return result

    def crash_sink(*args):
        result = sink_write(*args)
        if boundary == "sink":
            raise SystemExit("synthetic process death")
        return result

    monkeypatch.setattr(n, "_persist_dedupe_journal", crash_journal)
    monkeypatch.setattr(n, "_persist_queue", crash_queue)
    monkeypatch.setattr(neotoma_sink, "migrate_key", crash_sink)
    with pytest.raises(SystemExit, match="synthetic process death"):
        n.migrate_dedupe(old, new)
    monkeypatch.setattr(neotoma_sink, "migrate_key", sink_write)
    assert any(row["key"] in (old, new) for row in
               held_view.journal_receipt_status()[0])
    held_view.main([])
    assert "migration" in capsys.readouterr().out.lower()
    assert held_view.main(["--clear-unconfirmed", new]) == 1
    assert held_view.main(["--clear"]) == 1
    restarted = _notifier(tmp_path, NO_SILENCE, sent)
    restarted.should_flush_digest = lambda: False
    restarted._recover_sending_entries()
    assert restarted._dedupe_journal[new]["state"] == "delivered_unconfirmed"
    assert restarted._dedupe_journal[old]["state"] == "migrated"
    assert not restarted.send("crossed", Priority.WARN, handler="apis",
                              dedupe_key=old, retryable=True)
    assert not restarted.send("crossed", Priority.WARN, handler="apis",
                              dedupe_key=new, retryable=True)
    assert not restarted.flush_digest()
    restarted._in_silence_window = lambda: True
    assert not restarted.send("crossed", Priority.WARN, handler="apis",
                              dedupe_key=new, retryable=True)
    assert sent == []
    recovery = held_view.read_recovery(restarted._digest_path)
    assert len(recovery) == 1 and recovery[0]["key"] == new
    assert not recovery[0].get("migration_to")
    assert held_view.main(["--clear-unconfirmed", new]) == 0
    assert old not in restarted._dedupe_journal


def test_recovery_only_key_migrates_as_uncertain(tmp_path, monkeypatch):
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    old, new = "usage:monthly:old:50", "usage:monthly:corrected:50"
    n._raise_recovery(old, "synthetic receipt write failed", notify=False)
    assert old not in n._dedupe_journal
    assert n.migrate_dedupe(old, new)
    assert n._dedupe_journal[new]["state"] == "delivered_unconfirmed"
    n._raise_recovery(old, "late transport result", notify=False)
    assert [row["key"] for row in held_view.read_recovery(n._digest_path)] == [new]
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=new,
                      retryable=True)
    assert sent == []


def test_migrating_sending_receipt_creates_one_recovery_record(tmp_path, monkeypatch):
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    n = _notifier(tmp_path, NO_SILENCE, [])
    old, new = "usage:weekly:old:50", "usage:weekly:corrected:50"
    assert n._mark_dedupe_sending((old,), "apis")
    assert n.migrate_dedupe(old, new)
    assert n._dedupe_journal[new]["state"] == "delivered_unconfirmed"
    assert [row["key"] for row in held_view.read_recovery(n._digest_path)] == [new]


def test_clearing_legacy_alias_resolves_corrected_window(tmp_path, monkeypatch):
    from lib.notify import neotoma_sink

    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    monkeypatch.setattr(neotoma_sink, "resolve", lambda *args: True)
    n = _notifier(tmp_path, NO_SILENCE, [])
    old, new = "usage:weekly:no-reset:50", "usage:weekly:corrected:50"
    assert n._mark_dedupe_delivered_many((old,), "apis")
    assert n.migrate_dedupe(old, new)
    n.clear_dedupe(old)
    assert old not in n._dedupe_journal and new not in n._dedupe_journal


def test_journal_recovery_preserves_interrupted_migration(tmp_path, monkeypatch):
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(Notifier, "_try_deliver", lambda *args, **kwargs: False)
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: False)
    n = _notifier(tmp_path, NO_SILENCE, [])
    old, new = "usage:weekly:old:50", "usage:weekly:corrected:50"
    assert n._mark_dedupe_delivered_many((old,), "apis")
    assert not n.migrate_dedupe(old, new)
    assert [row["migration_to"] for row in held_view.read_recovery(n._digest_path)] == [new]
    n._dedupe_path.write_bytes(b"{synthetic unreadable journal")
    assert not n.send("crossed", Priority.WARN, handler="apis",
                      dedupe_key=new, retryable=True)
    assert held_view.main(["--clear-unreadable-journal"]) == 0
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    restarted = _notifier(tmp_path, NO_SILENCE, [])
    assert restarted._dedupe_journal[new]["state"] == "delivered_unconfirmed"
    assert restarted._dedupe_journal[old]["state"] == "migrated"
    assert [row["key"] for row in held_view.read_recovery(n._digest_path)] == [new]


def test_sink_resolution_failure_holds_cleared_key_until_retry(tmp_path, monkeypatch):
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("ATELES_NOTIFY_NEOTOMA_SINK", "shadow")
    monkeypatch.setenv("ATELES_NOTIFY_SINK_DIR", str(tmp_path / "sink"))
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    key = "usage:weekly:old:50"
    assert n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                  retryable=True)
    state_file = neotoma_sink.sink_dir() / "state-shadow-apis.json"
    original = state_file.read_bytes()
    state_file.write_bytes(b"{synthetic malformed sink")
    n.clear_dedupe(key)
    assert any(row["key"] == key and row["cause"] == "sink resolution pending"
               for row in held_view.read_recovery(n._digest_path))
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    assert len([message for message in sent if "crossed" in message]) == 1
    state_file.write_bytes(original)
    n.clear_dedupe(key)
    assert held_view.read_recovery(n._digest_path) == []
    assert n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                  retryable=True)


def test_sink_resolution_append_failure_retries_pending_projection(tmp_path, monkeypatch):
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("ATELES_NOTIFY_NEOTOMA_SINK", "shadow")
    monkeypatch.setenv("ATELES_NOTIFY_SINK_DIR", str(tmp_path / "sink"))
    n = _notifier(tmp_path, NO_SILENCE, [])
    key = "usage:weekly:old:50"
    assert n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                  retryable=True)
    append = neotoma_sink._append

    def fail_resolve(prefix, record):
        return False if record.get("kind") == "resolve" else append(prefix, record)

    monkeypatch.setattr(neotoma_sink, "_append", fail_resolve)
    n.clear_dedupe(key)
    state_file = neotoma_sink.sink_dir() / "state-shadow-apis.json"
    state = json.loads(state_file.read_text())
    assert any(entry.get("resolve_pending") for entry in state["entries"].values())
    assert any(row["key"] == key and row["cause"] == "sink resolution pending"
               for row in held_view.read_recovery(n._digest_path))
    monkeypatch.setattr(neotoma_sink, "_append", append)
    n.clear_dedupe(key)
    state = json.loads(state_file.read_text())
    assert not any(entry.get("resolve_pending") for entry in state["entries"].values())
    assert held_view.read_recovery(n._digest_path) == []


def test_unconfirmed_recovery_view_clear_and_direct_notice(tmp_path, monkeypatch, capsys):
    from lib.notify import held_view
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    key = "usage:weekly:reset:50"
    monkeypatch.setattr(n, "_mark_dedupe_delivered_many", lambda *args: False)
    n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key, retryable=True)
    records = held_view.read_recovery(n._digest_path)
    assert len(records) == 1 and records[0]["noticed"] is True
    assert len([message for message in sent if message.startswith("Unconfirmed alert")]) == 1
    held_view.main([])
    assert "unconfirmed alerts" in capsys.readouterr().out
    assert held_view.main(["--clear-unconfirmed", key]) == 0
    assert held_view.read_recovery(n._digest_path) == []
    assert key not in n._dedupe_journal


def test_operator_clear_of_uncertain_held_notice_allows_one_resend(tmp_path, monkeypatch):
    from lib.notify import held_view
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    key = "usage:weekly:reset:50"
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    original = n._mark_dedupe_delivered_many
    n._mark_dedupe_delivered_many = lambda *args: False
    n._in_silence_window = lambda: False
    assert not n.flush_digest()
    n._mark_dedupe_delivered_many = original
    assert len(held_view.read_queue(n._digest_path)) == 1
    assert held_view.main(["--clear-unconfirmed", key]) == 0
    assert held_view.read_queue(n._digest_path) == []
    assert n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                  retryable=True)
    assert len([message for message in sent if "crossed" in message]) == 2


def test_window_roll_keeps_uncertain_notice_until_explicit_clear(tmp_path, monkeypatch):
    from lib.notify import held_view
    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(n._digest_path))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(n._dedupe_path))
    key = "usage:weekly:old:50"
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    n._mark_dedupe_delivered_many = lambda *args: False
    n._in_silence_window = lambda: False
    assert not n.flush_digest()
    assert len([message for message in sent if "crossed" in message]) == 1
    n.clear_dedupe(key)
    assert held_view.read_recovery(n._digest_path)[0]["key"] == key
    assert held_view.read_queue(n._digest_path) == [_keyed_notice(key, "⚠ [apis] crossed")]
    assert held_view.main(["--clear-unconfirmed", key]) == 0
    assert held_view.read_recovery(n._digest_path) == []
    n.flush_digest()
    assert len([message for message in sent if "crossed" in message]) == 1


@pytest.mark.parametrize("restart", [False, True])
def test_failed_transport_remains_retryable(tmp_path, restart):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    key = "usage:weekly:50"
    n._deliver = lambda *args, **kwargs: False
    assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                      retryable=True)
    assert n._dedupe_journal[key]["state"] == "pending"
    if restart:
        n = _notifier(tmp_path, NO_SILENCE, sent)
    else:
        n._deliver = lambda message, **kwargs: sent.append(message) or True
    assert n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                  retryable=True)
    assert sent == ["⚠ [apis] crossed"]


@pytest.mark.parametrize("held", [False, True])
def test_transport_accepts_then_raises_is_never_resent(tmp_path, held):
    from . import held_view

    calls = []
    n = _notifier(tmp_path, ALWAYS_SILENT if held else NO_SILENCE, calls)
    key = "usage:weekly:uncertain:50"

    def accepted_then_lost(message, **kwargs):
        calls.append(message)
        raise OSError("reply lost")

    n._deliver = accepted_then_lost
    if held:
        n.should_flush_digest = lambda: False
        assert not n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
                          retryable=True)
        n._in_silence_window = lambda: False
        n.flush_digest()
    else:
        n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
               retryable=True)
    assert len([m for m in calls if "crossed" in m]) == 1
    assert n._dedupe_journal[key]["state"] == "delivered_unconfirmed"
    assert [r["key"] for r in held_view.read_recovery(n._digest_path)] == [key]
    n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
           retryable=True)
    n.flush_digest()
    original_now = n._now_local
    n._now_local = lambda: original_now() + timedelta(seconds=10)
    n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
           retryable=True)
    n = _notifier(tmp_path, NO_SILENCE, calls)
    n._deliver = accepted_then_lost
    n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
           retryable=True)
    n.flush_digest()
    assert len([m for m in calls if "crossed" in m]) == 1


def test_explicit_refusal_in_held_drain_is_retryable(tmp_path):
    calls = []
    n = _notifier(tmp_path, ALWAYS_SILENT, calls)
    n.should_flush_digest = lambda: False
    key = "usage:weekly:refused:50"
    n.send("crossed", Priority.WARN, handler="apis", dedupe_key=key,
           retryable=True)
    n._in_silence_window = lambda: False
    n._deliver = lambda *args, **kwargs: False
    n.flush_digest()
    assert n._dedupe_journal[key]["state"] == "held"
    n._deliver = lambda message, **kwargs: calls.append(message) or True
    n.flush_digest()
    assert calls == ["⚠ [apis] crossed"]


def test_retryable_notice_state_walk(tmp_path, monkeypatch, capsys):
    """Every three-step walk preserves queue, receipt and delivery invariants."""
    from . import held_view

    operations = ("send", "drain", "held_receipt_fail", "direct_receipt_fail",
                  "transport_refuse", "transport_uncertain", "crash_pending", "crash_sending", "restart",
                  "migrate", "window_correction", "migration_crash",
                  "migration_queue_fail", "migration_journal_fail",
                  "migration_sink_fail", "operator_clear", "window_roll",
                  "journal_corrupt", "journal_corrupt_bytes", "journal_reconcile", "malformed_receipt",
                  "unreadable_snapshot_twice")
    crash_boundaries = set()
    for length in range(1, 4):
        for index, sequence in enumerate(product(operations, repeat=length)):
            root = tmp_path / f"walk-{length}-{index}"
            root.mkdir()
            monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(root / "digest.json"))
            monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(root / "dedupe.json"))
            monkeypatch.setenv("ATELES_NOTIFY_SINK_DIR", str(root / "sink"))
            sent = []
            n = _notifier(root, ALWAYS_SILENT, sent)
            n.should_flush_digest = lambda: False
            key = "usage:weekly:reset0:50"
            clear_allowance: dict[str, int] = {}
            journal_bad = False
            for operation in sequence:
                before = held_view.read_queue(n._digest_path)
                if operation in ("send", "held_receipt_fail", "direct_receipt_fail"):
                    n._in_silence_window = lambda direct=operation == "direct_receipt_fail": not direct
                    if operation == "held_receipt_fail":
                        original = n._mark_dedupe_held
                        n._mark_dedupe_held = lambda *args: False
                    if operation == "direct_receipt_fail":
                        original = n._mark_dedupe_delivered_many
                        n._mark_dedupe_delivered_many = lambda *args: False
                    n.send(f"crossed {key}", Priority.WARN, handler="apis",
                           dedupe_key=key, retryable=True)
                    if operation == "held_receipt_fail":
                        n._mark_dedupe_held = original
                    if operation == "direct_receipt_fail":
                        n._mark_dedupe_delivered_many = original
                    n._in_silence_window = lambda: True
                elif operation == "drain":
                    n._in_silence_window = lambda: False
                    n.flush_digest()
                    n._in_silence_window = lambda: True
                elif operation == "restart":
                    n = _notifier(root, ALWAYS_SILENT, sent)
                    n.should_flush_digest = lambda: False
                    n._recover_sending_entries()
                elif operation in ("crash_pending", "crash_sending"):
                    if not journal_bad and key not in n._dedupe_journal:
                        if operation == "crash_pending":
                            assert n._mark_dedupe_pending(key, "apis")
                        else:
                            assert n._mark_dedupe_sending((key,), "apis")
                            journal = n._dedupe_journal
                            journal[key]["attempt_at"] = (datetime.now(timezone.utc)
                                - timedelta(minutes=1)).isoformat()
                            assert n._persist_dedupe_journal(journal)
                elif operation in ("migrate", "window_correction"):
                    moved = key.replace(":50", ":corrected:50")
                    if n.migrate_dedupe(key, moved):
                        key = moved
                elif operation == "migration_crash" and not journal_bad:
                    moved = key.replace(":50", ":corrected:50")
                    if (moved != key and key in n._dedupe_journal
                            and moved not in n._dedupe_journal
                            and not held_view.receipt_problem(n._dedupe_journal[key])
                            and n._dedupe_journal[key]["state"] not in ("migrating", "migrated")):
                        from lib.notify import neotoma_sink
                        has_queue = any(isinstance(item, dict) and key in item["keys"]
                                        for item in before)
                        boundary = ("marker", "journal", "queue", "sink", "final")[index % 5]
                        if boundary == "queue" and not has_queue:
                            boundary = "marker"
                        original_journal = n._persist_dedupe_journal
                        original_queue = n._persist_queue
                        original_sink = neotoma_sink.migrate_key
                        writes = 0

                        def crash_journal(doc):
                            nonlocal writes
                            result = original_journal(doc)
                            writes += 1
                            if (boundary, writes) in (("marker", 1), ("journal", 2), ("final", 3)):
                                raise SystemExit("synthetic process death")
                            return result

                        def crash_queue(items, path=None):
                            result = original_queue(items, path)
                            if boundary == "queue":
                                raise SystemExit("synthetic process death")
                            return result

                        def crash_sink(*args):
                            result = original_sink(*args)
                            if boundary == "sink":
                                raise SystemExit("synthetic process death")
                            return result

                        n._persist_dedupe_journal = crash_journal
                        n._persist_queue = crash_queue
                        neotoma_sink.migrate_key = crash_sink
                        try:
                            with pytest.raises(SystemExit, match="synthetic process death"):
                                n.migrate_dedupe(key, moved)
                        finally:
                            n._persist_dedupe_journal = original_journal
                            n._persist_queue = original_queue
                            neotoma_sink.migrate_key = original_sink
                        crash_boundaries.add(boundary)
                        state = n._dedupe_journal[key]["state"]
                        assert state in ("migrating", "migrated"), sequence
                        n = _notifier(root, ALWAYS_SILENT, sent)
                        n.should_flush_digest = lambda: False
                        n._recover_sending_entries()
                        if n._dedupe_journal.get(key, {}).get("state") == "migrated":
                            key = moved
                elif operation.startswith("migration_") and not journal_bad:
                    moved = key.replace(":50", ":corrected:50")
                    has_queue_key = any(isinstance(item, dict) and key in item["keys"]
                                        for item in held_view.read_queue(n._digest_path))
                    sink_state = root / "sink" / "state-shadow-apis.json"
                    if (moved != key and key in n._dedupe_journal
                            and (operation != "migration_queue_fail" or has_queue_key)
                            and (operation != "migration_sink_fail" or sink_state.exists())):
                        from lib.notify import neotoma_sink
                        if operation == "migration_queue_fail":
                            target, name = n, "_persist_queue"
                            replacement = lambda *args: False
                        elif operation == "migration_journal_fail":
                            target, name = n, "_persist_dedupe_journal"
                            replacement = lambda *args: False
                        else:
                            target, name = neotoma_sink, "_state"

                            @contextmanager
                            def replacement(*args, **kwargs):
                                raise OSError("synthetic sink failure")
                                yield

                        original = getattr(target, name)
                        setattr(target, name, replacement)
                        try:
                            assert not n.migrate_dedupe(key, moved)
                        finally:
                            setattr(target, name, original)
                        if moved in n._dedupe_journal:
                            key = moved
                elif operation == "operator_clear":
                    if journal_bad:
                        continue
                    recovery = held_view.read_recovery(n._digest_path)
                    if any(row.get("migration_to") and
                           key in (row["key"], row["migration_to"]) for row in recovery):
                        n.clear_dedupe(key)
                    elif (any(row["key"] == key for row in recovery)
                          or any(row["key"] == key for row in
                                 held_view.journal_receipt_status()[0])):
                        assert held_view.main(["--clear-unconfirmed", key]) == 0, sequence
                        clear_allowance[key] = clear_allowance.get(key, 0) + 1
                    elif before:
                        assert held_view.main([]) == 0
                        keyed = {k for item in before if isinstance(item, dict)
                                 for k in item["keys"]}
                        migrating = {k for row in recovery if row.get("migration_to")
                                     for k in (row["key"], row["migration_to"])}
                        migrating.update(k for k, entry in n._dedupe_journal.items()
                                         if isinstance(entry, dict) and (
                                             entry.get("state") == "migrating"
                                             or entry.get("migration_from")))
                        if (keyed and not keyed.issubset(n._dedupe_journal)
                                or keyed & migrating):
                            assert held_view.main(["--clear"]) == 1
                        else:
                            assert held_view.main(["--clear"]) == 0
                            clear_allowance[key] = clear_allowance.get(key, 0) + 1
                elif operation == "window_roll":
                    n.clear_dedupe(key)
                    key = key.replace("reset0", "reset1")
                elif operation in ("journal_corrupt", "journal_corrupt_bytes"):
                    n._dedupe_path.write_bytes(
                        b"\xff\xfe{" if operation == "journal_corrupt_bytes"
                        else b"{synthetic malformed journal")
                    journal_bad = True
                    n.send(f"crossed {key}", Priority.WARN, handler="apis",
                           dedupe_key=key, retryable=True)
                elif operation == "journal_reconcile" and journal_bad:
                    assert held_view.main(["--clear-unreadable-journal"]) == 0
                    journal_bad = False
                elif operation == "malformed_receipt" and not journal_bad:
                    if key in n._dedupe_journal:
                        doc = n._dedupe_journal
                        doc[key] = {"state": "delivered"}
                        n._dedupe_path.write_text(json.dumps(doc))
                        n.send(f"crossed {key}", Priority.WARN, handler="apis",
                               dedupe_key=key, retryable=True)
                        assert any(key in (row["key"], row.get("migration_to"))
                                   for row in held_view.read_recovery(n._digest_path))
                elif operation == "unreadable_snapshot_twice":
                    source = root / "usage.json"
                    for raw in (b"{first synthetic", b"{later synthetic"):
                        source.write_bytes(raw)
                        held_view.preserve_unreadable(source, raw)
                    assert (root / "usage.json.unreadable").read_bytes() == b"{first synthetic"
                    assert (root / "usage.json.unreadable.latest").read_bytes() == b"{later synthetic"
                elif operation == "transport_refuse":
                    n._in_silence_window = lambda: False
                    original = n._deliver
                    n._deliver = lambda *args, **kwargs: False
                    n.send(f"crossed {key}", Priority.WARN, handler="apis",
                           dedupe_key=key, retryable=True)
                    n._deliver = original
                    n._in_silence_window = lambda: True
                elif operation == "transport_uncertain":
                    n._in_silence_window = lambda: False
                    original = n._deliver

                    def uncertain(message, **kwargs):
                        sent.append(message)
                        raise OSError("transport result lost")

                    n._deliver = uncertain
                    n.send(f"crossed {key}", Priority.WARN, handler="apis",
                           dedupe_key=key, retryable=True)
                    n._deliver = original
                    n._in_silence_window = lambda: True
                after = held_view.read_queue(n._digest_path)
                assert len({held_view.notice_identity(item) for item in after}) == len(after), sequence
                recovery_keys = {row["key"] for row in held_view.read_recovery(n._digest_path)}
                recovery_related = recovery_keys | {row["migration_to"] for row in
                    held_view.read_recovery(n._digest_path) if row.get("migration_to")}
                for journal_key, entry in (() if journal_bad else n._dedupe_journal.items()):
                    if journal_key == held_view.RECOVERED_AT:
                        continue
                    if held_view.receipt_problem(entry):
                        assert journal_key in recovery_related, sequence
                        continue
                    if entry.get("state") == "held":
                        assert any(journal_key in item["keys"] for item in after if isinstance(item, dict)) \
                            or journal_key in recovery_keys or entry.get("migration_from") in recovery_keys, sequence
                    if entry.get("state") in ("sending", "delivered_unconfirmed"):
                        assert (journal_key in recovery_related or journal_key in {
                            row["key"] for row in held_view.journal_receipt_status()[0]
                        }), sequence
                delivered = [message.rsplit(" ", 1)[-1] for message in sent
                             if message.startswith("⚠ [apis] crossed ")]
                for delivered_key in set(delivered):
                    assert delivered.count(delivered_key) <= 1 + clear_allowance.get(delivered_key, 0), sequence
                if len(after) < len(before):
                    assert operation in ("drain", "operator_clear", "window_roll",
                                        "journal_reconcile", "migration_sink_fail"), sequence
    assert crash_boundaries == {"marker", "journal", "queue", "sink", "final"}
    capsys.readouterr()


@pytest.mark.parametrize("failure", [
    "journal_write", "queue_write", "held_receipt", "queue_lock",
    "malformed_journal", "unreadable_queue",
])
def test_retryable_failure_transition_recovers_single_copy(tmp_path, monkeypatch, failure):
    from . import held_view

    sent = []
    n = _notifier(tmp_path, ALWAYS_SILENT, sent)
    n.should_flush_digest = lambda: False
    key = "usage:weekly:50"
    original_queue_lock = n._queue_lock
    original_mark = n._mark_dedupe_held
    original_journal_write = n._persist_dedupe_journal
    original_queue_write = held_view.write_queue
    if failure == "journal_write":
        monkeypatch.setattr(n, "_persist_dedupe_journal", lambda doc: False)
    elif failure == "queue_write":
        monkeypatch.setattr(held_view, "write_queue", lambda path, items: (_ for _ in ()).throw(OSError("synthetic queue failure")))
    elif failure == "held_receipt":
        monkeypatch.setattr(n, "_mark_dedupe_held", lambda *args: False)
    elif failure == "queue_lock":
        @contextmanager
        def unavailable():
            yield False
        monkeypatch.setattr(n, "_queue_lock", unavailable)
    elif failure == "malformed_journal":
        n._dedupe_path.write_text(json.dumps({key: ["malformed"]}))
    else:
        n._digest_path.write_bytes(b"unreadable queue")

    assert not n.send("usage crossed", Priority.WARN, handler="apis",
                      dedupe_key=key, retryable=True)
    if failure == "unreadable_queue":
        assert n._digest_path.read_bytes() == b"unreadable queue"
        n._digest_path.unlink()
    monkeypatch.setattr(n, "_queue_lock", original_queue_lock)
    monkeypatch.setattr(n, "_mark_dedupe_held", original_mark)
    monkeypatch.setattr(n, "_persist_dedupe_journal", original_journal_write)
    monkeypatch.setattr(held_view, "write_queue", original_queue_write)
    if failure == "malformed_journal":
        assert any(row["key"] == key for row in held_view.read_recovery(n._digest_path))
        monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(n._digest_path))
        monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(n._dedupe_path))
        assert held_view.main(["--clear-unconfirmed", key]) == 0
        assert not n.send("usage crossed", Priority.WARN, handler="apis",
                          dedupe_key=key, retryable=True)
    assert not n.send("usage crossed", Priority.WARN, handler="apis",
                      dedupe_key=key, retryable=True)
    assert held_view.read_queue(n._digest_path) == [_keyed_notice(key)]
    assert n._dedupe_journal[key]["state"] == "held"
    n._in_silence_window = lambda: False
    assert n.flush_digest()
    assert [message for message in sent if message == "⚠ [apis] usage crossed"] == [
        "⚠ [apis] usage crossed"
    ]


def test_clear_dedupe_on_a_key_never_seen_does_not_raise(tmp_path):
    n = Notifier(rubric=NO_SILENCE)
    n._dedupe_path = tmp_path / "dedupe.json"
    n.clear_dedupe("never-sent-key")  # must not raise


# ── email_eligible gate — actionability, not severity (ateles#1127) ─────────
#
# The consent-channel-timeout body says "See escalation" — there is nothing
# the operator can decide or do from the email itself. email_eligible=False
# keeps the alert on Telegram/Apprise but stops it from becoming an inbox
# item he has no way to resolve by reading it.


def test_email_eligible_false_skips_email_but_still_delivers_telegram(monkeypatch):
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    n = Notifier(rubric=NO_SILENCE)
    n._email_primary = True
    n._operator_email = "op@test"
    n._notify_to = "op@test"
    email_calls = []
    monkeypatch.setattr(
        n, "_deliver_email", lambda m: (email_calls.append(m), True)[1]
    )
    n._apprise = None  # "would send" (logged) path stands in for Telegram here
    ok = n.send(
        "consent channel failed — see escalation",
        priority=Priority.BLOCKER,
        handler="monedula",
        email_eligible=False,
    )
    assert email_calls == []  # email transport was never tried
    assert ok is False  # no apprise configured in this test — logged, not sent
    # Contrast: the same send WITH email_eligible=True (default) does try email.
    ok2 = n.send(
        "consent channel failed — see escalation",
        priority=Priority.BLOCKER,
        handler="monedula",
    )
    assert email_calls == ["[monedula] consent channel failed — see escalation"]
    assert ok2 is True


def test_email_eligible_default_true_is_unchanged_behaviour(monkeypatch):
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    n = Notifier(rubric=NO_SILENCE)
    n._email_primary = True
    n._operator_email = "op@test"
    n._notify_to = "op@test"
    email_calls = []
    monkeypatch.setattr(
        n, "_deliver_email", lambda m: (email_calls.append(m), True)[1]
    )
    ok = n.send("routine blocker", priority=Priority.BLOCKER, handler="apis")
    assert ok is True
    assert email_calls == ["[apis] routine blocker"]


# ── held notices go out only by a route the raising send allowed ────────────
#
# Incident shape: Apis (no transport) held operator notices overnight in the
# shared queue; the first routine INFO from an email-armed daemon after the
# silence window drained the whole queue as one email per notice (55 and 68
# emails on two mornings). These tests use the real _deliver/_deliver_email
# with only subprocess.run (gws) and, where stated, Apprise faked.

import json  # noqa: E402
import os  # noqa: E402
import logging  # noqa: E402

from lib.notify import held_view  # noqa: E402


class _GwsRuns:
    """Stand-in for subprocess.run that records gws invocations."""

    def __init__(self, returncode=0, on_run=None):
        self.cmds = []
        self.returncode = returncode
        self.on_run = on_run

    def __call__(self, cmd, **kwargs):
        self.cmds.append(cmd)
        if self.on_run:
            self.on_run(cmd)

        class _P:
            stderr = ""

        p = _P()
        p.returncode = self.returncode
        return p

    def bodies(self):
        return [c[c.index("--body") + 1] for c in self.cmds]


def _daemon(tmp_path, *, email, silent, now=1_000_000.0, apprise=None):
    """A Notifier on a private queue dir. ``silent`` pins the silence window
    regardless of wall-clock time."""
    n = Notifier(rubric=NO_SILENCE)
    n._digest_path = tmp_path / "digest.json"
    n._dedupe_path = tmp_path / "dedupe.json"
    n._email_primary = email
    n._notify_to = "alerts@test" if email else ""
    n._swarm_email = ""
    n._apprise = apprise
    n._in_silence_window = lambda: silent
    # The 08:30/20:00 release windows are clock-dependent (ateles#1215); a
    # holding send must not opportunistically drain at those times of day.
    n.should_flush_digest = lambda: False
    n._wall_clock = lambda: now
    return n


def _hold(n, count, handler="apis", **kw):
    for i in range(count):
        n.send(f"PR #{i} needs a decision", Priority.OPERATOR_DECISION, handler=handler, **kw)


def _file(tmp_path, name):
    p = tmp_path / name
    return json.loads(p.read_text()) if p.exists() else []


def test_regression_other_daemons_held_notices_are_not_emailed(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    apis = _daemon(tmp_path, email=False, silent=True)
    _hold(apis, 55)
    assert len(_file(tmp_path, "digest.json")) == 55

    monedula = _daemon(tmp_path, email=True, silent=False)
    monedula.send("monedula started", Priority.INFO, handler="monedula")

    assert runs.cmds == []
    assert len(_file(tmp_path, "digest.json")) == 55


def test_email_ineligible_send_is_never_emailed_on_drain(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    armed = _daemon(tmp_path, email=True, silent=True)
    _hold(armed, 3, email_eligible=False)
    assert _file(tmp_path, "digest-email.json") == []
    assert len(_file(tmp_path, "digest.json")) == 3

    _daemon(tmp_path, email=True, silent=False).flush_digest()
    assert runs.cmds == []


def test_hold_routes_by_raising_send(tmp_path):
    armed = _daemon(tmp_path, email=True, silent=True)
    armed.send("a", Priority.WARN, handler="x")
    armed.send("b", Priority.WARN, handler="x", email_eligible=False)
    unarmed = _daemon(tmp_path, email=False, silent=True)
    unarmed.send("c", Priority.WARN, handler="x")
    assert _file(tmp_path, "digest-email.json") == ["⚠ [x] a"]
    assert _file(tmp_path, "digest.json") == ["⚠ [x] b", "⚠ [x] c"]


def test_legacy_main_queue_strings_are_never_emailed(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    (tmp_path / "digest.json").write_text(
        json.dumps([f"⚠️ [apis] legacy {i}" for i in range(3)])
    )
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    assert runs.cmds == []
    assert len(_file(tmp_path, "digest.json")) == 3


def test_main_queue_drains_through_apprise_without_email(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    pushed = []

    class _Apprise:
        def notify(self, body):
            pushed.append(body)
            return True

    _hold(_daemon(tmp_path, email=False, silent=True), 2)
    _daemon(tmp_path, email=True, silent=False, apprise=_Apprise()).flush_digest()
    assert len(pushed) == 2 and runs.cmds == []
    assert _file(tmp_path, "digest.json") == []


def test_held_email_budget_caps_a_drain_and_persists(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _hold(_daemon(tmp_path, email=True, silent=True), 55)
    assert len(_file(tmp_path, "digest-email.json")) == 55

    t0 = 2_000_000.0
    _daemon(tmp_path, email=True, silent=False, now=t0).flush_digest()
    assert len(runs.cmds) == 5
    assert len(_file(tmp_path, "digest-email.json")) == 50

    # A different instance (another daemon/process) sees the same budget.
    _daemon(tmp_path, email=True, silent=False, now=t0 + 600).flush_digest()
    assert len(runs.cmds) == 5
    assert len(_file(tmp_path, "digest-email.json")) == 50

    # Oldest-first order is preserved across drains.
    assert "PR #5 " in _file(tmp_path, "digest-email.json")[0]

    _daemon(tmp_path, email=True, silent=False, now=t0 + 3601).flush_digest()
    assert len(runs.cmds) == 10
    assert len(_file(tmp_path, "digest-email.json")) == 45


def test_failed_email_send_consumes_no_budget_and_keeps_item(tmp_path, monkeypatch):
    runs = _GwsRuns(returncode=1)
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _hold(_daemon(tmp_path, email=True, silent=True), 3)
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    assert len(_file(tmp_path, "digest-email.json")) == 3
    assert len(runs.cmds) == 1  # stopped hammering after the first failure
    assert not (tmp_path / "digest-email-budget.json").exists()

    ok = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", ok)
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    assert len(ok.cmds) == 3
    assert json.loads((tmp_path / "digest-email-budget.json").read_text())["count"] == 3


def test_over_budget_email_item_still_goes_via_apprise(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    pushed = []

    class _Apprise:
        def notify(self, body):
            pushed.append(body)
            return True

    _hold(_daemon(tmp_path, email=True, silent=True), 7)
    _daemon(tmp_path, email=True, silent=False, apprise=_Apprise()).flush_digest()
    assert len(runs.cmds) == 5 and len(pushed) == 2
    assert _file(tmp_path, "digest-email.json") == []


def test_notice_queued_during_a_drain_survives_and_delivered_do_not_return(
    tmp_path, monkeypatch
):
    other = _daemon(tmp_path, email=True, silent=True)
    fired = []

    def queue_mid_drain(cmd):
        if not fired:
            fired.append(1)
            other._queue_digest("⚠️ [late] arrived mid-drain")

    runs = _GwsRuns(on_run=queue_mid_drain)
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _hold(other, 2)
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    left = _file(tmp_path, "digest-email.json")
    assert left == ["⚠️ [late] arrived mid-drain"]
    assert len(runs.cmds) == 2


def test_a_second_concurrent_drain_skips(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _hold(_daemon(tmp_path, email=True, silent=True), 2)
    n = _daemon(tmp_path, email=True, silent=False)
    with held_view.file_lock(held_view.drain_lock_path(n._digest_path), blocking=False):
        assert n.flush_digest() is False
    assert runs.cmds == []
    assert len(_file(tmp_path, "digest-email.json")) == 2
    assert n.flush_digest() is True  # lock released: drains normally


def test_no_route_drain_is_quiet_and_does_not_rewrite(tmp_path, monkeypatch, caplog):
    _hold(_daemon(tmp_path, email=False, silent=True), 4)
    n = _daemon(tmp_path, email=False, silent=False)
    delivered = []
    n._deliver = lambda *a, **k: delivered.append(a) or True
    writes = []
    real_write = held_view.write_queue
    monkeypatch.setattr(
        held_view, "write_queue", lambda p, i: (writes.append(p), real_write(p, i))[1]
    )
    before = (tmp_path / "digest.json").stat().st_mtime_ns
    with caplog.at_level(logging.DEBUG, logger="lib.notify.notifier"):
        n.flush_digest()
        n.flush_digest()
    assert delivered == [] and writes == []
    assert (tmp_path / "digest.json").stat().st_mtime_ns == before
    assert not any("Would send" in r.getMessage() for r in caplog.records)
    # One summary line for the whole drain, none on the repeat.
    assert sum("no delivery route" in r.getMessage() for r in caplog.records) == 1


def test_full_queue_preserves_oldest_items(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(notifier_mod, "HELD_QUEUE_MAX", 10)
    n = _daemon(tmp_path, email=False, silent=True)
    with caplog.at_level(logging.ERROR, logger="lib.notify.notifier"):
        _hold(n, 13)
    items = _file(tmp_path, "digest.json")
    assert len(items) == 10
    assert "PR #0 " in items[0] and "PR #9 " in items[-1]
    assert sum("preserving existing notices" in r.getMessage() for r in caplog.records) == 3


# ── budget state validation, atomic writes, private files ───────────────────


def _budget_file(tmp_path, payload):
    (tmp_path / "digest-budget-placeholder").write_text("")  # keep dir non-empty
    (tmp_path / "digest-email-budget.json").write_text(payload)


def test_nan_window_start_holds_email_queue(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _budget_file(tmp_path, '{"window_start": NaN, "count": 5}')
    _hold(_daemon(tmp_path, email=True, silent=True), 8)
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    assert runs.cmds == []
    assert len(_file(tmp_path, "digest-email.json")) == 8


def test_negative_count_holds_email_queue(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _budget_file(tmp_path, '{"window_start": 1000000.0, "count": -100}')
    _hold(_daemon(tmp_path, email=True, silent=True), 20)
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    assert runs.cmds == []
    assert len(_file(tmp_path, "digest-email.json")) == 20


def test_infinite_window_start_is_rejected(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _budget_file(tmp_path, '{"window_start": Infinity, "count": 0}')
    _hold(_daemon(tmp_path, email=True, silent=True), 8)
    n = _daemon(tmp_path, email=True, silent=False)
    n.flush_digest()
    assert runs.cmds == []
    _daemon(tmp_path, email=True, silent=False, now=1_000_100.0).flush_digest()
    assert runs.cmds == []
    assert len(_file(tmp_path, "digest-email.json")) == 8


def test_budget_write_is_atomic_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", _GwsRuns())
    _hold(_daemon(tmp_path, email=True, silent=True), 1)
    seen = []
    real_replace = os.replace
    monkeypatch.setattr(
        os, "replace", lambda a, b: (seen.append((str(a), str(b))), real_replace(a, b))[1]
    )
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    budget = str(tmp_path / "digest-email-budget.json")
    assert any(dst == budget and src != budget for src, dst in seen), seen
    assert ((tmp_path / "digest-email-budget.json").stat().st_mode & 0o777) == 0o600


def test_queue_and_lock_files_are_private(tmp_path):
    _hold(_daemon(tmp_path, email=True, silent=True), 1)
    for name in ("digest-email.json", "digest.json.lock"):
        assert ((tmp_path / name).stat().st_mode & 0o777) == 0o600, name


def test_a_planted_symlink_at_the_queue_path_is_replaced_not_followed(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (tmp_path / "digest.json").symlink_to(victim)
    _hold(_daemon(tmp_path, email=False, silent=True), 1)
    assert victim.read_text() == "keep"
    assert not (tmp_path / "digest.json").is_symlink()
    assert len(_file(tmp_path, "digest.json")) == 1


# ── Neotoma alert sink is additive (lib/notify/neotoma_sink.py) ──────────────


def test_sink_never_changes_send_results_or_the_held_queue(monkeypatch, tmp_path):
    from lib.notify import neotoma_sink

    def run(mode):
        monkeypatch.setenv(neotoma_sink.MODE_ENV, mode)
        neotoma_sink._reset_for_tests()
        n = Notifier(rubric=ALWAYS_SILENT)
        n._digest_path = tmp_path / f"held-{mode}.json"
        results = [
            n.send("a", priority=Priority.CRITICAL, handler="h"),
            n.send("b", priority=Priority.BLOCKER, handler="h"),
            n.send("c", priority=Priority.OPERATOR_DECISION, handler="h"),
            n.send("d", priority=Priority.WARN, handler="h"),
            n.send("e", priority=Priority.INFO, handler="h"),
        ]
        return results, list(n._digest_queue)

    assert run("off") == run("shadow")


def test_a_failing_sink_cannot_break_send_or_clear_dedupe(monkeypatch):
    from lib.notify import neotoma_sink

    def boom(**kw):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(neotoma_sink, "record", boom)
    monkeypatch.setattr(neotoma_sink, "resolve", boom)
    n = Notifier(rubric=ALWAYS_SILENT)
    assert n.send("x", priority=Priority.WARN, handler="h", dedupe_key="k") is False
    assert any("x" in m for m in n._digest_queue)
    n.clear_dedupe("k")
    assert n._is_duplicate("k") is True
