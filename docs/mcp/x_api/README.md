# X API — post analytics (all posts on a timeline)

Use the official [X API](https://docs.x.com/x-api/introduction) to pull **your account’s posts** with engagement fields. This complements Typefully analytics (which is scoped to posts published through Typefully).

## Pricing and access

X API is **pay-per-usage** (credits in the [developer console](https://console.x.com)). There are no subscriptions in the model described in the [intro docs](https://docs.x.com/x-api/introduction); costs depend on which endpoints you call and volume.

## What you can get

| Auth | Metrics | Scope |
|------|---------|--------|
| **App Bearer token** (`X_API_BEARER_TOKEN`) | `public_metrics` (likes, reposts, replies, quotes, bookmarks where exposed) | `GET /2/users/:id/tweets` for a user ID your product can read (see [authentication mapping](https://docs.x.com/fundamentals/authentication/guides/v2-authentication-mapping)). |
| **OAuth 2 user access token** (`X_API_USER_ACCESS_TOKEN`, scopes `tweet.read` `users.read`) | Adds **`non_public_metrics`** (e.g. impression counts) for **the authenticated user’s own** posts, subject to X rules and retention windows. | Same timeline endpoint, but authorize as the account that owns the tweets. |

Impressions and other non-public fields are **not** available with app-only auth for arbitrary accounts.

## Setup

1. Open [console.x.com](https://console.x.com), create a project/app, and buy **API credits** if required for your use case.
2. Under **Keys and tokens**, create a **Bearer Token** (app-only).
3. (Optional) For private metrics on **your** posts, complete **OAuth 2.0** user authorization with PKCE, scopes `tweet.read` and `users.read`, and store the resulting **access token** (refresh flow is your responsibility for long-running use).
4. In repo root `.env`:

```bash
# Required for the default script path
X_API_BEARER_TOKEN=your_bearer_token

# Optional: OAuth 2 user access token for non_public_metrics on your own tweets
# X_API_USER_ACCESS_TOKEN=your_user_access_token
```

## Usage

```bash
# Public metrics, paginate recent posts (default up to 5 pages × 100)
python3 mcp/x_api/fetch_user_post_analytics.py --username markymark

# More history
python3 mcp/x_api/fetch_user_post_analytics.py --username markymark --max-pages 15

# Optional time window (ISO 8601 UTC)
python3 mcp/x_api/fetch_user_post_analytics.py --username markymark \
  --start-time 2026-03-01T00:00:00Z --end-time 2026-04-08T00:00:00Z

# Include non_public_metrics (requires X_API_USER_ACCESS_TOKEN; must be your own account)
python3 mcp/x_api/fetch_user_post_analytics.py --username markymark --private-metrics

# JSON output
python3 mcp/x_api/fetch_user_post_analytics.py --username markymark --raw
```

If `--private-metrics` is set, the script uses `X_API_USER_ACCESS_TOKEN` for timeline requests and checks that the resolved user matches `GET /2/users/me` (so the token is for the same account as `--username`).

## API base

The script calls `https://api.x.com/2/...` (see [X API](https://docs.x.com/x-api/introduction)).
