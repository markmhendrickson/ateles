#!/usr/bin/env python3
"""Publish: read canonical secrets from 1Password, write encrypted snapshots.

Direction:  1Password (canonical)  ──▶  secrets/<name>.sops.env (committed)

Run this when a secret value changes. Requires a live 1Password session
(`op signin`). The encrypted output is safe to commit; daemons/CI/other machines
then read it OFFLINE via secrets_materialize.py.

Usage:
    python execution/scripts/secrets_publish.py [file_name ...]
    ENVIRONMENT=production python execution/scripts/secrets_publish.py

With no args, publishes every file block in the manifest. Secret values are
never printed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_lib as sl  # noqa: E402


def publish_file(manifest: dict, name: str, environment: str | None) -> int:
    refs = sl.resolve_refs(manifest, name, environment)
    if not refs:
        print(f"[{name}] no references for environment={environment!r} — skipped")
        return 0

    pairs: dict[str, str] = {}
    for env_var, ref in refs.items():
        if ref.startswith("PLACEHOLDER_") or "placeholder" in ref.lower():
            print(f"[{name}] {env_var}: placeholder reference — skipped")
            continue
        try:
            pairs[env_var] = sl.op_read(ref)
        except Exception as exc:  # noqa: BLE001 — surface, never leak value
            print(f"[{name}] {env_var}: FAILED to read from 1Password ({exc})")
            return 1

    if not pairs:
        print(f"[{name}] nothing resolved — skipped")
        return 0

    dest = sl.enc_file(name)
    sl.sops_encrypt_dotenv(sl.to_dotenv(pairs), dest)
    print(f"[{name}] encrypted {len(pairs)} var(s) → {dest.relative_to(sl.SECRETS_BASE)}")
    print(f"[{name}] vars: {', '.join(sorted(pairs))}")
    return 0


def main(argv: list[str]) -> int:
    import os

    environment = os.environ.get("ENVIRONMENT")
    manifest = sl.load_manifest()
    names = argv or list(manifest.get("files", {}).keys())
    if not names:
        print("No file blocks in manifest.")
        return 1

    rc = 0
    for name in names:
        rc |= publish_file(manifest, name, environment)
    if rc == 0:
        print("\nDone. Review & commit the updated secrets/*.sops.env files.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
