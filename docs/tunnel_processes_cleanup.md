# Tunnel-related processes (summary and cleanup)

## Current layout

| What | Purpose |
|------|--------|
| **API on :3180** (node 46145) | Neotoma prod API started with ateles `.env` → `NEOTOMA_DATA_DIR=/Users/markmhendrickson/Documents/data`, `neotoma.prod.db`. This is what **neotoma.markmhendrickson.com** should use. |
| **cloudflared tunnel run mcp-servers** | Forwards **neotoma.markmhendrickson.com** → http://localhost:3180. You have **two** processes; one is enough. |
| **ngrok** (PID in `/tmp/ngrok-mcp.pid`) | Started by `watch:prod:tunnel`. Writes URL to `/tmp/ngrok-mcp-url.txt`. **Redundant** with Cloudflare for the public hostname. |
| **watch:prod:tunnel** (concurrently) | Runs: (1) `setup-https-tunnel.sh` → ngrok or cloudflare, (2) `run-dev-server-with-tunnel-url.sh` → API (tsx watch). Uses **neotoma repo default data dir** (or neotoma `.env`), **not** ateles `NEOTOMA_DATA_DIR`. If 3180 was already taken, this stack’s API may be on 3181. |
| **cloudflared --url localhost:8080 / 8081** | Separate quick tunnels (e.g. other apps). Not for Neotoma. |

## Redundancy

- **Ngrok** is redundant for **neotoma.markmhendrickson.com**; Cloudflare tunnel is the one in use for that hostname.
- **Two** `cloudflared tunnel run mcp-servers` are redundant; a single process is enough.

## Recommended cleanup (optional)

1. **Stop ngrok** (so only Cloudflare serves the public URL):
   ```bash
   [ -f /tmp/ngrok-mcp.pid ] && kill $(cat /tmp/ngrok-mcp.pid) 2>/dev/null; rm -f /tmp/ngrok-mcp.pid /tmp/ngrok-mcp-url.txt
   ```

2. **Run only one cloudflared mcp-servers** (kill the duplicate):
   ```bash
   pkill -f "cloudflared tunnel run mcp-servers"
   nohup cloudflared tunnel run mcp-servers >> /tmp/cloudflared-mcp-servers.log 2>&1 &
   ```

3. **If you want a single prod stack** (API + Cloudflare only, no ngrok / no watch:prod:tunnel):
   - Stop any `watch:prod:tunnel` / `watch:prod` runs (close the terminal or kill those npm/node processes).
   - Start API with ateles env:  
     `cd neotoma && set -a && source /Users/markmhendrickson/repos/ateles/.env && set +a && npm run start:api:prod`
   - Start tunnel once:  
     `cloudflared tunnel run mcp-servers`

Then **neotoma.markmhendrickson.com** → Cloudflare → **one** API on 3180 using **Documents/data/neotoma.prod.db**.
