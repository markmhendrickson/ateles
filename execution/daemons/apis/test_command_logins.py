"""The command guard is widenable; the approval guard is not.

ateles#1114 / CLAUDE.md 2026-09-11: a session holds standing authorization to
comment `/confirm-gates-clear` where a PR is blocked only by swarm MECHANICS.
That authorization was never exercisable — the guard accepted only the
operator's login, so every such comment was declined.

These tests pin the split that fixes it. They go RED if `_COMMAND_LOGINS`
collapses back onto `_OPERATOR_LOGIN`, and RED if the approval path is ever
widened the same way.
"""

import importlib
import os

import pytest


def _reload(monkeypatch, **env):
    for k in ("APIS_OPERATOR_LOGIN", "APIS_COMMAND_LOGINS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import execution.daemons.apis.swarm_dispatch as sd

    return importlib.reload(sd)


def test_unset_leaves_operator_only(monkeypatch):
    """No APIS_COMMAND_LOGINS means exactly the previous behaviour."""
    sd = _reload(monkeypatch)
    assert sd._COMMAND_LOGINS == frozenset({sd._OPERATOR_LOGIN.lower()})


def test_agent_login_is_admitted_when_named(monkeypatch):
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="ateles-agent")
    assert "ateles-agent" in sd._COMMAND_LOGINS
    assert sd._OPERATOR_LOGIN.lower() in sd._COMMAND_LOGINS


def test_operator_is_always_included(monkeypatch):
    """Naming other logins must never displace the operator."""
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="someone-else")
    assert sd._OPERATOR_LOGIN.lower() in sd._COMMAND_LOGINS


def test_a_login_not_named_is_still_refused(monkeypatch):
    """The guard is an allowlist, not an open door — the regression that matters."""
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="ateles-agent")
    assert "drive-by-contributor" not in sd._COMMAND_LOGINS


def test_whitespace_and_case_are_normalised(monkeypatch):
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS=" Ateles-Agent , , castor-agent ")
    assert "ateles-agent" in sd._COMMAND_LOGINS
    assert "castor-agent" in sd._COMMAND_LOGINS
    assert "" not in sd._COMMAND_LOGINS


def test_approval_guard_is_not_widened(monkeypatch):
    """APPROVAL stays the human act. This is the test that must never be
    'fixed' by widening it: a pr_review approved event is honoured only from
    _OPERATOR_LOGIN, so an agent cannot approve its own merge."""
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="ateles-agent")
    src = (
        __import__("pathlib")
        .Path(sd.__file__)
        .read_text()
    )
    assert "reviewer.lower() != _OPERATOR_LOGIN.lower()" in src, (
        "the pr_review approval guard must compare against _OPERATOR_LOGIN "
        "alone; widening it would let an agent approve its own merge"
    )
    assert "reviewer.lower() not in _COMMAND_LOGINS" not in src
