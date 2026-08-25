"""Tests for the private-key permission warning in aauth_signer.from_key_file."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from aauth_signer import AAuthSigner, _warn_if_key_world_readable


def _write_key(tmp_path: Path, name: str, mode: int) -> Path:
    p = tmp_path / f"{name}.jwk.json"
    # A syntactically-valid stub key; the loader tolerates a missing real key.
    p.write_text(json.dumps({"sub": f"{name}@ateles-swarm"}))
    os.chmod(p, mode)
    return p


def test_world_readable_key_warns(tmp_path, caplog):
    key = _write_key(tmp_path, "monedula", 0o644)
    with caplog.at_level(logging.WARNING):
        _warn_if_key_world_readable("monedula", key)
    assert any("group/other-readable" in r.message for r in caplog.records)
    assert any("chmod 600" in r.message for r in caplog.records)


def test_group_readable_key_warns(tmp_path, caplog):
    key = _write_key(tmp_path, "monedula", 0o640)
    with caplog.at_level(logging.WARNING):
        _warn_if_key_world_readable("monedula", key)
    assert any("group/other-readable" in r.message for r in caplog.records)


def test_owner_only_key_is_silent(tmp_path, caplog):
    key = _write_key(tmp_path, "monedula", 0o600)
    with caplog.at_level(logging.WARNING):
        _warn_if_key_world_readable("monedula", key)
    assert not any("readable" in r.message for r in caplog.records)


def test_missing_key_does_not_raise(tmp_path):
    # stat() failure must never be the reason a signer fails to load.
    _warn_if_key_world_readable("monedula", tmp_path / "does_not_exist.jwk.json")


def test_from_key_file_still_loads_a_loose_key(tmp_path, caplog):
    # The check is a warning, not a gate: a loose-permission key still loads.
    _write_key(tmp_path, "monedula", 0o644)
    signer = AAuthSigner.from_key_file("monedula", keys_dir=tmp_path)
    assert signer.sub == "monedula@ateles-swarm"
    assert any("group/other-readable" in r.message for r in caplog.records)
