#!/usr/bin/env python3
"""Extract host-only deployment config into the SOPS snapshot, sight unseen.

Direction:  running Fly machine  ──▶  ateles-private/secrets/<name>.sops.enc

Some deployed configuration exists ONLY in the running machine's environment.
Fly returns a digest and never a value, so once the machine is torn down or
moved those values are unrecoverable. This script captures them into the
existing age-encrypted store WITHOUT any value being displayed, logged, or
written to disk in plaintext -- so an agent can build and review the pipe while
only the operator, who holds the Fly session and the age key, ever runs it.

    THE OPERATOR RUNS THIS. It needs a live `flyctl` session and the machine-local
    age key. The swarm holds neither, by design.

Security properties (the point of this script, not incidental polish):
  * Values are read into a dict and passed to sops. They are NEVER interpolated
    into a print, a log record, or an exception message. Every failure path
    reports the KEY NAME and the failure MODE only.
  * No plaintext intermediate is written by this script. It reuses
    secrets_lib.sops_encrypt_dotenv, whose one temp file is created mode-0600
    inside the private secrets dir and unlinked in a `finally`. A signal handler
    converts SIGINT/SIGTERM into a normal exception so that `finally` still runs.
  * Merging is the default: existing keys in the snapshot are preserved and are
    NOT overwritten unless --overwrite is passed. Nothing is ever deleted.
  * Re-running is idempotent -- the same inputs produce the same key set.

Usage (see --help):
    python execution/scripts/secrets_extract_host.py \
        --app <fly-app> --block <manifest-block> --keys-from <file>
    python execution/scripts/secrets_extract_host.py --block <block> --verify
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_lib as sl  # noqa: E402


class ExtractionError(Exception):
    """Failure carrying only a key name and a mode -- never a secret value."""


def _install_signal_guards() -> None:
    """Turn SIGINT/SIGTERM into exceptions so `finally` cleanup still runs.

    Default SIGTERM handling exits without unwinding, which would strand the
    mode-0600 temp file sops needs. Raising instead guarantees the unlink.
    """

    def _raise(signum, _frame):  # noqa: ANN001
        raise ExtractionError(f"interrupted by signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _raise)
        except (ValueError, OSError):
            pass  # not on the main thread, or unsupported on this platform


def load_key_names(path: Path) -> list[str]:
    """Read the key list: one env var name per line, `#` comments allowed.

    Only NAMES live in this file. It carries no values and is safe to keep in
    the public repo or pass on the command line.
    """
    if not path.exists():
        raise ExtractionError(f"key list not found: {path}")
    names: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            raise ExtractionError(
                f"key list line looks like an assignment, not a bare name: "
                f"{line.partition('=')[0].strip()!r}. This file must contain "
                f"NAMES ONLY -- never values."
            )
        names.append(line)
    if not names:
        raise ExtractionError(f"key list is empty: {path}")
    return names


def read_host_env(app: str, keys: list[str], machine: str | None = None) -> dict[str, str]:
    """Read the requested vars from the running machine's environment.

    Returns {name: value}. The subprocess output is captured, parsed, and the
    parsed dict is returned; NOTHING from stdout is echoed. On failure, only
    flyctl's exit status is surfaced -- never its output, which may quote the
    environment it was reading.
    """
    cmd = ["flyctl", "ssh", "console", "--app", app]
    if machine:
        cmd += ["--machine", machine]
    cmd += ["-C", "printenv"]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise ExtractionError(
            "flyctl ssh console timed out after 120s (no output inspected)"
        ) from None
    except FileNotFoundError:
        raise ExtractionError("flyctl not found on PATH") from None

    if result.returncode != 0:
        # DELIBERATELY not including stderr/stdout. flyctl failure output can
        # echo the remote command and, on some paths, environment content.
        raise ExtractionError(
            f"flyctl ssh console exited {result.returncode} for app {app!r} "
            f"(output withheld to avoid leaking environment content)"
        )

    wanted = set(keys)
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        if name in wanted:
            found[name] = value

    # Explicitly drop the raw capture before doing anything else.
    del result

    missing = [k for k in keys if k not in found]
    empty = sorted(k for k, v in found.items() if not v.strip())
    if empty:
        raise ExtractionError(
            f"{len(empty)} requested key(s) are present but EMPTY on the host: "
            f"{', '.join(empty)}. Refusing to store empty values."
        )
    if missing:
        raise ExtractionError(
            f"{len(missing)} requested key(s) absent from the host environment: "
            f"{', '.join(missing)}. Fix the key list or the app, then re-run."
        )
    return found


def load_existing(block: str) -> dict[str, str]:
    """Decrypt the current snapshot for `block`, or {} if there is none.

    The decrypted dict stays in memory and is never printed.
    """
    src = sl.enc_file(block)
    if not src.exists():
        return {}
    try:
        return sl.sops_decrypt_dotenv(src)
    except Exception as exc:  # noqa: BLE001
        # secrets_lib embeds sops' stderr in its message. sops is not
        # contractually bound to keep content out of that stream, so report the
        # exception TYPE and the file name only -- never the message.
        raise ExtractionError(
            f"could not decrypt existing snapshot {src.name} "
            f"({type(exc).__name__}; sops output withheld). Check that the age "
            f"key is present at ~/.config/sops/age/keys.txt."
        ) from None


def verify(block: str, keys: list[str] | None) -> int:
    """Confirm the snapshot holds the expected keys, non-empty, WITHOUT showing them.

    Prints key names and a present/non-empty verdict per key. Never prints,
    lengths, prefixes, hashes, or any other function of a value -- only a bool.
    """
    src = sl.enc_file(block)
    if not src.exists():
        print(f"[{block}] FAIL: no snapshot at {src.name}")
        return 1

    values = load_existing(block)
    expected = keys if keys is not None else sorted(values)

    print(f"[{block}] snapshot {src.name}: {len(values)} key(s) present")
    ok = True
    for name in expected:
        if name not in values:
            print(f"  MISSING   {name}")
            ok = False
        elif not values[name].strip():
            print(f"  EMPTY     {name}")
            ok = False
        else:
            print(f"  ok        {name}")

    # Drop the plaintext as soon as the verdict is computed.
    values.clear()

    if ok:
        print(f"[{block}] PASS: {len(expected)}/{len(expected)} key(s) present and non-empty")
        return 0
    print(f"[{block}] FAIL: see MISSING/EMPTY above")
    return 1


def extract(app: str, block: str, keys: list[str], machine: str | None,
            overwrite: bool, dry_run: bool) -> int:
    existing = load_existing(block)
    print(f"[{block}] existing snapshot holds {len(existing)} key(s)")

    if dry_run:
        # Reads NOTHING from the host. Reports only what WOULD happen, by name.
        would_add = [k for k in keys if k not in existing]
        would_keep = [k for k in keys if k in existing and not overwrite]
        would_replace = [k for k in keys if k in existing and overwrite]
        print(f"[{block}] DRY RUN -- no host read, no write")
        print(f"  would add ({len(would_add)}): {', '.join(would_add) or '-'}")
        print(f"  would replace ({len(would_replace)}): {', '.join(would_replace) or '-'}")
        print(f"  would leave untouched ({len(would_keep)}): {', '.join(would_keep) or '-'}")
        existing.clear()
        return 0

    host = read_host_env(app, keys, machine)
    print(f"[{block}] read {len(host)} key(s) from the host environment")

    collisions = [k for k in host if k in existing]
    if collisions and not overwrite:
        print(f"[{block}] {len(collisions)} key(s) already in the snapshot and "
              f"PRESERVED (pass --overwrite to replace): {', '.join(sorted(collisions))}")

    # Merge. Existing wins unless --overwrite. Nothing is ever removed.
    merged = dict(existing)
    added: list[str] = []
    replaced: list[str] = []
    for name, value in host.items():
        if name in merged:
            if overwrite:
                if merged[name] != value:
                    replaced.append(name)
                merged[name] = value
        else:
            merged[name] = value
            added.append(name)

    if not added and not replaced:
        print(f"[{block}] snapshot already current -- nothing to write")
        merged.clear(); existing.clear(); host.clear()
        return 0

    dest = sl.enc_file(block)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        sl.sops_encrypt_dotenv(sl.to_dotenv(merged), dest)
    except Exception as exc:  # noqa: BLE001
        # As in load_existing: secrets_lib embeds sops' stderr, which on a
        # content-related failure can quote the dotenv it was handed. Report
        # the exception TYPE only. (A canary test confirmed the naive
        # `{exc}` form leaks the value here.)
        raise ExtractionError(
            f"sops encrypt failed for {dest.name} "
            f"({type(exc).__name__}; sops output withheld). The snapshot was "
            f"NOT modified."
        ) from None
    finally:
        # Clear plaintext from our own references regardless of outcome.
        merged.clear(); existing.clear(); host.clear()

    print(f"[{block}] wrote {dest}")
    print(f"  added ({len(added)}): {', '.join(sorted(added)) or '-'}")
    print(f"  replaced ({len(replaced)}): {', '.join(sorted(replaced)) or '-'}")
    print(f"\nNext: commit the snapshot in the PRIVATE repo, then verify:")
    print(f"  cd {sl.SECRETS_BASE} && git add secrets/{block}.sops.enc && "
          f"git commit -m 'chore(secrets): capture host-only config for {block}'")
    print(f"  python execution/scripts/secrets_extract_host.py --block {block} "
          f"--keys-from <file> --verify")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Extract host-only deployment config straight into the "
                    "SOPS store. No value is printed, logged, or written in "
                    "plaintext. Run by the OPERATOR (needs Fly session + age key).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--app", help="Fly app to read from (required unless --verify/--dry-run)")
    parser.add_argument("--machine", help="Specific machine id (optional)")
    parser.add_argument("--block", required=True,
                        help="Manifest block / snapshot name, e.g. the app's block")
    parser.add_argument("--keys-from", type=Path,
                        help="File of env var NAMES to capture, one per line")
    parser.add_argument("--key", action="append", default=[],
                        help="A single env var NAME to capture (repeatable)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace keys already in the snapshot (default: preserve)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change, by name. Does not read the host.")
    parser.add_argument("--verify", action="store_true",
                        help="Check the snapshot holds the keys, non-empty, without revealing values")
    args = parser.parse_args(argv)

    _install_signal_guards()

    try:
        keys: list[str] | None = None
        if args.keys_from or args.key:
            keys = list(args.key)
            if args.keys_from:
                keys = load_key_names(args.keys_from) + keys
            seen: set[str] = set()
            keys = [k for k in keys if not (k in seen or seen.add(k))]

        if args.verify:
            return verify(args.block, keys)

        if keys is None:
            raise ExtractionError("--keys-from or --key is required for extraction")
        if not args.app and not args.dry_run:
            raise ExtractionError("--app is required for extraction")

        return extract(args.app, args.block, keys, args.machine,
                       args.overwrite, args.dry_run)

    except ExtractionError as exc:
        # The ONLY error surface. ExtractionError is constructed exclusively
        # from key names and failure modes, never from a value.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        # Last-resort guard: an unexpected exception's message could in
        # principle carry payload, so report only its TYPE.
        print(f"ERROR: unexpected {type(exc).__name__} "
              f"(message withheld -- it may contain secret material)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
