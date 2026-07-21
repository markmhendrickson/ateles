"""
Effect tests for payment_profile.py — ateles#243 (NEOTOMA_BASE_URL fail-fast).

Covers the two distinct cases that must NOT be conflated:
  1. Neotoma reachable, zero profiles configured -> legitimate env-var fallback.
  2. Neotoma unreachable -> raise NeotomaUnavailableError, do NOT silently
     slide into the env-var fallback.

Run with: pytest execution/daemons/monedula/handlers/test_payment_profile.py -v
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HANDLERS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HANDLERS_DIR.parent.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_HANDLERS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os as _os  # noqa: E402

_os.environ.setdefault("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")

import payment_profile  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# ── load_profiles_from_neotoma: connection failure raises ──────────────────


class TestLoadProfilesFromNeotomaRaisesOnConnectionFailure:
    def test_url_error_raises_neotoma_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")

        def _boom(*args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)

        with pytest.raises(payment_profile.NeotomaUnavailableError):
            payment_profile.load_profiles_from_neotoma()

    def test_success_with_zero_profiles_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda *a, **k: _FakeResponse({"entities": []})
        )
        assert payment_profile.load_profiles_from_neotoma() == []


# ── load_profiles_with_neotoma_fallback: the two cases must not be conflated ─


class TestLoadProfilesWithNeotomaFallbackDistinguishesUnreachableFromEmpty:
    def test_neotoma_reachable_zero_profiles_falls_back_to_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(payment_profile, "load_profiles_from_neotoma", lambda: [])
        env_fallback_mock = MagicMock(return_value=["env-profile"])
        monkeypatch.setattr(payment_profile, "load_profiles", env_fallback_mock)

        result = payment_profile.load_profiles_with_neotoma_fallback()

        assert result == ["env-profile"]
        env_fallback_mock.assert_called_once()

    def test_neotoma_unreachable_raises_and_does_not_fall_back_to_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> list:
            raise payment_profile.NeotomaUnavailableError("connection refused")

        monkeypatch.setattr(payment_profile, "load_profiles_from_neotoma", _boom)
        env_fallback_mock = MagicMock(return_value=["env-profile"])
        monkeypatch.setattr(payment_profile, "load_profiles", env_fallback_mock)

        with pytest.raises(payment_profile.NeotomaUnavailableError):
            payment_profile.load_profiles_with_neotoma_fallback()

        env_fallback_mock.assert_not_called()

    def test_neotoma_profiles_found_skips_env_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            payment_profile, "load_profiles_from_neotoma", lambda: ["neotoma-profile"]
        )
        env_fallback_mock = MagicMock(return_value=["env-profile"])
        monkeypatch.setattr(payment_profile, "load_profiles", env_fallback_mock)

        result = payment_profile.load_profiles_with_neotoma_fallback()

        assert result == ["neotoma-profile"]
        env_fallback_mock.assert_not_called()
