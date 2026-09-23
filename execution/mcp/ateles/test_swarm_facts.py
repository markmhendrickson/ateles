#!/usr/bin/env python3
"""Tests for swarm_facts — the operational fact checker.

The tests are organised around the SIX wrong assumptions that motivated the
module (see its docstring). Each check gets a test proving it would have
produced the correct answer, plus a test proving it fails CLOSED — reporting
"unknown" with a reason rather than an empty result that reads as an all-clear.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import swarm_facts as sf  # noqa: E402


# ── #1 deploy triggers ───────────────────────────────────────────────────────


def _write_workflow(root: Path, name: str, body: str) -> None:
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(body)


def test_deploy_triggers_reports_workflow_dispatch_alongside_release(tmp_path):
    """The exact miss: reading `release:` and not seeing `workflow_dispatch`."""
    _write_workflow(
        tmp_path,
        "deploy.yml",
        "name: Deploy\non:\n  release:\n    types: [published]\n  workflow_dispatch:\n"
        "    inputs:\n      ref:\n        required: true\njobs:\n  d:\n    runs-on: ubuntu-latest\n",
    )
    r = sf.check_deploy_triggers(repo_root=str(tmp_path))
    assert r["status"] == "ok"
    wf = r["workflows"][0]
    assert "release" in wf["triggers"]
    assert "workflow_dispatch" in wf["triggers"]
    assert wf["manually_dispatchable"] is True
    assert "ref" in wf["dispatch_inputs"]
    assert "gh workflow run" in wf["dispatch_command"]
    # The headline must say a release is not required.
    assert "release is NOT required" in r["interpretation"]


def test_deploy_triggers_marks_event_only_workflow_as_not_dispatchable(tmp_path):
    _write_workflow(
        tmp_path, "ci.yml", "name: CI\non:\n  pull_request:\njobs:\n  t:\n    runs-on: ubuntu-latest\n"
    )
    r = sf.check_deploy_triggers(repo_root=str(tmp_path))
    assert r["workflows"][0]["manually_dispatchable"] is False
    assert "None accept workflow_dispatch" in r["interpretation"]


def test_deploy_triggers_handles_yaml_on_parsed_as_boolean_true(tmp_path):
    """YAML 1.1 resolves a bare `on` to True; reading only "on" silently misses."""
    _write_workflow(
        tmp_path, "x.yml", "on:\n  push:\n  workflow_dispatch:\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
    )
    r = sf.check_deploy_triggers(repo_root=str(tmp_path))
    assert set(r["workflows"][0]["triggers"]) == {"push", "workflow_dispatch"}


def test_deploy_triggers_unknown_when_no_workflow_dir(tmp_path):
    r = sf.check_deploy_triggers(repo_root=str(tmp_path))
    assert r["status"] == "unknown"
    assert "reason" in r
    assert "workflows" not in r  # must NOT read as "zero workflows, all clear"


def test_deploy_triggers_filters_by_name(tmp_path):
    _write_workflow(tmp_path, "deploy.yml", "on:\n  workflow_dispatch:\njobs:\n  a:\n    runs-on: x\n")
    _write_workflow(tmp_path, "ci.yml", "on:\n  push:\njobs:\n  a:\n    runs-on: x\n")
    r = sf.check_deploy_triggers(workflow="deploy", repo_root=str(tmp_path))
    assert [w["file"] for w in r["workflows"]] == ["deploy.yml"]


def test_deploy_triggers_unknown_for_nonexistent_workflow_name(tmp_path):
    _write_workflow(tmp_path, "ci.yml", "on:\n  push:\njobs:\n  a:\n    runs-on: x\n")
    r = sf.check_deploy_triggers(workflow="nope", repo_root=str(tmp_path))
    assert r["status"] == "unknown"


# ── #2 which app serves a domain ─────────────────────────────────────────────


def test_serving_app_identifies_app_and_flags_similarly_named_decoy(monkeypatch):
    """The exact miss: acting on `neotoma` when `neotoma-markmhendrickson` serves."""
    def fake_run(cmd, timeout=sf.CMD_TIMEOUT):
        if cmd[0] == "dig" and "CNAME" in cmd:
            if cmd[2] == "neotoma.example.com":
                return 0, "neotoma-markmhendrickson.fly.dev.", ""
            return 0, "", ""
        if cmd[0] == "flyctl":
            return 0, (
                '[{"Name":"neotoma","Status":"suspended","LatestDeploy":"May 12 2026"},'
                '{"Name":"neotoma-markmhendrickson","Status":"deployed",'
                '"LatestDeploy":"Aug 27 2026"}]'
            ), ""
        return 1, "", "unexpected"

    monkeypatch.setattr(sf, "_run", fake_run)
    r = sf.check_serving_app("neotoma.example.com")
    assert r["status"] == "ok"
    assert r["serving_fly_app"] == "neotoma-markmhendrickson"
    assert r["serving_app_status"] == "deployed"
    assert "neotoma" in r["similarly_named_apps_NOT_serving_this_domain"]
    assert "Do NOT act on these similarly-named apps" in r["interpretation"]


def test_serving_app_unknown_when_domain_is_not_on_fly(monkeypatch):
    monkeypatch.setattr(
        sf, "_run",
        lambda cmd, timeout=sf.CMD_TIMEOUT: (0, "", "") if cmd[0] == "dig" else (0, "[]", ""),
    )
    r = sf.check_serving_app("elsewhere.example.com")
    assert r["status"] == "unknown"
    assert "reason" in r


def test_serving_app_reports_dns_failure_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(
        sf, "_run",
        lambda cmd, timeout=sf.CMD_TIMEOUT: (1, "", "SERVFAIL") if cmd[0] == "dig" else (0, "[]", ""),
    )
    r = sf.check_serving_app("broken.example.com")
    assert r["status"] == "unknown"
    assert "SERVFAIL" in r["reason"]


def test_serving_app_missing_flyctl_still_answers_from_dns(monkeypatch):
    def fake_run(cmd, timeout=sf.CMD_TIMEOUT):
        if cmd[0] == "dig" and "CNAME" in cmd and cmd[2] == "a.example.com":
            return 0, "someapp.fly.dev.", ""
        if cmd[0] == "dig":
            return 0, "", ""
        return 127, "", "flyctl not found on PATH"

    monkeypatch.setattr(sf, "_run", fake_run)
    r = sf.check_serving_app("a.example.com")
    assert r["status"] == "ok"
    assert r["serving_fly_app"] == "someapp"
    assert "flyctl" in r["fly_list_error"]


# ── #5 checkout freshness ────────────────────────────────────────────────────


def _git(tmp_path: Path) -> Path:
    import subprocess

    up = tmp_path / "upstream"
    up.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(up)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(up), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "f.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD"], check=True)
    return work


def test_checkout_freshness_clean_checkout_is_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("ATELES_CHECKOUT_DRIFT_NO_FETCH", "1")
    work = _git(tmp_path)
    r = sf.check_checkout_freshness(str(work))
    assert r["status"] == "ok"
    assert r["behind"] == 0 and r["ahead"] == 0


def test_checkout_freshness_flags_unpushed_commits_as_drift(tmp_path, monkeypatch):
    """Ahead-only IS drift: unpushed work is invisible to review."""
    import subprocess

    monkeypatch.setenv("ATELES_CHECKOUT_DRIFT_NO_FETCH", "1")
    work = _git(tmp_path)
    (work / "f.txt").write_text("two\n")
    subprocess.run(["git", "-C", str(work), "commit", "-aqm", "local"], check=True)
    r = sf.check_checkout_freshness(str(work))
    assert r["status"] == "drifted"
    assert r["ahead"] == 1
    assert "unpushed" in r["interpretation"]


def test_checkout_freshness_flags_dirty_tracked_files_but_ignores_untracked(
    tmp_path, monkeypatch
):
    """Untracked files are not drift — deploy checkouts accumulate logs."""
    monkeypatch.setenv("ATELES_CHECKOUT_DRIFT_NO_FETCH", "1")
    work = _git(tmp_path)
    (work / "untracked.log").write_text("noise\n")
    r = sf.check_checkout_freshness(str(work))
    assert r["status"] == "ok"
    assert r["dirty_tracked_files"] == 0

    (work / "f.txt").write_text("modified\n")
    r = sf.check_checkout_freshness(str(work))
    assert r["status"] == "drifted"
    assert r["dirty_tracked_files"] == 1


def test_checkout_freshness_unknown_for_non_repo(tmp_path):
    r = sf.check_checkout_freshness(str(tmp_path))
    assert r["status"] == "unknown"


def test_checkout_freshness_failed_fetch_is_unknown_not_drift(tmp_path, monkeypatch):
    """Offline must not look identical to real drift."""
    monkeypatch.delenv("ATELES_CHECKOUT_DRIFT_NO_FETCH", raising=False)
    work = _git(tmp_path)
    real = sf._run

    def fake_run(cmd, timeout=sf.CMD_TIMEOUT):
        if "fetch" in cmd:
            return 1, "", "could not resolve host"
        return real(cmd, timeout)

    monkeypatch.setattr(sf, "_run", fake_run)
    r = sf.check_checkout_freshness(str(work))
    assert r["status"] == "unknown"
    assert "could not resolve host" in r["interpretation"]


# ── daemons: described vs installed vs running ───────────────────────────────


def test_daemons_separates_in_repo_from_actually_running(tmp_path, monkeypatch):
    d = tmp_path / "execution" / "daemons"
    for name in ("alpha", "beta", "ghost"):
        (d / name).mkdir(parents=True)

    monkeypatch.setattr(
        sf, "_run",
        lambda cmd, timeout=sf.CMD_TIMEOUT: (
            0,
            "PID\tStatus\tLabel\n"
            "123\t0\tcom.ateles.alpha\n"
            "-\t0\tcom.ateles.beta\n"
            "-\t0\tcom.other.thing\n",
            "",
        ),
    )
    r = sf.check_daemons(repo_root=str(tmp_path))
    assert r["status"] == "ok"
    assert r["running"] == ["alpha"]
    assert r["loaded_but_not_running"] == ["beta"]
    assert r["in_repo_but_never_loaded"] == ["ghost"]
    assert "NOT evidence it runs" in r["interpretation"]


def test_daemons_unknown_when_launchctl_unavailable(tmp_path, monkeypatch):
    (tmp_path / "execution" / "daemons" / "alpha").mkdir(parents=True)
    monkeypatch.setattr(
        sf, "_run", lambda cmd, timeout=sf.CMD_TIMEOUT: (127, "", "launchctl not found")
    )
    r = sf.check_daemons(repo_root=str(tmp_path))
    assert r["status"] == "unknown"
    assert "UNKNOWN, not clear" in r["interpretation"]


# ── #3/#6 does this code path exist ──────────────────────────────────────────


def test_code_path_zero_matches_says_so_unambiguously(tmp_path):
    (tmp_path / "a.py").write_text("def unrelated():\n    pass\n")
    r = sf.check_code_path("hallucination_filter", repo_root=str(tmp_path))
    assert r["status"] == "ok"
    assert r["match_count"] == 0
    assert "ZERO matches" in r["interpretation"]
    assert "do not assume it is inherited" in r["interpretation"]


def test_code_path_separates_definition_from_test_only_use(tmp_path):
    """Existence is not use — the checkout_drift shape: defined, barely called."""
    (tmp_path / "guard.py").write_text("def checkout_drift():\n    pass\n")
    (tmp_path / "test_guard.py").write_text("from guard import checkout_drift\n")
    r = sf.check_code_path("checkout_drift", repo_root=str(tmp_path))
    assert r["non_test_files"] == ["guard.py"]
    assert r["test_files"] == ["test_guard.py"]
    assert "Existence is not use" in r["interpretation"]


# ── dispatch ─────────────────────────────────────────────────────────────────


def test_unknown_check_lists_available_checks():
    r = sf.check_swarm_fact("nonsense")
    assert r["status"] == "unknown"
    assert "deploy_triggers" in r["available_checks"]


def test_dispatch_drops_args_the_check_does_not_accept(tmp_path):
    r = sf.check_swarm_fact(
        "deploy_triggers", repo_root=str(tmp_path), domain="ignored.example.com"
    )
    assert r["status"] == "unknown"  # no workflow dir; domain arg did not crash it


def test_dispatch_reports_exception_as_unverified_not_all_clear(monkeypatch):
    monkeypatch.setitem(
        sf.CHECKS, "boom", lambda: (_ for _ in ()).throw(RuntimeError("kaboom"))
    )
    r = sf.check_swarm_fact("boom")
    assert r["status"] == "unknown"
    assert "kaboom" in r["reason"]
    assert "NOT an all-clear" in r["note"]


def test_every_check_returns_a_status_field(tmp_path, monkeypatch):
    """No check may return a result an agent could read as a bare success."""
    monkeypatch.setattr(sf, "_run", lambda cmd, timeout=sf.CMD_TIMEOUT: (127, "", "absent"))
    for name in sf.CHECKS:
        kwargs = {"repo_root": str(tmp_path), "path": str(tmp_path),
                  "domain": "x.example.com", "pattern": "zzz"}
        r = sf.check_swarm_fact(name, **kwargs)
        assert "status" in r, name
        assert r["status"] in ("ok", "drifted", "unknown"), (name, r["status"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
