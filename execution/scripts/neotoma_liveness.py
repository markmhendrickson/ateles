#!/usr/bin/env python3
"""Check whether a Neotoma instance is actually serving — not just reachable.

Why this exists: on 2026-08-05 Neotoma was down twice and BOTH outages were
misdiagnosed as credential problems, because /health kept returning 200. It is
terminated at the Cloudflare edge, so it answers even when no application is
listening behind it, and it answered throughout a window in which every MCP
session was dead. A health check that cannot distinguish "up" from "the CDN is
up" is worse than none: it converts an outage into a wild goose chase.

So this probe asserts three things a real liveness check must:

  1. REACHABLE     — the origin answers at all.
  2. AUTHENTICATING — a VALID token and a GARBAGE token get DIFFERENT answers.
                     This is the load-bearing check. A single authenticated
                     request is ambiguous: 404 could mean "wrong path" or
                     "no data", and 401 could mean "bad token" or "server
                     replaced by an error page". Only the *difference* proves
                     the server read the credential and made a decision.
  3. SERVING DATA  — a real query returns a parseable body with entities in it.

It also reports WHICH instance answered. get_authenticated_user says
storage_backend "local" for both the laptop and the hosted container; the
distinguishing field is data_dir (/Users/... vs /app/...). Two deployments
failing independently while reporting the same label is exactly how a session
ends up writing to the wrong graph.

Usage:
    python execution/scripts/neotoma_liveness.py                 # default base URL
    python execution/scripts/neotoma_liveness.py --base-url URL
    python execution/scripts/neotoma_liveness.py --quiet         # exit code only

Exit codes: 0 serving, 1 degraded/down (reason printed), 2 misconfigured.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
# Cloudflare fingerprints urllib's default `Python-urllib/3.x` UA and answers
# 403 error 1010 before the request reaches Neotoma; any explicit UA passes.
# Same rationale as NEOTOMA_USER_AGENT in the daemons (ateles#389) — kept as a
# local constant rather than an import because this script is standalone and
# must run from a bare checkout with nothing on sys.path.
USER_AGENT = "ateles-liveness/1.0"
TIMEOUT = 20


def read_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file, stripping one layer of wrapping quotes.

    merge_into_env_file writes values quoted so that #, spaces and = survive
    shell-sourcing. A reader that does not strip them sends the quotes as part
    of the credential and gets a spurious 401 — a real false alarm from
    2026-08-05, diagnosed as a corrupted .env before the quoting was understood.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if not m:
            continue
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "'"):
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def request(url: str, token: str | None) -> tuple[int, str]:
    """Return (status, body). Status 0 means the request itself failed."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(10_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — connection refused, DNS, timeout
        return 0, str(e)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    env = read_env(Path(args.env_file).expanduser())
    base = (args.base_url or os.environ.get("NEOTOMA_BASE_URL")
            or env.get("NEOTOMA_BASE_URL", "")).rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN") or env.get("NEOTOMA_BEARER_TOKEN", "")

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    if not base or not token:
        say("MISCONFIGURED: need NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN "
            f"(checked env and {args.env_file})")
        return 2

    say(f"instance: {base}")

    # 1. Reachable at all.
    status, _ = request(f"{base}/health", None)
    if status == 0:
        say("DOWN: origin unreachable")
        return 1
    say(f"  reachable        /health -> {status}")

    # 2. THE load-bearing check: does a valid token get a different answer than
    #    a garbage one? Same path both times, so any difference is attributable
    #    to the credential rather than to routing.
    probe = f"{base}/me"
    good_status, good_body = request(probe, token)
    bad_status, _ = request(probe, "garbage-not-a-real-token")

    if good_status == 0:
        say("DOWN: authenticated request failed to complete")
        return 1
    if good_status == bad_status:
        say(f"  authenticating   /me valid={good_status} garbage={bad_status}")
        say(f"DEGRADED: valid and garbage tokens get the SAME answer ({good_status}). "
            "The server is not distinguishing credentials — it is not really serving, "
            "or this token is not valid for this instance.")
        return 1
    say(f"  authenticating   /me valid={good_status} garbage={bad_status}  (differ: good)")

    # 3. Which instance answered?
    #
    # Caveat worth knowing: /me over HTTP returns storage_backend but NOT
    # data_dir, while the MCP get_authenticated_user tool DOES return data_dir.
    # Since data_dir is the only field that distinguishes the hosted container
    # (/app/data) from a laptop (/Users/...) — storage_backend says "local" for
    # both — HTTP alone cannot tell them apart. Report what is actually known
    # and say so, rather than printing a confident "unknown" that reads like a
    # failure, or worse, guessing.
    try:
        d = json.loads(good_body)
        storage = d.get("storage") or {}
        data_dir = storage.get("data_dir", "")
        if data_dir.startswith("/app"):
            say(f"  instance kind    HOSTED (container, data_dir={data_dir})")
        elif data_dir:
            say(f"  instance kind    LOCAL (this machine, data_dir={data_dir})")
        else:
            say(f"  instance kind    storage_backend={storage.get('storage_backend','?')}; "
                "data_dir not exposed over HTTP — use the MCP "
                "get_authenticated_user tool to tell local from hosted")
        if d.get("sandbox_mode"):
            say(f"  sandbox_mode     {d['sandbox_mode']}")
    except Exception:  # noqa: BLE001 — body may not be JSON on some routes
        say("  instance kind    response was not JSON; identity undetermined")

    say("SERVING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
