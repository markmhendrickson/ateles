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


# ── migration gating ─────────────────────────────────────────────────────────


def test_wire_form_defaults_to_legacy_subject(monkeypatch) -> None:
    """Default must not change the wire: live agent_grants still match_sub
    on the legacy value, so flipping early would fail admission everywhere."""
    from daemon_runtime.aauth_identifier import normalize_for_wire

    monkeypatch.delenv("ATELES_AAUTH_SPEC_IDENTIFIERS", raising=False)
    assert normalize_for_wire("anthus@ateles-swarm") == "anthus@ateles-swarm"


def test_wire_form_uses_spec_identifier_when_enabled(monkeypatch) -> None:
    from daemon_runtime.aauth_identifier import normalize_for_wire

    monkeypatch.setenv("ATELES_AAUTH_SPEC_IDENTIFIERS", "1")
    monkeypatch.setenv("ATELES_AAUTH_AGENT_DOMAIN", "markmhendrickson.com")
    assert (
        normalize_for_wire("anthus@ateles-swarm")
        == "aauth:anthus@markmhendrickson.com"
    )


def test_agent_domain_is_env_overridable(monkeypatch) -> None:
    """A fork supplies its own provider domain without editing code."""
    from daemon_runtime.aauth_identifier import build

    monkeypatch.setenv("ATELES_AAUTH_AGENT_DOMAIN", "agent.example")
    assert build("planner") == "aauth:planner@agent.example"


def test_aauth_signer_imports_flat(tmp_path) -> None:
    """Daemon scripts put lib/daemon_runtime on sys.path and import aauth_signer.

    A package-only relative import raises ImportError at collection for
    phoenicurus-release (store_release_result.py and its tests).
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    pkg_dir = Path(__file__).resolve().parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(pkg_dir)
    result = subprocess.run(
        [sys.executable, "-c", "import aauth_signer; print(aauth_signer.AAuthSigner)"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"flat import failed:\n{result.stderr}"
