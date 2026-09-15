"""
Pure contract for hybrid recording storage (ateles#1019).

Deliberately free of vendor SDKs, daemon loops, and Neotoma I/O so the rules
can be unit-tested with a fake adapter before any live archive exists.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

_HASH_BLOCK_BYTES = 1024 * 1024

# Path substrings that identify iCloud / dataless placeholder trees. A working
# library rooted here is what hung Audio Hijack on dataless materialization.
_CLOUD_ROOT_MARKERS = (
    "Library/Mobile Documents",
    "com~apple~CloudDocs",
)


class MissingWorkingRootError(ValueError):
    """No explicit working-library binding was supplied."""


class UnsafeWorkingRootError(ValueError):
    """Resolved working root sits on a known cloud-placeholder path."""


@dataclass(frozen=True)
class SettleObservation:
    """Prior (mtime_ns, size) signature used by stable-complete checks."""

    mtime_ns: int
    size: int


@dataclass(frozen=True)
class ArchivalRecord:
    """Outcome of one archival attempt."""

    source_path: str
    archive_key: str
    content_sha256: str
    status: str  # "verified" | "failed"
    verified_at: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RetentionPolicy:
    """Explicit reclaim permission. Absence of a policy means retain forever."""

    allow_reclaim: bool = False


@dataclass(frozen=True)
class TranscriptionLinkage:
    """Fields already used by transcription entities — preserve, don't redesign."""

    audio_file_path: str
    audio_content_sha256: str


def _documents_is_icloud_synced(home: Path) -> bool:
    """Return True when ``~/Documents`` is the iCloud Documents stand-in.

    On Darwin, iCloud Desktop & Documents replaces ``~/Documents`` with a
    symlink (or firmlink) into Mobile Documents. Detect that structurally
    rather than by hostname so tests can monkeypatch this helper.
    """
    documents = home / "Documents"
    try:
        resolved = documents.resolve()
    except OSError:
        return False
    text = str(resolved)
    if any(marker in text for marker in _CLOUD_ROOT_MARKERS):
        return True
    # Symlink into Mobile Documents without resolving further.
    if documents.is_symlink():
        try:
            target = os.readlink(documents)
        except OSError:
            return False
        return any(marker in target for marker in _CLOUD_ROOT_MARKERS)
    return False


def is_unsafe_cloud_placeholder_root(path: Path, *, home: Path | None = None) -> bool:
    """True when ``path`` is under a known iCloud / placeholder tree."""
    home = home or Path.home()
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser()
    text = str(resolved)
    if any(marker in text for marker in _CLOUD_ROOT_MARKERS):
        return True
    # ~/Documents/... is unsafe when Documents is iCloud-synced (ADR: refuse
    # the default that produced 209/212 dataless recordings).
    try:
        documents = (home / "Documents").resolve()
    except OSError:
        documents = home / "Documents"
    try:
        if resolved == documents or documents in resolved.parents:
            if _documents_is_icloud_synced(home):
                return True
    except OSError:
        pass
    return False


def resolve_working_root(
    *,
    env: Mapping[str, str] | None = None,
    config_root: str | Path | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the Tier-A working library root.

    Preference order: explicit ``config_root``, then ``TYTO_RECORDINGS_DIR``,
    then ``RECORD_MEETING_DIR``. There is deliberately **no** silent fallback
    to ``~/Documents/data/recordings`` — that default is the unsafe path this
    contract exists to refuse. Callers must bind a root via config or env.
    """
    home = home or Path.home()
    env = env if env is not None else os.environ

    raw: str | Path | None = config_root
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = (env.get("TYTO_RECORDINGS_DIR") or "").strip() or None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = (env.get("RECORD_MEETING_DIR") or "").strip() or None
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        raise MissingWorkingRootError(
            "working library root requires config_root, TYTO_RECORDINGS_DIR, "
            "or RECORD_MEETING_DIR — refusing silent Documents/iCloud default"
        )

    path = Path(raw).expanduser()
    if is_unsafe_cloud_placeholder_root(path, home=home):
        raise UnsafeWorkingRootError(
            f"working library root is a known cloud-placeholder path: {path}"
        )
    return path.resolve() if path.exists() else path.absolute()


def content_sha256(path: Path) -> str:
    """Streaming sha256 of file bytes (same family as transcribe_audio)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_BLOCK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def is_fully_materialized(path: Path) -> bool:
    """False for dataless / placeholder files (size>0 but no allocated blocks)."""
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size <= 0:
        return False
    # APFS dataless / cloud stubs commonly report st_blocks == 0 while claiming
    # a non-zero size. Fully local files allocate at least one block.
    blocks = getattr(st, "st_blocks", None)
    if blocks is not None and blocks == 0:
        return False
    # macOS UF_DATALESS when the platform exposes it.
    flags = getattr(st, "st_flags", 0) or 0
    uf_dataless = getattr(os, "UF_DATALESS", 0)
    if uf_dataless and (flags & uf_dataless):
        return False
    return True


def is_stable_complete(
    path: Path,
    *,
    previous: SettleObservation | None,
    now: float,
    settle_secs: float,
) -> tuple[bool, SettleObservation]:
    """Tyto-compatible settle: size>0, (mtime_ns, size) stable, age >= settle.

    Also requires full materialization. Returns (eligible, current_observation).
    """
    try:
        st = path.stat()
    except OSError:
        return False, previous or SettleObservation(mtime_ns=0, size=0)

    obs = SettleObservation(mtime_ns=st.st_mtime_ns, size=st.st_size)
    if st.st_size <= 0:
        return False, obs
    if not is_fully_materialized(path):
        return False, obs
    if previous is None:
        return False, obs
    if (obs.mtime_ns, obs.size) != (previous.mtime_ns, previous.size):
        return False, obs
    if (now - st.st_mtime) < settle_secs:
        return False, obs
    return True, obs


@runtime_checkable
class ArchiveAdapter(Protocol):
    """Pluggable Tier-B archive. Concrete vendors are out of scope here."""

    def put(self, key: str, data: bytes) -> None:
        """Store ciphertext (or plaintext in the fake) under ``key``."""

    def get(self, key: str) -> bytes:
        """Decrypt/read-back bytes for hash verification."""

    def exists(self, key: str) -> bool:
        """Return True when ``key`` is present in the archive."""


class FakeArchiveAdapter:
    """In-memory adapter for red/green contract tests. No network, no secrets."""

    def __init__(
        self,
        *,
        fail_on_put: bool = False,
        corrupt_on_get: bool = False,
    ) -> None:
        self._store: dict[str, bytes] = {}
        self.fail_on_put = fail_on_put
        self.corrupt_on_get = corrupt_on_get
        self.put_count = 0

    def put(self, key: str, data: bytes) -> None:
        if self.fail_on_put:
            raise OSError("simulated interrupted archive copy")
        self._store[key] = data
        self.put_count += 1

    def get(self, key: str) -> bytes:
        data = self._store[key]
        if self.corrupt_on_get:
            return data + b"\x00"
        return data

    def exists(self, key: str) -> bool:
        return key in self._store


class ArchivalJournal:
    """Durable on-disk journal: success only after verified read-back."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._state: dict[str, dict] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                # An unreadable or interrupted journal cannot prove that an
                # archive copy was verified. Treat it as empty and let the
                # next attempt rebuild evidence from archive read-back.
                raw = None
            if isinstance(raw, dict):
                self._state = raw

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    def mark_pending(self, content_hash: str, record: dict) -> None:
        entry = dict(record)
        entry["status"] = "pending"
        entry.pop("verified_at", None)
        self._state[content_hash] = entry
        self._flush()

    def mark_verified(self, content_hash: str, record: dict) -> None:
        entry = dict(record)
        entry["status"] = "verified"
        self._state[content_hash] = entry
        self._flush()

    def mark_failed(self, content_hash: str, record: dict) -> None:
        entry = dict(record)
        entry["status"] = "failed"
        entry.pop("verified_at", None)
        self._state[content_hash] = entry
        self._flush()

    def success_marker(self, content_hash: str) -> bool:
        entry = self._state.get(content_hash) or {}
        return entry.get("status") == "verified"

    def pending_retries(self) -> list[dict]:
        return [
            dict(v)
            for v in self._state.values()
            if v.get("status") in {"pending", "failed"}
        ]

    def verified_record(self, content_hash: str) -> dict | None:
        entry = self._state.get(content_hash)
        if entry and entry.get("status") == "verified":
            return dict(entry)
        return None


def archive_key_for(content_hash: str) -> str:
    """Stable archive object key keyed on content sha256."""
    return f"recordings/sha256/{content_hash}"


def archive_recording(
    source: Path,
    adapter: ArchiveAdapter,
    journal: ArchivalJournal,
) -> ArchivalRecord:
    """Copy then verify. Never moves/deletes the local source.

    Idempotent on content hash: a prior verified journal entry skips put.
    Interrupted copy or hash mismatch → failed status, no success marker,
    local file retained, retry entry left in the journal.
    """
    digest = content_sha256(source)
    key = archive_key_for(digest)
    source_s = str(source.resolve())

    base = {
        "source_path": source_s,
        "archive_key": key,
        "content_sha256": digest,
    }
    journal.mark_pending(digest, base)

    try:
        data = source.read_bytes()
        if not adapter.exists(key):
            adapter.put(key, data)
        readback = adapter.get(key)
        readback_hash = hashlib.sha256(readback).hexdigest()
        if readback_hash != digest:
            raise ValueError(
                f"archive read-back hash mismatch: expected {digest}, got {readback_hash}"
            )
    except Exception as exc:  # noqa: BLE001 — surface as failed archival, not crash
        journal.mark_failed(digest, {**base, "error": str(exc)})
        return ArchivalRecord(
            source_path=source_s,
            archive_key=key,
            content_sha256=digest,
            status="failed",
            verified_at=None,
            error=str(exc),
        )

    verified_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    journal.mark_verified(digest, {**base, "verified_at": verified_at})
    return ArchivalRecord(
        source_path=source_s,
        archive_key=key,
        content_sha256=digest,
        status="verified",
        verified_at=verified_at,
    )


def may_reclaim_local(
    *,
    archive_verified: bool,
    retention: RetentionPolicy | None,
) -> bool:
    """Fail closed: reclaim only when verified AND explicitly permitted."""
    if not archive_verified:
        return False
    if retention is None:
        return False
    return bool(retention.allow_reclaim)


def preserve_transcription_linkage(
    source: Path,
    content_hash: str,
) -> TranscriptionLinkage:
    """Return the linkage fields transcription already stores — unchanged contract."""
    return TranscriptionLinkage(
        audio_file_path=str(source.resolve()),
        audio_content_sha256=content_hash,
    )
