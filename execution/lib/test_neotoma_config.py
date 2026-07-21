"""
Unit tests for execution/lib/neotoma_config.py — ateles#243.

Run with: pytest execution/lib/test_neotoma_config.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _LIB_DIR.parent.parent
for _p in (str(_REPO_ROOT), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from neotoma_config import NeotomaConfigError, resolve_neotoma_base_url  # noqa: E402


def test_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    with pytest.raises(NeotomaConfigError):
        resolve_neotoma_base_url()


def test_raises_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEOTOMA_BASE_URL", "   ")
    with pytest.raises(NeotomaConfigError):
        resolve_neotoma_base_url()


def test_error_message_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    with pytest.raises(NeotomaConfigError, match="NEOTOMA_BASE_URL"):
        resolve_neotoma_base_url()


def test_returns_exact_configured_value_no_default_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
    assert resolve_neotoma_base_url() == "https://neotoma.example.com:9180"


def test_does_not_strip_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    # Trailing-slash normalization is each call site's responsibility, not
    # this helper's — verify it passes the value through unmodified.
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://example.com:9180/")
    assert resolve_neotoma_base_url() == "http://example.com:9180/"


def test_strips_surrounding_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEOTOMA_BASE_URL", "  http://example.com:9180  ")
    assert resolve_neotoma_base_url() == "http://example.com:9180"


def test_never_returns_localhost_3180_default() -> None:
    import inspect

    import neotoma_config

    source = inspect.getsource(neotoma_config.resolve_neotoma_base_url)
    assert "3180" not in source
    assert "9180" not in source
    assert "localhost" not in source
