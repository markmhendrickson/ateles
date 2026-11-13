"""Piculet runs the importer chain under its OWN Neotoma identity, and paces it.

Everything here uses a synthetic key under a temp dir and fake CLIs; the real
key file, token files and Neotoma are never touched.
"""

import json
import logging
import os
import sys
import types
from pathlib import Path

import subprocess

import pytest

_REAL_RUN = subprocess.run  # captured before any test patches it

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import watch  # noqa: E402
from lib.daemon_runtime import neotoma_signed, pinned_cli_identity  # noqa: E402

SUB = "piculet@ateles-swarm"
NOW = 2_000_000.0
BEARER_VARS = pinned_cli_identity.BEARER_ENV_KEYS


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    watch._telegram_alert_state.clear()
    monkeypatch.delenv("NEOTOMA_AAUTH_SUB", raising=False)
    monkeypatch.delenv("PICULET_MAX_IMPORTS_PER_POLL", raising=False)


@pytest.fixture
def identity(tmp_path, monkeypatch):
    """A synthetic piculet key and runtime dir; bearer vars present in the env."""
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "piculet.jwk.json").write_text('{"kid": "synthetic-kid"}')
    rc = tmp_path / "rc"
    (rc / "dist" / "cli").mkdir(parents=True)
    (rc / "dist" / "cli" / "bootstrap.js").write_text("# synthetic runtime\n")
    monkeypatch.setattr(neotoma_signed, "AAUTH_KEYS_DIR", str(keys))
    monkeypatch.setattr(pinned_cli_identity, "default_state_dir", lambda daemon: tmp_path / "pinned-state")
    monkeypatch.setattr(pinned_cli_identity, "NEOTOMA_RC_DIR", str(rc))
    monkeypatch.setattr(watch, "verify_cli_runtime", lambda env: None)
    for name in BEARER_VARS:
        monkeypatch.setenv(name, f"synthetic-{name.lower()}")
    return types.SimpleNamespace(keys=keys, rc=rc, key=keys / "piculet.jwk.json")


class Poll:
    """Drives process_voice_memos against temp state with a scripted importer."""

    def __init__(self, monkeypatch, tmp_path, rc_for=lambda name: 0):
        self.recordings = tmp_path / "Recordings"
        self.recordings.mkdir(exist_ok=True)
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        for attr, fname in (
            ("STATE_FILE", "seen.json"),
            ("HELD_STATE_FILE", "held.json"),
            ("DEFERRED_STATE_FILE", "deferred.json"),
        ):
            monkeypatch.setattr(watch, attr, state / fname)
        monkeypatch.setattr(watch, "RECORDINGS_DIR", self.recordings)
        self.seen, self.held, self.deferred = set(), {}, {}
        self.runs = []  # (memo name, env)
        self.alerts = []
        self.rc_for = rc_for
        monkeypatch.setattr(watch, "notify", lambda *a, **k: None)
        monkeypatch.setattr(watch, "_telegram", lambda msg: self.alerts.append(msg))
        monkeypatch.setattr(watch, "_notify_lib", lambda *a, **k: None)
        monkeypatch.setattr(watch, "_activity", None)
        monkeypatch.setattr(
            watch, "assess_memo",
            lambda memo: (types.SimpleNamespace(clear=True, summary="ok", findings=[], metrics={}), None),
        )
        monkeypatch.setattr(watch, "run_entity_extraction", lambda files: "")

        def fake_run(cmd, **kwargs):
            name = Path(cmd[cmd.index("--file") + 1]).name
            self.runs.append((name, kwargs.get("env")))
            return types.SimpleNamespace(returncode=self.rc_for(name), stdout="", stderr="")

        monkeypatch.setattr(watch.subprocess, "run", fake_run)

    def memo(self, name, age_seconds):
        """Create a memo whose mtime is ``age_seconds`` before NOW (bigger = older)."""
        path = self.recordings / name
        path.write_bytes(b"\0" * 8)
        os.utime(path, (NOW - age_seconds, NOW - age_seconds))
        return path

    def poll(self, now=NOW):
        new = watch.find_new_files(self.seen)
        if new:
            watch.process_voice_memos(new, self.seen, self.held, self.deferred, now)

    @property
    def imported_names(self):
        return [n for n, _ in self.runs]


# ── 1. The importer subprocess is launched as piculet ───────────────────────


def test_importer_subprocess_runs_as_piculet_with_no_bearer(identity, monkeypatch, tmp_path):
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    p.poll()
    (name, env), = p.runs
    assert env["NEOTOMA_AAUTH_SUB"] == SUB
    assert env["NEOTOMA_AAUTH_PRIVATE_JWK_PATH"] == str(identity.key)
    assert env["NEOTOMA_AAUTH_KID"] == "synthetic-kid"
    assert env["NEOTOMA_CLI_SCRIPT"] == str(identity.rc / "dist" / "cli" / "bootstrap.js")
    assert env[pinned_cli_identity.PIN_ENV] == SUB
    assert "a.m4a" in p.seen


def test_no_bearer_style_variable_reaches_the_importer(identity, monkeypatch, tmp_path):
    """The shared bearer is in the daemon's env (the fixture sets all of them);
    none of it may survive into the child, or the CLI would send it."""
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    p.poll()
    env = p.runs[0][1]
    assert [name for name in BEARER_VARS if name in env] == []
    assert not any(str(v).startswith("synthetic-neotoma") for v in env.values())


def test_run_import_without_an_explicit_env_pins_it_too(identity, monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        watch.subprocess, "run",
        lambda cmd, **kw: captured.update(env=kw["env"]) or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert watch.run_import([tmp_path / "a.m4a"]) == 0
    assert captured["env"]["NEOTOMA_AAUTH_SUB"] == SUB
    assert "NEOTOMA_BEARER_TOKEN" not in captured["env"]


# ── 2-4. Fail closed ────────────────────────────────────────────────────────


def _assert_deferred_untouched(p, memos):
    assert p.runs == [], "the importer must not run under another identity"
    assert not (p.seen & set(memos))
    assert p.held == {}
    assert p.deferred == {}


def test_a_conflicting_subject_refuses_and_defers(identity, monkeypatch, tmp_path):
    monkeypatch.setenv("NEOTOMA_AAUTH_SUB", "waxwing@ateles-swarm")
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    p.poll()
    _assert_deferred_untouched(p, ["a.m4a"])
    assert len(p.alerts) == 1 and "waxwing" not in p.alerts[0] or "different principal" in p.alerts[0]


def test_a_missing_key_refuses_and_defers(identity, monkeypatch, tmp_path):
    identity.key.unlink()
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    p.poll()
    _assert_deferred_untouched(p, ["a.m4a"])
    assert len(p.alerts) == 1 and "no AAuth key" in p.alerts[0]


def test_an_unsupported_deployed_cli_refuses_and_defers(identity, monkeypatch, tmp_path):
    def refuse(env):
        raise pinned_cli_identity.PinnedIdentityError("the deployed Neotoma CLI does not support per-agent key paths")

    monkeypatch.setattr(watch, "verify_cli_runtime", refuse)
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    p.poll()
    _assert_deferred_untouched(p, ["a.m4a"])


def test_refusal_alerts_once_across_polls_and_logs_each_poll(identity, monkeypatch, tmp_path, caplog):
    identity.key.unlink()
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    with caplog.at_level(logging.WARNING, logger="piculet"):
        for minute in range(3):
            p.poll(now=NOW + 60 * minute)
    assert len(p.alerts) == 1  # deduplicated
    lines = [r for r in caplog.records if "cannot present its own Neotoma identity" in r.getMessage()]
    assert len(lines) == 3 and all("will not fall back" in r.getMessage() or "not fall back" in r.getMessage() for r in lines)


def test_a_refused_poll_never_falls_back_to_the_bearer(identity, monkeypatch, tmp_path):
    """The bearer IS in this process's env; with the key gone, nothing runs."""
    identity.key.unlink()
    assert os.environ["NEOTOMA_BEARER_TOKEN"]
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    p.poll()
    assert p.runs == []


def test_recovery_after_a_refusal_imports_normally(identity, monkeypatch, tmp_path):
    key_text = identity.key.read_text()
    identity.key.unlink()
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    p.poll()
    assert p.runs == []
    identity.key.write_text(key_text)
    p.poll(now=NOW + 60)
    assert p.imported_names == ["a.m4a"] and "a.m4a" in p.seen


# ── Per-poll cap, newest first, leftovers retried ───────────────────────────


def _eight(p):
    # m0 is the newest, m7 the oldest.
    for i in range(8):
        p.memo(f"m{i}.m4a", 100 * (i + 1))


def test_the_cap_defaults_to_five(identity, monkeypatch, tmp_path):
    p = Poll(monkeypatch, tmp_path)
    _eight(p)
    p.poll()
    assert len(p.runs) == 5 == watch.DEFAULT_MAX_IMPORTS_PER_POLL


def test_the_cap_comes_from_the_environment(identity, monkeypatch, tmp_path):
    monkeypatch.setenv("PICULET_MAX_IMPORTS_PER_POLL", "2")
    p = Poll(monkeypatch, tmp_path)
    _eight(p)
    p.poll()
    assert len(p.runs) == 2


@pytest.mark.parametrize("bad", ["0", "-3", "many", ""])
def test_a_nonsense_cap_reads_as_the_default_never_zero(monkeypatch, bad):
    monkeypatch.setenv("PICULET_MAX_IMPORTS_PER_POLL", bad)
    assert watch.max_imports_per_poll() == 5


def test_a_poll_imports_the_newest_memos_first(identity, monkeypatch, tmp_path):
    monkeypatch.setenv("PICULET_MAX_IMPORTS_PER_POLL", "3")
    p = Poll(monkeypatch, tmp_path)
    _eight(p)
    p.poll()
    assert p.imported_names == ["m0.m4a", "m1.m4a", "m2.m4a"]


def test_leftover_memos_are_not_seen_held_or_deferred_and_retry_next_poll(
    identity, monkeypatch, tmp_path
):
    monkeypatch.setenv("PICULET_MAX_IMPORTS_PER_POLL", "3")
    p = Poll(monkeypatch, tmp_path)
    _eight(p)
    p.poll()
    leftovers = {f"m{i}.m4a" for i in range(3, 8)}
    assert p.seen == {"m0.m4a", "m1.m4a", "m2.m4a"}
    assert not (p.seen & leftovers) and p.held == {} and p.deferred == {}

    p.poll(now=NOW + 60)
    assert p.imported_names[3:] == ["m3.m4a", "m4.m4a", "m5.m4a"]  # next newest, in order
    p.poll(now=NOW + 120)
    assert p.imported_names[6:] == ["m6.m4a", "m7.m4a"]
    assert len(p.seen) == 8 and sorted(p.imported_names) == sorted(set(p.imported_names))


def test_memos_in_retry_backoff_do_not_use_up_the_cap(identity, monkeypatch, tmp_path):
    monkeypatch.setenv("PICULET_MAX_IMPORTS_PER_POLL", "2")
    p = Poll(monkeypatch, tmp_path, rc_for=lambda n: 75 if n == "m0.m4a" else 0)
    _eight(p)
    p.poll()  # m0 (newest) is deferred; m1 imported
    assert "m0.m4a" in p.deferred and "m0.m4a" not in p.seen
    p.runs.clear()
    p.poll(now=NOW + 1)  # m0 still backing off
    assert "m0.m4a" not in p.imported_names and len(p.runs) == 2


def test_each_poll_logs_one_backlog_line(identity, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PICULET_MAX_IMPORTS_PER_POLL", "3")
    p = Poll(monkeypatch, tmp_path)
    _eight(p)
    with caplog.at_level(logging.INFO, logger="piculet"):
        p.poll()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Poll backlog:")]
    assert lines == [
        "Poll backlog: 8 unseen memo(s); importing 3 newest (cap 3), 5 wait for later polls, 0 in retry backoff."
    ]


# ── A deferred store defers the memo like exit 75 ───────────────────────────


def test_a_deferred_store_leaves_the_memo_unseen_unheld_and_retried(identity, monkeypatch, tmp_path):
    p = Poll(monkeypatch, tmp_path, rc_for=lambda n: watch.IMPORT_EXIT_STORE_DEFERRED)
    p.memo("a.m4a", 10)
    p.poll()
    assert "a.m4a" not in p.seen and p.held == {}
    entry = p.deferred["a.m4a"]
    assert entry["reason"] == watch.STORE_DEFERRED_REASON
    assert entry["next_attempt_at"] > NOW
    assert len(p.alerts) == 1
    p.poll(now=NOW + 10)  # inside the backoff: not retried
    assert len(p.runs) == 1
    p.rc_for = lambda n: 0
    p.poll(now=entry["next_attempt_at"] + 1)
    assert "a.m4a" in p.seen and p.deferred == {}


def test_the_store_deferred_code_matches_the_guard():
    from lib.daemon_runtime.neotoma_store_guard import EXIT_STORE_DEFERRED

    assert watch.IMPORT_EXIT_STORE_DEFERRED == EXIT_STORE_DEFERRED == 76


# ── (2) An unknown already-imported lookup means skip, end to end ───────────

FAKE_CLI = '''
import json, os, sys, time
log = os.environ["FAKE_CLI_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({
        "sub": os.environ.get("NEOTOMA_AAUTH_SUB"),
        "bearer": [k for k in ("NEOTOMA_BEARER_TOKEN", "NEOTOMA_TOKEN") if os.environ.get(k)],
        "args": sys.argv[1:],
    }) + "\\n")
mode = os.environ["FAKE_CLI_MODE"]
if mode == "hang":
    time.sleep(30)
elif mode == "error":
    sys.stderr.write("HTTP 502 Bad Gateway\\n")
    sys.exit(1)
elif mode == "garbage":
    print("<html>maintenance</html>")
'''


@pytest.mark.parametrize("mode", ["error", "hang", "garbage"])
def test_a_failed_lookup_defers_the_memo_end_to_end(identity, monkeypatch, tmp_path, mode):
    """Real piculet -> real importer -> a fake pinned CLI that fails its lookup."""
    (identity.rc / "dist" / "cli" / "bootstrap.js").write_text(FAKE_CLI)
    log_file = tmp_path / "cli.log"
    monkeypatch.setenv("NODE_BIN", sys.executable)
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_file))
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    monkeypatch.setenv("IMPORT_LOOKUP_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    p = Poll(monkeypatch, tmp_path)
    monkeypatch.setattr(watch.subprocess, "run", _REAL_RUN)  # the real chain
    p.memo("20261101 101010-AAAA0001.m4a", 10)
    p.poll()

    assert "20261101 101010-AAAA0001.m4a" not in p.seen, "an unknown lookup must never read as new/imported"
    assert p.held == {}
    entry = p.deferred["20261101 101010-AAAA0001.m4a"]
    assert entry["reason"] == watch.DEFERRED_REASON
    # Nothing was staged for it.
    assert not (tmp_path / "data" / "imports").exists() or not any((tmp_path / "data" / "imports").iterdir())
    # The lookup ran through the pinned CLI, as piculet, with no bearer.
    calls = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert calls and all(c["sub"] == SUB and c["bearer"] == [] for c in calls)
    assert all("entities" in c["args"] and "search" in c["args"] for c in calls)


# ── --list-pending: read-only sizing ────────────────────────────────────────


def test_list_pending_reports_count_and_date_range_and_touches_nothing(
    identity, monkeypatch, tmp_path, capsys
):
    p = Poll(monkeypatch, tmp_path)
    p.memo("old.m4a", 86400 * 40)
    p.memo("mid.m4a", 86400 * 10)
    p.memo("new.m4a", 60)
    state_before = sorted(x.name for x in (tmp_path / "state").iterdir())

    def forbidden(*a, **k):
        raise AssertionError("--list-pending must not run anything or reach Neotoma")

    monkeypatch.setattr(watch.subprocess, "run", forbidden)
    monkeypatch.setattr(watch, "_neotoma_query", forbidden)
    monkeypatch.setattr(watch, "_start_daemon_services", forbidden)
    monkeypatch.setattr(watch.time, "time", lambda: NOW)

    assert watch.cli(["watch.py", "--list-pending"]) == 0
    out = capsys.readouterr().out
    assert "3 unseen memo(s)" in out and "3 due now" in out
    assert "poll(s) to clear" in out and "At 5 per poll" in out
    from datetime import datetime

    assert f"Oldest: {datetime.fromtimestamp(NOW - 86400 * 40).strftime('%Y-%m-%d %H:%M')}" in out
    assert f"Newest: {datetime.fromtimestamp(NOW - 60).strftime('%Y-%m-%d %H:%M')}" in out
    assert sorted(x.name for x in (tmp_path / "state").iterdir()) == state_before
    assert p.seen == set() and p.deferred == {}


def test_list_pending_with_nothing_unseen(identity, monkeypatch, tmp_path, capsys):
    Poll(monkeypatch, tmp_path)
    assert watch.cli(["watch.py", "--list-pending"]) == 0
    assert "nothing would be imported" in capsys.readouterr().out


def test_list_pending_counts_memos_in_backoff_separately(identity, monkeypatch, tmp_path, capsys):
    p = Poll(monkeypatch, tmp_path)
    p.memo("a.m4a", 10)
    p.memo("b.m4a", 20)
    watch.record_deferral(p.deferred, "a.m4a", NOW)
    watch.save_deferred(p.deferred)
    monkeypatch.setattr(watch.time, "time", lambda: NOW + 1)
    watch.cli(["watch.py", "--list-pending"])
    out = capsys.readouterr().out
    assert "2 unseen memo(s); 1 due now (1 in retry backoff)" in out
