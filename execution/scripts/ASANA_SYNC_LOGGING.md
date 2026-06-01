# Asana Sync Logging Guide

Both the sync service and webhook server have comprehensive logging for debugging.

## Log Files

### Sync Service Logs

- **Main log**: `data/logs/asana_sync.log`
  - All sync operations (INFO level and above)
  - Sync statistics and progress
  - Task updates and creations

- **Error log**: `data/logs/asana_sync.error.log`
  - Errors only (ERROR level)
  - Stack traces for exceptions
  - API failures and sync errors

### Webhook Server Logs

- **Main log**: `data/logs/asana_webhook.log`
  - Webhook events received
  - Handshake confirmations
  - Task sync triggers

- **Error log**: `data/logs/asana_webhook.error.log`
  - Webhook signature verification failures
  - Sync errors triggered by webhooks
  - Server errors

## Log Rotation

All log files automatically rotate:
- **Max size**: 10MB per file
- **Backups**: 5 rotated files kept
- **Format**: `filename.log`, `filename.log.1`, `filename.log.2`, etc.

## Log Levels

### INFO (Default)
- Sync start/complete
- Tasks updated/created
- Webhook events received
- Statistics and summaries

### DEBUG (--debug flag)
- Detailed API calls
- Task comparisons
- Timestamp checks
- Full event data

### WARNING
- Skipped tasks (already syncing)
- Missing webhook secrets
- API rate limit warnings

### ERROR
- API failures
- Sync errors
- Signature verification failures
- Exceptions with stack traces

## Usage Examples

### View Recent Sync Activity

```bash
# Last 50 lines of sync log
tail -n 50 data/logs/asana_sync.log

# Follow sync log in real-time
tail -f data/logs/asana_sync.log

# Search for specific task
grep "task_gid_here" data/logs/asana_sync.log
```

### Debug Sync Issues

```bash
# View errors only
tail -f data/logs/asana_sync.error.log

# Search for specific error type
grep "Error fetching tasks" data/logs/asana_sync.error.log

# View with timestamps
tail -f data/logs/asana_sync.log | grep -E "(ERROR|WARNING)"
```

### Debug Webhook Issues

```bash
# View webhook events
tail -f data/logs/asana_webhook.log

# Check for signature failures
grep "Invalid signature" data/logs/asana_webhook.error.log

# View all webhook activity with timestamps
tail -f data/logs/asana_webhook.log
```

### Enable Debug Logging

```bash
# Sync service with debug logging
python scripts/sync_asana_tasks.py --debug

# Webhook server with debug logging
python scripts/asana_webhook_server.py --debug --port 8080
```

## Log Format

All logs use structured format:

```
YYYY-MM-DD HH:MM:SS - logger_name - LEVEL - message
```

Example:
```
2025-01-15 14:30:22 - asana_sync - INFO - Starting Asana Task Sync
2025-01-15 14:30:23 - asana_sync - INFO - Fetching tasks modified since 2025-01-15 14:25:00 from workspace 12345678...
2025-01-15 14:30:25 - asana_sync - INFO - Found 5 tasks modified since last sync
2025-01-15 14:30:26 - asana_sync - ERROR - Error updating Asana task 98765432: API rate limit exceeded
```

## Common Debugging Scenarios

### Tasks Not Syncing

1. Check sync state:
   ```bash
   cat data/logs/asana_sync_state.json
   ```

2. Check for errors:
   ```bash
   tail -n 100 data/logs/asana_sync.error.log
   ```

3. Enable debug logging:
   ```bash
   python scripts/sync_asana_tasks.py --debug
   ```

### Webhooks Not Working

1. Check webhook server logs:
   ```bash
   tail -f data/logs/asana_webhook.log
   ```

2. Verify webhooks registered:
   ```bash
   python scripts/register_asana_webhooks.py --list
   ```

3. Check for signature errors:
   ```bash
   grep "Invalid signature" data/logs/asana_webhook.error.log
   ```

### API Rate Limits

1. Check for rate limit errors:
   ```bash
   grep "rate limit" data/logs/asana_sync.error.log
   ```

2. Increase sync interval:
   ```bash
   python scripts/sync_asana_tasks.py --daemon --interval 300
   ```

### Sync Conflicts

1. Check for conflict resolution:
   ```bash
   grep -i "conflict\|newer\|skip" data/logs/asana_sync.log
   ```

2. Enable debug to see timestamp comparisons:
   ```bash
   python scripts/sync_asana_tasks.py --debug
   ```

## Log Analysis

### Count Sync Operations

```bash
# Count tasks synced today
grep "$(date +%Y-%m-%d)" data/logs/asana_sync.log | grep "updated\|created" | wc -l

# Count errors today
grep "$(date +%Y-%m-%d)" data/logs/asana_sync.error.log | wc -l
```

### Find Slow Operations

```bash
# Find syncs taking longer than expected
grep "Sync complete" data/logs/asana_sync.log | awk '{print $1, $2, $NF}'
```

### Monitor Webhook Activity

```bash
# Count webhook events in last hour
grep "$(date -v-1H +%Y-%m-%d)" data/logs/asana_webhook.log | grep "Webhook event" | wc -l
```

## Integration with Monitoring

Logs can be integrated with monitoring tools:

- **File-based monitoring**: Use `tail -f` or log aggregation tools
- **Log aggregation**: Ship logs to centralized logging (ELK, Splunk, etc.)
- **Alerting**: Set up alerts on ERROR level logs
- **Metrics**: Parse logs for sync statistics

## Best Practices

1. **Regular log review**: Check error logs weekly
2. **Debug mode**: Use `--debug` when troubleshooting
3. **Log retention**: Rotated logs kept for 5 cycles (~50MB total)
4. **Monitor errors**: Set up alerts for ERROR level logs
5. **Archive old logs**: Move old rotated logs to archive if needed








