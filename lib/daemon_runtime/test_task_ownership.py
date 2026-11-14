"""The session-ownership recognizer: what it must and must not match."""

from __future__ import annotations

import pytest

from lib.daemon_runtime.task_ownership import (
    SENTINEL_ASSIGNEES,
    SESSION_WORDS,
    canonical_assignee,
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
    "session",
    "session-email-review",
    "harness subagent (opus)",
    "Codex root",
    "codex_resume_audit",
    "Foundation coordinator 0a1b2c3d-1111",
    "theodore session (Claude Code)",
    "local_117abff0-4498-445c-858d-2c9da04b86a1",
    "constara (Claude local_77de7679)",
    "Logik client lead — Claude Code (from 2026-11-12, under ateles inbox "
    "local_117abff0-4498-445c-858d-2c9da04b86a1); previously Codex 01a186bf",
]

# A session verdict silences the dispatcher's only escalation, so a person, a
# vendor, a job title or prose holding one marker word must stay unroutable.
SINGLE_MARKER_WORD_IS_NOT_A_SESSION = [
    "Claude Martin",
    "claude",
    "Marcus Chat",
    "chat",
    "Customer chat support",
    "Chat support vendor",
    "pavo chat",
    "Session Bank",
    "operator session notes",
    "a session took this on",
    "Marketing coordinator",
    "coordinator",
    "Existing platform coordinator; maintenance queue",
    "operator (separate session)",
    "operator (not performable in-session)",
    # Ordinary words spelled in a-f are not ids.
    "coordinator facade",
    "chat decade",
    "root facade",
    # `local` is a session marker only with an id.
    "local",
    "local facade",
    "local clinic",
    "Claude local office",
    "local 2026",
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


@pytest.mark.parametrize("value", SESSIONS)
def test_every_accepted_session_contains_a_search_word(value):
    # A server-side `contains` narrowing on SESSION_WORDS must never miss a
    # value the recognizer accepts.
    assert names_a_session(value) is True
    assert any(word in value.lower() for word in SESSION_WORDS)


def test_session_words_are_lowercase_sorted_and_unique():
    assert list(SESSION_WORDS) == sorted(set(SESSION_WORDS))
    assert all(w == w.lower() for w in SESSION_WORDS)
    assert {"session", "subagent", "codex", "root"} <= set(SESSION_WORDS)


@pytest.mark.parametrize("value", NOT_SESSIONS)
def test_does_not_name_a_session(value):
    assert names_a_session(value) is False


@pytest.mark.parametrize("value", SINGLE_MARKER_WORD_IS_NOT_A_SESSION)
def test_one_marker_word_alone_does_not_name_a_session(value):
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


@pytest.mark.parametrize(
    "dispatcher",
    ["apis", "APIS", " Apis ", "apis@ateles", "Apis@ateles-swarm", "@ateles-swarm"],
)
def test_session_owned_reads_the_dispatcher_as_no_assignee(dispatcher):
    assert session_owned(dispatcher, "Codex session")
    assert not session_owned(dispatcher, "Jane Doe")
    assert not session_owned(dispatcher, None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("apis@ateles-swarm@ateles-swarm", "apis"),
        ("apis@ateles@ateles-swarm", "apis"),
        ("cicada @ateles-swarm", "cicada"),
        ("unassigned@ateles-swarm", None),
        ("None@ateles", None),
        ("@ateles@ateles-swarm", None),
        ("Codex session@ateles", "codex session"),
    ],
)
def test_canonical_assignee_reduces_all_the_way_in_one_pass(value, expected):
    # The dispatcher reduces and the router reduces again, while the delivery
    # check reduces once. A second pass must change nothing, or they disagree.
    assert canonical_assignee(value) == expected
    assert canonical_assignee(canonical_assignee(value)) == expected


@pytest.mark.parametrize(
    "value", [7, ["Codex session"], {"name": "Codex session"}, True]
)
def test_non_string_assignee_counts_as_absent(value):
    assert canonical_assignee(value) is None
    assert session_owned(value, "Codex session")
    assert not session_owned(value, None)


import pytest as _pytest_lead


@_pytest_lead.mark.parametrize("value", [
    "claude-theodore-lead", "codex-recordings-desk", "Claude finances lead",
    "claude_ateles_lead",
])
def test_harness_marker_with_lead_or_desk_names_a_session(value):
    assert names_a_session(value)


@_pytest_lead.mark.parametrize("value", [
    "Claude", "Claude Martin", "Finance lead", "desk", "Lead Claude", "Claude Leadbetter",
])
def test_marker_or_role_word_alone_names_no_session(value):
    assert not names_a_session(value)
