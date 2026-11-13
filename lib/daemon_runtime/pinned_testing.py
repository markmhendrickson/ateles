"""Test support: a complete, synthetic pinned environment. Never touches a real key.

`apply_pinned_env` writes a synthetic key under ``tmp_path``, builds the same
environment `build_pinned_env` hands a daemon's children (isolated CLI home and
project root included), applies it with ``monkeypatch`` and removes every
variable the real builder removes. It returns the key path.
"""

from __future__ import annotations

import os
from pathlib import Path

from lib.daemon_runtime import pinned_cli_identity as pci


def make_synthetic_key(directory: Path, daemon: str = "piculet", iss: "str | None" = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / f"{daemon}.jwk.json"
    body = '{"kid": "synthetic-kid"' + (f', "iss": "{iss}"' if iss else "") + "}"
    key.write_text(body)
    return key


def apply_pinned_env(monkeypatch, tmp_path: Path, daemon: str = "piculet") -> Path:
    keys = tmp_path / "keys"
    key = make_synthetic_key(keys, daemon)
    rc = tmp_path / "rc"
    (rc / "dist" / "cli").mkdir(parents=True, exist_ok=True)
    (rc / "dist" / "cli" / "bootstrap.js").write_text("// synthetic runtime\n")
    built = pci.build_pinned_env(
        daemon, dict(os.environ), keys_dir=keys, rc_dir=rc, state_dir=tmp_path / "pinned-state"
    )
    for name in (*pci.BEARER_ENV_KEYS, *pci.SIGNING_DISABLE_KEYS, *pci.CLAIM_OVERRIDE_KEYS):
        monkeypatch.delenv(name, raising=False)
    for name, value in built.items():
        if os.environ.get(name) != value:
            monkeypatch.setenv(name, value)
    return key
