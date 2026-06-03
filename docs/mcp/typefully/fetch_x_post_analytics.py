#!/usr/bin/env python3
"""Fetch X post analytics from Typefully API v2 (not exposed by npm typefully-mcp-server).

Requires TYPEFULLY_API_KEY in repo-root .env (same as MCP).

Analytics may return MONETIZATION_ERROR if the Typefully plan does not include analytics.
See: https://typefully.com/docs/api — GET /v2/social-sets/{id}/analytics/x/posts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_dotenv() -> None:
    env_path = repo_root() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def api_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"_http_error": e.code, "_body": body}


def main() -> int:
    parser = argparse.ArgumentParser(description="Typefully v2 X post analytics")
    parser.add_argument(
        "--social-set-id",
        type=int,
        default=None,
        help="Social set id (default: first from /v2/social-sets)",
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="YYYY-MM-DD (social set timezone)",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="YYYY-MM-DD (inclusive)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Page size (max 100)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print JSON only",
    )
    args = parser.parse_args()

    load_dotenv()
    token = os.environ.get("TYPEFULLY_API_KEY", "").strip()
    if not token:
        print("Set TYPEFULLY_API_KEY (e.g. in repo .env).", file=sys.stderr)
        return 1

    base = "https://api.typefully.com"
    sid = args.social_set_id
    if sid is None:
        listing = api_get(f"{base}/v2/social-sets?limit=50", token)
        if "error" in listing:
            print(json.dumps(listing, indent=2))
            return 1
        results = listing.get("results") or []
        if not results:
            print("No social sets found.", file=sys.stderr)
            return 1
        sid = results[0]["id"]

    from urllib.parse import urlencode

    q = urlencode(
        {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "limit": min(max(args.limit, 1), 100),
        }
    )
    url = f"{base}/v2/social-sets/{sid}/analytics/x/posts?{q}"
    data = api_get(url, token)

    if args.raw:
        print(json.dumps(data, indent=2))
        return 0

    if "error" in data:
        err = data["error"]
        print(f"API error: {err.get('code', '')} — {err.get('message', err)}", file=sys.stderr)
        if err.get("code") == "MONETIZATION_ERROR":
            print(
                "Analytics is a paid Typefully feature; upgrade to use this endpoint.",
                file=sys.stderr,
            )
        return 1

    posts = data.get("results") or []
    print(f"Social set {sid} — {args.start_date} .. {args.end_date} — {len(posts)} posts\n")
    for row in posts:
        m = row.get("metrics") or {}
        eng = m.get("engagement") or {}
        preview = (row.get("preview_text") or "")[:80].replace("\n", " ")
        print(
            f"  {row.get('url', row.get('post_id', '?'))}\n"
            f"    impressions={m.get('impressions', '?')}  "
            f"likes={eng.get('likes', '?')}  replies={eng.get('comments', '?')}  "
            f"reposts={eng.get('shares', '?')}  quotes={eng.get('quotes', '?')}  "
            f"engagement_total={eng.get('total', '?')}\n"
            f"    {preview!r}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
