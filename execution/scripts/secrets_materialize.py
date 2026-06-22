#!/usr/bin/env python3
"""Materialize: decrypt SOPS snapshots into a local .env. OFFLINE.

Direction:  secrets/<name>.sops.env  ──▶  ~/.config/neotoma/.env

Uses the local age key (bootstrapped once from 1Password) — no live 1Password
session required. Safe to run on every daemon start. Unmanaged vars already in
the target .env are preserved. Secret values are never printed.

Usage:
    python execution/scripts/secrets_materialize.py [file_name ...]
    python execution/scripts/secrets_materialize.py --env-file /path/to/.env
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_lib as sl  # noqa: E402

DEFAULT_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"


def main(argv: list[str]) -> int:
    override_env_file: Path | None = None
    names: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--env-file":
            override_env_file = Path(argv[i + 1]).expanduser()
            i += 2
        else:
            names.append(argv[i])
            i += 1

    manifest = sl.load_manifest()
    files = manifest.get("files", {})
    if not names:
        names = list(files.keys())

    rc = 0
    for name in names:
        # Destination precedence: --env-file override > manifest block `target`
        # > DEFAULT_ENV_FILE. Lets one run materialize each consumer to its own
        # .env (e.g. neotoma → ~/.config/neotoma/.env, openclaw → repo .env).
        target = files.get(name, {}).get("target")
        env_file = override_env_file or (
            Path(target).expanduser() if target else DEFAULT_ENV_FILE
        )
        src = sl.enc_file(name)
        if not src.exists():
            print(f"[{name}] no snapshot at {src.relative_to(sl.SECRETS_BASE)} — skipped")
            continue
        try:
            values = sl.sops_decrypt_dotenv(src)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] decrypt FAILED ({exc})")
            rc = 1
            continue
        changed = sl.merge_into_env_file(env_file, values)
        print(f"[{name}] → {env_file}: materialized {len(values)} var(s); "
              f"{len(changed)} changed")

    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
