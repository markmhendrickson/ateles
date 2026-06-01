# HomeKit Integration Network Troubleshooting

## Problem: Can't Connect HomeKit Integration with Multiple Routers

HomeKit uses mDNS/Bonjour for device discovery, which requires multicast DNS to work across your network. Multiple routers or VLANs can break mDNS discovery.

## Solutions

### Solution 1: Enable mDNS Forwarding on Routers (Recommended)

If you have multiple routers, you need to enable mDNS forwarding between them:

1. **Enable IGMP Snooping/Proxy** on your routers:
   - This allows multicast traffic (mDNS) to propagate between network segments
   - Look for "IGMP Snooping" or "Multicast" settings in router admin
   - Enable it on all routers

2. **Enable mDNS/Bonjour Forwarding**:
   - Some routers have explicit "mDNS" or "Bonjour" forwarding options
   - Enable on all routers in your network

3. **Check Firewall Rules**:
   - Ensure UDP port 5353 (mDNS) is not blocked
   - Allow multicast traffic between network segments

### Solution 2: Use Manual IP-Based Pairing

If mDNS doesn't work, you can pair manually using the IP address:

1. **Find Home Assistant's IP address**:
   ```bash
   docker exec homeassistant hostname -I
   # Or check your Mac's IP:
   ifconfig | grep "inet " | grep -v 127.0.0.1
   ```

2. **In Home app on iPhone/iPad**:
   - Tap "+" to add accessory
   - Tap "I Don't Have a Code or Cannot Scan"
   - Enter the IP address manually: `[home-assistant-ip]:21064`
   - Enter the pairing code: `471-42-540` (from your notification)

### Solution 3: Configure Zeroconf to Use Specific Interface

If Home Assistant can't discover the right network interface:

1. **Add to `configuration.yaml`**:
   ```yaml
   zeroconf:
     default_interface: true
     # Or specify interface:
     # ipv6: false
   ```

2. **Restart Home Assistant**

### Solution 4: Use Docker Host Network Mode (Linux Only)

On Linux, you can use host networking:
```bash
docker run -d \
  --name homeassistant \
  --network=host \
  ...
```

**Note**: This doesn't work on macOS/Docker Desktop. On Mac, use port mapping instead.

### Solution 5: Ensure Devices Are on Same Network Segment

For initial pairing, ensure:
- iPhone/iPad is on the same network segment as Home Assistant
- Both devices can reach each other (ping test)
- No firewall blocking port 21064

### Solution 6: Restart HomeKit Integration

Sometimes restarting the integration helps:

1. In Home Assistant: Settings → Devices & Services → HomeKit
2. Click on "HASS Bridge:21064"
3. Click "Disable" then "Enable"
4. Try pairing again

## Testing mDNS Discovery

Test if mDNS is working:

1. **From Mac terminal**:
   ```bash
   # Install Bonjour browser (optional)
   brew install bonjour

   # Test discovery
   dns-sd -B _hap._tcp
   ```

2. **From iPhone/iPad**:
   - Open Home app
   - Tap "+" → "Add Accessory"
   - You should see "HASS Bridge" appear in the list
   - If it doesn't appear, mDNS isn't working

## Alternative: Use Home Assistant Cloud

If local HomeKit pairing continues to fail, consider:

1. **Home Assistant Cloud** (Nabu Casa):
   - Provides remote access
   - Can expose HomeKit devices via cloud
   - Requires subscription but bypasses local network issues

2. **Home Assistant Remote UI**:
   - Access Home Assistant remotely
   - Control devices via web interface
   - Doesn't require HomeKit integration

## Current Configuration

Your HomeKit integration is configured as:
- **Name**: HASS Bridge
- **Port**: 21064
- **Pairing Code**: 471-42-540 (from notification)
- **Mode**: Bridge (exposes Home Assistant devices to HomeKit)

## Next Steps

1. Try Solution 2 (Manual IP pairing) first - it's the quickest
2. If that works, then work on fixing mDNS (Solution 1) for automatic discovery
3. Check router settings for mDNS/IGMP forwarding
4. Ensure all devices are on the same network segment for initial pairing
