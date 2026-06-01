# TP-Link Router Configuration for HomeKit mDNS

Step-by-step instructions for configuring TP-Link routers to enable HomeKit automatic discovery.

## Step 1: Access TP-Link Router Admin

1. **Open a web browser**
2. **Navigate to**: `http://tplinkwifi.net` or `http://192.168.0.1`
3. **Login** with your admin credentials
   - Default username: `admin`
   - Default password: `admin` (or check router sticker)

## Step 2: Enable IGMP Snooping

IGMP Snooping allows multicast traffic (mDNS) to propagate across your network.

### For TP-Link Archer Series / Deco Series:

1. **Go to**: "Advanced" → "Network" → "IGMP Snooping"
2. **Enable**: "IGMP Snooping" ✅
3. **Click**: "Save"

### Alternative Path (some models):

1. **Go to**: "Advanced" → "System Tools" → "IGMP Settings"
2. **Enable**: "IGMP Snooping" ✅
3. **Click**: "Save"

## Step 3: Enable UPnP

UPnP helps with device discovery and automatic port forwarding.

1. **Go to**: "Advanced" → "NAT Forwarding" → "UPnP"
2. **Enable**: "UPnP" ✅
3. **Click**: "Save"

### Alternative Path (some models):

1. **Go to**: "Advanced" → "System Tools" → "UPnP"
2. **Enable**: "UPnP" ✅
3. **Click**: "Save"

## Step 4: Check Firewall Settings

Ensure UDP port 5353 (mDNS) is not blocked.

1. **Go to**: "Advanced" → "Security" → "Firewall"
2. **Check**: Firewall is enabled (should be ✅)
3. **Go to**: "Advanced" → "Security" → "Access Control" (if available)
4. **Ensure**: No rules blocking UDP port 5353
5. **Ensure**: Multicast traffic (224.0.0.0/4) is allowed

## Step 5: Enable mDNS (if available)

Some newer TP-Link models have explicit mDNS settings.

1. **Go to**: "Advanced" → "System Tools" → "mDNS"
2. **If you see mDNS option**: Enable it ✅
3. **Click**: "Save"

**Note**: Not all TP-Link models have this option. If you don't see it, IGMP Snooping + UPnP should be sufficient.

## Step 6: For Multiple TP-Link Routers (Mesh/AP Mode)

If you have multiple TP-Link routers:

### Option A: Using TP-Link Deco Mesh System

1. **Open TP-Link Deco app**
2. **Go to**: "More" → "Advanced" → "IPv6" (if available)
3. **Enable**: IPv6 (helps with multicast)
4. **Go to**: "More" → "Advanced" → "UPnP"
5. **Enable**: UPnP ✅
6. **Note**: Deco systems handle mDNS automatically, but ensure all nodes are on same network

### Option B: Using TP-Link Routers as Access Points

1. **Configure main router** with steps above
2. **For secondary routers**:
   - Set to "Access Point Mode" (not Router Mode)
   - Connect via Ethernet to main router
   - This avoids double NAT and simplifies mDNS

### Option C: Multiple Routers in Router Mode

If you must use multiple routers in router mode:

1. **Configure ALL routers** with IGMP Snooping + UPnP
2. **Ensure all routers are on same subnet** (192.168.0.x)
3. **Use Ethernet backhaul** between routers (more reliable)
4. **Disable DHCP on secondary routers** (let main router handle it)

## Step 7: Apply Changes and Restart

1. **Click "Save"** on all pages where you made changes
2. **Go to**: "System Tools" → "Reboot"
3. **Click**: "Reboot"
4. **Wait 2-3 minutes** for router to fully restart

## Step 8: Test mDNS Discovery

### Test from Mac:

```bash
# Install Bonjour tools (if needed)
brew install bonjour

# Test mDNS discovery
dns-sd -B _hap._tcp
```

You should see HomeKit devices appear. Press `Ctrl+C` to stop.

### Test from iPhone/iPad:

1. **Open Home app**
2. **Tap "+" → "Add Accessory"**
3. **You should see "HASS Bridge" appear automatically**
4. **If it appears, mDNS is working! ✅**

## Troubleshooting TP-Link Specific Issues

### Issue: Can't find IGMP Snooping option

**Solution**:
- Some older TP-Link models don't have IGMP Snooping
- Enable UPnP instead (it helps with discovery)
- Use manual IP pairing: `192.168.0.252:21064`

### Issue: Settings don't save

**Solution**:
- Make sure you're logged in as admin (not guest)
- Try a different browser (Chrome, Firefox, Safari)
- Clear browser cache and try again

### Issue: Router firmware is old

**Solution**:
1. **Check current firmware**: "System Tools" → "Firmware Upgrade"
2. **Download latest firmware** from TP-Link website
3. **Upgrade firmware** (this may add mDNS support)
4. **Reconfigure settings** after upgrade

### Issue: Multiple TP-Link routers not working

**Solution**:
1. **Set secondary routers to Access Point Mode**
2. **Connect via Ethernet** (not WiFi)
3. **Disable DHCP on secondary routers**
4. **Configure main router only** with IGMP Snooping

### Issue: Still can't discover devices

**Solution**:
1. **Try manual pairing first** (proves HomeKit works):
   - IP: `192.168.0.252:21064`
   - Code: `471-42-540`
2. **Check if all devices are on same network**:
   - Mac: `192.168.0.252`
   - Router: `192.168.0.1`
   - iPhone: Should also be `192.168.0.x`
3. **Check router logs**: "System Tools" → "System Log"
   - Look for blocked multicast traffic

## TP-Link Model-Specific Notes

### Archer Series (AX/AXE models):
- Usually have IGMP Snooping in "Advanced" → "Network"
- Good mDNS support
- May have explicit "mDNS" option

### Deco Series (Mesh):
- Handles mDNS automatically
- Ensure all nodes are on same network
- Use Ethernet backhaul for best results

### Older TP-Link Models:
- May not have IGMP Snooping
- Enable UPnP as alternative
- Use manual IP pairing if needed

## Quick Reference: Settings to Enable

✅ **IGMP Snooping**: "Advanced" → "Network" → "IGMP Snooping"
✅ **UPnP**: "Advanced" → "NAT Forwarding" → "UPnP"
✅ **mDNS** (if available): "Advanced" → "System Tools" → "mDNS"
✅ **Firewall**: Ensure UDP 5353 not blocked

## After Configuration

1. **Restart router**
2. **Wait 2-3 minutes**
3. **Test mDNS discovery** (see Step 8)
4. **If working**: HomeKit devices should appear automatically in Home app
5. **If not working**: Use manual IP pairing as workaround

## Need More Help?

- Check TP-Link support: https://www.tp-link.com/support/
- TP-Link community forum: https://community.tp-link.com/
- See `ROUTER_MDNS_CONFIGURATION.md` for general troubleshooting
