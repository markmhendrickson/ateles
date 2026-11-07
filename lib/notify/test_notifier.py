"""Regression tests for lib/notify Priority routing.

Locks in the Priority.WARN fix: daemons (formica, neotoma-agent, apis a2a)
send WARN on their failure-reporting paths; before the enum member existed,
those paths raised AttributeError instead of notifying.
"""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def test_unreadable_queue_file_does_not_crash_send(tmp_path):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n._digest_path.write_text("{not json")
    assert n._digest_queue == []
    n.send("still works", Priority.BLOCKER, handler="apis")
    assert any("still works" in m for m in sent)


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


def test_unreadable_dedupe_journal_does_not_crash_send(tmp_path):
    sent = []
    n = _notifier(tmp_path, NO_SILENCE, sent)
    n._dedupe_path.write_text("not json")
    ok = n.send(
        "consent channel failed", Priority.BLOCKER, handler="monedula",
        dedupe_key="monedula:consent_channel_failed",
    )
    assert ok is True  # treated as empty journal, not a crash


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


def test_queue_is_capped_to_newest_items(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(notifier_mod, "HELD_QUEUE_MAX", 10)
    n = _daemon(tmp_path, email=False, silent=True)
    with caplog.at_level(logging.ERROR, logger="lib.notify.notifier"):
        _hold(n, 13)
    items = _file(tmp_path, "digest.json")
    assert len(items) == 10
    assert "PR #3 " in items[0] and "PR #12 " in items[-1]
    assert sum("dropped the 1 oldest" in r.getMessage() for r in caplog.records) == 3


# ── budget state validation, atomic writes, private files ───────────────────


def _budget_file(tmp_path, payload):
    (tmp_path / "digest-budget-placeholder").write_text("")  # keep dir non-empty
    (tmp_path / "digest-email-budget.json").write_text(payload)


def test_nan_window_start_starts_a_fresh_window(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _budget_file(tmp_path, '{"window_start": NaN, "count": 5}')
    _hold(_daemon(tmp_path, email=True, silent=True), 8)
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    assert len(runs.cmds) == 5  # a NaN start would otherwise never roll over: 0 sent
    saved = json.loads((tmp_path / "digest-email-budget.json").read_text())
    assert saved["window_start"] == saved["window_start"] and saved["count"] == 5


def test_negative_count_does_not_raise_the_cap(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _budget_file(tmp_path, '{"window_start": 1000000.0, "count": -100}')
    _hold(_daemon(tmp_path, email=True, silent=True), 20)
    _daemon(tmp_path, email=True, silent=False).flush_digest()
    assert len(runs.cmds) == 5


def test_infinite_window_start_is_rejected(tmp_path, monkeypatch):
    runs = _GwsRuns()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run", runs)
    _budget_file(tmp_path, '{"window_start": Infinity, "count": 0}')
    _hold(_daemon(tmp_path, email=True, silent=True), 8)
    n = _daemon(tmp_path, email=True, silent=False)
    n.flush_digest()
    assert len(runs.cmds) == 5
    _daemon(tmp_path, email=True, silent=False, now=1_000_100.0).flush_digest()
    assert len(runs.cmds) == 5  # and the new window holds


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
