"""Tests for token resolution in triage_backfill_sweep (ateles#524).

The sweep must run from a bare operator/CI shell — env var absent — by falling
back to the SOPS snapshot, while the daemon's env-var path stays primary.
secrets_lib is stubbed so the test needs no real secrets.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _fresh_module(monkeypatch, sops_pairs, snapshot_exists=True):
    """Import triage_backfill_sweep with secrets_lib stubbed."""
    stub = types.ModuleType("secrets_lib")

    class _P:
        def __init__(self, exists):
            self._exists = exists

        def exists(self):
            return self._exists

        def __str__(self):
            return "/fake/neotoma.sops.enc"

    stub.enc_file = lambda name: _P(snapshot_exists)  # type: ignore[attr-defined]
    stub.DEFAULT_AGE_KEY_FILE = "/fake/age/keys.txt"  # type: ignore[attr-defined]
    stub.sops_decrypt_dotenv = lambda src: dict(sops_pairs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "secrets_lib", stub)

    sys.modules.pop("triage_backfill_sweep", None)
    return importlib.import_module("triage_backfill_sweep")


def test_env_var_is_primary(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN_PROD", "env-token")
    mod = _fresh_module(monkeypatch, {"NEOTOMA_BEARER_TOKEN_PROD": "sops-token"})
    assert mod._token() == "env-token"  # env wins, SOPS not consulted


def test_falls_back_to_sops_when_env_absent(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN_PROD", raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    mod = _fresh_module(monkeypatch, {"NEOTOMA_BEARER_TOKEN_PROD": "sops-token"})
    assert mod._token() == "sops-token"


def test_sops_plain_token_key_also_read(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN_PROD", raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    mod = _fresh_module(monkeypatch, {"NEOTOMA_BEARER_TOKEN": "sops-plain"})
    assert mod._token() == "sops-plain"


def test_empty_when_no_env_and_no_snapshot(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN_PROD", raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    mod = _fresh_module(monkeypatch, {}, snapshot_exists=False)
    assert mod._token() == ""


def test_empty_when_snapshot_lacks_token(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN_PROD", raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    mod = _fresh_module(monkeypatch, {"SOMETHING_ELSE": "x"})
    assert mod._token() == ""
