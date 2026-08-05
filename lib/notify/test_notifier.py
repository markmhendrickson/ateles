"""Regression tests for lib/notify Priority routing.

Locks in the Priority.WARN fix: daemons (formica, neotoma-agent, apis a2a)
send WARN on their failure-reporting paths; before the enum member existed,
those paths raised AttributeError instead of notifying.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.notify import Notifier, Priority  # noqa: E402

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
    n = Notifier(rubric=ALWAYS_SILENT)
    sent = n.send("dispatch failed", priority=Priority.WARN, handler="formica")
    assert sent is False
    assert any("dispatch failed" in m for m in n._digest_queue)


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
    n = Notifier(rubric=NO_SILENCE)  # apprise unconfigured → Telegram returns False
    n._email_primary = True
    n._operator_email = "op@test"

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


# ── Edge-triggered alerts (state_key) ────────────────────────────────────────
#
# Regression: a 60s poll loop against a multi-hour outage sent one identical
# email per poll (106 on 2026-08-04). Repeats of the SAME condition must
# collapse to one notification, with a single follow-up when it recovers.


def _counting_notifier(tmp_path, monkeypatch):
    """Notifier whose deliveries are counted instead of sent."""
    n = Notifier(rubric=NO_SILENCE)
    n._state_path = tmp_path / "notify_state.json"
    n._alert_state = {}
    sent = []
    monkeypatch.setattr(n, "_deliver", lambda msg, force=False: sent.append(msg) or True)
    return n, sent


def test_repeat_alerts_suppressed_until_resolved(tmp_path, monkeypatch):
    n, sent = _counting_notifier(tmp_path, monkeypatch)
    for _ in range(60):  # an hour of once-a-minute polls
        n.send("Neotoma unavailable", priority=Priority.BLOCKER,
               handler="piculet", state_key="neotoma-unavailable")
    assert len(sent) == 1, f"expected 1 alert for 60 polls, got {len(sent)}"


def test_resolve_sends_one_recovery_then_is_idempotent(tmp_path, monkeypatch):
    n, sent = _counting_notifier(tmp_path, monkeypatch)
    n.send("Neotoma unavailable", priority=Priority.BLOCKER,
           handler="piculet", state_key="neotoma-unavailable")
    assert n.resolve("neotoma-unavailable", "Neotoma reachable", "piculet") is True
    # Every later success must stay silent — resolve is safe to call each poll.
    for _ in range(10):
        assert n.resolve("neotoma-unavailable", "Neotoma reachable", "piculet") is False
    assert len(sent) == 2  # one down, one up
    assert sent[1].startswith("✅")
    assert "was failing" in sent[1]


def test_recurrence_after_resolve_alerts_again(tmp_path, monkeypatch):
    n, sent = _counting_notifier(tmp_path, monkeypatch)
    n.send("down", priority=Priority.BLOCKER, state_key="k")
    n.resolve("k")
    n.send("down", priority=Priority.BLOCKER, state_key="k")  # new outage
    assert len([m for m in sent if not m.startswith("✅")]) == 2


def test_state_survives_restart_mid_outage(tmp_path, monkeypatch):
    """launchd KeepAlive restarts a crashed daemon; the outage is still ongoing
    and must NOT produce a second 'down' alert from the fresh process."""
    state = tmp_path / "notify_state.json"
    monkeypatch.setenv("ATELES_NOTIFY_STATE_FILE", str(state))

    first = Notifier(rubric=NO_SILENCE)
    sent_a = []
    monkeypatch.setattr(first, "_deliver", lambda m, force=False: sent_a.append(m) or True)
    first.send("down", priority=Priority.BLOCKER, state_key="neotoma-unavailable")
    assert len(sent_a) == 1

    second = Notifier(rubric=NO_SILENCE)  # simulates the restarted process
    sent_b = []
    monkeypatch.setattr(second, "_deliver", lambda m, force=False: sent_b.append(m) or True)
    second.send("down", priority=Priority.BLOCKER, state_key="neotoma-unavailable")
    assert sent_b == [], "restart re-alerted for an outage already reported"


def test_distinct_conditions_alert_independently(tmp_path, monkeypatch):
    n, sent = _counting_notifier(tmp_path, monkeypatch)
    n.send("neotoma down", priority=Priority.BLOCKER, state_key="neotoma")
    n.send("wise down", priority=Priority.BLOCKER, state_key="wise")
    assert len(sent) == 2  # unrelated failures must not mask each other


def test_omitting_state_key_is_unchanged(tmp_path, monkeypatch):
    """The 20 existing daemons pass no state_key — every call must still send."""
    n, sent = _counting_notifier(tmp_path, monkeypatch)
    for _ in range(5):
        n.send("payment sent", priority=Priority.BLOCKER, handler="monedula")
    assert len(sent) == 5


def test_corrupt_state_file_fails_open(tmp_path, monkeypatch):
    state = tmp_path / "notify_state.json"
    state.write_text("{not json")
    monkeypatch.setenv("ATELES_NOTIFY_STATE_FILE", str(state))
    n = Notifier(rubric=NO_SILENCE)
    sent = []
    monkeypatch.setattr(n, "_deliver", lambda m, force=False: sent.append(m) or True)
    n.send("down", priority=Priority.BLOCKER, state_key="k")
    assert len(sent) == 1  # unreadable state must not silence alerting


def test_undelivered_state_key_does_not_suppress(tmp_path, monkeypatch):
    """A state_key call that queues instead of delivering must NOT mark the
    condition alerting — otherwise the first poll silently arms suppression and
    the operator is never told about the outage at all."""
    n = Notifier(rubric=ALWAYS_SILENT)
    n._state_path = tmp_path / "notify_state.json"
    n._alert_state = {}
    sent = []
    monkeypatch.setattr(n, "_deliver", lambda m, force=False: sent.append(m) or True)

    # WARN inside a silence window queues for digest — it does not deliver.
    assert n.send("degraded", priority=Priority.WARN, state_key="k") is False
    assert sent == []
    assert "k" not in n._alert_state, "armed suppression without delivering"

    # Because nothing was delivered, a later deliverable alert still gets through.
    assert n.send("down", priority=Priority.BLOCKER, state_key="k") is True
    assert len(sent) == 1
