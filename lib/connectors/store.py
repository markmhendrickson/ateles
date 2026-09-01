"""
lib/connectors/store.py — the Neotoma write path for connectors.

## The runaway this module is designed against

The previous GitHub sync produced 520+ duplicate issues and 35 orphaned
entities. The chain:

  1. ``ops.correct()`` passed ``{corrections: <map>}``;
  2. the server expects ``{entity_id, entity_type, field, value,
     idempotency_key}``;
  3. Zod rejected the payload **silently**;
  4. the caller read the non-error as success and re-corrected, in a loop.

Its push leg was disabled and never re-enabled. Three properties here break
that chain at three separate links:

  - **The correct payload shape**, matching ``lib/daemon_runtime/gating.py``,
    which is the in-repo reference implementation for ``/correct``.
  - **Deterministic idempotency keys** from stable identity, never a clock, so
    a re-run over unchanged data coalesces instead of duplicating.
  - **Read-back verification**, because ``success: true`` means "the request
    parsed", not "the data persisted" — a ``body`` field on a ``task`` was
    accepted with ``success: true`` and silently dropped on this instance.

``correct()`` for existing fields, ``store()`` only for new entities: writing a
``last_write`` field with ``store()`` clobbers concurrent updates
(neotoma#2033).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from .base import ConnectorStatus

log = logging.getLogger("connectors.store")

DEFAULT_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)

#: Cloudflare fronts the hosted instance and 1010-blocks urllib's default
#: User-Agent with a 403 "browser signature". Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-connectors/1.0"

STATUS_ENTITY_TYPE = "connector_status"


def content_hash(payload: "dict[str, Any]") -> str:
    """Short, stable hash of a record's content.

    Sorted keys so equal content hashes equally regardless of dict ordering.
    This is half of the idempotency key: identity says *which* record, the hash
    says *which version of it*, so an unchanged re-observation coalesces while
    a genuine change lands as a new one.
    """
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def idempotency_key(connector: str, external_id: str, payload: "dict[str, Any]") -> str:
    """``connector-{name}-{external_id}-{content_hash}`` — never a clock.

    A timestamp here would make every run's key unique, which is precisely how
    a re-run becomes a duplicate rather than a no-op.
    """
    safe_id = str(external_id).replace(" ", "-")[:80]
    return f"connector-{connector}-{safe_id}-{content_hash(payload)}"


class NeotomaUnavailable(RuntimeError):
    """Neotoma could not be reached or refused the request."""


class ConnectorStore:
    """Reads and writes connector records in Neotoma over plain HTTP.

    Uses ``urllib`` rather than ``httpx`` to stay stdlib-only, matching the
    daemon convention (``aquila``, ``phoenicurus-release``) so this runs under
    launchd without depending on the venv having extra packages.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: int = 30,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get(
            "NEOTOMA_BEARER_TOKEN", ""
        )
        self.timeout = timeout

    # ── transport ──────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        """Whether a write could even be attempted.

        An unconfigured store is reported, never silently skipped: "no token"
        and "nothing to report" must not look the same.
        """
        return bool(self.token and self.base_url)

    def _request(self, path: str, body: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        if not self.configured:
            raise NeotomaUnavailable("no NEOTOMA_BEARER_TOKEN configured")

        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", NEOTOMA_USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode() or "{}"
        except urllib.error.HTTPError as exc:
            # Never echo the response body: it can contain the request we sent,
            # and these messages are rendered in the app.
            hint = " (token may need rotation)" if exc.code == 401 else ""
            raise NeotomaUnavailable(f"HTTP {exc.code} from {path}{hint}") from exc
        except Exception as exc:  # noqa: BLE001
            raise NeotomaUnavailable(f"{type(exc).__name__} reaching {path}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NeotomaUnavailable(f"non-JSON response from {path}") from exc

    # ── entity writes ──────────────────────────────────────────────────────

    def store_entities(
        self, entities: "list[dict[str, Any]]", *, key: str
    ) -> "list[str]":
        """Create entities. Returns their ids.

        For NEW records only. Use ``correct_field`` to change an existing
        field — ``store()`` on a ``last_write`` field clobbers concurrent
        updates (neotoma#2033).
        """
        if not entities:
            return []
        payload = {"entities": entities, "idempotency_key": key}
        data = self._request("/store", payload)
        out: list[str] = []
        for ent in data.get("entities") or []:
            eid = ent.get("entity_id")
            if eid:
                out.append(str(eid))
        return out

    def correct_field(
        self,
        entity_id: str,
        entity_type: str,
        field: str,
        value: Any,
        *,
        key: str,
    ) -> bool:
        """Correct ONE field.

        The payload shape is the load-bearing detail: ``{entity_id,
        entity_type, field, value, idempotency_key}``. The runaway passed
        ``{corrections: <map>}``, which Zod rejected silently while the caller
        looped on the non-error.
        """
        self._request(
            "/correct",
            {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "field": field,
                "value": value,
                "idempotency_key": key,
            },
        )
        return True

    # ── entity reads ───────────────────────────────────────────────────────

    def query(
        self, entity_type: str, *, limit: int = 100, search: str | None = None
    ) -> "list[dict[str, Any]]":
        body: dict[str, Any] = {"entity_type": entity_type, "limit": limit}
        if search:
            body["search"] = search
        data = self._request("/entities/query", body)
        ents = data.get("entities")
        return list(ents) if isinstance(ents, list) else []

    @staticmethod
    def _fields_of(entity: "dict[str, Any]") -> "dict[str, Any]":
        """Unwrap the nested snapshot shape Neotoma returns."""
        snap = entity.get("snapshot")
        if isinstance(snap, dict):
            inner = snap.get("snapshot")
            if isinstance(inner, dict):
                return inner
            return snap
        return entity

    def verify_stored(
        self, entity_type: str, field: str, expected: Any, *, search: str | None = None
    ) -> bool:
        """Read back one field and confirm it actually persisted.

        This exists because ``success: true`` is not evidence: a ``body`` field
        on a ``task`` was accepted with ``success: true`` and silently dropped.
        A write is verified or it is unverified — there is no third option that
        a success code satisfies.
        """
        try:
            for ent in self.query(entity_type, limit=25, search=search):
                if self._fields_of(ent).get(field) == expected:
                    return True
        except NeotomaUnavailable as exc:
            log.warning(f"read-back verification unavailable: {exc}")
        return False

    # ── connector_status ───────────────────────────────────────────────────

    def read_status(self, connector_name: str) -> ConnectorStatus | None:
        """Current status for one connector, or None if never recorded."""
        try:
            for ent in self.query(
                STATUS_ENTITY_TYPE, limit=50, search=connector_name
            ):
                f = self._fields_of(ent)
                if f.get("connector_name") != connector_name:
                    continue
                return ConnectorStatus(
                    connector_name=connector_name,
                    status=str(f.get("status") or "never_run"),
                    last_attempt_at=f.get("last_attempt_at"),
                    last_success_at=f.get("last_success_at"),
                    last_error=str(f.get("last_error") or ""),
                    records_written=int(f.get("records_written") or 0),
                    poll_interval_seconds=int(f.get("poll_interval_seconds") or 0),
                    stale_after_seconds=int(f.get("stale_after_seconds") or 0),
                    consecutive_failures=int(f.get("consecutive_failures") or 0),
                    entity_id=str(ent.get("entity_id") or "") or None,
                )
        except NeotomaUnavailable as exc:
            log.warning(f"could not read status for {connector_name}: {exc}")
        return None

    def write_status(self, status: ConnectorStatus) -> None:
        """Persist a connector's status, correcting in place when it exists.

        One entity per connector, keyed on ``connector_name`` — corrected
        field-by-field on update so concurrent writers cannot clobber each
        other, created only when genuinely absent.
        """
        fields = status.to_entity_fields()
        existing = self.read_status(status.connector_name)

        if existing is None or not existing.entity_id:
            self.store_entities(
                [{"entity_type": STATUS_ENTITY_TYPE, **fields}],
                key=idempotency_key(status.connector_name, "status", fields),
            )
            self._verify_status_written(status)
            return

        entity_id = existing.entity_id
        for field_name, value in fields.items():
            if field_name == "connector_name" or value is None:
                continue
            try:
                self.correct_field(
                    entity_id,
                    STATUS_ENTITY_TYPE,
                    field_name,
                    value,
                    key=self._status_field_key(status.connector_name, field_name, value),
                )
            except NeotomaUnavailable as exc:
                log.warning(f"status field {field_name!r} not written: {exc}")
        self._verify_status_written(status)

    @staticmethod
    def _status_field_key(connector_name: str, field_name: str, value: Any) -> str:
        """Stable key for one status field correction.

        The field and value are the operation identity. Adding the run's
        attempt timestamp to every key would make unchanged fields look new on
        every run, which is the duplicate-write pattern this module avoids.
        """
        return idempotency_key(
            connector_name,
            f"status-{field_name}",
            {"field": field_name, "value": value},
        )

    def _verify_status_written(self, status: ConnectorStatus) -> None:
        """Confirm the status row's observable fields survived the write."""
        expected = status.to_entity_fields()
        for entity in self.query(STATUS_ENTITY_TYPE, limit=25, search=status.connector_name):
            fields = self._fields_of(entity)
            if fields.get("connector_name") != status.connector_name:
                continue
            missing = {
                key: value
                for key, value in expected.items()
                if value is not None and fields.get(key) != value
            }
            if not missing:
                return
            log.warning(
                "connector_status read-back mismatch for %s: %s",
                status.connector_name,
                ", ".join(sorted(missing)),
            )
            break

        raise NeotomaUnavailable(
            f"connector_status read-back verification failed for {status.connector_name}"
        )
