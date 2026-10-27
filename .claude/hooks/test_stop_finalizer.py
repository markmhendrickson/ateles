"""Tests for stop_finalizer.py's integrity classification under the
per-state-change rule (operator ruling 2026-10-27, agent_policy
ent_cc109b186805de3d5e141eca: write to Neotoma per state change, not per turn).

The old gate called a session with domain writes "integral" only with "stored
turns", but its turn count was the transcript's own user/assistant events, so
it could neither demand per-turn rows nor tell a session that captured its
conversation from one that never did. The gate now asks the question the
ruling poses: did the session record its conversation capture alongside the
writes that changed state?

These tests drive the real transcript scanner on JSONL fixtures (no mocked
summary), so they fail if the scanner stops detecting capture, not only if the
classifier changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _session_integrity as si  # noqa: E402
import stop_finalizer as sf  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", Path("/nonexistent/neotoma/.env"))
    state_dir = tmp_path / ".session_state"
    state_dir.mkdir()
    monkeypatch.setattr(si, "state_dir", lambda: state_dir)
    monkeypatch.setattr(
        si, "state_path", lambda sid: state_dir / f"{(sid or 'unknown')}.json"
    )
    # The missing-credentials notice is not under test here.
    monkeypatch.setattr(sf, "credentials_warning_already_delivered", lambda _sid: True)


def _user(text="do the thing"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text="done"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}}


def _store(entities, relationships=None, name="mcp__neotoma__store"):
    payload = {"entities": entities}
    if relationships:
        payload["relationships"] = relationships
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": payload}]}}


TASK = {"entity_type": "task", "title": "ship the fix"}
CONVERSATION = {"entity_type": "conversation", "conversation_id": "c1"}
USER_MSG = {"entity_type": "conversation_message", "role": "user", "content": "do the thing"}
PLAN_LINK = {"relationship_type": "PART_OF", "source": "task", "target_plan_id": "p1"}


def _run(monkeypatch, tmp_path, events, enforce):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    monkeypatch.setattr(
        sf, "read_hook_input",
        lambda: {"session_id": "s1", "transcript_path": str(transcript)},
    )
    monkeypatch.setattr(sf, "ENFORCE", enforce)
    seen = []
    monkeypatch.setattr(
        sf, "emit_harness_event",
        lambda sid, summary, status, **kw: seen.append(status),
    )
    rc = sf.main()
    assert len(seen) == 1
    return rc, seen[0]


def test_state_changing_session_with_capture_is_integral_without_per_turn_rows(
    monkeypatch, tmp_path, capsys
):
    """One store carries the task, its conversation capture and the plan link.
    Five further turns changed nothing and stored nothing. The session is
    integral because capture exists, with no row required per turn."""
    events = [
        _user(), _store([TASK, CONVERSATION, USER_MSG], [PLAN_LINK]),
        _assistant_text(),
    ]
    for _ in range(5):
        events += [_user("a question that changes nothing"), _assistant_text("an answer")]
    rc, status = _run(monkeypatch, tmp_path, events, enforce=True)
    assert (rc, status) == (0, "integral")
    assert "Session integrity violation" not in capsys.readouterr().err
    summary = si.scan_transcript(str(tmp_path / "t.jsonl"))
    assert summary["captured_conversation"] is True


def test_domain_writes_with_plan_but_no_capture_is_violated_and_blocks(
    monkeypatch, tmp_path, capsys
):
    """A plan-bound session that wrote domain state and never recorded its
    conversation. The old gate called this integral (transcript turns > 0);
    it must now be violated."""
    events = [_user(), _store([TASK], [PLAN_LINK]), _assistant_text()]
    rc, status = _run(monkeypatch, tmp_path, events, enforce=True)
    assert (rc, status) == (2, "violated")
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "conversation capture" in out["reason"]
    assert "plan OR task link" not in out["reason"]


def test_domain_writes_with_capture_but_no_plan_or_task_link_is_still_violated(
    monkeypatch, tmp_path, capsys
):
    events = [_user(), _store([{"entity_type": "note", "title": "x"}, CONVERSATION, USER_MSG])]
    rc, status = _run(monkeypatch, tmp_path, events, enforce=True)
    assert (rc, status) == (2, "violated")
    assert "plan OR task link" in json.loads(capsys.readouterr().out)["reason"]


def test_warn_mode_records_violation_but_does_not_block(monkeypatch, tmp_path):
    events = [_user(), _store([TASK], [PLAN_LINK])]
    rc, status = _run(monkeypatch, tmp_path, events, enforce=False)
    assert (rc, status) == (0, "violated")


def test_read_only_session_stays_exempt_even_when_enforcing(monkeypatch, tmp_path, capsys):
    events = [_user(), _assistant_text(), _user("another question"), _assistant_text()]
    rc, status = _run(monkeypatch, tmp_path, events, enforce=True)
    assert (rc, status) == (0, "exempt")
    assert capsys.readouterr().out == ""


def test_capture_only_session_with_no_domain_write_stays_exempt(monkeypatch, tmp_path):
    """Conversation bookkeeping alone is not a domain write (session_integrity.md 2.1)."""
    events = [_user(), _store([CONVERSATION, USER_MSG])]
    rc, status = _run(monkeypatch, tmp_path, events, enforce=True)
    assert (rc, status) == (0, "exempt")


def test_capture_in_a_separate_store_call_counts(monkeypatch, tmp_path):
    """Capture need not share a call with the domain write: it is a session
    property, since a session may record the conversation once and change
    state in later turns."""
    events = [
        _user(), _store([CONVERSATION, USER_MSG]),
        _user("next"), _store([TASK], [PLAN_LINK]),
    ]
    rc, status = _run(monkeypatch, tmp_path, events, enforce=True)
    assert (rc, status) == (0, "integral")
