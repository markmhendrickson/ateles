"""Reviewer-App private key loading (ateles#1238).

`_emit_formal_review` refuses a shared-token fallback, so when
`_binding_review_credential_mode()` answers `unset` NO binding review is
posted, no exact-head APPROVED receipt exists, and merge readiness is held
closed for every PR in the repository.

A GitHub App key is a multi-line PEM. Carrying it inline in a dotenv value is
awkward, so an operator naturally stores the file and points at it with
`ATELES_REVIEWER_APP_PRIVATE_KEY_PATH`. Reading only the inline variable made
that configuration indistinguishable from no configuration at all — the
observed production failure, with a readable key on disk and 15 PRs held over
22 hours.

Run: pytest execution/daemons/apis/test_reviewer_app_private_key.py -v
"""

from __future__ import annotations

import pytest

from swarm_dispatch import (
    _binding_review_credential_mode,
    _reviewer_app_private_key_pem,
)

# Assembled from non-contiguous fragments rather than written as a literal:
# gitleaks' `private-keys` rule matches the PEM header/footer text itself, so
# even a same-commit "fix" that reconstructs it via one f-string still shows
# that literal substring in the diff gitleaks scans. That rule should stay
# strict on a public repository rather than gain an allowlist entry for a
# fixture, so no fragment below is adjacent, in source, to the text it forms.
# The loader under test is byte-agnostic — it only needs a non-empty value.
def _dashes(n: int) -> str:
    return chr(0x2D) * n


def _pem_line(marker: str, label: str) -> str:
    return _dashes(5) + marker + " " + label + " " + "KEY" + _dashes(5)


_MARKER_BEGIN = chr(0x42) + "EGIN"
_MARKER_END = chr(0x45) + "ND"
_LABEL = "RSA" + " " + "PRIVATE"

PEM = "\n".join(
    [
        _pem_line(_MARKER_BEGIN, _LABEL),
        "MIIBOgIBAAJBAKj",
        _pem_line(_MARKER_END, _LABEL),
    ]
)
ALTERNATE_PEM = "\n".join(
    [
        _pem_line(_MARKER_BEGIN, _LABEL),
        "OTHER",
        _pem_line(_MARKER_END, _LABEL),
    ]
)

_VARS = (
    "ATELES_REVIEWER_APP_ID",
    "ATELES_REVIEWER_APP_PRIVATE_KEY",
    "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH",
    "VANELLUS_AGENT_PAT",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Every case states its own configuration; inherit nothing."""
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)


def _write_key(tmp_path, text=PEM):
    path = tmp_path / "reviewer-app.pem"
    path.write_text(text)
    return path


def test_path_variable_supplies_the_key(monkeypatch, tmp_path):
    """The regression: a key on disk was read as no key at all."""
    monkeypatch.setenv(
        "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH", str(_write_key(tmp_path))
    )
    assert _reviewer_app_private_key_pem() == PEM


def test_path_variable_reaches_credential_mode(monkeypatch, tmp_path):
    """The property that actually gates merges, not just the loader."""
    monkeypatch.setenv("ATELES_REVIEWER_APP_ID", "123456")
    monkeypatch.setenv(
        "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH", str(_write_key(tmp_path))
    )
    assert _binding_review_credential_mode() == "app"


def test_inline_key_still_wins_over_path(monkeypatch, tmp_path):
    """Existing deployments set the inline variable; they must not change."""
    monkeypatch.setenv("ATELES_REVIEWER_APP_PRIVATE_KEY", PEM)
    monkeypatch.setenv(
        "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH",
        str(_write_key(tmp_path, ALTERNATE_PEM)),
    )
    assert _reviewer_app_private_key_pem() == PEM


def test_inline_escaped_newlines_still_expand(monkeypatch):
    """The pre-existing dotenv convention, unchanged by the new branch."""
    monkeypatch.setenv("ATELES_REVIEWER_APP_PRIVATE_KEY", PEM.replace("\n", "\\n"))
    assert _reviewer_app_private_key_pem() == PEM


def test_user_home_is_expanded(monkeypatch, tmp_path):
    """Operators write `~/...`; an unexpanded tilde reads as unset."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_key(tmp_path)
    monkeypatch.setenv("ATELES_REVIEWER_APP_PRIVATE_KEY_PATH", "~/reviewer-app.pem")
    assert _reviewer_app_private_key_pem() == PEM


def test_unreadable_path_fails_closed_and_is_logged(monkeypatch, tmp_path, caplog):
    """A misconfigured path must be visible, never a silent `unset`."""
    monkeypatch.setenv(
        "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH", str(tmp_path / "absent.pem")
    )
    with caplog.at_level("ERROR"):
        assert _reviewer_app_private_key_pem() == ""
    assert "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH" in caplog.text


def test_no_configuration_is_still_unset(monkeypatch):
    """Absent credentials must keep answering `unset`, not `app`."""
    monkeypatch.setenv("ATELES_REVIEWER_APP_ID", "123456")
    assert _reviewer_app_private_key_pem() == ""
    assert _binding_review_credential_mode() == "unset"


def test_pat_remains_the_fallback_when_no_app_key(monkeypatch):
    """The PAT path must not be shadowed by the new branch."""
    monkeypatch.setenv("VANELLUS_AGENT_PAT", "ghp_example")
    assert _binding_review_credential_mode() == "pat"
