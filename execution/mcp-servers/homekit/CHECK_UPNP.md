# Check UPnP Settings

Your IGMP settings are already configured correctly! ✅

**IGMP Proxy**: Enabled ✅
**IGMP Snooping**: Enabled ✅

Now check UPnP:

1. **In the router admin** (where you are now):
   - Click "NAT Forwarding" in the left sidebar
   - Click "UPnP"
   - Ensure "UPnP" is **Enabled** ✅
   - Click "Save" if you made changes

2. **After enabling UPnP**:
   - Go to "System Tools" → "Reboot"
   - Restart the router
   - Wait 2-3 minutes

3. **Test mDNS**:
   - On iPhone/iPad: Open Home app → "+" → "Add Accessory"
   - You should see "HASS Bridge" appear automatically

If it still doesn't appear, use manual pairing:
- IP: `192.168.0.252:21064`
- Code: `471-42-540`
