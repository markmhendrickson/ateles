# Pairing HomeKit When Manual Entry Isn't Available

## The Problem

Newer iOS versions (iOS 16+) often hide the manual IP/port entry option, requiring automatic mDNS discovery. Since mDNS isn't working with Docker, we need alternative methods.

## Solution 1: Use QR Code (Best Option)

Home Assistant should display a QR code that contains all pairing information:

1. **Get QR Code from Home Assistant:**
   - Open: `http://localhost:8123`
   - Go to: **Settings → Devices & Services → HomeKit**
   - Click: **"HASS Bridge:21064"**
   - Look for a **QR code image** on the page

2. **Scan QR Code:**
   - Open Home app on iPhone
   - Tap **"+"** → **"Add Accessory"**
   - Point camera at the QR code
   - The QR code contains the IP address and pairing code
   - Even if mDNS doesn't work, scanning the QR code should connect

3. **If QR code doesn't scan:**
   - Take a screenshot of the QR code
   - In Home app camera, tap the **photo icon** (bottom left)
   - Select the QR code screenshot

## Solution 2: Use Third-Party HomeKit App

Some third-party apps support manual pairing:

### Option A: Controller for HomeKit
1. Download "Controller for HomeKit" from App Store
2. Open the app
3. Look for manual pairing option
4. Enter IP, port, and code

### Option B: HomePass
1. Download "HomePass" from App Store
2. May have manual pairing features

## Solution 3: Trigger Pairing Notification

Sometimes Home Assistant needs to show the pairing notification again:

1. **In Home Assistant:**
   - Go to: **Settings → Devices & Services → HomeKit**
   - Click on **"HASS Bridge:21064"**
   - Click **"Disable"** (three dots menu)
   - Wait a few seconds
   - Click **"Enable"** again
   - This should trigger a new pairing notification with QR code

2. **Check notifications:**
   - Look for bell icon in Home Assistant
   - Should show "HomeKit Pairing" notification
   - QR code should be in the notification

## Solution 4: Check QR Code URL

The QR code might be accessible via URL:

1. **In Home Assistant notification:**
   - Look for an image URL like: `/api/homekit/pairingqr?...`
   - Open that URL in a browser: `http://localhost:8123/api/homekit/pairingqr?...`
   - Save the QR code image
   - Scan it from Photos

## Solution 5: Use iPad or Different Device

Different iOS devices/versions may show different options:
- Try an iPad if available
- Older iOS versions may still show manual entry
- Different device might have different Home app version

## Solution 6: Check Home Assistant Logs for Pairing Info

The pairing code and setup info might be in logs:

```bash
docker logs homeassistant 2>&1 | grep -i "homekit\|pairing\|pincode" | tail -20
```

## What to Look For

**In Home Assistant UI:**
- QR code image (most important!)
- 8-digit pairing code (format: XXX-XX-XXX)
- Pairing notification with QR code

**The QR code should work even without mDNS** because it contains:
- IP address
- Port number
- Pairing code
- All encoded in the QR format

## If QR Code Still Doesn't Work

1. **Verify port is accessible:**
   ```bash
   nc -zv 192.168.0.252 21064
   ```

2. **Check Home Assistant is running:**
   ```bash
   docker ps | grep homeassistant
   ```

3. **Try restarting HomeKit integration:**
   - Disable and re-enable in Home Assistant
   - This regenerates the pairing code and QR code

4. **Last resort: Use Home Assistant web interface**
   - Control devices directly from Home Assistant
   - Doesn't require HomeKit pairing
   - Can use MCP server to control devices

## Quick Checklist

- [ ] QR code visible in Home Assistant?
- [ ] Pairing code visible (8 digits)?
- [ ] Tried scanning QR code in Home app?
- [ ] Tried screenshot method?
- [ ] Tried third-party HomeKit app?
- [ ] Port 21064 accessible from network?
