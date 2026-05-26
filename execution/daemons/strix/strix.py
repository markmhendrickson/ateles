#!/usr/bin/env python3
"""
Strix — menu bar toggle for meeting/ambient audio recording.

Strix genus: wood owls. T3 daemon in the Ateles swarm.

Single click on the icon toggles recording on/off. No dropdown menu.
Hover tooltip shows current state.

Icon:
  🔴  recording active (mic live)
  ⚫  recording off (mic muted)
"""

import shutil
import subprocess
import sys
from pathlib import Path

import objc
import rumps
from AppKit import NSObject

ROOT = Path(__file__).resolve().parent.parent.parent.parent  # ateles repo root
CONTROL_SCRIPT = ROOT / "execution" / "scripts" / "meeting-recording-control.sh"

ICON_RECORDING = "🔴"
ICON_IDLE = "⚫"
TOOLTIP_RECORDING = "Strix: recording active — click to stop"
TOOLTIP_IDLE = "Strix: idle — click to start recording"


def _osascript(expr: str) -> str:
    result = subprocess.run(["osascript", "-e", expr], capture_output=True, text=True)
    return result.stdout.strip()


def set_mic_muted(muted: bool) -> None:
    level = 0 if muted else 100
    _osascript(f"set volume input volume {level}")


def recording_is_active() -> bool:
    result = subprocess.run(
        ["bash", str(CONTROL_SCRIPT), "status"],
        capture_output=True,
        text=True,
    )
    return "running" in result.stdout


def start_recording() -> None:
    subprocess.Popen(
        ["bash", str(CONTROL_SCRIPT), "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_recording() -> None:
    subprocess.Popen(
        ["bash", str(CONTROL_SCRIPT), "stop"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _telegram(message: str) -> None:
    """Send a Telegram notification via telegram-send (best-effort)."""
    telegram = shutil.which("telegram-send")
    if not telegram:
        return
    try:
        subprocess.run([telegram, message], timeout=10, capture_output=True)
    except Exception:
        pass


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
        active = recording_is_active()
        # Empty menu so rumps doesn't show a dropdown; quit_button disabled.
        super().__init__(
            ICON_RECORDING if active else ICON_IDLE,
            menu=[],
            quit_button=None,
        )
        self._recording = active
        set_mic_muted(not active)
        # Wire direct click and tooltip after the run loop sets up the status item.
        rumps.Timer(self._setup_direct_click, 0.1).start()

    def _setup_direct_click(self, _timer):
        _timer.stop()
        nsitem = self._nsapp.nsstatusitem
        # Remove the menu so clicks fire the action instead of opening a dropdown.
        nsitem.setMenu_(None)
        self._click_target = ClickTarget.alloc().initWithApp_(self)
        nsitem.setTarget_(self._click_target)
        nsitem.setAction_("handleClick:")
        # Enable both left and right click to reach the action.
        nsitem.button().sendActionOn_(3)  # NSLeftMouseDown | NSRightMouseDown
        self._update_tooltip()

    def _update_tooltip(self):
        nsitem = self._nsapp.nsstatusitem
        tooltip = TOOLTIP_RECORDING if self._recording else TOOLTIP_IDLE
        nsitem.setToolTip_(tooltip)

    def toggle(self):
        if self._recording:
            set_mic_muted(True)
            stop_recording()
            self._recording = False
            self.title = ICON_IDLE
            _telegram("⚫ [strix] Recording stopped — transcription starting.")
        else:
            set_mic_muted(False)
            start_recording()
            self._recording = True
            self.title = ICON_RECORDING
            _telegram("🔴 [strix] Recording started.")
        self._update_tooltip()


if __name__ == "__main__":
    if not CONTROL_SCRIPT.exists():
        print(f"Control script not found: {CONTROL_SCRIPT}", file=sys.stderr)
        sys.exit(1)
    StrixApp().run()
