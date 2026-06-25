"""Tests for the pluggable secret backends (W4)."""

from __future__ import annotations

import pytest

from ateles import secrets
from ateles.secrets import EnvBackend, SecretBackend, get_backend, resolve


def test_env_backend_reads_from_mapping():
    be = EnvBackend(environ={"FOO": "bar", "EMPTY": ""})
    assert be.get("FOO") == "bar"
    assert be.get("EMPTY") is None   # empty string normalises to None
    assert be.get("MISSING") is None
    assert be.available() is True


def test_env_backend_satisfies_protocol():
    assert isinstance(EnvBackend(), SecretBackend)


def test_get_backend_defaults_to_env():
    assert get_backend().name == "env"


def test_get_backend_honours_env_selector(monkeypatch):
    monkeypatch.setenv("ATELES_SECRET_BACKEND", "env")
    assert get_backend().name == "env"


def test_get_backend_unknown_raises():
    with pytest.raises(ValueError):
        get_backend("does-not-exist")


def test_resolve_uses_supplied_backend():
    assert resolve("FOO", backend=EnvBackend(environ={"FOO": "baz"})) == "baz"


def test_env_is_a_registered_backend():
    assert "env" in secrets.available_backends()
