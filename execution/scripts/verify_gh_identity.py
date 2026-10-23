#!/usr/bin/env python3
"""Verify the GitHub identity a token actually resolves to, before using it.

Motivated by a wrong-identity PR: a PR on an operator-controlled repo was
opened as the operator's own personal GitHub account instead of the intended
agent account. Root cause, confirmed empirically (see repro below): `gh`
treats an EMPTY string in `GH_TOKEN`/`GITHUB_TOKEN` the same as the variable
being unset, and silently falls back to whatever account is active in the
shared `gh` keyring session on the host — no error, no warning, exit code 0.
A non-empty but INVALID token, by contrast, already fails loudly (401,
non-zero exit). The gap is specifically the empty-but-set case, plus the
case where the token was simply never exported into the child's environment.

Repro (safe to re-run; makes no writes):
    GH_TOKEN="" gh api user --jq .login   # succeeds, prints the KEYRING account
    GH_TOKEN="bad" gh api user --jq .login  # fails loudly, exit 1, "Bad credentials"

This script closes the gap by verifying the RESOLVED IDENTITY, not just
whether a token variable is present. A token that is empty, missing, or
resolves to the wrong login is a hard failure — never a fallback.

Usage:
    python3 execution/scripts/verify_gh_identity.py --expect-login ateles-agent
    python3 execution/scripts/verify_gh_identity.py --expect-login ateles-agent --token-env GH_TOKEN

Exit codes:
    0  — token present, well-formed, and `gh api user` resolves to the
         expected login.
    1  — hard failure: token missing/empty, malformed, or resolves to a
         DIFFERENT login than expected (including a keyring fallback).
         A message naming expected-vs-found is printed to stderr.

This script never prints a token value. It reports only presence, length,
and the resolved login (a GitHub username, not a secret).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# A GitHub PAT/OAuth token is never this short. Anything under this length
# is almost certainly a stray/truncated value, not a real credential — catch
# it before it ever reaches `gh`, rather than let `gh` decide what to do with
# a near-empty string.
MIN_PLAUSIBLE_TOKEN_LEN = 20

# Known classic/fine-grained/OAuth prefixes. Not exhaustive (some enterprise
# or future token forms won't match) — used only to WARN, never to block,
# since a plausibly-shaped token still has to pass the live identity check
# below to be trusted.
PLAUSIBLE_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")


def _fail(msg: str) -> int:
    print(f"[verify_gh_identity] REFUSED: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expect-login",
        required=True,
        help="The GitHub login this token MUST resolve to (e.g. ateles-agent).",
    )
    parser.add_argument(
        "--token-env",
        default="GH_TOKEN",
        help="Env var holding the token to verify (default: GH_TOKEN). "
        "Also checks GITHUB_TOKEN as a fallback source, matching gh's own "
        "precedence, but the identity check below is what actually matters.",
    )
    args = parser.parse_args()

    token = os.environ.get(args.token_env, "")
    source = args.token_env
    if not token:
        # gh itself falls back GH_TOKEN -> GITHUB_TOKEN; check the same way so
        # this script's verdict matches what `gh` will actually do.
        token = os.environ.get("GITHUB_TOKEN", "")
        source = "GITHUB_TOKEN"

    if not token:
        return _fail(
            f"no token found in {args.token_env} or GITHUB_TOKEN (both empty/unset). "
            f"Expected a token resolving to '{args.expect_login}'. "
            f"Never proceed on ambient/keyring fallback — provision the token."
        )

    if len(token) < MIN_PLAUSIBLE_TOKEN_LEN:
        return _fail(
            f"{source} is set but only {len(token)} chars — too short to be a "
            f"real GitHub token. Expected a token resolving to '{args.expect_login}'."
        )

    if not token.startswith(PLAUSIBLE_PREFIXES):
        print(
            f"[verify_gh_identity] WARNING: {source} (len={len(token)}) does not "
            f"match a known gh*_ / github_pat_ prefix. Proceeding to the live "
            f"identity check, which is authoritative.",
            file=sys.stderr,
        )

    # The check that actually catches keyring fallback: ask GitHub who this
    # token IS, using the SAME env var gh itself would read (GH_TOKEN takes
    # precedence over GITHUB_TOKEN in gh's own resolution), rather than
    # trusting the variable's mere presence.
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)  # isolate: prove THIS token is what resolves

    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return _fail("`gh` CLI not found on PATH — cannot verify identity.")
    except subprocess.TimeoutExpired:
        return _fail("`gh api user` timed out — treat as unverified, do not proceed.")

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
        return _fail(
            f"token in {source} (len={len(token)}) did NOT authenticate "
            f"(gh exit {result.returncode}: {stderr_tail[0]}). "
            f"Expected login '{args.expect_login}'. Never fall back silently."
        )

    resolved_login = result.stdout.strip()
    if resolved_login != args.expect_login:
        return _fail(
            f"token in {source} resolved to '{resolved_login}', NOT the expected "
            f"'{args.expect_login}'. This is exactly the keyring-fallback failure "
            f"mode (an empty/wrong token silently authenticating as some other "
            f"account) — hard-failing instead of proceeding as '{resolved_login}'."
        )

    print(
        f"[verify_gh_identity] OK: {source} (len={len(token)}) resolves to "
        f"expected login '{resolved_login}'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
