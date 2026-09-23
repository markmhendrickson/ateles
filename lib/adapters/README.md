# Adapter runtime (`lib.adapters`)

Shared runtime for external-system adapters: drop counting (obligation 1),
coverage-stamped observations with no sync log / cursor table / local artifact
cache (obligation 4), and read-back-before-ack with halt that writes and acks
nothing (obligation 5). Design basis:
[adapters.md#the-admission-contract](../../docs/foundation/adapters.md#the-admission-contract).

## Public import

```python
from lib.adapters import (
    admit, write_observation, commit, DropCounter,
    Disposition, DropReason, Observation, Coverage, HaltError,
)
```

## Do not rebuild

Do **not** invent a sync log, last-seen cursor table, or local artifact cache
beside the record. Persist only through `write_observation` → auth-scoped
`POST /store`.

## Error hints

| code | meaning |
|------|---------|
| `missing_provenance` | `source` / `sourced_time` / `coverage` absent |
| `obligation_4_local_state` | banned local-state kwarg rejected |
| `halt` | unreachable record — no write, no ack |
| `readback_unconfirmed` | write not confirmed; do not ack |
| `user_id_widen_forbidden` | caller `user_id` refused |

Runnable example: `lib/adapters/reference/` and
`reference/test_reference_adapter.py`.
