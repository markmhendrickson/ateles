"""
markers.py — Pending-approval marker state for Monedula.

A marker tracks the lifecycle of ONE payment obligation instance (one
calendar session for one payment profile):

    awaiting_approval -> paid
                       -> skipped

Markers are keyed by (event_id, date) so that:
  - the same session is never re-notified on a later poll tick, and
  - the same session is never paid twice, even across daemon restarts.

Persistence is a flat JSON file (`.monedula_pending.json`) next to this
module. This is the minimum durable store required by the spec. A
best-effort Neotoma `payment_event` mirror can be layered on top (gated
behind NEOTOMA_BASE_URL being set) but the JSON file is authoritative for
idempotency decisions — Neotoma writes must never block or fail a decision
that already has a definitive local answer.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

MARKERS_FILE = Path(__file__).parent / ".monedula_pending.json"

MarkerStatus = Literal["awaiting_approval", "approved", "skipped", "paid"]

_lock = threading.Lock()


@dataclass
class Marker:
    event_id: str
    date: str  # ISO date of the session (event end date), used in the key
    profile_name: str
    gmail_thread_id: str = ""
    gmail_message_id: str = ""
    notified_at: str = ""
    status: MarkerStatus = "awaiting_approval"
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return marker_key(self.event_id, self.date)


def marker_key(event_id: str, iso_date: str) -> str:
    return f"{event_id}::{iso_date}"


def _load_raw() -> dict:
    if not MARKERS_FILE.exists():
        return {}
    try:
        text = MARKERS_FILE.read_text().strip()
        if not text:
            return {}
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(f"Failed to read markers file {MARKERS_FILE}: {exc} — treating as empty")
        return {}


def _save_raw(data: dict) -> None:
    tmp = MARKERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(MARKERS_FILE)


def load_all() -> dict[str, Marker]:
    """Load all markers keyed by marker_key."""
    raw = _load_raw()
    out: dict[str, Marker] = {}
    for k, v in raw.items():
        try:
            out[k] = Marker(**v)
        except TypeError as exc:
            log.warning(f"Skipping malformed marker {k!r}: {exc}")
    return out


def get(event_id: str, iso_date: str) -> Marker | None:
    return load_all().get(marker_key(event_id, iso_date))


def exists(event_id: str, iso_date: str) -> bool:
    """True if a marker already exists for this session (any status)."""
    return get(event_id, iso_date) is not None


def save(marker: Marker) -> None:
    """Insert or overwrite a marker (keyed by event_id+date)."""
    with _lock:
        raw = _load_raw()
        raw[marker.key] = asdict(marker)
        _save_raw(raw)


_MARKER_FIELDS = {f.name for f in Marker.__dataclass_fields__.values()}


def update_status(event_id: str, iso_date: str, status: MarkerStatus, **extra_fields) -> None:
    """
    Update the status of an existing marker. Any keyword in extra_fields
    that is not a real Marker field (e.g. 'result_status') is merged into
    the marker's own 'extra' dict instead of being written top-level —
    writing an unknown top-level key would make the record unloadable as
    a Marker dataclass on the next read (see Marker(**v) in load_all()).
    """
    with _lock:
        raw = _load_raw()
        key = marker_key(event_id, iso_date)
        if key not in raw:
            log.warning(f"update_status: no marker for {key!r} — nothing to update")
            return
        raw[key]["status"] = status
        for k, v in extra_fields.items():
            if k in _MARKER_FIELDS:
                raw[key][k] = v
            else:
                raw[key].setdefault("extra", {})
                raw[key]["extra"][k] = v
        _save_raw(raw)


def pending_awaiting_approval() -> list[Marker]:
    """Return all markers currently awaiting an operator reply."""
    return [m for m in load_all().values() if m.status == "awaiting_approval"]


# ---------------------------------------------------------------------------
# Optional Neotoma payment_event mirror (best-effort, non-blocking)
# ---------------------------------------------------------------------------


def mirror_to_neotoma(marker: Marker) -> None:
    """
    Best-effort mirror of a marker into a Neotoma `payment_event` entity.

    Gated behind NEOTOMA_BASE_URL being set. Any failure is logged and
    swallowed — the JSON file remains the source of truth for idempotency,
    so a Neotoma outage must never block or duplicate a payment decision.
    """
    base_url = os.environ.get("NEOTOMA_BASE_URL", "").strip()
    if not base_url:
        return

    import shutil
    import subprocess

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.debug("neotoma CLI not found — skipping payment_event mirror")
        return

    payload = {
        "entity_type": "payment_event",
        "event_id": marker.event_id,
        "date": marker.date,
        "profile_name": marker.profile_name,
        "gmail_thread_id": marker.gmail_thread_id,
        "notified_at": marker.notified_at,
        "status": marker.status,
    }
    try:
        subprocess.run(
            [
                neotoma,
                "--api-only",
                "entities",
                "store",
                "--json",
                json.dumps(payload),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=os.environ,
        )
    except Exception as exc:
        log.debug(f"payment_event mirror failed (non-fatal): {exc}")
