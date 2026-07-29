#!/usr/bin/env python3
"""
slack_cli.py — read-side Slack access for the swarm.

Wraps the Slack Web API the way `gws` wraps Google Workspace: a thin,
scriptable surface over the endpoints agents actually need, with the token
sourced from the environment (materialized from `ateles-private` via
secrets_lib / SOPS) rather than handled here.

Read-only by design: issue #248 scopes this integration to search + channel
history, explicitly excluding send capability. (The watchdog webhook,
OPENCLAW_WATCHDOG_WEBHOOK_URL, remains the path for automated alert posts.)

Scope posture: prefer `search:read.public` (public channels only). Slack's
legacy `search:read` also sweeps in DM content, and this token reads a SHARED
team workspace containing other people's messages — grant the narrowest scope
that does the job.

Usage:
  python slack_cli.py search "manju leads deck" [--count 20] [--json]
  python slack_cli.py history <channel_id> [--limit 50] [--json]
  python slack_cli.py channels [--types public_channel] [--json]
  python slack_cli.py whoami

Environment variables:
  SLACK_USER_TOKEN   Slack user token (xoxp-...), required.
                     User token — not a bot token — because search.messages
                     is only available to user tokens.
  SLACK_BASE_URL     API base (default: https://slack.com/api)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("SLACK_BASE_URL", "https://slack.com/api").rstrip("/")
TOKEN = os.environ.get("SLACK_USER_TOKEN", "").strip()


def _call(method: str, params: dict | None = None) -> dict:
    """GET a Slack Web API method. Raises on transport or API-level error."""
    if not TOKEN:
        raise SystemExit(
            "SLACK_USER_TOKEN is not set. Materialize it from ateles-private "
            "(see docs/slack_integration.md) before running."
        )
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{BASE_URL}/{method}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        raise SystemExit(f"HTTP {exc.code} calling {method}: {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        raise SystemExit(f"Network error calling {method}: {exc.reason}") from exc

    return _check(payload, method)


def _check(payload: dict, method: str) -> dict:
    """Shared Slack API-level error handling for _call."""
    if not payload.get("ok"):
        err = payload.get("error", "unknown_error")
        hint = ""
        if err == "missing_scope":
            hint = (
                f" — token lacks a required scope (needed: {payload.get('needed')}, "
                f"has: {payload.get('provided')})"
            )
        elif err in ("invalid_auth", "not_authed", "token_revoked"):
            hint = " — token is invalid or revoked; re-issue it in the Slack app config"
        elif err == "not_in_channel":
            hint = " — the token's user is not a member of that channel; join it first"
        elif err == "channel_not_found":
            hint = " — no such channel id, or the token's user can't see it"
        raise SystemExit(f"Slack API error on {method}: {err}{hint}")
    return payload


def cmd_whoami(args: argparse.Namespace) -> int:
    data = _call("auth.test")
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"user:      {data.get('user')} ({data.get('user_id')})")
        print(f"team:      {data.get('team')} ({data.get('team_id')})")
        print(f"url:       {data.get('url')}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    data = _call(
        "search.messages",
        {"query": args.query, "count": args.count, "sort": args.sort},
    )
    matches = (data.get("messages") or {}).get("matches") or []
    if args.json:
        print(json.dumps(matches, indent=2))
        return 0
    total = (data.get("messages") or {}).get("total", len(matches))
    print(f"{len(matches)} shown of {total} match(es) for {args.query!r}\n")
    for m in matches:
        chan = (m.get("channel") or {}).get("name") or (m.get("channel") or {}).get("id", "?")
        who = m.get("username") or m.get("user") or "?"
        text = " ".join((m.get("text") or "").split())
        print(f"#{chan} | {who} | {m.get('ts','')}")
        print(f"  {text[:280]}")
        if m.get("permalink"):
            print(f"  {m['permalink']}")
        print()
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    data = _call(
        "conversations.history",
        {"channel": args.channel, "limit": args.limit},
    )
    msgs = data.get("messages") or []
    if args.json:
        print(json.dumps(msgs, indent=2))
        return 0
    print(f"{len(msgs)} message(s) in {args.channel}\n")
    for m in msgs:
        text = " ".join((m.get("text") or "").split())
        print(f"{m.get('ts','')} | {m.get('user') or m.get('bot_id') or '?'}")
        print(f"  {text[:280]}")
        files = m.get("files") or []
        for f in files:
            print(f"  FILE: {f.get('name')} ({f.get('filetype')}) {f.get('permalink','')}")
        print()
    return 0


def cmd_channels(args: argparse.Namespace) -> int:
    data = _call(
        "conversations.list",
        {"types": args.types, "limit": args.limit, "exclude_archived": "true"},
    )
    chans = data.get("channels") or []
    if args.json:
        print(json.dumps(chans, indent=2))
        return 0
    for c in sorted(chans, key=lambda x: x.get("name", "")):
        member = "member" if c.get("is_member") else "-"
        print(f"{c.get('id')}  {member:6}  #{c.get('name')}")
    print(f"\n{len(chans)} channel(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slack_cli.py", description=__doc__.splitlines()[1])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="Search messages (needs search:read.public)")
    s.add_argument("query")
    s.add_argument("--count", type=int, default=20)
    s.add_argument("--sort", default="timestamp", choices=["score", "timestamp"])
    s.add_argument("--json", action="store_true", help="Emit raw JSON")
    s.set_defaults(func=cmd_search)

    h = sub.add_parser("history", help="Read a channel's recent messages")
    h.add_argument("channel", help="Channel ID (e.g. C0123ABC)")
    h.add_argument("--limit", type=int, default=50)
    h.add_argument("--json", action="store_true", help="Emit raw JSON")
    h.set_defaults(func=cmd_history)

    c = sub.add_parser("channels", help="List channels")
    c.add_argument("--types", default="public_channel")
    c.add_argument("--limit", type=int, default=200)
    c.add_argument("--json", action="store_true", help="Emit raw JSON")
    c.set_defaults(func=cmd_channels)

    w = sub.add_parser("whoami", help="Verify the token (auth.test)")
    w.add_argument("--json", action="store_true", help="Emit raw JSON")
    w.set_defaults(func=cmd_whoami)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
