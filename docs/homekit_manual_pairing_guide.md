# HomeKit Manual Pairing Guide

## Step-by-Step Instructions

### Step 1: Get the Pairing Code from Home Assistant

**Option A: Check Notifications**
1. Open Home Assistant: `http://localhost:8123`
2. Look for the **bell icon** (🔔) in the top right
3. Click it to see notifications
4. Find **"HomeKit Pairing"** notification
5. The **8-digit code** will be shown (format: `XXX-XX-XXX`)

**Option B: Check Integration Settings**
1. Go to **Settings → Devices & Services**
2. Find **"HASS Bridge:21064"** (or similar HomeKit entry)
3. Click on it
4. The pairing code should be displayed on the integration page

**Option C: Check Logs (if notification is gone)**
```bash
docker logs homeassistant 2>&1 | grep -i "pincode\|pairing.*code" | tail -5
```

### Step 2: Pair in Home App (iPhone/iPad)

1. **Open the Home app** on your iPhone or iPad

2. **Tap the "+" button** (top right, or bottom center)

3. **Tap "Add Accessory"**

4. **Tap "I Don't Have a Code or Cannot Scan"**
   - This appears at the bottom of the screen
   - Or look for "More Options"

5. **Tap "Add Manually"** (if shown)
   - Some iOS versions skip this step

6. **Enter the connection details:**
   - **IP Address:** `192.168.0.252`
   - **Port:** `21064`
   - **Pairing Code:** Enter the 8-digit code from Step 1
     - Format: `XXX-XX-XXX` (e.g., `471-42-540`)
     - You can enter with or without dashes

7. **Tap "Add" or "Continue"**

8. **Wait for pairing to complete**
   - This may take 10-30 seconds
   - Home app will show "Connecting..." or "Adding accessory..."

9. **Success!**
   - The HASS Bridge should appear in your Home app
   - All your Home Assistant devices will be available

### Troubleshooting

**"Cannot connect to accessory"**
- Verify Home Assistant is running: `docker ps | grep homeassistant`
- Check port is accessible: `nc -zv 192.168.0.252 21064`
- Ensure iPhone/iPad is on the same network (192.168.0.x)

**"Invalid pairing code"**
- Double-check the code from Home Assistant
- Make sure you're entering it correctly (with or without dashes)
- Try removing and re-adding the HomeKit integration in Home Assistant

**"Accessory not found"**
- This usually means the port isn't accessible
- Verify: `docker ps` shows port mapping `21064:21064`
- Check firewall isn't blocking port 21064

**Can't find pairing code**
- Go to Home Assistant UI
- Settings → Devices & Services → HomeKit
- If integration isn't there, add it:
  - Click "Add Integration"
  - Search for "HomeKit Bridge"
  - Follow setup wizard (will show pairing code)

### Connection Details Summary

- **IP Address:** `192.168.0.252` (your Mac's IP)
- **Port:** `21064` (HomeKit bridge port)
- **Pairing Code:** Get from Home Assistant notifications or integration page

### After Pairing

Once paired, you can:
- Control all Home Assistant devices from Home app
- Use Siri to control devices
- Create automations in Home app
- Access devices from other Apple devices (same iCloud account)

The MCP server will also work - you can control devices via the HomeKit MCP tools.
