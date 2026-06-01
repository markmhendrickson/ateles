# Asana Import Monitor

Automatic monitoring and restart system for the Asana import script with macOS notifications.

## Overview

The `monitor_asana_import.py` script checks every 5 minutes (via cron) to detect if the import process has stalled and automatically restarts it with `--resume`. It also sends macOS notifications for key events.

## macOS Notifications

The monitor sends notifications for:
- **Import Complete**: When import finishes successfully (includes stats)
- **Import Stalled**: When a stalled process is detected and restarted
- **Process Terminated**: When a stalled process is killed
- **Import Started**: When import is restarted with `--resume`
- **Errors**: When errors occur during monitoring

Notifications include progress information when available (e.g., "6029/14429 tasks (42%)").

### Persistent Notifications

To make notifications persistent (stay on screen until dismissed):
1. Install `terminal-notifier`: `brew install terminal-notifier`
2. Open **System Settings** > **Notifications**
3. Find **terminal-notifier** in the list
4. Set **Alert Style** to **Persistent**

The script automatically uses `terminal-notifier` if installed, otherwise falls back to standard AppleScript notifications.

## Stalled Detection

A process is considered stalled if:
- Log file hasn't been updated in 5+ minutes AND checkpoint hasn't been updated in 10+ minutes
- Process has CLOSE_WAIT network connections (indicates hung connection)
- Process is in uninterruptible sleep (D state) with stale logs

## Files

- **Monitor script**: `scripts/monitor_asana_import.py`
- **Monitor log**: `/tmp/asana_import_monitor.log`
- **Import log**: `/tmp/asana_import.log`
- **Checkpoint**: `data/logs/import_checkpoint.json`

## Cron Job

The cron job runs every 5 minutes (update `$REPO_ROOT` to your actual repo path):
```
*/5 * * * * cd $REPO_ROOT && $REPO_ROOT/execution/venv/bin/python execution/scripts/monitor_asana_import.py >> /tmp/asana_import_monitor.log 2>&1
```

## Manual Usage

Run the monitor script manually:
```bash
# From the repo root directory
execution/venv/bin/python execution/scripts/monitor_asana_import.py
```

## Monitoring

Check monitor activity:
```bash
tail -f /tmp/asana_import_monitor.log
```

Check import progress:
```bash
tail -f /tmp/asana_import.log
```

## Disabling

To disable automatic monitoring:
```bash
crontab -e
# Remove or comment out the monitor_asana_import line
```

