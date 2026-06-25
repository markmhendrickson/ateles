# Typefully MCP

Connects Cursor (and other MCP clients) to [Typefully](https://typefully.com) for drafting, scheduling, and managing posts (X, LinkedIn, Bluesky, Threads, etc.) via the [Typefully MCP server](https://support.typefully.com/en/articles/13128440-typefully-mcp-server).

## Setup

1. In Typefully: **Settings → API** (or **Integrations**) → create an API key.
2. In the repo root `.env`, add:
   ```bash
   TYPEFULLY_API_KEY=your_key_here
   ```
3. Ensure **Node.js 18+** is available (the wrapper loads `nvm` if present).
4. Cursor already references `mcp/typefully/run-typefully-mcp.sh` in `.cursor/mcp.json`. Reload MCP servers or restart Cursor.

The runner uses `npx -y typefully-mcp-server` ([npm](https://www.npmjs.com/package/typefully-mcp-server)).

### Tweet / X analytics (v2 API)

The npm MCP only calls **v1** draft endpoints and may return **403** with keys that work on **v2**. It does **not** implement analytics.

Post metrics (impressions, likes, reposts, etc.) use:

`GET /v2/social-sets/{social_set_id}/analytics/x/posts?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`

This repo includes a helper (reads repo `.env`):

```bash
python3 mcp/typefully/fetch_x_post_analytics.py --start-date 2026-03-01 --end-date 2026-04-07
```

If the API responds with `MONETIZATION_ERROR`, the Typefully plan does not include analytics; upgrade in Typefully to use this endpoint.

For **all posts** on your X account (not only Typefully-published), use the official X API helper in `mcp/x_api/` (`fetch_user_post_analytics.py`).

## References

- [API improvements for AI and agents](https://typefully.com/changelog/api-improvements-for-ai-and-agents-137) (analytics, queue, LinkedIn org mentions, quote posts)
- [Typefully API docs](https://typefully.com/docs/api)
