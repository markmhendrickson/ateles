"""
Red-before-fix tests for the hybrid recording storage contract (ateles#1019).

Pins the architecture in ent_9672be918f8146ad45738035 without a live archive
vendor, credentials, backlog movement, or daemon wiring.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.recording_storage import (  # noqa: E402
    ArchivalJournal,
    FakeArchiveAdapter,
    MissingWorkingRootError,
    RetentionPolicy,
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


def _write(path: Path, data: bytes = b"recording-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ── Working root ─────────────────────────────────────────────────────────────


def test_resolve_working_root_prefers_explicit_config(tmp_path):
    root = tmp_path / "working"
    root.mkdir()
    resolved = resolve_working_root(config_root=root, env={})
    assert resolved == root.resolve()


def test_resolve_working_root_uses_tyto_env_over_record_meeting(tmp_path):
    tyto = tmp_path / "tyto"
    meeting = tmp_path / "meeting"
    tyto.mkdir()
    meeting.mkdir()
    resolved = resolve_working_root(
        env={
            "TYTO_RECORDINGS_DIR": str(tyto),
            "RECORD_MEETING_DIR": str(meeting),
        }
    )
    assert resolved == tyto.resolve()


def test_resolve_working_root_refuses_missing_binding():
    with pytest.raises(MissingWorkingRootError):
        resolve_working_root(env={}, config_root=None)


def test_resolve_working_root_refuses_icloud_documents_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    docs = home / "Documents" / "data" / "recordings"
    docs.mkdir(parents=True)
    # Treat ~/Documents as an iCloud-synced Documents tree for this host.
    monkeypatch.setattr(
        "lib.recording_storage.contract._documents_is_icloud_synced",
        lambda _home: True,
    )
    with pytest.raises(UnsafeWorkingRootError):
        resolve_working_root(
            env={"TYTO_RECORDINGS_DIR": str(docs)},
            home=home,
        )


def test_is_unsafe_cloud_placeholder_root_flags_mobile_documents(tmp_path):
    cloud = (
        tmp_path
        / "Library"
        / "Mobile Documents"
        / "com~apple~CloudDocs"
        / "Documents"
        / "data"
        / "recordings"
    )
    cloud.mkdir(parents=True)
    assert is_unsafe_cloud_placeholder_root(cloud) is True


# ── Stable-complete eligibility ──────────────────────────────────────────────


def test_stable_complete_requires_size_and_settle(tmp_path):
    path = _write(tmp_path / "a.m4a", b"abc")
    now = time.time()
    ok, obs = is_stable_complete(path, previous=None, now=now, settle_secs=8)
    assert ok is False
    ok2, _ = is_stable_complete(path, previous=obs, now=now + 1, settle_secs=8)
    assert ok2 is False
    ok3, _ = is_stable_complete(path, previous=obs, now=now + 9, settle_secs=8)
    assert ok3 is True


def test_stable_complete_rejects_zero_byte(tmp_path):
    path = _write(tmp_path / "empty.m4a", b"")
    now = time.time()
    ok, obs = is_stable_complete(path, previous=None, now=now, settle_secs=0)
    assert ok is False
    ok2, _ = is_stable_complete(path, previous=obs, now=now + 1, settle_secs=0)
    assert ok2 is False


def test_stable_complete_rejects_growing_file(tmp_path):
    path = _write(tmp_path / "grow.m4a", b"a")
    now = time.time()
    _, obs = is_stable_complete(path, previous=None, now=now, settle_secs=0)
    path.write_bytes(b"ab")
    ok, _ = is_stable_complete(path, previous=obs, now=now + 10, settle_secs=0)
    assert ok is False


def test_fully_materialized_rejects_dataless_blocks(tmp_path, monkeypatch):
    path = _write(tmp_path / "ghost.m4a", b"not-really-here")

    class _Stat:
        st_size = 100
        st_blocks = 0

    monkeypatch.setattr(Path, "stat", lambda self: _Stat())
    assert is_fully_materialized(path) is False


# ── Content-hash idempotency ─────────────────────────────────────────────────


def test_content_hash_is_stable_and_content_keyed(tmp_path):
    a = _write(tmp_path / "a.m4a", b"same-bytes")
    b = _write(tmp_path / "b.m4a", b"same-bytes")
    c = _write(tmp_path / "c.m4a", b"other")
    assert content_sha256(a) == content_sha256(b)
    assert content_sha256(a) != content_sha256(c)


def test_archive_is_idempotent_on_content_hash(tmp_path):
    source = _write(tmp_path / "rec.m4a", b"payload")
    adapter = FakeArchiveAdapter()
    journal = ArchivalJournal(tmp_path / "journal.json")
    first = archive_recording(source, adapter, journal)
    second = archive_recording(source, adapter, journal)
    assert first.status == "verified"
    assert second.status == "verified"
    assert first.archive_key == second.archive_key
    assert first.content_sha256 == content_sha256(source)
    assert adapter.put_count == 1


# ── Copy / verify / retry ────────────────────────────────────────────────────


def test_successful_archive_verifies_readback_hash(tmp_path):
    source = _write(tmp_path / "ok.m4a", b"verified-bytes")
    adapter = FakeArchiveAdapter()
    journal = ArchivalJournal(tmp_path / "journal.json")
    record = archive_recording(source, adapter, journal)
    assert record.status == "verified"
    assert record.verified_at is not None
    assert journal.success_marker(record.content_sha256) is True
    assert source.exists()


def test_hash_mismatch_leaves_no_success_marker_and_keeps_local(tmp_path):
    source = _write(tmp_path / "bad.m4a", b"source-bytes")
    adapter = FakeArchiveAdapter(corrupt_on_get=True)
    journal = ArchivalJournal(tmp_path / "journal.json")
    record = archive_recording(source, adapter, journal)
    assert record.status == "failed"
    assert record.verified_at is None
    assert journal.success_marker(content_sha256(source)) is False
    assert source.exists()
    assert journal.pending_retries()


def test_interrupted_copy_leaves_no_success_marker(tmp_path):
    source = _write(tmp_path / "cut.m4a", b"partial")
    adapter = FakeArchiveAdapter(fail_on_put=True)
    journal = ArchivalJournal(tmp_path / "journal.json")
    record = archive_recording(source, adapter, journal)
    assert record.status == "failed"
    assert journal.success_marker(content_sha256(source)) is False
    assert source.exists()
    assert adapter.exists(record.archive_key) is False


def test_journal_is_durable_across_instances(tmp_path):
    source = _write(tmp_path / "dur.m4a", b"durable")
    path = tmp_path / "journal.json"
    adapter = FakeArchiveAdapter(fail_on_put=True)
    first = ArchivalJournal(path)
    archive_recording(source, adapter, first)
    second = ArchivalJournal(path)
    assert second.success_marker(content_sha256(source)) is False
    assert second.pending_retries()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)


# ── Retention / reclaim ──────────────────────────────────────────────────────


def test_reclaim_refused_without_verified_archive():
    assert may_reclaim_local(archive_verified=False, retention=RetentionPolicy(allow_reclaim=True)) is False


def test_reclaim_refused_when_retention_missing_or_false():
    assert may_reclaim_local(archive_verified=True, retention=None) is False
    assert may_reclaim_local(
        archive_verified=True, retention=RetentionPolicy(allow_reclaim=False)
    ) is False


def test_reclaim_allowed_only_when_verified_and_explicitly_permitted():
    assert may_reclaim_local(
        archive_verified=True, retention=RetentionPolicy(allow_reclaim=True)
    ) is True


# ── Transcription linkage ────────────────────────────────────────────────────


def test_preserve_transcription_linkage_fields(tmp_path):
    source = _write(tmp_path / "link.m4a", b"linked")
    digest = content_sha256(source)
    link = preserve_transcription_linkage(source, digest)
    assert isinstance(link, TranscriptionLinkage)
    assert link.audio_file_path == str(source.resolve())
    assert link.audio_content_sha256 == digest
