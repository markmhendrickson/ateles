"""Rules 3–5 are gaps in the skill body a load returns, not mirror drift."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".claude" / "hooks"))

from execution.scripts.sync_skills import required_skill_rule_gaps  # noqa: E402
import session_start  # noqa: E402

EMOTION = (
    "evidence present → the claim only with that evidence shown; "
    "no evidence → omit the claim."
)
SHOW_FAILED = "If showing the draft fails, say the show failed."
DISCARDED = "names the discarded draft id and states that the delete is permanent."


def test_writing_voice_gap_tracks_the_emotion_sentence():
    body = f"## 3. Never invent\n\n{EMOTION}\n"
    assert required_skill_rule_gaps("writing-voice", body) == []
    stripped = body.replace(EMOTION, "")
    assert required_skill_rule_gaps("writing-voice", stripped) == [
        "skill_rule_missing writing-voice 3"
    ]


def test_email_mechanics_gaps_are_per_sentence():
    body = f"Then show the operator the email. {SHOW_FAILED}\nDiscard. {DISCARDED}\n"
    assert required_skill_rule_gaps("email-mechanics", body) == []
    assert required_skill_rule_gaps("email-mechanics", body.replace(SHOW_FAILED, "")) == [
        "skill_rule_missing email-mechanics 4"
    ]
    assert required_skill_rule_gaps(
        "email-mechanics", body.replace("names the discarded draft id", "")
    ) == ["skill_rule_missing email-mechanics 5"]
    assert required_skill_rule_gaps("email-mechanics", body) == []
    # Rule 6 is not required in this skill.
    assert "skill_rule_missing email-mechanics 6" not in required_skill_rule_gaps(
        "email-mechanics", body
    )


def test_gaps_are_not_mirror_labels():
    gaps = required_skill_rule_gaps("writing-voice", "")
    assert gaps == ["skill_rule_missing writing-voice 3"]
    assert all(not line.startswith(("MISSING", "DRIFTED", "[skill-sync]")) for line in gaps)


def test_skill_drift_notice_prints_gap_lines(monkeypatch, capsys):
    import subprocess

    class Proc:
        returncode = 0
        stdout = "skill_rule_missing writing-voice 3\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc())
    monkeypatch.setattr(session_start.Path, "exists", lambda self: True)
    session_start._emit_skill_drift_notice()
    out = capsys.readouterr().out
    assert "skill_rule_missing writing-voice 3" in out
    assert "[skill-sync]" not in out


def test_drifted_row_still_prints_skill_sync(monkeypatch, capsys):
    import subprocess

    class Proc:
        returncode = 1
        stdout = (
            "DRIFTED  writing-voice               [body]  /tmp/SKILL.md\n"
            "skill_rule_missing writing-voice 3\n"
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc())
    session_start._emit_skill_drift_notice()
    out = capsys.readouterr().out
    assert "[skill-sync]" in out
    assert "skill_rule_missing writing-voice 3" in out
