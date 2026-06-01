#!/usr/bin/env python3
"""
Monitor and auto-restart Asana export script if it stalls.

Checks every run:
1. Is export process running?
2. If running, is it making progress? (log file updated recently, CPU activity)
3. If stalled, kill and restart with --resume

Usage:
    python execution/scripts/monitor_asana_export.py

Or via cron:
    */5 * * * * cd /path/to/personal && execution/venv/bin/python execution/scripts/monitor_asana_export.py >> /tmp/asana_export_monitor.log 2>&1
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import get_data_dir

EXPORT_SCRIPT = PROJECT_ROOT / "execution" / "scripts" / "export_asana_tasks.py"
LOG_FILE = Path("/tmp/asana_export.log")
CHECKPOINT_FILE = get_data_dir() / "logs" / "export_checkpoint.json"
MONITOR_LOG = Path("/tmp/asana_export_monitor.log")

# Stalled detection thresholds
STALLED_LOG_SECONDS = 300  # 5 minutes without log updates
STALLED_CPU_THRESHOLD = 0.5  # Less than 0.5% CPU = likely stalled
STALLED_CHECKPOINT_SECONDS = 600  # 10 minutes without checkpoint updates


def send_notification(title: str, message: str, subtitle: str = ""):
    """Send persistent macOS notification that stays on screen until dismissed.

    Uses 'terminal-notifier' if available (supports persistent alerts via system settings),
    otherwise falls back to AppleScript 'display notification'.

    To make notifications persistent:
    1. Install terminal-notifier: brew install terminal-notifier
    2. System Settings > Notifications > terminal-notifier > Alert Style: Persistent
    """
    try:
        # Try terminal-notifier first (supports persistent notifications)
        terminal_notifier = subprocess.run(
            ["which", "terminal-notifier"], capture_output=True, timeout=2
        )

        if terminal_notifier.returncode == 0:
            # Use terminal-notifier for persistent notifications
            cmd = ["terminal-notifier", "-title", title]
            if subtitle:
                cmd.extend(["-subtitle", subtitle])
            cmd.extend(["-message", message])
            cmd.extend(["-group", "asana_export_monitor"])
            cmd.extend(
                ["-sender", "com.apple.Terminal"]
            )  # Helps with notification grouping

            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        # Fallback to AppleScript (notifications will auto-dismiss but remain in Notification Center)
        message_escaped = (
            message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        )
        title_escaped = title.replace("\\", "\\\\").replace('"', '\\"')
        subtitle_escaped = (
            subtitle.replace("\\", "\\\\").replace('"', '\\"') if subtitle else ""
        )

        script = (
            f'display notification "{message_escaped}" with title "{title_escaped}"'
        )
        if subtitle_escaped:
            script += f' subtitle "{subtitle_escaped}"'

        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        # Silently fail - notifications are optional
        pass


def log(
    message: str,
    notify: bool = False,
    notify_title: str = None,
    notify_subtitle: str = "",
):
    """Log message to monitor log file and optionally send notification."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}\n"
    MONITOR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(MONITOR_LOG, "a") as f:
        f.write(log_entry)
    print(log_entry.strip())

    if notify and notify_title:
        send_notification(notify_title, message, notify_subtitle)


def find_export_processes():
    """Find running export_asana_tasks.py processes."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "export_asana_tasks.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            pids = [
                pid.strip() for pid in result.stdout.strip().split("\n") if pid.strip()
            ]
            return pids
        return []
    except Exception as e:
        log(f"Error finding processes: {e}")
        return []


def get_process_info(pid: str):
    """Get process info (CPU, memory, state, elapsed time)."""
    try:
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "etime,pcpu,pmem,state"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 4:
                    return {
                        "etime": parts[0],
                        "cpu": float(parts[1]) if parts[1] != "-" else 0.0,
                        "mem": float(parts[2]) if parts[2] != "-" else 0.0,
                        "state": parts[3],
                    }
    except Exception as e:
        log(f"Error getting process info for PID {pid}: {e}")
    return None


def get_file_mtime(file_path: Path) -> datetime:
    """Get file modification time."""
    if file_path.exists():
        return datetime.fromtimestamp(file_path.stat().st_mtime)
    return datetime.fromtimestamp(0)


def is_log_stalled() -> bool:
    """Check if log file hasn't been updated recently."""
    if not LOG_FILE.exists():
        return True

    mtime = get_file_mtime(LOG_FILE)
    age_seconds = (datetime.now() - mtime).total_seconds()

    if age_seconds > STALLED_LOG_SECONDS:
        log(f"Log file stale: last updated {age_seconds:.0f} seconds ago")
        return True
    return False


def is_checkpoint_stalled() -> bool:
    """Check if checkpoint hasn't been updated recently."""
    if not CHECKPOINT_FILE.exists():
        return False  # No checkpoint = just started, not stalled

    mtime = get_file_mtime(CHECKPOINT_FILE)
    age_seconds = (datetime.now() - mtime).total_seconds()

    if age_seconds > STALLED_CHECKPOINT_SECONDS:
        log(f"Checkpoint stale: last updated {age_seconds:.0f} seconds ago")
        return True
    return False


def check_network_connections(pid: str) -> bool:
    """Check if process has active network connections (indicates activity)."""
    try:
        result = subprocess.run(
            ["lsof", "-p", pid], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Check for ESTABLISHED connections (not CLOSE_WAIT which indicates stall)
            output = result.stdout
            if "ESTABLISHED" in output:
                return True
            if "CLOSE_WAIT" in output:
                log(f"Process {pid} has CLOSE_WAIT connections (likely stalled)")
                return False
    except Exception as e:
        log(f"Error checking network connections: {e}")
    return True


def is_process_stalled(pid: str) -> bool:
    """Determine if process is stalled."""
    proc_info = get_process_info(pid)
    if not proc_info:
        return True

    cpu = proc_info["cpu"]
    state = proc_info["state"]

    # Check CPU usage
    if cpu < STALLED_CPU_THRESHOLD:
        log(f"Process {pid} CPU usage low: {cpu}%")
        # Low CPU alone doesn't mean stalled - check other indicators

    # Check log file staleness
    if is_log_stalled():
        # Log is stale - check if checkpoint is also stale
        if is_checkpoint_stalled():
            log(f"Process {pid} appears stalled: log and checkpoint both stale")
            return True

    # Check network connections
    if not check_network_connections(pid):
        return True

    # Check if process is in uninterruptible sleep (D state) for too long
    if state == "D":
        log(f"Process {pid} in uninterruptible sleep (D state)")
        # D state can be normal for I/O, but combined with stale log, likely stalled
        if is_log_stalled():
            return True

    return False


def kill_process(pid: str):
    """Kill the export process."""
    try:
        log(f"Killing process {pid}")
        subprocess.run(["kill", "-9", pid], timeout=5)
        time.sleep(2)  # Wait for process to die
        # Verify it's dead
        result = subprocess.run(["ps", "-p", pid], capture_output=True, timeout=5)
        if result.returncode == 0:
            log(
                f"Warning: Process {pid} still running after kill",
                notify=True,
                notify_title="Export Monitor Warning",
            )
        else:
            log(f"Process {pid} terminated", notify=True, notify_title="Export Monitor")
    except Exception as e:
        log(
            f"Error killing process {pid}: {e}",
            notify=True,
            notify_title="Export Monitor Error",
        )


def start_export():
    """Start export script with --resume flag."""
    try:
        log("Starting export script with --resume")
        cmd = [
            sys.executable,
            "-u",
            str(EXPORT_SCRIPT),
            "--resume",
            "--limit",
            "0",  # No limit - export all remaining tasks
            "--only-my-tasks",  # Export tasks assigned to current user
        ]
        # Start in background
        subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log(
            "Export script started",
            notify=True,
            notify_title="Export Monitor",
            notify_subtitle="Resuming export",
        )
        return True
    except Exception as e:
        log(
            f"Error starting export script: {e}",
            notify=True,
            notify_title="Export Monitor Error",
        )
        return False


def check_export_completion():
    """Check if export has completed successfully."""
    if not LOG_FILE.exists():
        return False, None

    try:
        # Read last 50 lines of log
        with open(LOG_FILE) as f:
            lines = f.readlines()
            last_lines = "".join(lines[-50:])

            # Check for completion indicators
            if (
                "Export completed successfully" in last_lines
                or "checkpoint cleared" in last_lines
            ):
                # Extract stats if available
                stats_lines = []
                for line in reversed(lines[-30:]):
                    if any(
                        keyword in line
                        for keyword in [
                            "Synced",
                            "task(s) to Asana",
                            "main tasks",
                            "subtasks",
                        ]
                    ):
                        stats_lines.insert(0, line.strip())
                    if "checkpoint cleared" in line:
                        break

                stats = "\n".join(stats_lines) if stats_lines else None
                return True, stats
    except Exception:
        pass

    return False, None


def get_progress_info():
    """Get current export progress from checkpoint or log."""
    progress_info = []

    # Check checkpoint
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE) as f:
                checkpoint = json.load(f)
                processed = checkpoint.get("processed_count", 0)
                total = checkpoint.get("total_count", 0)
                if total > 0:
                    progress_info.append(
                        f"Progress: {processed}/{total} tasks ({100 * processed // total}%)"
                    )
        except Exception:
            pass

    return "\n".join(progress_info) if progress_info else None


def main():
    """Main monitoring loop."""
    log("=== Monitoring check ===")

    # Check if export completed
    completed, stats = check_export_completion()
    if completed:
        message = "Asana export completed successfully"
        if stats:
            # Extract key stats for notification
            stats_lines = stats.split("\n")[:4]  # First 4 lines
            message = "\n".join(stats_lines)
        log(
            f"Export completed: {stats}",
            notify=True,
            notify_title="Export Complete",
            notify_subtitle="All tasks exported",
        )
        # Don't restart if already completed
        return

    pids = find_export_processes()

    if not pids:
        log("No export process running, starting new export")
        start_export()
        return

    log(f"Found {len(pids)} export process(es): {pids}")

    # Get progress info for notification
    progress = get_progress_info()

    stalled_pids = []
    for pid in pids:
        proc_info = get_process_info(pid)
        if proc_info:
            log(
                f"Process {pid}: CPU={proc_info['cpu']}%, State={proc_info['state']}, Elapsed={proc_info['etime']}"
            )

        if is_process_stalled(pid):
            stalled_pids.append(pid)

    if stalled_pids:
        message = f"Detected {len(stalled_pids)} stalled process(es)"
        if progress:
            message += f"\n{progress}"
        log(
            f"Detected {len(stalled_pids)} stalled process(es): {stalled_pids}",
            notify=True,
            notify_title="Export Stalled",
            notify_subtitle="Restarting automatically",
        )
        for pid in stalled_pids:
            kill_process(pid)

        # Wait a moment before restarting
        time.sleep(3)

        # Restart export
        start_export()
    else:
        log("All processes appear healthy")


if __name__ == "__main__":
    main()
