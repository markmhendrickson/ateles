"""Tests for AAuth agent identifiers (draft-10 §5.1)."""

from __future__ import annotations

import pytest

from daemon_runtime.aauth_identifier import (
    InvalidAgentIdentifier,
    build,
    is_legacy,
    is_subagent,
    local_part,
    normalize,
    subagent,
    validate,
)


def test_builds_spec_form_identifier() -> None:
    assert build("anthus", "markmhendrickson.com") == "aauth:anthus@markmhendrickson.com"


def test_normalizes_legacy_swarm_subject() -> None:
    """The pre-draft-10 form gains the scheme and a real domain."""
    assert (
        normalize("anthus@ateles-swarm", "markmhendrickson.com")
        == "aauth:anthus@markmhendrickson.com"
    )


def test_normalize_is_idempotent() -> None:
    once = normalize("anthus@ateles-swarm", "markmhendrickson.com")
    assert normalize(once) == once


def test_normalizes_scheme_missing_but_domain_present() -> None:
    assert (
        normalize("cursor@markmhendrickson.com") == "aauth:cursor@markmhendrickson.com"
    )


def test_is_legacy_detects_only_the_old_form() -> None:
    assert is_legacy("anthus@ateles-swarm")
    assert not is_legacy("aauth:anthus@markmhendrickson.com")
    assert not is_legacy("cursor@markmhendrickson.com")


@pytest.mark.parametrize(
    "bad, reason",
    [
        ("anthus@markmhendrickson.com", "scheme"),
        ("aauth:@markmhendrickson.com", "empty local"),
        ("aauth:My Agent@markmhendrickson.com", "local part"),
        ("aauth:agent@ateles-swarm", "domain"),
        ("aauth:agent@http://x.example", "domain"),
        ("aauth:agent", "local@domain"),
    ],
)
def test_rejects_invalid_identifiers(bad: str, reason: str) -> None:
    with pytest.raises(InvalidAgentIdentifier):
        validate(bad)


def test_local_part_extraction() -> None:
    assert local_part("aauth:planner.7f3c@vendor.example") == "planner.7f3c"


def test_subagent_uses_plus_delimiter() -> None:
    parent = "aauth:planner.7f3c@vendor.example"
    child = subagent(parent, "search1")
    assert child == "aauth:planner.7f3c+search1@vendor.example"
    assert is_subagent(child)
    assert not is_subagent(parent)


def test_subagent_nesting_is_rejected() -> None:
    """draft-10 §10.2: single-level depth only."""
    child = subagent("aauth:planner@vendor.example", "search1")
    with pytest.raises(InvalidAgentIdentifier, match="nesting"):
        subagent(child, "deeper")
