# Google Maps MCP server

Uses [`google-maps-mcp-server`](https://github.com/david-pivonka/google-maps-mcp-server) (npm) for Places (New), Geocoding, Routes, and related tools.

## Prerequisites

1. **Google Cloud project** with [billing enabled](https://console.cloud.google.com/billing). (The repo default `gcloud` project `ateles2` may not have billing; use another project or link billing before enabling Maps APIs.)
2. **API key** with these APIs enabled (enable only what you need):

   | Use case | Enable in Cloud Console |
   |----------|-------------------------|
   | Places search / nearby / details / photos | **Places API (New)** |
   | Address ↔ coordinates | **Geocoding API** (gcloud: `geocoding-backend.googleapis.com`) |
   | Directions / distance matrix | **Routes API** |
   | Optional tools in server README | Elevation, Time Zone, Geolocation, Roads |

3. **Key restrictions** (recommended): restrict the key to the APIs above and, if possible, to your IP or app.

## Configuration

Add to repo root **`.env`** (never commit the key):

```bash
GOOGLE_MAPS_API_KEY=your_key_here
```

Optional (see upstream README):

- `GOOGLE_MAPS_RATE_LIMIT_ENABLED` (default `true`)
- `GOOGLE_MAPS_RATE_LIMIT_WINDOW_MS`
- `GOOGLE_MAPS_RATE_LIMIT_MAX_REQUESTS`

## Cursor

`.cursor/mcp.json` should contain:

```json
"google-maps": {
  "command": "/Users/markmhendrickson/repos/ateles/mcp/google-maps/run-google-maps-mcp.sh"
}
```

Restart Cursor or reload MCP after changing `.env`.

## Verify

From repo root:

```bash
export $(grep -E '^GOOGLE_MAPS_API_KEY=' .env | xargs)
npx -y google-maps-mcp-server
```

The process should stay running on stdio (no immediate error if the key is valid).
