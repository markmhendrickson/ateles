# Cloudflare Tunnel Troubleshooting

## Error 1033: Hostname Not Resolved

**Symptom:** `error code: 1033` when accessing tunnel URL

**Cause:** Cloudflare cannot resolve the hostname to the tunnel. This typically happens when:
1. Domain is managed by external DNS (e.g., DNSimple) instead of Cloudflare DNS
2. Hostname is not properly linked to the tunnel
3. DNS record exists but tunnel doesn't recognize the hostname

## Solutions

### Option 1: Add Domain to Cloudflare (Recommended)

For full Cloudflare features (proxy, SSL, etc.):

1. **Add domain to Cloudflare:**
   - Go to Cloudflare Dashboard → Add a Site
   - Enter `neotoma.io`
   - Choose Free plan
   - Cloudflare will scan existing DNS records

2. **Update nameservers in DNSimple:**
   - Copy nameservers from Cloudflare (e.g., `alice.ns.cloudflare.com`, `bob.ns.cloudflare.com`)
   - In DNSimple, go to `neotoma.io` → DNS → Nameservers
   - Update to Cloudflare nameservers

3. **Verify DNS records:**
   - Cloudflare will import existing records
   - Ensure `dev` CNAME points to `64cffaf9-7704-4d12-9b35-436c31be34f6.cfargotunnel.com`
   - Enable proxy (orange cloud)

4. **Register route with tunnel:**
   ```bash
   cloudflared tunnel route dns mcp-servers dev.neotoma.io
   ```

### Option 2: Keep DNSimple DNS (Current Setup)

If you want to keep DNS in DNSimple:

1. **Ensure CNAME record exists:**
   - Type: CNAME
   - Name: `dev`
   - Target: `64cffaf9-7704-4d12-9b35-436c31be34f6.cfargotunnel.com`
   - TTL: 3600

2. **Add hostname to tunnel in Cloudflare Dashboard:**
   - Go to Cloudflare Dashboard → Zero Trust → Networks → Tunnels
   - Select `mcp-servers` tunnel
   - Go to Public Hostnames tab
   - Click "Add a public hostname"
   - Enter: `dev.neotoma.io`
   - Path: `/mcp/*`
   - Service: `http://localhost:8080`
   - Save

3. **Restart tunnel:**
   ```bash
   pkill -f "cloudflared tunnel run mcp-servers"
   cloudflared tunnel run mcp-servers
   ```

### Option 3: Use Cloudflare DNS for Subdomain Only

Create a subdomain delegation:

1. **In DNSimple:**
   - Add NS record: `dev.neotoma.io` → Cloudflare nameservers
   - Or use CNAME: `dev.neotoma.io` → `64cffaf9-7704-4d12-9b35-436c31be34f6.cfargotunnel.com`

2. **In Cloudflare:**
   - Add `dev.neotoma.io` as a site (or use DNS-only)
   - Create CNAME record pointing to tunnel

## Verification Steps

1. **Check tunnel status:**
   ```bash
   cloudflared tunnel list
   cloudflared tunnel info mcp-servers
   ```

2. **Check DNS resolution:**
   ```bash
   dig +short dev.neotoma.io
   # Should return Cloudflare IPs (if proxied) or tunnel CNAME
   ```

3. **Check local service:**
   ```bash
   curl http://localhost:8080/
   # Should return 401 Unauthorized (expected)
   ```

4. **Test tunnel:**
   ```bash
   curl https://dev.neotoma.io/mcp/
   # Should return 401 Unauthorized (not error 1033)
   ```

## Common Issues

### Tunnel Running But Error 1033

- **Check:** Is hostname added to tunnel in Cloudflare dashboard?
- **Fix:** Add hostname via Zero Trust → Networks → Tunnels → Public Hostnames

### DNS Resolves But Tunnel Fails

- **Check:** Is the tunnel process running?
- **Fix:** Restart tunnel: `cloudflared tunnel run mcp-servers`

### Config File Changes Not Applied

- **Check:** Did you restart the tunnel after config changes?
- **Fix:** Kill and restart: `pkill -f "cloudflared tunnel run mcp-servers" && cloudflared tunnel run mcp-servers`

## Current Status

- ✅ Tunnel created: `mcp-servers` (64cffaf9-7704-4d12-9b35-436c31be34f6)
- ✅ DNS record created in DNSimple: `dev.neotoma.io` → tunnel CNAME
- ✅ Config file updated with ingress rules
- ⚠️ Hostname needs to be added to tunnel in Cloudflare dashboard (for DNSimple-managed domains)

## Next Steps

1. Add hostname to tunnel in Cloudflare Dashboard (Zero Trust → Networks → Tunnels)
2. Restart tunnel: `cloudflared tunnel run mcp-servers`
3. Test: `curl https://dev.neotoma.io/mcp/`
