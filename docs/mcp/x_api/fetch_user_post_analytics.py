#!/usr/bin/env python3
"""Fetch post analytics from the X API v2 user timeline.

Docs: https://docs.x.com/x-api/introduction
Timeline: GET /2/users/:id/tweets — see authentication mapping for Bearer vs user context.

- App Bearer: public_metrics for tweets the product allows you to read.
- OAuth 2 user access token (optional): non_public_metrics for the authenticated user's own tweets.

Requires X_API_BEARER_TOKEN in repo-root .env (or environment).
Optional: X_API_USER_ACCESS_TOKEN when using --private-metrics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.x.com/2"


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


def api_get(url: str, bearer: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            err = json.loads(body)
        except json.JSONDecodeError:
            err = {"title": str(e.code), "detail": body[:500]}
        err["_http_status"] = e.code
        return err


def user_id_by_username(username: str, bearer: str) -> str | None:
    u = urllib.parse.quote(username.lstrip("@"), safe="")
    data = api_get(f"{API_BASE}/users/by/username/{u}?user.fields=id,username,name", bearer)
    if "data" in data and "id" in data["data"]:
        return str(data["data"]["id"])
    return None


def users_me(bearer: str) -> dict | None:
    data = api_get(f"{API_BASE}/users/me?user.fields=id,username,name", bearer)
    return data.get("data")


def fetch_timeline(
    user_id: str,
    bearer: str,
    *,
    max_pages: int,
    start_time: str | None,
    end_time: str | None,
    private_metrics: bool,
) -> list[dict]:
    fields = [
        "created_at",
        "id",
        "text",
        "public_metrics",
        "conversation_id",
        "referenced_tweets",
    ]
    if private_metrics:
        fields.append("non_public_metrics")

    params: dict[str, str] = {
        "max_results": "100",
        "tweet.fields": ",".join(fields),
    }
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time

    out: list[dict] = []
    next_token: str | None = None
    for _ in range(max_pages):
        p = dict(params)
        if next_token:
            p["pagination_token"] = next_token
        q = urllib.parse.urlencode(p)
        url = f"{API_BASE}/users/{user_id}/tweets?{q}"
        data = api_get(url, bearer)
        if "errors" in data or "title" in data:
            data["_fetch_failed"] = True
            if not out:
                raise RuntimeError(json.dumps(data, indent=2))
            break
        batch = data.get("data") or []
        out.extend(batch)
        meta = data.get("meta") or {}
        next_token = meta.get("next_token")
        if not next_token:
            break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="X API v2: user tweet timeline with public (and optional non-public) metrics",
    )
    parser.add_argument("--username", required=True, help="X handle without @")
    parser.add_argument("--max-pages", type=int, default=5, help="Max pagination pages (100 tweets each)")
    parser.add_argument("--start-time", default=None, help="ISO8601 UTC, e.g. 2026-03-01T00:00:00Z")
    parser.add_argument("--end-time", default=None, help="ISO8601 UTC")
    parser.add_argument(
        "--private-metrics",
        action="store_true",
        help="Request non_public_metrics (needs X_API_USER_ACCESS_TOKEN for your own account)",
    )
    parser.add_argument("--raw", action="store_true", help="Print JSON array only")
    args = parser.parse_args()

    load_dotenv()
    app_bearer = (os.environ.get("X_API_BEARER_TOKEN") or "").strip()
    user_token = (os.environ.get("X_API_USER_ACCESS_TOKEN") or "").strip()

    if not app_bearer:
        print("Set X_API_BEARER_TOKEN (e.g. in repo .env). See mcp/x_api/README.md", file=sys.stderr)
        return 1

    username = args.username.lstrip("@")
    uid = user_id_by_username(username, app_bearer)
    if not uid:
        err = api_get(
            f"{API_BASE}/users/by/username/{urllib.parse.quote(username, safe='')}",
            app_bearer,
        )
        print(f"Could not resolve username @{username}:\n{json.dumps(err, indent=2)}", file=sys.stderr)
        return 1

    timeline_bearer = user_token if args.private_metrics else app_bearer
    if args.private_metrics:
        if not user_token:
            print("--private-metrics requires X_API_USER_ACCESS_TOKEN in .env", file=sys.stderr)
            return 1
        me = users_me(user_token)
        if not me or str(me.get("id")) != uid:
            print(
                "X_API_USER_ACCESS_TOKEN must be for the same account as --username "
                f"(token user id {me.get('id') if me else None} != timeline {uid}).",
                file=sys.stderr,
            )
            return 1

    try:
        tweets = fetch_timeline(
            uid,
            timeline_bearer,
            max_pages=max(1, args.max_pages),
            start_time=args.start_time,
            end_time=args.end_time,
            private_metrics=args.private_metrics,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.raw:
        print(json.dumps(tweets, indent=2))
        return 0

    print(f"User @{username} (id={uid}) — {len(tweets)} tweets\n")
    for t in tweets:
        pm = t.get("public_metrics") or {}
        npm = t.get("non_public_metrics") or {}
        tid = t.get("id", "")
        line1 = (
            f"  https://x.com/{username}/status/{tid}\n"
            f"    {t.get('created_at', '')}\n"
            f"    likes={pm.get('like_count', '?')} reposts={pm.get('retweet_count', '?')} "
            f"replies={pm.get('reply_count', '?')} quotes={pm.get('quote_count', '?')}"
        )
        if npm:
            line1 += f" impressions={npm.get('impression_count', npm)}"
        preview = (t.get("text") or "")[:100].replace("\n", " ")
        print(f"{line1}\n    {preview!r}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
