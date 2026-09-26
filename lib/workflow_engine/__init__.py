"""
lib/workflow_engine — the engine that opens a declared workflow step as
claimable work.

Ateles issue #956 ("Build the workflow engine: nothing opens a declared step
as claimable work"), Neotoma task ent_bc34f2e4bc27d59f2e2ad8bd, implementation
spec ent_133b0d1b39661f2f7961455f. Foundation plan ent_81aadb43caf2fa493361e8ed.

This package is the FIRST SLICE only: the declaration reader and the
unreadable-workflow halt (GW-1, GW-2, GW-6, GW-30, GW-31, GW-42). It does not
yet implement hydration of `reads_to_enter` beyond a readability probe, the
lease, the verdict, or the claim path — those are #957/#958/#959/#960 and
land as later slices per the implementation spec's phase 0 ordering.

Build dependency, in order (per the engine task's own notes field): #962
(the relationship-type registry, G25) must land before LEASE/ADDRESSED_BY/
FOLLOWS/CLOSES/SIGNED_BY can be written on any real instance. As of this
slice none of the fourteen relationship types are registered on prod, so
this package is exercised only against an in-memory record double
(`lib.workflow_engine.record.FakeRecordClient`) — never against prod
Neotoma, and never against a "dev" Neotoma instance, because no such
instance is configured anywhere in this repository (only
`NEOTOMA_ENV=production` / `NEOTOMA_BASE_URL=https://neotoma.markmhendrickson.com`
exist). `RecordReader` (reads) and `CheckpointWriter`/`NonProductionCheckpointWriter`
(the one write) are the seams a real dev/disposable instance (#921) plugs
into later without changing this package's logic; the writer seam is a
structural refusal, not a convention — `NonProductionCheckpointWriter`
raises `ProductionWriteRefused` at construction if the wrapped writer's
`instance_label` is not on an explicit non-production allow-list, and
`open_steps()` only accepts a checkpoint-writing capability of that
wrapped type.
"""

from __future__ import annotations
