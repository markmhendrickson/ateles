#!/usr/bin/env python3
"""
Strix — menu bar toggle for Audio Hijack meeting recording.

Strix genus: wood owls. T3 daemon in the Ateles swarm.

Single click on the icon toggles the Audio Hijack "Tyto" session on/off.
No dropdown menu. Hover tooltip shows current state.

Icon:
  🔴  recording active (AH Tyto session running)
  ⚫  recording off (AH Tyto session stopped)

Control mechanism: macOS Accessibility API (AXUIElement). Strix finds the
"Tyto" window in Audio Hijack and presses its Run/Stop button. This requires
the process to be trusted for accessibility (granted automatically on first
run — macOS shows the dialog because strix is a proper AppKit/rumps app).

Startup safety: if a recording appears active at startup (stale PID file from
a prior run), strix silently cleans up without sending a Telegram notification,
to avoid phantom "Recording stopped" messages when launchd restarts the daemon.
"""

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import objc
import rumps
from AppKit import NSObject, NSWorkspace

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent.parent  # ateles repo root

AH_SESSION_NAME = os.environ.get("TYTO_AH_SESSION", "Tyto")
AH_BUNDLE_ID = "com.rogueamoeba.audiohijack"

ICON_RECORDING = "🔴"
ICON_IDLE = "⚫"
TOOLTIP_RECORDING = "Strix: recording active — click to stop"
TOOLTIP_IDLE = "Strix: idle — click to start recording"

# Minimum recording duration (seconds) before a "stopped" notification is sent.
# Set to 0 to always notify. Kept nonzero only to suppress phantom messages when
# launchd restarts strix and finds AH already running from before the crash.
_MIN_NOTIFY_DURATION = 0

# ---------------------------------------------------------------------------
# Audio Hijack control via Accessibility API
# ---------------------------------------------------------------------------


def _ah_pid() -> int | None:
    """Return the PID of the running Audio Hijack process, or None."""
    result = subprocess.run(
        ["pgrep", "-x", "Audio Hijack"],
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip())
    except (ValueError, TypeError):
        return None


def _ensure_ah_running() -> int | None:
    """Launch Audio Hijack if not running. Returns PID or None on failure."""
    pid = _ah_pid()
    if pid:
        return pid
    ws = NSWorkspace.sharedWorkspace()
    ws.launchApplication_("Audio Hijack")
    for _ in range(20):
        time.sleep(0.5)
        pid = _ah_pid()
        if pid:
            time.sleep(1.0)  # let AH finish initialising
            return pid
    log.warning("[strix] Audio Hijack failed to launch")
    return None


def _find_tyto_run_button(pid: int) -> object | None:
    """
    Walk AH's accessibility tree to find the Run/Stop button in the Tyto
    session window. Returns the AXUIElement, or None if not found.
    """
    import ApplicationServices as AX

    ah_ref = AX.AXUIElementCreateApplication(pid)
    err, windows = AX.AXUIElementCopyAttributeValue(
        ah_ref, AX.kAXWindowsAttribute, None
    )
    if err or not windows:
        log.warning("[strix] AX: could not read AH windows (err=%s)", err)
        return None

    for win in windows:
        err, title = AX.AXUIElementCopyAttributeValue(
            win, AX.kAXTitleAttribute, None
        )
        if title != AH_SESSION_NAME:
            continue
        err, children = AX.AXUIElementCopyAttributeValue(
            win, AX.kAXChildrenAttribute, None
        )
        if not children:
            continue
        for child in children:
            err, ctitle = AX.AXUIElementCopyAttributeValue(
                child, AX.kAXTitleAttribute, None
            )
            if ctitle in ("Run", "Stop"):
                return child
    log.warning("[strix] AX: Tyto window or Run/Stop button not found")
    return None


def _ah_session_is_running(pid: int) -> bool:
    """Check whether the Tyto session is currently running via AX Status text."""
    import ApplicationServices as AX

    ah_ref = AX.AXUIElementCreateApplication(pid)
    err, windows = AX.AXUIElementCopyAttributeValue(
        ah_ref, AX.kAXWindowsAttribute, None
    )
    if err or not windows:
        return False
    for win in windows:
        err, title = AX.AXUIElementCopyAttributeValue(
            win, AX.kAXTitleAttribute, None
        )
        if title != AH_SESSION_NAME:
            continue
        err, children = AX.AXUIElementCopyAttributeValue(
            win, AX.kAXChildrenAttribute, None
        )
        if not children:
            continue
        for child in children:
            err, ctitle = AX.AXUIElementCopyAttributeValue(
                child, AX.kAXTitleAttribute, None
            )
            err2, cval = AX.AXUIElementCopyAttributeValue(
                child, AX.kAXValueAttribute, None
            )
            if ctitle == "Status":
                return cval != "Stopped"
    return False


def _ax_click(button) -> bool:
    """Press an AXUIElement button. Returns True on success."""
    import ApplicationServices as AX

    err = AX.AXUIElementPerformAction(button, AX.kAXPressAction)
    return err == 0


def _check_ax_trusted() -> bool:
    """Return True if this process is trusted for Accessibility. Prompts if not."""
    import ApplicationServices as AX

    if AX.AXIsProcessTrusted():
        return True
    # Prompt user — only works from a proper AppKit app (which strix is).
    AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True})
    return AX.AXIsProcessTrusted()


def recording_is_active() -> bool:
    """Check whether the AH Tyto session is currently running."""
    pid = _ah_pid()
    if not pid:
        return False
    try:
        return _ah_session_is_running(pid)
    except Exception:
        return False


def start_recording() -> bool:
    """Start the AH Tyto session. Returns True on success."""
    if not _check_ax_trusted():
        log.warning("[strix] Accessibility not trusted; cannot control Audio Hijack")
        return False
    pid = _ensure_ah_running()
    if not pid:
        return False
    btn = _find_tyto_run_button(pid)
    if not btn:
        return False
    # Only press if it says "Run" (not already running)
    import ApplicationServices as AX
    _, title = AX.AXUIElementCopyAttributeValue(btn, AX.kAXTitleAttribute, None)
    if title == "Stop":
        return True  # already running
    return _ax_click(btn)


def stop_recording() -> bool:
    """Stop the AH Tyto session. Returns True on success."""
    pid = _ah_pid()
    if not pid:
        return False
    btn = _find_tyto_run_button(pid)
    if not btn:
        return False
    import ApplicationServices as AX
    _, title = AX.AXUIElementCopyAttributeValue(btn, AX.kAXTitleAttribute, None)
    if title == "Run":
        return True  # already stopped
    return _ax_click(btn)


# ---------------------------------------------------------------------------
# Mic mute helper (optional, best-effort)
# ---------------------------------------------------------------------------


def set_mic_muted(muted: bool) -> None:
    level = 0 if muted else 100
    subprocess.run(
        ["osascript", "-e", f"set volume input volume {level}"],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------


def _telegram(message: str) -> None:
    """Send a Telegram notification via telegram-send (best-effort)."""
    telegram = shutil.which("telegram-send")
    if not telegram:
        return
    cmd = [telegram, message]
    topic = os.environ.get("TELEGRAM_TOPIC_CYPHORHINUS", "").strip()
    if topic:
        cmd = [telegram, "--reply-to-message-id", topic, message]
    try:
        subprocess.run(cmd, timeout=10, capture_output=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Rumps app
# ---------------------------------------------------------------------------


class ClickTarget(NSObject):
    """PyObjC target for direct status-item clicks (no menu)."""

    def initWithApp_(self, app):
        self = objc.super(ClickTarget, self).init()
        if self is not None:
            self._app = app
        return self

    def handleClick_(self, sender):
        self._app.toggle()


class StrixApp(rumps.App):
    def __init__(self):
        # Check actual AH state — but do NOT act on it at startup.
        # If AH is already recording (e.g. stale state from last run or normal
        # operation), we sync the icon without sending Telegram and without
        # stopping anything.
        active = recording_is_active()
        super().__init__(
            ICON_RECORDING if active else ICON_IDLE,
            menu=[],
            quit_button=None,
        )
        self._recording = active
        self._recording_started_at: float | None = time.monotonic() if active else None
        # Wire direct click and tooltip after the run loop sets up the status item.
        rumps.Timer(self._setup_direct_click, 0.1).start()

    def _setup_direct_click(self, _timer):
        _timer.stop()
        nsitem = self._nsapp.nsstatusitem
        nsitem.setMenu_(None)
        self._click_target = ClickTarget.alloc().initWithApp_(self)
        nsitem.setTarget_(self._click_target)
        nsitem.setAction_("handleClick:")
        nsitem.button().sendActionOn_(3)  # NSLeftMouseDown | NSRightMouseDown
        self._update_tooltip()

    def _update_tooltip(self):
        nsitem = self._nsapp.nsstatusitem
        tooltip = TOOLTIP_RECORDING if self._recording else TOOLTIP_IDLE
        nsitem.setToolTip_(tooltip)

    def toggle(self):
        if self._recording:
            ok = stop_recording()
            if ok:
                set_mic_muted(True)
                elapsed = (
                    time.monotonic() - self._recording_started_at
                    if self._recording_started_at is not None
                    else _MIN_NOTIFY_DURATION
                )
                self._recording = False
                self._recording_started_at = None
                self.title = ICON_IDLE
                # Only notify if the recording was long enough to be intentional.
                if elapsed >= _MIN_NOTIFY_DURATION:
                    _telegram("⚫ [strix] Recording stopped — transcription starting.")
            else:
                log.warning("[strix] stop_recording() failed")
        else:
            ok = start_recording()
            if ok:
                set_mic_muted(False)
                self._recording = True
                self._recording_started_at = time.monotonic()
                self.title = ICON_RECORDING
                _telegram("🔴 [strix] Recording started.")
            else:
                log.warning("[strix] start_recording() failed")
        self._update_tooltip()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    StrixApp().run()
