# Apis

Apis genus: honeybees. T3 daemon in the Ateles swarm — universal task
dispatcher. Runs as launchd `com.ateles.apis`.

Two independent pipelines run inside one process (`asyncio.gather` in
`main()`):

1. **Neotoma task dispatch** — subscribes to `task`/`checkpoint_brief` SSE
   events, infers domain tags, and routes tasks to T4 agents.
2. **GitHub issue/PR pipeline** (`github_gateway.py` → `swarm_dispatch.py`) —
   the webhook gateway, review panel, and merge/release flow. Independent of
   pipeline 1: nothing in this pipeline is gated by the task-dispatch flags
   below.

## Environment variables

The full reference lives in `apis.py`'s module docstring (every `APIS_*` /
`ATELES_*` variable the daemon reads, with its default). This table covers
only the task-dispatch kill switches — the ones an operator is most likely to
need to flip in a hurry.

| Variable | Default | Effect |
|---|---|---|
| `APIS_TASK_DISPATCH_ENABLED` | `0` (off) | Master switch for pipeline 1. Off: `task.created`/`task.due_today` SSE events are logged and skipped (no `dispatch_task` call, no Neotoma write), `handle_checkpoint_brief` does not re-dispatch a task, and neither the stall watchdog nor the reconciliation sweep starts — regardless of `APIS_RECONCILE_ENABLED`. On: task dispatch behaves exactly as it did before this flag existed. Pipeline 2 (GitHub) is unaffected either way. |
| `APIS_DRY_RUN` | `0` (off) | Log dispatch intent without spawning an agent. Applies only once `APIS_TASK_DISPATCH_ENABLED=1` — with the kill switch off, dispatch never reaches this check. |
| `APIS_AUTO_EXECUTE` | `0` (off) | Auto-execute tasks due today instead of only notifying. Also gated behind `APIS_TASK_DISPATCH_ENABLED`. |
| `APIS_RECONCILE_ENABLED` | `0` (off) | Runs the level-triggered sweep that dispatches `pending` tasks the SSE create path never saw (ateles#586). Held off regardless of this value when `APIS_TASK_DISPATCH_ENABLED=0`. |

Set `APIS_TASK_DISPATCH_ENABLED=1` to restore task dispatch once the
foundation workflow overhaul it was paused for has landed. The daemon logs
its posture (`task dispatch: ENABLED`/`DISABLED`) once at startup — check
that line after a restart to confirm which mode is live.

## Tests

```bash
pytest execution/daemons/apis/ -v
```

`test_task_dispatch_kill_switch.py` covers both flag states end-to-end
through `handle_event`/`handle_checkpoint_brief`, and confirms the GitHub
pipeline's wiring in `main()` carries no reference to the flag.
