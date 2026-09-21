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


@pytest.mark.asyncio
async def test_agent_in_command_logins_still_cannot_approve_a_merge(monkeypatch):
    """APPROVAL stays the human act.

    This is the test that must never be "fixed" by widening the approval
    guard. An agent named in APIS_COMMAND_LOGINS may drive commands; it may
    NOT have its `pr_review` approved event honoured, because approving a
    merge is the human act the gate exists to require.

    Exercised behaviourally through `_handle_pr_review` rather than by
    matching source text: a substring assertion over an untouched region goes
    red on an innocent reformat and green on a real regression that happens to
    keep the same characters.
    """
    # A NON-bot login, deliberately: `ateles-agent` is already caught by
    # `_is_bot_author` and returns before the approval guard is reached, so a
    # test using it can never exercise the guard. (That double block is real
    # defence-in-depth, and is asserted separately below.)
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="castor-agent")
    assert "castor-agent" in sd._COMMAND_LOGINS  # admitted for COMMANDS
    assert not sd._is_bot_author("castor-agent")  # so the guard IS reached

    dispatcher = sd.SwarmDispatcher.__new__(sd.SwarmDispatcher)

    reached: list[str] = []

    async def _spy(*a, **k):
        reached.append("_approve_and_maybe_merge")

    # The single call the approval path makes once its guard admits the
    # reviewer. Stubbing the WRONG name is how this test silently became
    # decoration on a first pass: it passed even with the approval guard
    # widened, because nothing it watched was ever reached.
    assert hasattr(sd.SwarmDispatcher, "_approve_and_maybe_merge")
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_approve_and_maybe_merge", _spy, raising=True
    )

    trigger = sd.SwarmTrigger(
        kind="pr_review",
        repository="markmhendrickson/ateles",
        number=1,
        title="t",
        body="",
        author="castor-agent",
        html_url="https://github.com/markmhendrickson/ateles/pull/1",
        delivery_id="test-delivery",
        action="submitted",
        review_author="castor-agent",
        review_state="approved",
    )

    await dispatcher._handle_pr_review(trigger)

    assert reached == [], (
        "an agent login admitted for COMMANDS must not have its pr_review "
        f"approval honoured; reached {reached}"
    )


def test_the_agent_identity_is_also_caught_as_a_bot(monkeypatch):
    """Defence in depth, asserted so it is not removed by accident.

    `ateles-agent` is blocked from the approval path TWICE: once by
    `_is_bot_author` (self-trigger prevention) and once by the approval guard.
    Naming it in APIS_COMMAND_LOGINS changes neither.
    """
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="ateles-agent")
    assert sd._is_bot_author("ateles-agent")
    assert "ateles-agent" in sd._COMMAND_LOGINS
