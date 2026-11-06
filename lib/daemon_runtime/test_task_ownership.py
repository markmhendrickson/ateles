"""The session-ownership recognizer: what it must and must not match."""

from __future__ import annotations

import pytest

from lib.daemon_runtime.task_ownership import (
    SENTINEL_ASSIGNEES,
    names_a_session,
    session_owned,
)

SESSIONS = [
    "Codex session billing",
    "Codex session",
    "reports session (Claude Code)",
    "session subagent (web session)",
    "session-subagent",
    "session-root",
    "subagent",
    "claude:acme-session-subagent",
    "Claude session 'reports' (local_ab12cd34)",
    "codex-subagent:parents_meeting",
    "codex_subagent:lens_review",
    "codex:/root/audit_reviewer",
    "codex",
    "operator_session",
    "claude-code-session",
    "ateles session",
    "coordinator: Claude Code platform session (worktree x); operator ruling relayed by another session",
    "Codex coordinator0a1b2c3d-1111-2222-3333-444444444444",
    "codex:0a1b2c3d-1111-2222-3333-444444444444",
    "Foundation coordinator0a1b2c3d-1111-2222-3333-444444444444",
    "Coordinator Codex 0a1b2c3d-1111",
    "Root0a1b2c3d coordinator; independent reviewer",
    "Codex street coordinator chat0a1b2c3d-1111-2222-3333-444444444444",
    "Codex reliability chat 0a1b2c3d-1111",
    "root0a1b2c3d",
    "root 0a1b2c3d",
]

NOT_SESSIONS = [
    "",
    None,
    42,
    "root",
    "swarm",
    "payments",
    "planner",
    "ateles",
    "pavo",
    "operator",
    "mark",
    "Jane Doe",
    "meeting: acme",
    "pavo independent SOURCE lens",
    "Lanius source assessment; root coordinates recording",
    "falco independent review; root verifies",
    "operator confirms identity and assigns license; Codex provisions and verifies",
    "Operator rehearsal now; later reconcile through the governed path",
    "operator, Jane Doe, John Roe",
    "swarm (draft); operator (review and approval)",
]


@pytest.mark.parametrize("value", SESSIONS)
def test_names_a_session(value):
    assert names_a_session(value) is True


@pytest.mark.parametrize("value", NOT_SESSIONS)
def test_does_not_name_a_session(value):
    assert names_a_session(value) is False


def test_session_owned_assignee_decides_alone():
    assert session_owned("Codex session billing", None)
    # An assignee naming someone else: owner is not consulted.
    assert not session_owned("Jane Doe", "Codex session")
    assert not session_owned("pavo", "ateles session")


def test_session_owned_falls_back_to_owner_when_assignee_absent():
    assert session_owned(None, "Codex session")
    assert session_owned("", "Codex session")
    for sentinel in SENTINEL_ASSIGNEES:
        assert session_owned(sentinel, "Codex session")
    assert not session_owned(None, "meeting: acme")
    assert not session_owned(None, None)
