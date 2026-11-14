"""Post-deploy probes: the open sandbox must be judged on its own expectations,
a closed host strictly. Fixture rows are the v0.15.0 report's shape/values."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import probe_gate  # noqa: E402
import publish  # noqa: E402

SANDBOX = "https://neotoma-sandbox.fly.dev"
CLOSED = "https://closed.example"
HEADER = (
    "# Post-deploy security probes — v0.29.0\n\n## Results\n\n"
    "| Host | Method | Route | No-auth | Expected | Invalid-auth | Expected | Verdict |\n"
    "|------|--------|-------|---------|----------|--------------|----------|---------|\n"
)


def row(host, method, route, no_auth, invalid, verdict=None):
    v = verdict or ("pass" if (no_auth == 401 and invalid == 401) else "fail")
    return f"| {host} | {method} | {route} | {no_auth} | [401] | {invalid} | [401] | {v} |\n"


# What the sandbox does by design: anonymous answered, bad bearer refused.
SANDBOX_OK = (
    row(SANDBOX, "GET", "/access_policies", 200, 401)
    + row(SANDBOX, "POST", "/correct", 400, 401)
    + row(SANDBOX, "GET", "/server-info", 200, 200)
    + row(SANDBOX, "GET", "/entities/duplicates", 404, 401)
)


def test_sandbox_behaving_as_designed_passes():
    j = probe_gate.judge(probe_gate.parse_report(HEADER + SANDBOX_OK), [SANDBOX])
    assert j.passed and j.ok == 4, j.failures


def test_sandbox_accepting_a_bad_bearer_fails():
    rep = HEADER + SANDBOX_OK + row(SANDBOX, "GET", "/agents", 200, 200)
    j = probe_gate.judge(probe_gate.parse_report(rep), [SANDBOX])
    assert not j.passed
    assert any("/agents" in f and "bad-bearer 200" in f for f in j.failures)


def test_sandbox_route_down_fails():
    rep = HEADER + row(SANDBOX, "GET", "/agents", 502, 502)
    assert not probe_gate.judge(probe_gate.parse_report(rep), [SANDBOX]).passed


def test_closed_host_auth_regression_still_fails():
    rep = HEADER + row(CLOSED, "GET", "/entities", 200, 401) + row(CLOSED, "POST", "/store", 401, 401)
    j = probe_gate.judge(probe_gate.parse_report(rep), [SANDBOX])
    assert not j.passed and j.ok == 1 and "closed.example" in j.failures[0]


def test_closed_host_healthy_passes():
    rep = HEADER + row(CLOSED, "GET", "/entities", 401, 401)
    assert probe_gate.judge(probe_gate.parse_report(rep), [SANDBOX]).passed


def test_empty_report_is_not_a_pass():
    j = probe_gate.judge(probe_gate.parse_report("nothing"), [SANDBOX])
    assert not j.passed and "no rows" in j.summary()


def test_post_release_summary_uses_gate_not_script_exit_code(monkeypatch, tmp_path):
    """The script exits 1 on the sandbox's anonymous answers (raw 'fail' rows)."""
    monkeypatch.setattr(publish, "SANDBOX_URL", SANDBOX)
    seen = {}

    def fake_run(cmd, **kw):
        if cmd[0] == "bash":
            out = cmd[-1].split("--out '")[1].rstrip("'")
            Path(out).write_text(HEADER + SANDBOX_OK)
            seen["cmd"] = cmd[-1]
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, "https://gh/rel\n", "")

    monkeypatch.setattr(publish, "run", fake_run)
    summary = publish.post_release("v0.29.0", dry_run=False)
    assert "probes: 4/4 as expected" in summary
    assert "https://gh/rel" in summary
    assert "--out" in seen["cmd"]


def test_post_release_reports_unreadable_report(monkeypatch):
    monkeypatch.setattr(publish, "SANDBOX_URL", SANDBOX)
    monkeypatch.setattr(
        publish, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "", ""),
    )
    assert "probes: no report" in publish.post_release("v0.29.0", dry_run=False)
