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
    n._deliver = lambda m, force=False: (sent.append(m), True)[1]
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

    n._deliver = lambda m, force=False: False  # transport down
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

    monkeypatch.setattr(
        "lib.notify.notifier.subprocess.run",
        lambda cmd, **k: calls.setdefault("cmd", cmd) or _P(),
    )
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

    monkeypatch.setattr(
        "lib.notify.notifier.subprocess.run",
        lambda cmd, **k: calls.setdefault("cmd", cmd) or _P(),
    )
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
