# Alternative HomeKit Manual Pairing Methods

## If "Add Manually" Option is Missing

Some iOS versions hide the manual pairing option. Here are alternative methods:

### Method 1: Use QR Code with Manual Entry

1. **Get QR Code from Home Assistant:**
   - Open Home Assistant: `http://localhost:8123`
   - Go to **Settings → Devices & Services → HomeKit**
   - Click on **"HASS Bridge:21064"**
   - Look for a **QR code image** on the page

2. **In Home App:**
   - Tap **"+"** → **"Add Accessory"**
   - Point camera at QR code
   - If it doesn't scan, try the code below the QR code

### Method 2: Enter Code Directly (iOS 16+)

1. **In Home App:**
   - Tap **"+"** → **"Add Accessory"**
   - Look for **"Enter Code"** or **"Use Code"** option
   - Enter the 8-digit pairing code directly
   - The app may then ask for IP address

### Method 3: Scan QR Code from Screenshot

1. **Get QR Code:**
   - Take a screenshot of the QR code from Home Assistant
   - Save to Photos

2. **In Home App:**
   - Tap **"+"** → **"Add Accessory"**
   - When camera opens, tap the **photo icon** (bottom left)
   - Select the QR code screenshot
   - It should read the code and connect

### Method 4: Use HomeKit Accessory Simulator (Advanced)

If you have Xcode installed:
1. Open **HomeKit Accessory Simulator**
2. Create a bridge with the same details
3. Use it to test pairing

### Method 5: Check iOS Version

**iOS 15 and earlier:**
- Manual option is usually available
- Look for "I Don't Have a Code" → "Add Manually"

**iOS 16+:**
- May require scanning QR code first
- Then fall back to manual entry if scan fails

**iOS 17+:**
- May have different UI
- Try: Settings → Home → Add Accessory

### Method 6: Force Manual Entry via Settings

1. **iPhone Settings:**
   - Go to **Settings → Home**
   - Look for **"Add Accessory"** or **"Pairing"** options
   - May have manual entry option here

### Method 7: Use Home Assistant's Pairing URL

Some Home Assistant versions provide a pairing URL:
1. Check Home Assistant HomeKit integration page
2. Look for a **pairing URL** or **link**
3. Open it on your iPhone
4. It may trigger the Home app automatically

## Troubleshooting

**If none of these work:**

1. **Check Home Assistant HomeKit Integration:**
   - Make sure it's enabled and running
   - Restart the integration if needed
   - Check logs for errors

2. **Try Different Device:**
   - Use iPad instead of iPhone (or vice versa)
   - Different iOS versions may have different options

3. **Update iOS:**
   - Ensure iOS is up to date
   - Newer versions may have better manual pairing support

4. **Contact Home Assistant:**
   - Check Home Assistant forums for iOS-specific pairing issues
   - May be a known issue with your iOS version

## Quick Test

Try this sequence:
1. Open Home app
2. Tap "+"
3. Tap "Add Accessory"
4. **Don't scan anything** - just wait
5. Look for any text links at the bottom (may say "More Options" or similar)
6. Tap any available option
7. Look for code entry field

The manual option might be hidden but accessible through a different path.
