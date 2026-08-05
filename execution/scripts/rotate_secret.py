#!/usr/bin/env python3
"""Rotate one secret end-to-end without the value touching disk, shell history,
or an agent transcript.

The new value is read from the clipboard (or stdin with --stdin), written to
1Password as the canonical copy, republished into the encrypted SOPS snapshot,
and materialized into the consuming .env files. The value is never printed,
never passed as an argv, and never written to a temp file.

Direction:  clipboard ──▶ 1Password ──▶ secrets/<file>.sops.enc ──▶ .env

Usage:
    # copy the new credential to the clipboard first, then:
    python execution/scripts/rotate_secret.py ANTHROPIC_API_KEY
    python execution/scripts/rotate_secret.py ATELES_AGENT_PAT --stdin
    python execution/scripts/rotate_secret.py --list

Exit codes: 0 ok, 1 failure (message describes the stage that failed).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_lib as sl  # noqa: E402

# Shape checks catch a mis-copied clipboard before it overwrites a good value.
# Deliberately loose on length — providers change it — but strict on prefix.
SHAPES: dict[str, tuple[str, str]] = {
    "ANTHROPIC_API_KEY": (r"^sk-ant-api\d{2}-[A-Za-z0-9_-]{80,}$", "sk-ant-api03-…"),
    "CLAUDE_CODE_OAUTH_TOKEN": (r"^sk-ant-oat\d{2}-[A-Za-z0-9_-]{80,}$", "sk-ant-oat01-…"),
    "ATELES_AGENT_PAT": (r"^gh[po]_[A-Za-z0-9]{36,}$|^github_pat_[A-Za-z0-9_]{60,}$", "ghp_… or github_pat_…"),
    "NEOTOMA_AGENT_PAT": (r"^gh[po]_[A-Za-z0-9]{36,}$|^github_pat_[A-Za-z0-9_]{60,}$", "ghp_… or github_pat_…"),
    "GITHUB_TOKEN": (r"^gh[po]_[A-Za-z0-9]{36,}$|^github_pat_[A-Za-z0-9_]{60,}$", "ghp_… or github_pat_…"),
}


# Prefixes of credentials this repo handles. Used only to recognise a value
# that was mistakenly passed as an argument — never to validate a real secret.
SECRET_PREFIXES = ("sk-ant-", "sk-", "ghp_", "gho_", "github_pat_", "AKIA", "xoxb-", "xoxp-")


def looks_like_a_secret(arg: str) -> bool:
    """True when the argument is evidently a credential, not a variable name.

    Env var names are short, uppercase, and underscore-separated. Anything with
    a known credential prefix, or that is long and mixed-case, is a value.
    """
    if arg.startswith(SECRET_PREFIXES):
        return True
    return len(arg) > 40 and not arg.isupper()


def known_env_vars() -> set[str]:
    manifest = sl.load_manifest()
    names: set[str] = set()
    for blk in manifest.get("files", {}).values():
        for section in ("default", "production", "development"):
            names.update((blk.get(section) or {}).keys())
    return names


def read_clipboard() -> str:
    out = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def find_ref(env_var: str) -> tuple[str, str] | None:
    """Return (file_name, op_ref) for env_var, or None when unmanaged."""
    manifest = sl.load_manifest()
    for fname, blk in manifest.get("files", {}).items():
        for section in ("default", "production", "development"):
            ref = (blk.get(section) or {}).get(env_var)
            if ref:
                return fname, ref
    return None


def op_write(ref: str, value: str) -> None:
    """Write value to an op:// reference without the secret touching argv.

    `op item edit <item> field=value` would work, but argv is world-readable in
    the process table for the life of the call. `field=-` does NOT read the raw
    value from stdin — op parses stdin as an item JSON template — so the correct
    stdin path is: fetch the item as JSON, substitute the field's value, and
    pipe the whole template back via `op item edit … -`.
    """
    m = re.match(r"^op://([^/]+)/([^/]+)/(.+)$", ref)
    if not m:
        raise ValueError(f"unparseable reference: {ref}")
    vault, item, field = m.groups()

    got = subprocess.run(
        ["op", "item", "get", item, "--vault", vault, "--format", "json"],
        check=True, capture_output=True, text=True,
    )
    doc = json.loads(got.stdout)

    matched = False
    for f in doc.get("fields", []):
        if f.get("id") == field or f.get("label") == field:
            f["value"] = value
            matched = True
    if not matched:
        raise ValueError(
            f"item has no field {field!r} — "
            f"available: {sorted(x.get('id') for x in doc.get('fields', []))}"
        )

    subprocess.run(
        ["op", "item", "edit", item, "--vault", vault, "-"],
        input=json.dumps(doc), text=True, check=True, capture_output=True,
    )


def verify_roundtrip(ref: str, expected: str) -> bool:
    """Read the value back and compare — never assert a write you did not verify."""
    try:
        return sl.op_read(ref) == expected
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("env_var", nargs="?", help="e.g. ANTHROPIC_API_KEY")
    ap.add_argument("--stdin", action="store_true", help="read value from stdin, not clipboard")
    ap.add_argument("--list", action="store_true", help="list managed env vars and exit")
    ap.add_argument("--no-materialize", action="store_true", help="skip the .env write")
    ap.add_argument(
        "--gh-secret", action="append", default=[], metavar="OWNER/REPO",
        help="also set this GitHub Actions secret to the same value (repeatable). "
             "Runs only after the shape check and the 1Password round-trip pass.",
    )
    args = ap.parse_args(argv)

    if args.list:
        manifest = sl.load_manifest()
        for fname, blk in manifest.get("files", {}).items():
            for section in ("default", "production", "development"):
                for k in sorted((blk.get(section) or {}).keys()):
                    print(f"  {k:32s} [{fname}]")
        return 0

    if not args.env_var:
        ap.error("env_var is required unless --list")

    # Catch the dangerous mistake FIRST: a secret value passed as the argument.
    # argv is visible in the process table and lands in shell history, so the
    # credential is already compromised by the time we see it. Say that plainly
    # rather than talking about the manifest.
    if looks_like_a_secret(args.env_var):
        print("STOP — you passed a SECRET VALUE, not a variable name.")
        print()
        print("That value is now in your shell history and was visible in the")
        print("process table. Treat it as compromised:")
        print("  1. Revoke it at the provider console NOW and issue a new one.")
        print("  2. Scrub your shell history (e.g. ~/.zsh_history).")
        print()
        print("This script never takes the value as an argument — it reads your")
        print("clipboard. Copy the NEW credential, then run:")
        print(f"    {Path(__file__).name} <VARIABLE_NAME>")
        print("e.g.  rotate_secret.py ANTHROPIC_API_KEY")
        return 2

    found = find_ref(args.env_var)
    if not found:
        print(f"ERROR: {args.env_var} is not in the manifest — nothing manages it.")
        print()
        print("Known variable names:")
        for k in sorted(known_env_vars()):
            print(f"    {k}")
        print()
        print("If this is a genuinely new secret, add a 1Password item and a")
        print("manifest entry first, or it will keep being pasted by hand")
        print("(which is how the current batch leaked).")
        return 1
    fname, ref = found

    value = sys.stdin.read().strip() if args.stdin else read_clipboard()
    if not value:
        print("ERROR: no value on the clipboard/stdin.")
        return 1

    shape = SHAPES.get(args.env_var)
    if shape and not re.match(shape[0], value):
        print(f"ERROR: value does not look like {args.env_var} (expected {shape[1]}).")
        print(f"       got {len(value)} chars starting {value[:12]!r} — refusing to write.")
        return 1

    print(f"[1/4] writing {args.env_var} → 1Password ({ref.rsplit('/', 1)[0]}/…)")
    try:
        op_write(ref, value)
    except subprocess.CalledProcessError as exc:
        print(f"      FAILED: {exc.stderr.strip() if exc.stderr else exc}")
        return 1

    print("[2/4] verifying round-trip from 1Password")
    if not verify_roundtrip(ref, value):
        print("      FAILED: value read back does not match what was written.")
        return 1

    print(f"[3/4] republishing encrypted snapshot: {fname}")
    rc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "secrets_publish.py"), fname]
    ).returncode
    if rc != 0:
        return rc

    if args.no_materialize:
        print("[4/4] skipped (--no-materialize)")
    else:
        print("[4/4] materializing into .env")
        rc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "secrets_materialize.py"), fname]
        ).returncode
        if rc != 0:
            return rc

    # GitHub Actions secrets last: only a value that already passed the shape
    # check and the 1Password round-trip is allowed to reach CI. Doing this with
    # a bare `pbpaste | gh secret set` is how the neotoma CLAUDE_CODE_OAUTH_TOKEN
    # secret was overwritten with 15 characters of unrelated text on 2026-08-05 —
    # gh accepts any stdin and reports success, and a corrupted CI secret is
    # write-only, so the damage only surfaces as a confusing auth failure in a
    # later workflow run.
    for repo in args.gh_secret:
        print(f"[+]   setting Actions secret in {repo}")
        r = subprocess.run(
            ["gh", "secret", "set", args.env_var, "-R", repo],
            input=value, text=True, capture_output=True,
        )
        if r.returncode != 0:
            print(f"      FAILED: {r.stderr.strip() or r.returncode}")
            print("      (1Password/.env are already updated; re-run with only "
                  "--gh-secret to retry just this step)")
            return 1
        print(f"      ok — {repo}")

    print(f"\nDone. {args.env_var} rotated. Clear your clipboard:  pbcopy </dev/null")
    print(f"Commit the snapshot in ateles-private: git -C ~/repos/ateles-private commit -am 'rotate {args.env_var}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
