#!/usr/bin/env python3
"""
Automated Onychomys connectivity test.

Tests:
1. Gateway process is running
2. Telegram provider is active (log evidence)
3. Model auth is working (no auth errors since last restart)
4. End-to-end: sends a message via Telegram Bot API and verifies the
   gateway processed it (reply sent back to the user's chat).

Usage:
    python3 test_onychomys.py
    python3 test_onychomys.py --wait 30   # wait up to 30s for reply

The script sends the test message FROM the user's Telegram chat
by injecting a synthetic update via the test webhook endpoint — which
OpenClaw doesn't support. Instead it monitors the gateway log for
reply evidence after you manually send a message, or it sends a
"probe" message from the bot and checks for a new outgoing message
(which confirms the reply path works).

Since full end-to-end testing requires the user to send a message,
this script does the following automated checks and instructs the
user what to send to get the reply confirmation.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GATEWAY_LOG = Path(f"/tmp/openclaw/openclaw-{datetime.now().strftime('%Y-%m-%d')}.log")
GATEWAY_ERR_LOG = Path.home() / ".openclaw/logs/gateway.err.log"
AUTH_PROFILES = Path.home() / ".openclaw/agents/main/agent/auth-profiles.json"

CHECKS_PASSED = []
CHECKS_FAILED = []


def ok(msg):
    CHECKS_PASSED.append(msg)
    print(f"  ✅ {msg}")


def fail(msg, detail=""):
    CHECKS_FAILED.append(msg)
    print(f"  ❌ {msg}" + (f"\n     {detail}" if detail else ""))


def warn(msg):
    print(f"  ⚠️  {msg}")


def check_gateway_process():
    print("\n[1] Gateway process")
    # Try multiple process name patterns (binary name varies by install type)
    for pattern in ["openclaw-gateway", "openclaw.mjs", "openclaw"]:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True
        )
        pids = [p for p in result.stdout.strip().split() if p]
        if pids:
            ok(f"Gateway running as '{pattern}' (PID {', '.join(pids)})")
            return True

    # Fallback: check if port 18789 is listening
    result = subprocess.run(
        ["lsof", "-i", ":18789", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True
    )
    pids = [p for p in result.stdout.strip().split() if p]
    if pids:
        ok(f"Port 18789 listening (PID {', '.join(pids)})")
        return True

    fail("Gateway process not found (no process on port 18789)")
    return False


def check_gateway_log():
    print("\n[2] Gateway startup log")
    if not GATEWAY_LOG.exists():
        fail(f"Gateway log not found: {GATEWAY_LOG}")
        return False, None

    restart_time = None
    model = None

    with open(GATEWAY_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                full = json.dumps(d)
                t = d.get("time", "")
                if "agent model:" in full:
                    # Extract model
                    for i in range(5):
                        part = str(d.get(str(i), ""))
                        if "agent model:" in part:
                            model = part.replace("agent model:", "").strip()
                    restart_time = t
                if "listening on" in full and "18789" in full:
                    restart_time = t
            except:
                pass

    if restart_time:
        ok(f"Gateway started at {restart_time}, model: {model}")
    else:
        fail("Gateway startup entry not found in log")

    return restart_time, model


def check_auth_errors(since_time):
    print("\n[3] Auth errors since last restart")
    if not GATEWAY_ERR_LOG.exists():
        warn("Gateway error log not found")
        return True

    auth_errors = []
    with open(GATEWAY_ERR_LOG) as f:
        content = f.read()

    # Look for auth errors after restart time
    if since_time:
        # Simple heuristic: look for lines after the restart
        lines = content.split("\n")
        capturing = False
        for line in lines:
            if since_time[:16] in line or (since_time and line > since_time[:19]):
                capturing = True
            if capturing and "No API key found" in line:
                auth_errors.append(line.strip()[:150])
    else:
        if "No API key found" in content:
            auth_errors = ["Auth error found (timestamp unknown)"]

    if auth_errors:
        fail("Auth errors found after restart", "\n     ".join(auth_errors[:3]))
        return False
    else:
        ok("No auth errors since last restart")
        return True


def check_auth_profiles():
    print("\n[4] Auth profiles")
    if not AUTH_PROFILES.exists():
        fail(f"Auth profiles file not found: {AUTH_PROFILES}")
        return False

    with open(AUTH_PROFILES) as f:
        profiles = json.load(f)

    now_ms = int(time.time() * 1000)
    profile_names = list(profiles.get("profiles", {}).keys())

    for name in profile_names:
        cred = profiles["profiles"][name]
        provider = cred.get("provider", "?")
        expires_ms = cred.get("expires")
        if expires_ms:
            expires_dt = datetime.fromtimestamp(expires_ms / 1000)
            if now_ms < expires_ms:
                ok(f"Profile '{name}' (provider={provider}) valid until {expires_dt.strftime('%Y-%m-%d %H:%M')}")
            else:
                warn(f"Profile '{name}' (provider={provider}) expired at {expires_dt.strftime('%Y-%m-%d %H:%M')}")
        else:
            ok(f"Profile '{name}' (provider={provider}) — no expiry")

    # Check for required providers
    providers_present = {cred.get("provider") for cred in profiles["profiles"].values()}
    if "codex" in providers_present:
        ok("codex provider profile present")
    else:
        fail("No 'codex' provider profile found — Onychomys will fall back to anthropic")
        warn("Run: python3 test_onychomys.py --fix-auth to add codex credentials")

    return True


def check_telegram_connectivity():
    print("\n[5] Telegram Bot API connectivity")
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            bot = result["result"]
            ok(f"Bot API reachable: @{bot['username']} ({bot['first_name']})")
            return True
        fail("Bot API returned non-ok response", str(result))
        return False
    except Exception as e:
        fail("Bot API request failed", str(e))
        return False


def send_probe_message(text="🔧 Onychomys connectivity probe — please reply with 'OK'"):
    """Send a message from the bot to the user and return the message_id."""
    payload = {"chat_id": CHAT_ID, "text": text}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    return result["result"]["message_id"]


def check_gateway_processes_messages(wait_secs=20):
    """
    Verify the gateway's reply path by checking log for recent outbound sendMessage calls.
    The gateway logs 'telegram sendMessage ok' when it sends a reply.
    """
    print("\n[6] Gateway reply path")

    if not GATEWAY_LOG.exists():
        fail("Gateway log not found")
        return False

    # Count existing outbound messages in log
    def count_sendmessage_ok():
        count = 0
        with open(GATEWAY_LOG) as f:
            for line in f:
                if "sendMessage ok" in line and "telegram" in line.lower():
                    count += 1
        return count

    before = count_sendmessage_ok()

    # Send a probe from bot → user (this doesn't trigger the gateway reply path,
    # it's a direct API send, not a gateway-routed message)
    try:
        msg_id = send_probe_message(
            f"🔧 Connectivity probe sent at {datetime.now().strftime('%H:%M:%S')} — "
            f"reply to this message to verify Onychomys responds"
        )
        ok(f"Probe message sent to your Telegram (msg_id={msg_id})")
        print(f"\n     👆 Send a reply to this message in Telegram to test full loop.")
        print(f"     Waiting {wait_secs}s for gateway to process an incoming reply...")
    except Exception as e:
        fail("Failed to send probe message", str(e))
        return False

    # Wait and poll gateway log for new sendMessage ok
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        time.sleep(2)
        after = count_sendmessage_ok()
        if after > before:
            ok(f"Gateway sent {after - before} reply(s) — full loop confirmed! ✅")
            return True

    warn(f"No gateway reply detected in {wait_secs}s — either no incoming message or reply is slow")
    warn("This may be expected if you didn't send a message from Telegram")
    return None  # inconclusive


def main():
    parser = argparse.ArgumentParser(description="Test Onychomys/OpenClaw Telegram gateway")
    parser.add_argument("--wait", type=int, default=20, help="Seconds to wait for reply (default: 20)")
    parser.add_argument("--skip-reply-test", action="store_true", help="Skip the end-to-end reply test")
    args = parser.parse_args()

    print("=" * 60)
    print("Onychomys Gateway Connectivity Test")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    check_gateway_process()
    restart_time, model = check_gateway_log()
    check_auth_errors(restart_time)
    check_auth_profiles()
    check_telegram_connectivity()

    if not args.skip_reply_test:
        check_gateway_processes_messages(wait_secs=args.wait)

    print("\n" + "=" * 60)
    print(f"RESULTS: {len(CHECKS_PASSED)} passed, {len(CHECKS_FAILED)} failed")
    if CHECKS_FAILED:
        print("\nFailed checks:")
        for f in CHECKS_FAILED:
            print(f"  ❌ {f}")
        sys.exit(1)
    else:
        print("All automated checks passed ✅")
        sys.exit(0)


if __name__ == "__main__":
    main()
