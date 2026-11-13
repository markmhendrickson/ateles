"""The importer under a pinned (daemon) identity: lookups and the deferred-store exit."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_audio_from_desktop as iad  # noqa: E402
from lib.daemon_runtime.neotoma_store_guard import EXIT_STORE_DEFERRED  # noqa: E402
from lib.daemon_runtime.pinned_testing import apply_pinned_env  # noqa: E402

SUB = "piculet@ateles-swarm"


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    apply_pinned_env(monkeypatch, tmp_path)
    cli = Path(os.environ["NEOTOMA_CLI_SCRIPT"])
    monkeypatch.setenv("NODE_BIN", "/fixture/node")
    monkeypatch.setattr(
        iad.shutil, "which", lambda value: value if value == "/fixture/node" else None
    )
    return cli


class _Done:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def test_lookup_uses_the_pinned_cli_not_the_one_on_path(pinned, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("env")))
        return _Done(json.dumps({"entities": []}))

    monkeypatch.setattr(iad.subprocess, "run", fake_run)
    assert iad.import_status("memo.m4a").status == iad.NOT_IMPORTED
    assert calls
    for argv, _ in calls:
        # Only the CLI runs under the private HOME and project root.
        assert argv[0] == "/usr/bin/env"
        assert argv[1] == f"HOME={os.environ['ATELES_PINNED_CLI_HOME']}"
        assert argv[2] == f"NEOTOMA_PROJECT_ROOT={os.environ['ATELES_PINNED_CLI_PROJECT_ROOT']}"
        assert argv[3:5] == ["/fixture/node", str(pinned)]
        assert "neotoma" not in argv[:5]


def test_a_missing_pinned_cli_is_unknown_not_new(pinned, monkeypatch):
    pinned.unlink()
    assert iad.import_status("memo.m4a") == iad.LookupResult(iad.LOOKUP_UNKNOWN, "cli_missing")


def test_a_bearer_beside_the_pinned_identity_makes_the_lookup_unknown(pinned, monkeypatch):
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-bearer")
    called = []
    monkeypatch.setattr(iad.subprocess, "run", lambda *a, **k: called.append(1))
    assert iad.import_status("memo.m4a") == iad.LookupResult(iad.LOOKUP_UNKNOWN, "identity_refused")
    assert called == []  # not one lookup went out under the wrong identity


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired("cli", 1), OSError("boom"), _Done("", 1), _Done("", 0), _Done("{}", 0)],
    ids=["timeout", "error", "nonzero", "empty", "no-entities"],
)
def test_a_failed_pinned_lookup_is_unknown_never_new(pinned, monkeypatch, failure):
    def fake_run(argv, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(iad.subprocess, "run", fake_run)
    assert iad.import_status("memo.m4a").status == iad.LOOKUP_UNKNOWN


def test_the_lookup_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("IMPORT_LOOKUP_TIMEOUT_SECONDS", "2.5")
    assert iad._lookup_timeout_seconds() == 2.5
    monkeypatch.delenv("IMPORT_LOOKUP_TIMEOUT_SECONDS")
    assert iad._lookup_timeout_seconds() == 15


# ── A deferred store ends the run with the deferred code ────────────────────


def _run_import(monkeypatch, tmp_path, outcomes):
    recordings = tmp_path / "Recordings"
    recordings.mkdir()
    files = []
    for i, _ in enumerate(outcomes):
        f = recordings / f"memo-{i}.m4a"
        f.write_bytes(b"\0" * 16)
        files.append(f)
    monkeypatch.setattr(iad, "VOICE_MEMOS_PATH", recordings)
    monkeypatch.setattr(iad, "AUDIO_IMPORTS_DIR", tmp_path / "staging")
    monkeypatch.setattr(iad, "import_status", lambda name: iad.LookupResult(iad.NOT_IMPORTED))
    queue = iter(outcomes)
    monkeypatch.setattr(iad, "transcribe_file", lambda *a, **k: next(queue))
    argv = ["import_audio_from_desktop.py", "--no-analyze"]
    for f in files:
        argv += ["--file", str(f)]
    monkeypatch.setattr(sys, "argv", argv)
    try:
        iad.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_a_deferred_store_exits_with_the_deferred_code(monkeypatch, tmp_path):
    rc = _run_import(monkeypatch, tmp_path, [{"success": False, "deferred": True, "error": ""}])
    assert rc == EXIT_STORE_DEFERRED == 76


def test_a_real_failure_alongside_a_deferral_is_still_a_failure(monkeypatch, tmp_path):
    rc = _run_import(
        monkeypatch,
        tmp_path,
        [{"success": False, "deferred": True, "error": ""}, {"success": False, "error": "x"}],
    )
    assert rc == 1


def test_transcribe_file_reports_the_deferred_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        iad.subprocess, "run", lambda *a, **k: _Done("", EXIT_STORE_DEFERRED)
    )
    result = iad.transcribe_file(tmp_path / "a.m4a")
    assert result["success"] is False and result["deferred"] is True
