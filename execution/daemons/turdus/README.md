# Turdus state recovery

Turdus persists its poll position and consumed reply IDs in `.turdus_state.json`
beside `turdus.py`. The companion `.turdus_state.json.initialized` marker records
that a state write completed. An unreadable or malformed state file holds **all**
Turdus triage, including invoices and email approval routing. The blocker notice
names both paths and a fixed failure code, without including a state value.

Restore the state file from a backup that retains its consumed `processed_ids`;
this is safe. Do not delete or reset the state file to resume polling: doing so
replays consumed approvals. Keep the marker with a restored initialized state.
If recovery is uncertain, leave Turdus held and investigate the backup rather
than discarding the consumed IDs.

On upgrade, an existing file without the marker is accepted only when it has
the legacy fields `last_message_id`, `processed_count`, `processed_ids`, and
optionally `last_poll_at`, with valid values and no other keys. Turdus preserves
those IDs, fills the new fields empty in memory, and writes the marker only
after a successful state write. Once the marker exists, every new field is
required. A deleted marker next to a legacy-shaped file gets the same lenient
load. State lost before the marker's first successful write is indistinguishable
from first use; this pre-initialization window remains a recovery limit.
