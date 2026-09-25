"""Round-trip tests for secrets_keys.py (encrypted agent-key backup).

Uses a throwaway age key and a synthetic key file; never touches real keys.
Skipped when sops or age-keygen is not installed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_keys as sk  # noqa: E402
import secrets_lib as sl  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (shutil.which("sops") and shutil.which("age-keygen")),
    reason="sops and age-keygen are required",
)


@pytest.fixture()
def private_repo(tmp_path, monkeypatch):
    agekey = tmp_path / "age.key"
    subprocess.run(["age-keygen", "-o", str(agekey)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(agekey)], check=True, capture_output=True, text=True
    ).stdout.strip()
    base = tmp_path / "ateles-private"
    keys = base / "keys"
    keys.mkdir(parents=True)
    (base / ".sops.yaml").write_text(
        "creation_rules:\n"
        "  - path_regex: keys/encrypted/.*\\.sops\\.json$\n"
        f"    age: {recipient}\n"
    )
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(agekey))
    monkeypatch.setattr(sl, "SECRETS_BASE", base)
    monkeypatch.setattr(sl, "KEYS_DIR", keys)
    monkeypatch.setattr(sl, "ENC_KEYS_DIR", keys / "encrypted")
    return keys


def _write_key(keys: Path, name: str) -> bytes:
    # Synthetic, non-functional values: the test is about bytes, not crypto.
    data = (json.dumps({"kty": "EC", "crv": "P-256", "x": "AA", "y": "BB", "d": "CC"},
                       indent=2) + "\n").encode()
    (keys / name).write_bytes(data)
    return data


def test_backup_then_restore_is_byte_exact(private_repo, capsys):
    original = _write_key(private_repo, "demo.jwk.json")
    assert sk.backup([]) == 0
    enc = private_repo / "encrypted" / "demo.jwk.sops.json"
    assert enc.exists()
    assert b'"d": "CC"' not in enc.read_bytes()  # plaintext is not in the ciphertext

    (private_repo / "demo.jwk.json").unlink()
    assert sk.restore([], force=False) == 0
    restored = private_repo / "demo.jwk.json"
    assert hashlib.sha256(restored.read_bytes()).digest() == hashlib.sha256(original).digest()
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert "CC" not in capsys.readouterr().out  # nothing prints key material


def test_verify_detects_mismatch(private_repo):
    _write_key(private_repo, "demo.json")
    assert sk.backup(["demo"]) == 0
    assert sk.verify([]) == 0
    (private_repo / "demo.json").write_bytes(b"{}\n")
    assert sk.verify([]) == 1


def test_restore_refuses_to_overwrite_without_force(private_repo):
    _write_key(private_repo, "demo.jwk.json")
    assert sk.backup([]) == 0
    (private_repo / "demo.jwk.json").write_bytes(b"{}\n")
    sk.restore([], force=False)
    assert (private_repo / "demo.jwk.json").read_bytes() == b"{}\n"


def test_backup_does_not_rewrite_unchanged_ciphertext(private_repo):
    _write_key(private_repo, "demo.jwk.json")
    assert sk.backup([]) == 0
    enc = private_repo / "encrypted" / "demo.jwk.sops.json"
    first = enc.read_bytes()
    assert sk.backup([]) == 0
    assert enc.read_bytes() == first


def test_name_mapping_round_trips():
    for name in ("apus.jwk.json", "neotoma_agent.json"):
        assert sl.plain_key_file(sl.enc_key_file(sl.KEYS_DIR / name)).name == name
