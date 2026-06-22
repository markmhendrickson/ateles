"""Shared helpers for the SOPS-backed secrets flow (Design B).

Canonical secret values live in 1Password. The age-encrypted snapshots and
manifest live in the PRIVATE `ateles-private` repo (NOT here — ateles is public),
produced by `secrets_publish.py` and consumed offline by `secrets_materialize.py`
and the daemons. Even in a private repo the snapshots stay age-encrypted for
defense-in-depth (a leaked token or an accidental public flip never exposes them).

Stdlib-only by design — these run in minimal daemon/CI environments.
Security: secret VALUES are never logged or printed by anything in this module.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# The private secrets repo holds the .sops.yaml, manifest, and encrypted
# snapshots. Override with ATELES_SECRETS_DIR; default to the conventional clone.
SECRETS_BASE = Path(
    os.environ.get("ATELES_SECRETS_DIR", str(Path.home() / "repos" / "ateles-private"))
).expanduser()
SECRETS_DIR = SECRETS_BASE / "secrets"
MANIFEST_PATH = SECRETS_DIR / "manifest.env-map.json"

# sops' default age-key location is OS-specific (on macOS it's under
# ~/Library/Application Support, NOT ~/.config). We standardize on this path and
# pass it explicitly via SOPS_AGE_KEY_FILE so behavior is identical everywhere.
DEFAULT_AGE_KEY_FILE = Path.home() / ".config" / "sops" / "age" / "keys.txt"


def _sops_env() -> dict[str, str]:
    """Environment for sops subprocesses, defaulting SOPS_AGE_KEY_FILE."""
    env = dict(os.environ)
    if not env.get("SOPS_AGE_KEY_FILE") and not env.get("SOPS_AGE_KEY"):
        if DEFAULT_AGE_KEY_FILE.exists():
            env["SOPS_AGE_KEY_FILE"] = str(DEFAULT_AGE_KEY_FILE)
    return env


def sops_path() -> str:
    return os.environ.get("SOPS_BIN", "sops")


def op_path() -> str:
    return os.environ.get("OP_BIN", "op")


def enc_file(name: str) -> Path:
    """Path to the encrypted snapshot for a manifest file block."""
    return SECRETS_DIR / f"{name}.sops.enc"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text())


def resolve_refs(manifest: dict, file_name: str, environment: str | None) -> dict[str, str]:
    """Return {env_var: op_reference} for a file block, applying the env overlay.

    `default` always applies; the overlay for `environment` (if present)
    overrides matching keys.
    """
    block = manifest.get("files", {}).get(file_name)
    if block is None:
        raise KeyError(f"No file block '{file_name}' in manifest")
    refs: dict[str, str] = dict(block.get("default", {}))
    if environment:
        refs.update(block.get(environment, {}))
    return refs


# ---------------------------------------------------------------------------
# dotenv helpers
# ---------------------------------------------------------------------------

def to_dotenv(pairs: dict[str, str]) -> str:
    """Render the plaintext fed to SOPS.

    Values are written raw (no wrapping quotes): SOPS dotenv preserves them
    verbatim, so `parse_dotenv` recovers them exactly. Secret tokens/keys do not
    contain newlines, which dotenv cannot represent.
    """
    return "".join(f"{k}={v}\n" for k, v in pairs.items())


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def merge_into_env_file(env_file: Path, updates: dict[str, str]) -> list[str]:
    """Merge `updates` into a dotenv file, preserving unmanaged keys.

    Returns the list of var NAMES changed (never values). Creates parent dirs
    and the file if missing.
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = env_file.read_text().splitlines() if env_file.exists() else []
    seen: set[str] = set()
    changed: list[str] = []
    out_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in updates:
                new_line = f'{key}="{updates[key]}"'
                if new_line != line:
                    changed.append(key)
                out_lines.append(new_line)
                seen.add(key)
                continue
        out_lines.append(line)

    for key, val in updates.items():
        if key not in seen:
            out_lines.append(f'{key}="{val}"')
            changed.append(key)

    env_file.write_text("\n".join(out_lines).rstrip("\n") + "\n")
    return changed


# ---------------------------------------------------------------------------
# SOPS / 1Password wrappers
# ---------------------------------------------------------------------------

def op_read(reference: str) -> str:
    """Read a single secret value from 1Password. Requires a live session."""
    result = subprocess.run(
        [op_path(), "read", reference],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        # stderr may name the item but never the value
        raise RuntimeError(f"op read failed for {reference}: {result.stderr.strip()}")
    return result.stdout.strip()


def sops_encrypt_dotenv(plaintext: str, dest: Path) -> None:
    """Encrypt dotenv `plaintext` to `dest` using rules in .sops.yaml.

    Writes plaintext to a 0600 temp file (sops needs a real file), encrypts,
    then unlinks it.
    """
    import tempfile

    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=".plain.env", dir=str(SECRETS_DIR))
        tmp = Path(tmp_name)
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(plaintext)
        # --config pins the rules to ateles-private's .sops.yaml (else sops
        # discovers whatever .sops.yaml sits above the cwd). --filename-override
        # matches the rule against the DEST name, so the temp file can keep its
        # gitignored *.plain.env name.
        cmd = [sops_path(), "--encrypt", "--input-type", "dotenv",
               "--output-type", "dotenv", "--filename-override", str(dest)]
        config = SECRETS_BASE / ".sops.yaml"
        if config.exists():
            cmd += ["--config", str(config)]
        cmd.append(str(tmp))
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=_sops_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(f"sops encrypt failed: {result.stderr.strip()}")
        dest.write_text(result.stdout)
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


def sops_decrypt_dotenv(src: Path) -> dict[str, str]:
    """Decrypt a SOPS dotenv snapshot to {key: value}. Offline (uses local age key)."""
    result = subprocess.run(
        [sops_path(), "--decrypt", "--input-type", "dotenv",
         "--output-type", "dotenv", str(src)],
        capture_output=True, text=True, timeout=30, env=_sops_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"sops decrypt failed for {src.name}: {result.stderr.strip()}")
    return parse_dotenv(result.stdout)
