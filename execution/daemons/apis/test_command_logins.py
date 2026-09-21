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


@pytest.mark.asyncio
async def test_a_named_non_bot_login_actually_reaches_the_command(monkeypatch):
    """Planted positive: the fix must be REACHABLE by the principal it names.

    qa's blocking finding on PR #1131 was that nothing proved the widening
    could ever fire. Guard 0 (`_is_bot_author`) short-circuits before the
    command guard, so naming a bot login in APIS_COMMAND_LOGINS has no effect
    at all — the documented enablement could not exercise the fix it enabled.

    This drives `_handle_issue_comment` end to end and asserts the command
    handler is REACHED for a named non-bot login. It goes red if Guard 0 is
    ever widened to swallow the command principal, or if the command guard
    stops consulting `_COMMAND_LOGINS`.
    """
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="castor-agent")
    assert not sd._is_bot_author("castor-agent"), (
        "this test is meaningless if its principal is caught by Guard 0"
    )

    dispatcher = sd.SwarmDispatcher.__new__(sd.SwarmDispatcher)
    reached: list[str] = []

    async def _spy(*a, **k):
        reached.append("swarm_run")

    assert hasattr(sd.SwarmDispatcher, "_handle_swarm_run")
    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_swarm_run", _spy, raising=True)

    trigger = sd.SwarmTrigger(
        kind="issue_comment",
        repository="markmhendrickson/ateles",
        number=1,
        title="t",
        body="",
        author="castor-agent",
        html_url="https://github.com/markmhendrickson/ateles/issues/1",
        delivery_id="test-delivery",
        action="created",
        comment_id=1,
        comment_author="castor-agent",
        comment_body="/swarm-run",
    )

    await dispatcher._handle_issue_comment(trigger)

    assert reached == ["swarm_run"], (
        "a login named in APIS_COMMAND_LOGINS must reach the command handler; "
        f"reached {reached}"
    )


def test_a_bot_login_named_here_is_still_dropped_by_guard_0(monkeypatch):
    """The converse, asserted so the carve-out is never added by accident.

    Naming `ateles-agent` changes nothing: Guard 0 drops it first. This is
    the self-trigger defence (neotoma#1686) and widening it for commands
    would reopen the loop it closes.
    """
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="ateles-agent")
    assert "ateles-agent" in sd._COMMAND_LOGINS  # admitted by THIS guard
    assert sd._is_bot_author("ateles-agent")  # and dropped by the earlier one
