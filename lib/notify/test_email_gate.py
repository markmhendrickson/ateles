"""Tests for the global outbound-email kill-switch (ateles#645).

Two properties matter and are asserted independently:

  1. The switch actually stops mail reaching the wire (no subprocess call).
  2. A suppressed notification is still RECORDED — the ateles#583/#636 failure
     was silence that looked like calm, so "not emailed" must never mean "lost".
"""

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.notify import Notifier, Priority  # noqa: E402
from lib.notify.email_gate import (  # noqa: E402
    ENV_FLAG,
    email_enabled,
    record_suppressed,
    suppressed_log_path,
)

NO_SILENCE = {"silence_start": "", "silence_end": "", "timezone": "Europe/Madrid"}


# ── the flag itself ──────────────────────────────────────────────────────────


def test_enabled_by_default(monkeypatch):
    """Unset means ENABLED. This is an opt-out: a missing variable must never
    silently mute operator alerting."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert email_enabled() is True


def test_falsey_values_disable(monkeypatch):
    for raw in ("0", "false", "FALSE", "no", "off", " 0 ", "Off"):
        monkeypatch.setenv(ENV_FLAG, raw)
        assert email_enabled() is False, f"{raw!r} should disable"


def test_truthy_and_unrecognized_values_enable(monkeypatch):
    for raw in ("1", "true", "yes", "on", ""):
        monkeypatch.setenv(ENV_FLAG, raw)
        assert email_enabled() is True, f"{raw!r} should leave email enabled"


def test_flag_is_read_at_call_time_not_cached(monkeypatch):
    """A long-lived daemon must observe a flip without a code reload."""
    monkeypatch.setenv(ENV_FLAG, "1")
    assert email_enabled() is True
    monkeypatch.setenv(ENV_FLAG, "0")
    assert email_enabled() is False


# ── suppression is recorded, not dropped ─────────────────────────────────────


def test_record_suppressed_writes_recoverable_entry(monkeypatch, tmp_path):
    sink = tmp_path / "suppressed.jsonl"
    monkeypatch.setenv("ATELES_SUPPRESSED_EMAIL_LOG", str(sink))
    assert suppressed_log_path() == sink

    assert record_suppressed(
        channel="unit", subject="[Ateles] something", body="full body",
        to="op@test", meta={"task_id": "ent_1"},
    ) is True

    entry = json.loads(sink.read_text().strip())
    assert entry["channel"] == "unit"
    assert entry["subject"] == "[Ateles] something"
    assert entry["body"] == "full body"           # body preserved verbatim
    assert entry["to"] == "op@test"
    assert entry["meta"]["task_id"] == "ent_1"
    assert entry["ts"]


def test_record_suppressed_appends(monkeypatch, tmp_path):
    sink = tmp_path / "suppressed.jsonl"
    monkeypatch.setenv("ATELES_SUPPRESSED_EMAIL_LOG", str(sink))
    for i in range(3):
        record_suppressed(channel="unit", subject=f"s{i}", body="b")
    assert len(sink.read_text().strip().splitlines()) == 3


def test_record_suppressed_is_fail_open(monkeypatch, tmp_path):
    """An unwritable sink must not raise — auditing may degrade, never crash."""
    bad = tmp_path / "a-file"
    bad.write_text("not a dir")
    monkeypatch.setenv("ATELES_SUPPRESSED_EMAIL_LOG", str(bad / "nested.jsonl"))
    assert record_suppressed(channel="unit", subject="s", body="b") is False


# ── the notifier honours it ──────────────────────────────────────────────────


def _email_notifier():
    n = Notifier(rubric=NO_SILENCE)
    n._email_primary = True
    n._operator_email = "op@test"
    n._swarm_email = "swarm@test"
    n._notify_to = "op@test"
    return n


def test_notifier_does_not_shell_out_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.setenv("ATELES_SUPPRESSED_EMAIL_LOG", str(tmp_path / "s.jsonl"))
    n = _email_notifier()

    def boom(cmd, **k):  # pragma: no cover - must never run
        raise AssertionError(f"email was sent despite kill-switch: {cmd}")

    monkeypatch.setattr("lib.notify.notifier.subprocess.run", boom)
    # Telegram is unconfigured in the test rubric, so _deliver returns False;
    # the assertion that matters is that no send was attempted.
    n.send("blocker happened", priority=Priority.BLOCKER, handler="apis")


def test_notifier_records_the_suppressed_message(monkeypatch, tmp_path):
    sink = tmp_path / "s.jsonl"
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.setenv("ATELES_SUPPRESSED_EMAIL_LOG", str(sink))
    n = _email_notifier()
    monkeypatch.setattr("lib.notify.notifier.subprocess.run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))

    n.send("disk is full", priority=Priority.BLOCKER, handler="apis")

    # The notifier may opportunistically flush a persisted digest first, so scan
    # all recorded entries rather than assuming ours is the first line.
    entries = [json.loads(ln) for ln in sink.read_text().strip().splitlines()]
    assert entries, "nothing recorded — a suppressed alert was lost"
    mine = [e for e in entries if "disk is full" in e["body"]]
    assert mine, f"suppressed alert not recorded; got {[e['subject'] for e in entries]}"
    assert mine[0]["channel"] == "notifier"
    assert mine[0]["to"] == "op@test"


def test_notifier_still_sends_when_enabled(monkeypatch):
    """The switch defaults open — the fix must not mute mail on its own."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    n = _email_notifier()
    calls = {}

    class _P:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **k):
        calls["cmd"] = cmd
        return _P()

    monkeypatch.setattr("lib.notify.notifier.subprocess.run", fake_run)
    assert n.send("hello", priority=Priority.BLOCKER, handler="apis") is True
    assert calls["cmd"][:3] == ["gws", "gmail", "+send"]
