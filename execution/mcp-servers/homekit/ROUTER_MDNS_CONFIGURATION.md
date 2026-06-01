# Router Configuration for HomeKit mDNS/Bonjour

This guide helps you configure your routers to enable mDNS (multicast DNS) forwarding, which is required for HomeKit automatic device discovery across multiple routers or network segments.

## What Needs to Be Enabled

HomeKit uses **mDNS (multicast DNS)** on **UDP port 5353** for device discovery. For this to work across multiple routers, you need:

1. **IGMP Snooping/Proxy** - Allows multicast traffic to propagate
2. **mDNS/Bonjour Forwarding** - Specifically forwards mDNS packets
3. **Multicast Forwarding** - General multicast support
4. **Firewall Rules** - Ensure UDP 5353 is not blocked

## Router-Specific Instructions

### Apple AirPort Routers

1. **Open AirPort Utility**
2. **Select your base station**
3. **Click "Edit"**
4. **Go to "Network" tab**
5. **Enable "Enable multicast forwarding"**
6. **Click "Update"**

### Google Nest WiFi / Google WiFi

1. **Open Google Home app**
2. **Tap "WiFi" → "Settings" → "Advanced networking"**
3. **Enable "IPv6"** (helps with multicast)
4. **Enable "UPnP"** (Universal Plug and Play)
5. **Note**: Google WiFi has limited mDNS support. Consider using a dedicated mDNS reflector.

### Netgear Routers

1. **Access router admin**: `http://routerlogin.net` or `192.168.1.1`
2. **Go to "Advanced" → "Advanced Setup" → "Wireless Settings"**
3. **Enable "Enable IGMP Snooping"**
4. **Go to "Advanced" → "Advanced Setup" → "UPnP"**
5. **Enable "Turn UPnP On"**
6. **Some models**: Look for "Multicast" or "mDNS" in Advanced settings

### TP-Link Routers

1. **Access router admin**: `http://tplinkwifi.net` or `192.168.0.1`
2. **Go to "Advanced" → "Network" → "IGMP Snooping"**
3. **Enable "IGMP Snooping"**
4. **Go to "Advanced" → "NAT Forwarding" → "UPnP"**
5. **Enable "UPnP"**
6. **Some models**: "Advanced" → "System Tools" → "mDNS" (if available)

### ASUS Routers

1. **Access router admin**: `http://router.asus.com` or `192.168.1.1`
2. **Go to "Advanced Settings" → "LAN" → "IPTV"**
3. **Enable "Enable multicast routing (IGMP Proxy)"**
4. **Go to "Advanced Settings" → "WAN" → "NAT Passthrough"**
5. **Enable "UPnP"**
6. **Some models**: "Advanced Settings" → "LAN" → "mDNS" (if available)

### Linksys Routers

1. **Access router admin**: `http://myrouter.local` or `192.168.1.1`
2. **Go to "Connectivity" → "Local Network"**
3. **Enable "IGMP Snooping"**
4. **Go to "Connectivity" → "Administration" → "UPnP"**
5. **Enable "UPnP"**

### Ubiquiti UniFi

1. **Open UniFi Controller**
2. **Go to "Settings" → "Networks"**
3. **Select your network → "Advanced"**
4. **Enable "IGMP Snooping"**
5. **Enable "Multicast Enhancement"** (if available)
6. **Go to "Settings" → "Services" → "mDNS"**
7. **Enable "mDNS" service**

### Eero Routers

1. **Open Eero app**
2. **Tap "Settings" → "Advanced" → "DNS"**
3. **Enable "Local DNS Caching"** (helps with mDNS)
4. **Note**: Eero has good mDNS support by default, but ensure all Eero nodes are on the same network segment

### Orbi (Netgear Mesh)

1. **Access router admin**: `http://orbilogin.com` or `192.168.1.1`
2. **Go to "Advanced" → "Advanced Setup" → "Wireless Settings"**
3. **Enable "Enable IGMP Snooping"**
4. **Go to "Advanced" → "Advanced Setup" → "UPnP"**
5. **Enable "Turn UPnP On"**

### Generic Router Instructions

If your router brand isn't listed, look for these settings:

1. **IGMP Snooping** or **IGMP Proxy**
   - Location: Usually in "Advanced" → "Network" or "LAN Settings"
   - Purpose: Allows multicast traffic to propagate

2. **Multicast Forwarding** or **Multicast Routing**
   - Location: "Advanced" → "Network" or "Routing"
   - Purpose: Forwards multicast packets between network segments

3. **UPnP (Universal Plug and Play)**
   - Location: "Advanced" → "NAT" or "Port Forwarding"
   - Purpose: Helps with device discovery

4. **mDNS** or **Bonjour**
   - Location: "Advanced" → "Services" or "Network"
   - Purpose: Explicit mDNS forwarding (if available)

5. **Firewall Rules**
   - Ensure UDP port **5353** is not blocked
   - Allow multicast traffic (224.0.0.0/4)

## Testing mDNS After Configuration

### From Mac Terminal

```bash
# Install Bonjour tools (if not already installed)
brew install bonjour

# Test mDNS discovery
dns-sd -B _hap._tcp

# You should see HomeKit devices appear
# Press Ctrl+C to stop
```

### From iPhone/iPad

1. **Open Home app**
2. **Tap "+" → "Add Accessory"**
3. **You should see "HASS Bridge" appear automatically**
4. **If it appears, mDNS is working!**

## Alternative: Use mDNS Reflector

If your routers don't support mDNS forwarding, you can use a dedicated mDNS reflector:

### Option 1: Avahi (Linux/Mac)

```bash
# Install Avahi
brew install avahi

# Start Avahi daemon
brew services start avahi
```

### Option 2: Docker mDNS Reflector

```bash
docker run -d \
  --name mdns-reflector \
  --network=host \
  --cap-add=NET_ADMIN \
  --cap-add=NET_RAW \
  mcr.microsoft.com/mirror/docker/library/alpine:latest \
  sh -c "apk add --no-cache avahi && avahi-daemon -D"
```

### Option 3: Home Assistant mDNS Integration

Home Assistant's `zeroconf` component can help with mDNS, but it's primarily for discovery, not forwarding.

## Network Architecture Considerations

### Multiple Routers / Access Points

If you have multiple routers:

1. **Configure all routers** with the same mDNS settings
2. **Ensure they're on the same network segment** (same subnet)
3. **Use router mode, not access point mode** for secondary routers (if they need to forward traffic)
4. **Or use access point mode** and connect via Ethernet backhaul (simpler)

### VLANs

If you use VLANs:

1. **Enable "Multicast VLAN Registration"** (MVR) if available
2. **Configure inter-VLAN multicast forwarding**
3. **Or place Home Assistant and HomeKit devices on the same VLAN**

### Mesh Networks

Most mesh systems (Eero, Orbi, Google WiFi) handle mDNS automatically, but:

1. **Ensure all nodes are on the same network segment**
2. **Use Ethernet backhaul** if possible (more reliable)
3. **Check mesh system's mDNS settings** in admin panel

## Troubleshooting

### Still Not Working?

1. **Check firewall logs**:
   - Look for blocked UDP 5353 packets
   - Check if multicast traffic is being dropped

2. **Test connectivity**:
   ```bash
   # From Mac, test if you can reach Home Assistant
   curl http://192.168.0.252:8123

   # Test if port 21064 is accessible
   nc -zv 192.168.0.252 21064
   ```

3. **Check router logs**:
   - Look for multicast forwarding errors
   - Check IGMP membership reports

4. **Try manual pairing first**:
   - Use IP address: `192.168.0.252:21064`
   - This confirms HomeKit works, just not discovery

5. **Consider network topology**:
   - Are all devices on the same subnet?
   - Are there firewalls between network segments?
   - Is NAT interfering with multicast?

## Recommended Configuration

For best results:

1. **Enable IGMP Snooping** on all routers
2. **Enable UPnP** (helps with device discovery)
3. **Ensure UDP 5353 is not blocked** by firewall
4. **Use Ethernet backhaul** for mesh systems (if possible)
5. **Keep all HomeKit devices on the same network segment** for initial setup

## Next Steps

1. **Identify your router brand/model**
2. **Follow the specific instructions above**
3. **Restart routers** after making changes
4. **Test mDNS discovery** using the methods above
5. **If still not working**, use manual IP pairing as a workaround
