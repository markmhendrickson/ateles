"""Tests for run-thread outbound email (E2 of the execution loop)."""

from __future__ import annotations

from lib.daemon_runtime import run_email as re_mod


def test_subject_carries_token_and_collapses_whitespace():
    subj = re_mod.run_subject("ent_abc123", "Pay the   teacher\n  for June")
    assert subj.startswith("[#ent_abc123] ")
    assert "\n" not in subj
    assert "  " not in subj.split("] ", 1)[1]


def test_parse_task_id_from_subject_and_references():
    assert re_mod.parse_task_id("[#ent_abc123] anything") == "ent_abc123"
    assert re_mod.parse_task_id("<run-ent_def456-created-0@d.test>") == "ent_def456"
    assert re_mod.parse_task_id("no token here") is None


def test_kickoff_is_thread_root():
    root, irt, refs = re_mod.thread_ids("ent_abc", "created-0", "kickoff", domain="d.test")
    assert root == "<run-ent_abc-created-0@d.test>"
    assert irt is None and refs is None


def test_later_stage_references_root():
    root, _, _ = re_mod.thread_ids("ent_abc", "created-0", "kickoff", domain="d.test")
    mid, irt, refs = re_mod.thread_ids("ent_abc", "created-0", "done", domain="d.test")
    assert mid == "<run-ent_abc-created-0-done@d.test>"
    assert irt == root and refs == root


def test_build_eml_has_threading_headers_and_token():
    eml = re_mod.build_run_eml(
        from_addr="swarm@d.test", to_addr="op@d.test",
        subject="[#ent_abc] t", body="hi",
        message_id="<m@d.test>", in_reply_to="<r@d.test>", references="<r@d.test>",
    )
    assert b"In-Reply-To" in eml and b"References" in eml
    assert b"[#ent_abc]" in eml and b"swarm@d.test" in eml


def test_send_fail_open_without_config(monkeypatch):
    for var in ("ATELES_SWARM_EMAIL", "OPERATOR_EMAIL", "ATELES_GMAIL_SEND_CMD"):
        monkeypatch.delenv(var, raising=False)
    assert re_mod.send_run_email(
        task_id="ent_abc", run_key="r", stage="kickoff", title="t", body="b"
    ) is False


def test_send_builds_but_skips_without_send_cmd(monkeypatch):
    # Addresses present but no send command → built, not sent → False (no-deadlock).
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@d.test")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@d.test")
    monkeypatch.delenv("ATELES_GMAIL_SEND_CMD", raising=False)
    assert re_mod.send_run_email(
        task_id="ent_abc", run_key="r", stage="kickoff", title="t", body="b"
    ) is False


def test_send_invokes_template(monkeypatch):
    calls = {}

    def fake_run(cmd, shell=False, capture_output=False, text=False, timeout=None):
        calls["cmd"] = cmd

        class _P:
            returncode = 0
            stderr = ""

        return _P()

    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@d.test")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@d.test")
    monkeypatch.setenv("ATELES_GMAIL_SEND_CMD", "gws gmail send --raw {eml} --to {to}")
    monkeypatch.setattr(re_mod.subprocess, "run", fake_run)
    ok = re_mod.send_run_email(
        task_id="ent_abc", run_key="created-0", stage="kickoff", title="t", body="b"
    )
    assert ok is True
    assert "gws gmail send --raw" in calls["cmd"] and "--to op@d.test" in calls["cmd"]
