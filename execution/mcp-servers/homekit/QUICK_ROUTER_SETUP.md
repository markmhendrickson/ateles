# Quick Router Setup for HomeKit mDNS

## Step 1: Identify Your Router

Your router's IP address is: **192.168.0.1**

To identify your router brand:

1. **Open a web browser**
2. **Navigate to**: `http://192.168.0.1` or `http://router.local`
3. **Look at the login page** - it will show the router brand/model
4. **Or check the physical router** - the brand/model is usually on a sticker

Common router admin URLs:
- `http://192.168.0.1`
- `http://192.168.1.1`
- `http://router.local`
- `http://routerlogin.net` (Netgear)
- `http://tplinkwifi.net` (TP-Link)
- `http://myrouter.local` (Linksys)

## Step 2: Access Router Admin

1. **Open browser**: `http://192.168.0.1`
2. **Login** with admin credentials (check router sticker if you don't know them)
3. **Look for these settings** (names vary by brand):

## Step 3: Enable Required Settings

### What to Enable (in order of importance):

1. **IGMP Snooping** or **IGMP Proxy**
   - **Why**: Allows multicast traffic (mDNS) to propagate
   - **Where to find**: Usually in "Advanced" → "Network" or "LAN Settings"
   - **Enable it**: ✅

2. **Multicast Forwarding** or **Multicast Routing**
   - **Why**: Forwards multicast packets between network segments
   - **Where to find**: "Advanced" → "Network" or "Routing"
   - **Enable it**: ✅

3. **UPnP (Universal Plug and Play)**
   - **Why**: Helps with device discovery
   - **Where to find**: "Advanced" → "NAT" or "Port Forwarding"
   - **Enable it**: ✅

4. **mDNS** or **Bonjour** (if available)
   - **Why**: Explicit mDNS forwarding
   - **Where to find**: "Advanced" → "Services" or "Network"
   - **Enable it**: ✅

5. **Firewall Rules**
   - **Ensure UDP port 5353 is NOT blocked**
   - **Allow multicast traffic** (224.0.0.0/4)

## Step 4: Apply and Restart

1. **Click "Save" or "Apply"**
2. **Restart the router** (usually in "Administration" → "Reboot")
3. **Wait 2-3 minutes** for router to fully restart

## Step 5: Test mDNS

### Quick Test from Mac:

```bash
# Test if mDNS is working
dns-sd -B _hap._tcp
```

You should see HomeKit devices appear. Press `Ctrl+C` to stop.

### Test from iPhone/iPad:

1. **Open Home app**
2. **Tap "+" → "Add Accessory"**
3. **You should see "HASS Bridge" appear automatically**
4. **If it appears, mDNS is working! ✅**

## If You Have Multiple Routers

If you have multiple routers (main router + access points):

1. **Configure ALL routers** with the same settings
2. **Ensure they're on the same network segment** (192.168.0.x)
3. **Use Ethernet backhaul** if possible (more reliable)
4. **Or configure as access points** (not routers) to avoid double NAT

## Common Router Brands - Quick Reference

### If you see "Netgear":
- Go to: "Advanced" → "Advanced Setup" → "Wireless Settings"
- Enable: "IGMP Snooping"
- Go to: "Advanced" → "Advanced Setup" → "UPnP"
- Enable: "Turn UPnP On"

### If you see "TP-Link":
- Go to: "Advanced" → "Network" → "IGMP Snooping"
- Enable: "IGMP Snooping"
- Go to: "Advanced" → "NAT Forwarding" → "UPnP"
- Enable: "UPnP"

### If you see "ASUS":
- Go to: "Advanced Settings" → "LAN" → "IPTV"
- Enable: "Enable multicast routing (IGMP Proxy)"
- Go to: "Advanced Settings" → "WAN" → "NAT Passthrough"
- Enable: "UPnP"

### If you see "Linksys":
- Go to: "Connectivity" → "Local Network"
- Enable: "IGMP Snooping"
- Go to: "Connectivity" → "Administration" → "UPnP"
- Enable: "UPnP"

### If you see "Google" or "Nest":
- Open Google Home app
- Go to: "WiFi" → "Settings" → "Advanced networking"
- Enable: "IPv6" and "UPnP"
- Note: Limited mDNS support, may need manual pairing

### If you see "Eero":
- Eero handles mDNS automatically
- Ensure all nodes are on same network
- Use Ethernet backhaul if possible

## Still Not Working?

1. **Try manual pairing first** (proves HomeKit works):
   - Use IP: `192.168.0.252:21064`
   - Code: `471-42-540`

2. **Check if all devices are on same network**:
   - Mac: `192.168.0.252`
   - Router: `192.168.0.1`
   - iPhone should also be on `192.168.0.x`

3. **Check router logs** for blocked multicast traffic

4. **Consider using a dedicated mDNS reflector** (see ROUTER_MDNS_CONFIGURATION.md)

## Need More Help?

See `ROUTER_MDNS_CONFIGURATION.md` for detailed instructions for specific router brands.
