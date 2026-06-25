"""Tests for ``ateles doctor`` diagnostics (W1)."""

from __future__ import annotations

import sys
import urllib.error

from ateles.config import AtelesConfig
from ateles.doctor import (
    Check,
    check_config,
    check_neotoma,
    check_python,
    next_rung,
    render,
    run_checks,
)


def _clean_cfg() -> AtelesConfig:
    return AtelesConfig(values={
        "operator_domain": "example.com",
        "operator_name": "Jane",
        "operator_email": "jane@example.com",
        "neotoma_base_url": "https://neotoma.example.com",
        "neotoma_bearer_token": "tok",
    })


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_check_python_matches_interpreter():
    assert check_python().ok == (sys.version_info[:2] >= (3, 13))


def test_check_config_clean_is_single_ok_check():
    checks = check_config(_clean_cfg())
    assert len(checks) == 1 and checks[0].ok


def test_check_config_reports_each_problem():
    checks = check_config(AtelesConfig(values={}))
    assert checks and all(not c.ok for c in checks)


def test_check_neotoma_reachable():
    cfg = AtelesConfig(values={"neotoma_base_url": "https://n.example"})
    assert check_neotoma(cfg, opener=lambda url, timeout: _Resp()).ok


def test_check_neotoma_http_error_counts_as_reachable():
    def opener(url, timeout):
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)

    cfg = AtelesConfig(values={"neotoma_base_url": "https://n.example"})
    assert check_neotoma(cfg, opener=opener).ok  # server responded => up


def test_check_neotoma_connection_failure_is_unreachable():
    def opener(url, timeout):
        raise urllib.error.URLError("connection refused")

    cfg = AtelesConfig(values={"neotoma_base_url": "https://n.example"})
    assert not check_neotoma(cfg, opener=opener).ok


def test_check_neotoma_without_url_fails_without_network():
    # No url -> returns early, never invokes the opener.
    assert not check_neotoma(AtelesConfig(values={})).ok


def test_next_rung_and_render_point_at_first_failure():
    checks = [Check("a", True, "ok"), Check("b", False, "bad")]
    assert next_rung(checks) == "b"
    assert "Next rung: fix 'b'" in render(checks)


def test_next_rung_none_and_ready_message_when_all_ok():
    checks = [Check("a", True, "ok")]
    assert next_rung(checks) is None
    assert "ready to `ateles provision`" in render(checks)


def test_run_checks_can_skip_network():
    names = [c.name for c in run_checks(_clean_cfg(), check_network=False)]
    assert "neotoma" not in names
    assert "python" in names
    assert "config" in names
