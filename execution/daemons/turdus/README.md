# Turdus state recovery

Turdus persists its poll position and consumed reply IDs in `.turdus_state.json`
beside `turdus.py`. The companion `.turdus_state.json.initialized` marker records
that a state write completed. An unreadable or malformed state file holds **all**
Turdus triage, including invoices and email approval routing. The blocker notice
names both paths and a fixed failure code, without including a state value.

Restore the state file from a backup only if it was taken after the most recent
approval reply Turdus consumed within the three-day reply window. The backup
must retain the consumed `processed_ids` and `approval_route_pending` entries.
Keep the marker with a restored initialized state. If that timing cannot be
confirmed, leave Turdus held or approve through Telegram and wait until the
three-day reply window has passed before restoring or resetting state.
Deleting or resetting state sooner replays consumed approvals.

On upgrade, an existing file without the marker is accepted only when it has
the legacy fields `last_message_id`, `processed_count`, `processed_ids`, and
optionally `last_poll_at`, with valid values and no other keys. Turdus preserves
those IDs, fills the new fields empty in memory, and writes the marker only
after a successful state write. Once the marker exists, every new field is
required. A deleted marker next to a legacy-shaped file gets the same lenient
load. State lost before the marker's first successful write is indistinguishable
from first use; this pre-initialization window remains a recovery limit.
