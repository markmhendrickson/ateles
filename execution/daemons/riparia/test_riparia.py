"""Tests for Riparia's pure reply-routing logic (E3 of the execution loop)."""

from __future__ import annotations

import pathlib
import sys

_RIPARIA_DIR = pathlib.Path(__file__).resolve().parent
if str(_RIPARIA_DIR) not in sys.path:
    sys.path.insert(0, str(_RIPARIA_DIR))

import riparia  # noqa: E402


def _msg(subject="[#ent_abc123] Pay teacher", sender="op@d.test", labels=None):
    return {"id": "m1", "subject": subject, "sender": sender, "labels": labels or []}


def test_should_process_operator_reply_with_token():
    assert riparia.should_process(_msg(), "op@d.test") is True


def test_should_process_skips_already_labelled():
    assert riparia.should_process(_msg(labels=[riparia.PROCESSED_LABEL]), "op@d.test") is False


def test_should_process_skips_non_operator_sender():
    assert riparia.should_process(_msg(sender="stranger@x.test"), "op@d.test") is False


def test_should_process_skips_without_token():
    assert riparia.should_process(_msg(subject="Re: lunch?"), "op@d.test") is False


def test_should_process_refuses_when_operator_unset_empty_or_malformed():
    for bad in ("", "   ", "not-an-address", "a@b@c", "op@d.test, x@y.test", "<op@d.test>"):
        assert riparia.should_process(_msg(sender="anyone@x.test"), bad) is False, bad
        assert riparia.should_process(_msg(sender="op@d.test"), bad) is False, bad


def test_should_process_matches_bare_address_by_equality():
    op = "op@d.test"
    assert riparia.should_process(_msg(sender="Op <op@d.test>"), op) is True
    assert riparia.should_process(_msg(sender="OP@D.TEST"), op) is True
    # suffix / prefix / embedded spoofs must fail equality
    assert riparia.should_process(_msg(sender=op + ".attacker.invalid"), op) is False
    assert riparia.should_process(_msg(sender="xop@d.test"), op) is False
    assert riparia.should_process(_msg(sender="op@d.test <evil@x.test>"), op) is False
    assert riparia.should_process(_msg(sender="op@d.test@evil.test"), op) is False



def _conv(eid, summary, ts):
    return {"entity_id": eid, "snapshot": {"snapshot": {"summary": summary, "name": "run"}},
            "last_observation_at": ts}


def test_select_returns_none_when_no_task_match():
    convs = [_conv("c1", "Execution run for task ent_other (agent cicada, run created-0).", "2026-06-24T10:00:00Z")]
    assert riparia.select_run_conversation(convs, "ent_abc123") is None


def test_select_picks_task_match():
    convs = [_conv("c1", "Execution run for task ent_abc123 (agent cicada, run created-0).", "2026-06-24T10:00:00Z")]
    assert riparia.select_run_conversation(convs, "ent_abc123") == "c1"


def test_select_prefers_run_key():
    convs = [
        _conv("c1", "Execution run for task ent_abc123 (agent cicada, run created-0).", "2026-06-24T09:00:00Z"),
        _conv("c2", "Execution run for task ent_abc123 (agent cicada, run retry-1).", "2026-06-24T11:00:00Z"),
    ]
    assert riparia.select_run_conversation(convs, "ent_abc123", run_key="created-0") == "c1"


def test_select_most_recent_when_no_run_key():
    convs = [
        _conv("c1", "Execution run for task ent_abc123 (agent cicada, run created-0).", "2026-06-24T09:00:00Z"),
        _conv("c2", "Execution run for task ent_abc123 (agent cicada, run retry-1).", "2026-06-24T11:00:00Z"),
    ]
    assert riparia.select_run_conversation(convs, "ent_abc123") == "c2"


def test_should_process_refuses_multi_address_headers():
    op = "op@d.test"
    for sender in (
        "Other <other@x.test>, Operator <op@d.test>",
        "Other <other@x.test> <op@d.test>",
        "Other <other@x.test>; Operator <op@d.test>",
        "op@d.test, other@x.test",
        "op@d.test, op@d.test",
    ):
        assert riparia.should_process(_msg(sender=sender), op) is False, sender
    assert riparia.should_process(_msg(sender='"A, B" <op@d.test>'), op) is True


def test_should_process_malformed_operator_equal_to_sender_refused():
    assert riparia.should_process(_msg(sender="ops"), "ops") is False


def test_main_exits_without_valid_operator_email(monkeypatch):
    import pytest

    monkeypatch.setattr(riparia, "SWARM_EMAIL", "swarm@d.test")
    for bad in ("", "ops", "a@b.test, c@d.test"):
        monkeypatch.setattr(riparia, "OPERATOR_EMAIL", bad)
        with pytest.raises(SystemExit):
            riparia.main()
