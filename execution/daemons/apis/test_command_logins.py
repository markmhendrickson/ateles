"""The command guard is widenable; the approval guard is not.

ateles#1132 / CLAUDE.md 2026-09-11: a session holds standing authorization to
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
    would reopen the loop it closes. Option B: enablement names a non-bot
    principal (`castor-agent`), not `ateles-agent`.
    """
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="ateles-agent")
    assert "ateles-agent" in sd._COMMAND_LOGINS  # admitted by THIS guard
    assert sd._is_bot_author("ateles-agent")  # and dropped by the earlier one


@pytest.mark.asyncio
async def test_login_not_in_command_logins_never_reaches_command_dispatch(monkeypatch):
    """Guard 2 behavioural negative: outside the allowlist never dispatches.

    Membership assertions alone are decoration — reverting Guard 2 to
    ``_OPERATOR_LOGIN`` while leaving ``_COMMAND_LOGINS`` intact would leave
    those green. This drives ``_handle_issue_comment`` and spies both handlers.
    """
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="castor-agent")
    assert "drive-by-contributor" not in sd._COMMAND_LOGINS

    dispatcher = sd.SwarmDispatcher.__new__(sd.SwarmDispatcher)
    reached: list[str] = []

    async def _spy_confirm(*a, **k):
        reached.append("_handle_confirm_gates_clear")

    async def _spy_swarm(*a, **k):
        reached.append("_handle_swarm_run")

    assert hasattr(sd.SwarmDispatcher, "_handle_confirm_gates_clear")
    assert hasattr(sd.SwarmDispatcher, "_handle_swarm_run")
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_handle_confirm_gates_clear", _spy_confirm, raising=True
    )
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_handle_swarm_run", _spy_swarm, raising=True
    )

    trigger = sd.SwarmTrigger(
        kind="issue_comment",
        repository="markmhendrickson/ateles",
        number=1,
        title="t",
        body="",
        author="drive-by-contributor",
        html_url="https://github.com/markmhendrickson/ateles/issues/1",
        delivery_id="test-delivery",
        action="created",
        comment_id=1,
        comment_author="drive-by-contributor",
        comment_body="/confirm-gates-clear",
    )

    await dispatcher._handle_issue_comment(trigger)

    assert reached == [], (
        "a login not in APIS_COMMAND_LOGINS must never reach command dispatch; "
        f"reached {reached}"
    )


@pytest.mark.asyncio
async def test_named_non_bot_reaches_confirm_gates_clear(monkeypatch):
    """Planted positive on the QA-named command token (/confirm-gates-clear)."""
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="castor-agent")
    assert "castor-agent" in sd._COMMAND_LOGINS
    assert not sd._is_bot_author("castor-agent")

    dispatcher = sd.SwarmDispatcher.__new__(sd.SwarmDispatcher)
    reached: list[str] = []

    async def _spy_confirm(*a, **k):
        reached.append("_handle_confirm_gates_clear")

    async def _spy_swarm(*a, **k):
        reached.append("_handle_swarm_run")

    monkeypatch.setattr(
        sd.SwarmDispatcher, "_handle_confirm_gates_clear", _spy_confirm, raising=True
    )
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_handle_swarm_run", _spy_swarm, raising=True
    )

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
        comment_body="/confirm-gates-clear",
    )

    await dispatcher._handle_issue_comment(trigger)

    assert reached == ["_handle_confirm_gates_clear"], (
        "a non-bot login named in APIS_COMMAND_LOGINS must reach "
        f"_handle_confirm_gates_clear; reached {reached}"
    )


@pytest.mark.asyncio
async def test_command_login_cannot_merge_via_the_approve_comment(monkeypatch):
    """qa's blocking finding on PR #1131, made into a test.

    `/approve` is a comment command, so before this fix it passed the SAME
    widened `_COMMAND_LOGINS` guard as the mechanics commands — and
    `_handle_approve` -> `_approve_and_maybe_merge` has no operator check of
    its own. A login admitted for pipeline mechanics could therefore trigger a
    real merge.

    Widening the `pr_review` path would have been the obvious hole; routing
    `/approve` through a widened COMMAND guard is the same hole by a different
    door. This test watches that door.
    """
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="castor-agent")
    assert "castor-agent" in sd._COMMAND_LOGINS  # admitted for MECHANICS
    assert not sd._is_bot_author("castor-agent")  # so Guard 0 is not the reason

    dispatcher = sd.SwarmDispatcher.__new__(sd.SwarmDispatcher)
    reached: list[str] = []

    async def _spy(*a, **k):
        reached.append("approve_and_maybe_merge")

    monkeypatch.setattr(
        sd.SwarmDispatcher, "_approve_and_maybe_merge", _spy, raising=True
    )

    trigger = sd.SwarmTrigger(
        kind="issue_comment",
        repository="markmhendrickson/ateles",
        number=1,
        title="t",
        body="",
        author="castor-agent",
        html_url="https://github.com/markmhendrickson/ateles/pull/1",
        delivery_id="test-delivery",
        action="created",
        comment_id=1,
        comment_author="castor-agent",
        comment_body="/approve",
    )

    await dispatcher._handle_issue_comment(trigger)

    assert reached == [], (
        "a login admitted for MECHANICS commands must not reach a merge via "
        f"/approve; reached {reached}"
    )


@pytest.mark.asyncio
async def test_reject_and_hold_are_also_operator_only(monkeypatch):
    """The siblings of /approve, asserted so the split is not half-applied.

    qa flagged /reject and /hold reachability as non-blocking. They resolve a
    blocking checkpoint, so they carry the same verdict authority as /approve
    even though they do not merge.
    """
    sd = _reload(monkeypatch, APIS_COMMAND_LOGINS="castor-agent")
    dispatcher = sd.SwarmDispatcher.__new__(sd.SwarmDispatcher)

    for cmd, attr in (("/reject", "_handle_reject"), ("/hold", "_handle_hold")):
        if not hasattr(sd.SwarmDispatcher, attr):
            continue
        reached: list[str] = []

        async def _spy(*a, _n=attr, **k):
            reached.append(_n)

        monkeypatch.setattr(sd.SwarmDispatcher, attr, _spy, raising=True)
        trigger = sd.SwarmTrigger(
            kind="issue_comment",
            repository="markmhendrickson/ateles",
            number=1,
            title="t",
            body="",
            author="castor-agent",
            html_url="https://github.com/markmhendrickson/ateles/pull/1",
            delivery_id="d",
            action="created",
            comment_id=1,
            comment_author="castor-agent",
            comment_body=cmd,
        )
        await dispatcher._handle_issue_comment(trigger)
        assert reached == [], f"{cmd} must stay operator-only; reached {reached}"
