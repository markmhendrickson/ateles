"""
Hybrid recording storage contract — local working library + encrypted archive.

Implements the filesystem-side contract from architectural decision
``ent_9672be918f8146ad45738035`` / ateles#1019:

* Tier A: configurable local working root (refuse known cloud-placeholder roots)
* Tier B: pluggable ``ArchiveAdapter`` with copy-then-verify semantics
* Fail-closed local reclaim (verified archive AND explicit retention permission)
* Content-hash idempotency; durable retry journal; transcription linkage fields

No concrete vendor, credentials, daemon wiring, or live backlog movement.
"""

from __future__ import annotations

from lib.recording_storage.contract import (
    ArchivalJournal,
    ArchivalRecord,
    ArchiveAdapter,
    FakeArchiveAdapter,
    MissingWorkingRootError,
    RetentionPolicy,
    SettleObservation,
    TranscriptionLinkage,
    UnsafeWorkingRootError,
    archive_recording,
    content_sha256,
    is_fully_materialized,
    is_stable_complete,
    is_unsafe_cloud_placeholder_root,
    may_reclaim_local,
    preserve_transcription_linkage,
    resolve_working_root,
)

__all__ = [
    "ArchivalJournal",
    "ArchivalRecord",
    "ArchiveAdapter",
    "FakeArchiveAdapter",
    "MissingWorkingRootError",
    "RetentionPolicy",
    "SettleObservation",
    "TranscriptionLinkage",
    "UnsafeWorkingRootError",
    "archive_recording",
    "content_sha256",
    "is_fully_materialized",
    "is_stable_complete",
    "is_unsafe_cloud_placeholder_root",
    "may_reclaim_local",
    "preserve_transcription_linkage",
    "resolve_working_root",
]
