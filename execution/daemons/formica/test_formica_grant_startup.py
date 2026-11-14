"""Formica startup tells an unreadable grant apart from a revoked one."""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.daemon_runtime.grant_checker import GrantChecker  # noqa: E402

_spec = importlib.util.spec_from_file_location("formica_under_test", _HERE / "formica.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["formica_under_test"] = mod
_spec.loader.exec_module(mod)


def _unreadable() -> GrantChecker:
    c = GrantChecker("formica@ateles-swarm")
    c._load_error = "NEOTOMA_BEARER_TOKEN not set"
    c._loaded = True
    return c


def _revoked() -> GrantChecker:
    c = GrantChecker("formica@ateles-swarm")
    c._grants = [GrantChecker._parse(
        {"entity_id": "e", "snapshot": {"match_sub": "formica@ateles-swarm", "status": "revoked"}}
    )]
    c._loaded = True
    return c


def test_unreadable_grant_exits_without_restore_advice(monkeypatch, caplog):
    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        mod._enforce_grant_status(_unreadable())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "could not be read" in text
    assert "revoked" not in text and "manage_grants.py" not in text


def test_revoked_grant_keeps_reconsent_text(monkeypatch, caplog):
    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        mod._enforce_grant_status(_revoked())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "revoked" in text and "manage_grants.py restore" in text
