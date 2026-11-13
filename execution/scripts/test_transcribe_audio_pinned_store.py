"""A pinned (daemon-identity) run of transcribe_audio: identity, pacing, recovery.

Synthetic keys and fake CLIs only; nothing here touches a real key, token or
Neotoma instance.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
os.environ.setdefault("DATA_DIR", str(_SCRIPTS_DIR / "_test_data_dir"))

import transcribe_audio as ta  # noqa: E402
from test_transcription_results import complete_result  # noqa: E402
from lib.daemon_runtime import neotoma_store_guard as guard  # noqa: E402
from lib.daemon_runtime.pinned_cli_identity import PIN_ENV  # noqa: E402
from lib.daemon_runtime.pinned_testing import apply_pinned_env  # noqa: E402

SUB = "piculet@ateles-swarm"


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    key = apply_pinned_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(tmp_path / "pace" / "pace.json"))
    monkeypatch.setenv("NEOTOMA_STORE_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("NEOTOMA_STORE_RETRY_DELAY_SECONDS", "120")
    return key


# ── Identity: the preflight is the last line against a bearer fallback ──────


def test_pinned_preflight_needs_no_bearer(pinned):
    assert ta._neotoma_auth_preflight() == (True, "ok")


def test_pinned_preflight_refuses_a_bearer_beside_the_identity(pinned, monkeypatch):
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-bearer")
    ok, reason = ta._neotoma_auth_preflight()
    assert ok is False
    assert "bearer" in reason
    assert "synthetic-bearer" not in reason


def test_pinned_preflight_refuses_a_missing_key(pinned, monkeypatch):
    pinned.unlink()
    ok, reason = ta._neotoma_auth_preflight()
    assert ok is False and "key is missing" in reason


def test_pinned_preflight_refuses_a_different_subject(pinned, monkeypatch):
    monkeypatch.setenv("NEOTOMA_AAUTH_SUB", "waxwing@ateles-swarm")
    ok, reason = ta._neotoma_auth_preflight()
    assert ok is False and "subject" in reason


def test_unpinned_preflight_still_requires_the_bearer(monkeypatch):
    monkeypatch.delenv(PIN_ENV, raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    assert ta._neotoma_auth_preflight()[0] is False


def test_a_refused_preflight_blocks_the_store_before_any_call(pinned, monkeypatch, tmp_path):
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-bearer")
    monkeypatch.setattr(ta, "_neotoma_cli_available", lambda: True)
    audio = _audio(tmp_path)
    with patch.object(ta.subprocess, "run") as run:
        with pytest.raises(RuntimeError, match="aborted before any HTTP call"):
            ta.save_transcription(audio, _result(), attach_audio_file=False)
    run.assert_not_called()


# ── Store recovery ──────────────────────────────────────────────────────────


def _audio(tmp_path: Path) -> Path:
    audio = tmp_path / "rec.m4a"
    audio.write_bytes(b"RIFFfake-audio-bytes-0123456789")
    return audio


def _result():
    return {"transcription_text": "hello", "language": "en", "audio_duration_seconds": 1.0,
            "file_size_bytes": 10}


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


_OK = _Proc(0, json.dumps({"structured": {"entities": [{"entity_id": "ent_new"}]}}))
_UNAVAILABLE = _Proc(1, "", "HTTP 503 Service Unavailable")


class _Cli:
    """Scripted store attempts plus a read-back that reports a given landing."""

    def __init__(self, monkeypatch, attempts, landed=None):
        self.attempts = list(attempts)
        self.landed = landed
        self.events = []
        self.store_cmds = []
        self.sleeps = []

        def fake_run(cmd, *a, **k):
            self.events.append("store")
            self.store_cmds.append(list(cmd))
            outcome = self.attempts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def fake_find(content_hash, resolved_audio):
            self.events.append("readback")
            return self.landed if "store" in self.events and self.landed_after_store() else None

        monkeypatch.setattr(ta.subprocess, "run", fake_run)
        monkeypatch.setattr(ta, "_neotoma_prod_cli_argv", lambda args: ["neotoma", *args])
        monkeypatch.setattr(ta, "_neotoma_cli_available", lambda: True)
        monkeypatch.setattr(ta, "_find_existing_transcription", fake_find)
        monkeypatch.setattr(ta, "_store_sleep", lambda s: (self.events.append("sleep"), self.sleeps.append(s)))
        monkeypatch.setattr(ta, "_verify_transcription_storage", lambda *a, **k: None)
        monkeypatch.setattr(ta, "_write_transcript_sidecars", lambda *a, **k: None)
        monkeypatch.setattr(ta, "_neotoma_cli_json", lambda args: {})

    def landed_after_store(self):
        return self.landed is not None


def _store(tmp_path):
    return ta.save_transcription(_audio(tmp_path), _result(), attach_audio_file=False)


def test_a_503_that_actually_landed_is_read_back_not_retried(pinned, monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, [_UNAVAILABLE], landed="ent_landed")
    record = _store(tmp_path)
    assert record["entity_id"] == "ent_landed"
    assert cli.events == ["store", "readback"]  # no sleep, no second store
    assert len(cli.store_cmds) == 1


def test_a_503_that_did_not_land_is_retried_once_with_the_same_key_after_the_delay(
    pinned, monkeypatch, tmp_path
):
    cli = _Cli(monkeypatch, [_UNAVAILABLE, _OK], landed=None)
    record = _store(tmp_path)
    assert record["entity_id"] == "ent_new"
    # Read back BEFORE the retry, then wait, then store again.
    assert cli.events == ["store", "readback", "sleep", "store"]
    assert cli.sleeps == [120]
    first, second = cli.store_cmds
    assert first == second  # byte-identical: same idempotency key, same payload file
    assert "--idempotency-key" in second
    assert second[second.index("--idempotency-key") + 1].startswith("transcription-audio-")


def test_a_timeout_is_treated_like_a_503(pinned, monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, [subprocess.TimeoutExpired("neotoma", 900), _OK])
    assert _store(tmp_path)["entity_id"] == "ent_new"
    assert cli.events == ["store", "readback", "sleep", "store"]


def test_two_transient_failures_defer_the_memo(pinned, monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, [_UNAVAILABLE, _UNAVAILABLE], landed=None)
    with pytest.raises(guard.StoreDeferred):
        _store(tmp_path)
    assert cli.events == ["store", "readback", "sleep", "store", "readback"]
    assert len(cli.store_cmds) == 2  # exactly one retry, never more


def test_a_permanent_failure_is_not_retried(pinned, monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, [_Proc(1, "", "403 capability_denied")])
    with pytest.raises(RuntimeError, match="Neotoma store failed"):
        _store(tmp_path)
    assert cli.events == ["store"]


def test_every_store_carries_an_idempotency_key(pinned, monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, [_OK])
    _store(tmp_path)
    assert all("--idempotency-key" in cmd for cmd in cli.store_cmds)


def test_an_unpinned_run_keeps_its_old_failure_behaviour(monkeypatch, tmp_path):
    monkeypatch.delenv(PIN_ENV, raising=False)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-bearer")
    cli = _Cli(monkeypatch, [_UNAVAILABLE])
    with pytest.raises(RuntimeError, match="Neotoma store failed"):
        _store(tmp_path)
    assert cli.events == ["store"]  # no read-back, no retry


def test_a_deferred_store_exits_with_the_store_deferred_code(pinned, monkeypatch, tmp_path):
    audio = _audio(tmp_path)
    monkeypatch.setattr(sys, "argv", ["transcribe_audio.py", str(audio)])
    with patch.object(ta, "transcribe_audio_file", return_value=complete_result(audio)), patch.object(
        ta, "_merge_transcription_relate_targets", return_value=([], None)
    ), patch.object(ta, "_write_pending_store"), patch.object(
        ta, "save_transcription", side_effect=guard.StoreDeferred("synthetic")
    ):
        with pytest.raises(SystemExit) as exc:
            ta.main()
    assert exc.value.code == guard.EXIT_STORE_DEFERRED == 76


# ── Pacing covers every write the transcriber makes ─────────────────────────


def test_the_store_and_relationship_writes_are_paced(pinned, monkeypatch, tmp_path):
    paced_calls = []
    real = guard.paced

    def recording(call, **kw):
        paced_calls.append(1)
        return real(call, **kw)

    monkeypatch.setattr(ta.neotoma_store_guard, "paced", recording)
    _Cli(monkeypatch, [_OK])
    _store(tmp_path)
    assert len(paced_calls) == 1  # the store

    monkeypatch.setattr(ta.subprocess, "run", lambda *a, **k: _Proc(0, "ok"))
    ok, _ = ta._neotoma_cli_relationship_create("ent_a", "ent_b")
    assert ok and len(paced_calls) == 2  # the relationship write


def test_the_gap_is_enforced_between_the_store_and_a_relationship(pinned, monkeypatch, tmp_path):
    monkeypatch.setenv("NEOTOMA_STORE_MIN_INTERVAL_SECONDS", "12")
    slept = []
    now = {"t": 1_000.0}
    real = guard.paced

    def with_fake_time(call, **kw):
        return real(call, clock=lambda: now["t"], sleep=lambda s: (slept.append(s), now.__setitem__("t", now["t"] + s)))

    monkeypatch.setattr(ta.neotoma_store_guard, "paced", with_fake_time)
    _Cli(monkeypatch, [_OK])
    _store(tmp_path)
    monkeypatch.setattr(ta.subprocess, "run", lambda *a, **k: _Proc(0, "ok"))
    ta._neotoma_cli_relationship_create("ent_a", "ent_b")
    assert slept and slept[-1] == pytest.approx(12)


# ── Relationship writes get the store's recovery policy ─────────────────────


class _RelCli:
    def __init__(self, monkeypatch, attempts, exists_after_failure=False):
        self.attempts = list(attempts)
        self.events = []
        self.cmds = []
        self.exists = exists_after_failure

        def fake_run(cmd, *a, **k):
            self.events.append("write")
            self.cmds.append(list(cmd))
            outcome = self.attempts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def fake_json(args):
            self.events.append("readback")
            assert args[:2] == ["relationships", "list"]
            return {"relationships": [{"id": "rel_1"}]} if self.exists else {"relationships": []}

        monkeypatch.setattr(ta.subprocess, "run", fake_run)
        monkeypatch.setattr(ta, "_neotoma_prod_cli_argv", lambda args: ["neotoma", *args])
        monkeypatch.setattr(ta, "_neotoma_cli_available", lambda: True)
        monkeypatch.setattr(ta, "_neotoma_cli_json", fake_json)
        monkeypatch.setattr(ta, "_store_sleep", lambda s: self.events.append(f"sleep{int(s)}"))


def test_a_503_relationship_that_landed_is_read_back_not_retried(pinned, monkeypatch):
    cli = _RelCli(monkeypatch, [_UNAVAILABLE], exists_after_failure=True)
    ok, msg = ta._neotoma_cli_relationship_create("ent_a", "ent_b")
    assert ok and "read-back" in msg
    assert cli.events == ["write", "readback"]


def test_a_503_relationship_is_retried_once_with_the_same_command_after_the_delay(pinned, monkeypatch):
    cli = _RelCli(monkeypatch, [_UNAVAILABLE, _Proc(0, "created")])
    ok, _ = ta._neotoma_cli_relationship_create("ent_a", "ent_b")
    assert ok
    assert cli.events == ["write", "readback", "sleep120", "write"]
    assert cli.cmds[0] == cli.cmds[1]


def test_a_timed_out_relationship_is_treated_like_a_503(pinned, monkeypatch):
    cli = _RelCli(monkeypatch, [subprocess.TimeoutExpired("neotoma", 120), _Proc(0, "created")])
    assert ta._neotoma_cli_relationship_create("ent_a", "ent_b")[0]
    assert cli.events == ["write", "readback", "sleep120", "write"]


def test_two_transient_relationship_failures_defer(pinned, monkeypatch):
    cli = _RelCli(monkeypatch, [_UNAVAILABLE, _UNAVAILABLE])
    with pytest.raises(guard.StoreDeferred):
        ta._neotoma_cli_relationship_create("ent_a", "ent_b")
    assert cli.events == ["write", "readback", "sleep120", "write", "readback"]


def test_a_permanent_relationship_failure_keeps_the_normal_path(pinned, monkeypatch):
    cli = _RelCli(monkeypatch, [_Proc(1, "", "403 capability_denied")])
    ok, msg = ta._neotoma_cli_relationship_create("ent_a", "ent_b")
    assert ok is False and "capability_denied" in msg
    assert cli.events == ["write"]


def test_a_deferred_relationship_exits_76_through_the_transcriber(pinned, monkeypatch, tmp_path):
    _RelCli(monkeypatch, [_UNAVAILABLE, _UNAVAILABLE])
    with pytest.raises(guard.StoreDeferred):
        ta.apply_transcription_neotoma_relationships("ent_t", ["ent_c"], None)


def test_an_unpinned_relationship_failure_is_unchanged(monkeypatch):
    monkeypatch.delenv(PIN_ENV, raising=False)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-bearer")
    cli = _RelCli(monkeypatch, [_UNAVAILABLE])
    ok, _ = ta._neotoma_cli_relationship_create("ent_a", "ent_b")
    assert ok is False and cli.events == ["write"]
