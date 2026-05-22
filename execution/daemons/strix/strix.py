#!/usr/bin/env python3
"""
Strix — menu bar toggle for meeting/ambient audio recording.

Strix genus: wood owls. T3 daemon in the Ateles swarm.

Click the menu bar icon to open the one-item menu, then click the action.
macOS doesn't support single-click-no-menu on status items without private
APIs, so this is the minimal approach: one item, labelled with current state.

Icon:
  🔴  recording active (mic live)
  ⚫  recording off (mic muted)
"""

import shutil
import subprocess
import sys
from pathlib import Path

import rumps

ROOT = Path(__file__).resolve().parent.parent.parent.parent  # ateles repo root
CONTROL_SCRIPT = ROOT / "execution" / "scripts" / "meeting-recording-control.sh"

ICON_RECORDING = "🔴"
ICON_IDLE = "⚫"
LABEL_STOP = "Stop recording"
LABEL_START = "Start recording"


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


class StrixApp(rumps.App):
    def __init__(self):
        active = recording_is_active()
        super().__init__(
            ICON_RECORDING if active else ICON_IDLE,
            menu=[LABEL_STOP if active else LABEL_START],
            quit_button=None,
        )
        self._recording = active
        set_mic_muted(not active)

    @rumps.clicked(LABEL_START)
    def on_start(self, _sender):
        set_mic_muted(False)
        start_recording()
        self._recording = True
        self.title = ICON_RECORDING
        self.menu[LABEL_START].title = LABEL_STOP
        _telegram("🔴 [strix] Recording started.")

    @rumps.clicked(LABEL_STOP)
    def on_stop(self, _sender):
        set_mic_muted(True)
        stop_recording()
        self._recording = False
        self.title = ICON_IDLE
        self.menu[LABEL_STOP].title = LABEL_START
        _telegram("⚫ [strix] Recording stopped — transcription starting.")


if __name__ == "__main__":
    if not CONTROL_SCRIPT.exists():
        print(f"Control script not found: {CONTROL_SCRIPT}", file=sys.stderr)
        sys.exit(1)
    StrixApp().run()
