#!/usr/bin/env python3
"""Back up and restore agent signing keys as age-encrypted files. OFFLINE.

Direction:
    backup:   ateles-private/keys/<name>.json            (plaintext, gitignored)
          ──▶ ateles-private/keys/encrypted/<stem>.sops.json   (committed)
    restore:  keys/encrypted/<stem>.sops.json ──▶ keys/<name>.json  (mode 0600)
    verify:   decrypt each encrypted copy in memory and compare its SHA-256
              with the plaintext on disk; prints match / mismatch / missing.

Covers every ``keys/*.json`` file: the canonical ``<agent>.jwk.json`` JWKs and
any legacy PEM-in-JSON ``<agent>.json`` files. Encryption uses the same age
recipient as the secrets snapshots (the ``keys/encrypted/`` rule in
ateles-private's ``.sops.yaml``); decryption needs only the machine-local age
key (``~/.config/sops/age/keys.txt``). Files are encrypted in sops binary mode,
so a restore reproduces the original bytes exactly.

Key material is never printed: output names files and reports status only.

Usage:
    python execution/scripts/secrets_keys.py backup [name ...]
    python execution/scripts/secrets_keys.py verify [name ...]
    python execution/scripts/secrets_keys.py restore [--force] [name ...]
    python execution/scripts/secrets_keys.py prune      # drop encrypted copies
                                                        # whose plaintext is gone

``name`` is a plaintext file name (``apus.jwk.json``) or its stem (``apus.jwk``).
``backup`` re-encrypts a file only when its encrypted copy is missing or no
longer decrypts to the same bytes, so the committed ciphertext does not churn.
``restore`` refuses to overwrite an existing plaintext file unless ``--force``.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_lib as sl  # noqa: E402


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain_files(names: list[str]) -> list[Path]:
    files = sorted(p for p in sl.KEYS_DIR.glob("*.json") if p.is_file())
    if not names:
        return files
    wanted = {n if n.endswith(".json") else f"{n}.json" for n in names}
    return [p for p in files if p.name in wanted]


def _enc_files(names: list[str]) -> list[Path]:
    files = sorted(sl.ENC_KEYS_DIR.glob(f"*{sl.ENC_KEY_SUFFIX}"))
    if not names:
        return files
    wanted = {n if n.endswith(".json") else f"{n}.json" for n in names}
    return [p for p in files if sl.plain_key_file(p).name in wanted]


def _matches(plain: Path, enc: Path) -> bool:
    return enc.exists() and _sha256(sl.sops_decrypt_file_binary(enc)) == _sha256(plain.read_bytes())


def backup(names: list[str]) -> int:
    plains = _plain_files(names)
    if not plains:
        print(f"no key files found in {sl.KEYS_DIR}")
        return 1
    rc = 0
    for plain in plains:
        enc = sl.enc_key_file(plain)
        try:
            if _matches(plain, enc):
                print(f"{plain.name}: unchanged")
                continue
            sl.sops_encrypt_file_binary(plain, enc)
            ok = _matches(plain, enc)
        except Exception as exc:  # noqa: BLE001 — names only, never contents
            print(f"{plain.name}: FAILED ({exc})")
            rc = 1
            continue
        print(f"{plain.name}: encrypted -> encrypted/{enc.name} "
              f"({'verified' if ok else 'VERIFY MISMATCH'})")
        rc |= 0 if ok else 1
    return rc


def verify(names: list[str]) -> int:
    rc = 0
    plains = _plain_files(names)
    for plain in plains:
        enc = sl.enc_key_file(plain)
        if not enc.exists():
            print(f"{plain.name}: MISSING encrypted copy")
            rc = 1
            continue
        try:
            ok = _matches(plain, enc)
        except Exception as exc:  # noqa: BLE001
            print(f"{plain.name}: FAILED ({exc})")
            rc = 1
            continue
        print(f"{plain.name}: {'match' if ok else 'MISMATCH'}")
        rc |= 0 if ok else 1
    plain_names = {p.name for p in plains}
    for enc in _enc_files(names):
        if sl.plain_key_file(enc).name not in plain_names:
            print(f"encrypted/{enc.name}: no plaintext on disk (restorable)")
    return rc


def restore(names: list[str], force: bool) -> int:
    encs = _enc_files(names)
    if not encs:
        print(f"no encrypted key files found in {sl.ENC_KEYS_DIR}")
        return 1
    rc = 0
    for enc in encs:
        plain = sl.plain_key_file(enc)
        if plain.exists() and not force:
            print(f"{plain.name}: exists, skipped (use --force to overwrite)")
            continue
        try:
            sl.write_private_file(plain, sl.sops_decrypt_file_binary(enc))
        except Exception as exc:  # noqa: BLE001
            print(f"{plain.name}: FAILED ({exc})")
            rc = 1
            continue
        print(f"{plain.name}: restored (0600)")
    return rc


def prune(names: list[str]) -> int:
    for enc in _enc_files(names):
        if not sl.plain_key_file(enc).exists():
            enc.unlink()
            print(f"encrypted/{enc.name}: removed (plaintext no longer on disk)")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"backup", "verify", "restore", "prune"}:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    force = "--force" in rest
    names = [a for a in rest if a != "--force"]
    if cmd == "backup":
        return backup(names)
    if cmd == "verify":
        return verify(names)
    if cmd == "restore":
        return restore(names, force)
    return prune(names)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
