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

    # Design B is a dual-writer contract: secrets_extract_host.py can
    # merge-write host-only keys into this SAME snapshot (see
    # docs/secrets_management.md). Decrypt what's there first and preserve
    # every key this publish run did not just resolve, or a plain
    # replace-encrypt from `pairs` alone would silently drop them.
    #
    # managed_keys is `set(pairs)` -- ONLY the refs actually resolved this
    # run -- not `set(refs)`. A ref skipped as a placeholder (line 36) or one
    # `op_read` failed on (line 41, which returns 1 before reaching here) is
    # never in `pairs`; treating it as "managed" would delete its existing
    # snapshot value instead of leaving it untouched, exactly the
    # host-only-key-loss bug this merge exists to prevent.
    existing: dict[str, str] = {}
    if dest.exists():
        try:
            existing = sl.sops_decrypt_dotenv(dest)
        except Exception as exc:  # noqa: BLE001 — type only, never sops' message
            print(f"[{name}] FAILED to decrypt existing snapshot before merge "
                  f"({type(exc).__name__}; sops output withheld) — refusing to "
                  f"publish, since a blind replace here would drop any "
                  f"host-only keys already in the snapshot")
            return 1

    preserved = sorted(k for k in existing if k not in pairs)
    merged = sl.merge_preserve_unmanaged(existing, set(pairs), pairs)
    existing.clear()

    sl.sops_encrypt_dotenv(sl.to_dotenv(merged), dest)
    print(f"[{name}] encrypted {len(pairs)} var(s) → {dest.relative_to(sl.SECRETS_BASE)}")
    print(f"[{name}] vars: {', '.join(sorted(pairs))}")
    if preserved:
        print(f"[{name}] preserved {len(preserved)} unmanaged key(s) already in "
              f"the snapshot: {', '.join(preserved)}")
    merged.clear()
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
