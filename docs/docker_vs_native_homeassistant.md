# Docker vs Native Home Assistant for HomeKit

## The mDNS Problem with Docker on macOS

**Docker on macOS:**
- ❌ mDNS auto-discovery doesn't work (Docker bridge networking limitation)
- ✅ Easy setup and updates
- ✅ Isolation from system
- ✅ Easy to remove/restart
- ✅ Works for most use cases
- ⚠️ Requires manual pairing for HomeKit

**Native Home Assistant (Python venv):**
- ✅ mDNS auto-discovery works perfectly
- ✅ Direct network access
- ✅ No Docker overhead
- ⚠️ More complex setup
- ⚠️ Requires Python environment management
- ⚠️ System dependencies

## Recommendation

### If HomeKit Auto-Discovery is Critical

**Switch to native Home Assistant:**
- You already have `execution/homeassistant-venv` set up in the repository
- mDNS will work immediately
- QR code scanning will work automatically
- Devices will appear in Home app automatically

### If Manual Pairing is Acceptable

**Keep Docker:**
- Manual pairing works fine (one-time setup)
- After pairing, everything works normally
- Easier to manage and update
- Better isolation

## Migration Path: Docker → Native

If you want to switch:

1. **Stop Docker container:**
   ```bash
   docker stop homeassistant
   ```

2. **Use existing venv:**
   ```bash
   cd /Users/markmhendrickson/repos/personal
   source execution/homeassistant-venv/bin/activate
   hass
   ```

3. **Or create new venv:**
   ```bash
   cd /Users/markmhendrickson/repos/personal
   python3 -m venv execution/homeassistant-venv
   source execution/homeassistant-venv/bin/activate
   pip install homeassistant
   hass
   ```

4. **Same config directory:**
   - Config is already in `~/.homeassistant`
   - Native Home Assistant will use the same config
   - All your settings, integrations, and devices remain

5. **Test mDNS:**
   ```bash
   dns-sd -B _hap._tcp
   ```
   - HASS Bridge should appear immediately

## Trade-offs Summary

| Feature | Docker | Native |
|---------|--------|--------|
| **mDNS Auto-Discovery** | ❌ No | ✅ Yes |
| **Manual Pairing** | ✅ Works | ✅ Works |
| **Setup Complexity** | ✅ Easy | ⚠️ Medium |
| **Updates** | ✅ Easy | ⚠️ Manual |
| **Isolation** | ✅ Full | ❌ None |
| **Resource Usage** | ⚠️ Higher (VM) | ✅ Lower |
| **Portability** | ✅ High | ❌ Low |

## My Recommendation

**For HomeKit specifically:** Native is better if auto-discovery matters to you.

**For general use:** Docker is fine - manual pairing is a one-time setup, and after that everything works the same.

**Best of both worlds:** Use Docker for development/testing, native for production if HomeKit is critical.

## Quick Decision Guide

**Choose Native if:**
- HomeKit auto-discovery is important
- You want devices to appear automatically
- You're comfortable with Python environments
- You want lower resource usage

**Choose Docker if:**
- You prefer easy setup/updates
- Manual pairing is acceptable (one-time)
- You want better isolation
- You might switch systems/configs

Would you like me to help you switch to native Home Assistant? It's straightforward since you already have the venv set up.
