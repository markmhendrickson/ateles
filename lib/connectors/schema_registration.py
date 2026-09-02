"""
lib/connectors/schema_registration.py — expected Neotoma schemas for connectors.

Registration is read-back gated: ``success: true`` from ``POST /register_schema``
is never enough. The CLI and library both compare the persisted active schema
to these constants before reporting verified.

``connector_status`` is registered as **2.0** so the legacy chat/provider shape
still present as active 1.0 is superseded rather than silently field-accreted.
``deployment_observation`` identity is ``instance_ref + release_id`` (not
version) so same-label distinct images (v15/v16) do not collapse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .store import ConnectorStore, NeotomaUnavailable

CONNECTOR_STATUS_TYPE = "connector_status"
DEPLOYMENT_OBSERVATION_TYPE = "deployment_observation"

#: New active version — supersedes legacy chat/provider ``connector_status`` 1.0.
CONNECTOR_STATUS_VERSION = "2.0"
DEPLOYMENT_OBSERVATION_VERSION = "1.0"

_SCHEMA_MISSING = "missing"


def _field(type_name: str, *, required: bool = False, description: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"type": type_name, "required": required}
    if description:
        out["description"] = description
    return out


def _last_write_policies(*names: str) -> dict[str, Any]:
    return {
        name: {"strategy": "last_write", "tie_breaker": "observed_at"} for name in names
    }


CONNECTOR_STATUS_FIELDS: dict[str, Any] = {
    "connector_name": _field(
        "string", required=True, description="Stable connector identity"
    ),
    "status": _field(
        "string",
        required=True,
        description="Allowed values: ok, failing, never_run",
    ),
    "last_attempt_at": _field("date"),
    "last_success_at": _field("date"),
    "last_error": _field(
        "string", description="One line, no secrets — rendered in the app"
    ),
    "records_written": _field("number"),
    "poll_interval_seconds": _field("number"),
    "stale_after_seconds": _field("number"),
    "consecutive_failures": _field("number"),
    "ingestion_mode": _field("string", description="poll or hybrid"),
    "last_push_at": _field("date"),
    # Fly Stage 2a extensions on the same connector_status row.
    "instance_ref": _field(
        "string", description="Opaque Neotoma-resolved instance reference"
    ),
    "machine_memory_mb": _field("number"),
    "machine_cpus": _field("number"),
    "machine_cpu_kind": _field("string"),
    "machine_health_check_count": _field("number"),
    "machine_count": _field("number"),
    "release_count_observed": _field("number"),
    "fly_observed_at": _field("date"),
    "config_drift_detected": _field("boolean"),
    "config_drift_messages": _field("string"),
    "config_drift_notes": _field("string"),
    "config_drift_warnings": _field("string"),
}

CONNECTOR_STATUS_MUTABLE = tuple(
    name for name in CONNECTOR_STATUS_FIELDS if name != "connector_name"
)

CONNECTOR_STATUS_SCHEMA: dict[str, Any] = {
    "entity_type": CONNECTOR_STATUS_TYPE,
    "schema_version": CONNECTOR_STATUS_VERSION,
    "schema_definition": {
        "fields": CONNECTOR_STATUS_FIELDS,
        "canonical_name_fields": ["connector_name"],
    },
    "reducer_config": {
        "merge_policies": _last_write_policies(*CONNECTOR_STATUS_MUTABLE),
    },
}

DEPLOYMENT_OBSERVATION_FIELDS: dict[str, Any] = {
    "source": _field("string", required=True, description="Observation source, e.g. fly"),
    "instance_ref": _field(
        "string",
        required=True,
        description="Opaque Neotoma-resolved instance reference; not a hostname",
    ),
    "release_id": _field(
        "string",
        required=True,
        description="Stable release identity (Fly release ID); half of composite identity",
    ),
    "version": _field("string", required=True, description="Release version label"),
    "release_version": _field("string"),
    "image_ref": _field("string", required=True),
    "deployed_at": _field("date", required=True),
    "status": _field("string", required=True),
    "triggered_by": _field(
        "string",
        description="Opaque actor ref (hashed); never a raw email address",
    ),
    "observed_at": _field("date", required=True),
    "observed_by": _field("string"),
    "connector_name": _field("string"),
    "image_changed_from_previous_release": _field("boolean"),
}

DEPLOYMENT_OBSERVATION_SCHEMA: dict[str, Any] = {
    "entity_type": DEPLOYMENT_OBSERVATION_TYPE,
    "schema_version": DEPLOYMENT_OBSERVATION_VERSION,
    "schema_definition": {
        "fields": DEPLOYMENT_OBSERVATION_FIELDS,
        "canonical_name_fields": [{"composite": ["instance_ref", "release_id"]}],
    },
    "reducer_config": {"merge_policies": {}},
}

EXPECTED_CONNECTOR_SCHEMAS: tuple[dict[str, Any], ...] = (
    CONNECTOR_STATUS_SCHEMA,
    DEPLOYMENT_OBSERVATION_SCHEMA,
)


@dataclass
class SchemaVerdict:
    entity_type: str
    action: str  # registered | already_registered | failed
    verified: bool
    phase: str = ""  # preflight | register | read_back | ""
    identity: str = ""
    mutable: str = ""
    reducer: str = ""
    problems: list[str] = field(default_factory=list)
    read_back_at: str = ""
    schema_version: str = ""


@dataclass
class RegistrationSummary:
    ok: bool
    verdicts: list[SchemaVerdict] = field(default_factory=list)
    empty_records_note: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "empty_records_note": self.empty_records_note,
            "schemas": [
                {
                    "entity_type": v.entity_type,
                    "action": v.action,
                    "verified": v.verified,
                    "phase": v.phase,
                    "identity": v.identity,
                    "mutable": v.mutable,
                    "reducer": v.reducer,
                    "problems": v.problems,
                    "read_back_at": v.read_back_at,
                    "schema_version": v.schema_version,
                }
                for v in self.verdicts
            ],
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persisted_fields(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not schema:
        return {}
    definition = schema.get("schema_definition") or {}
    fields = definition.get("fields") or schema.get("fields") or {}
    return fields if isinstance(fields, dict) else {}


def _persisted_identity(schema: dict[str, Any] | None) -> Any:
    if not schema:
        return None
    definition = schema.get("schema_definition") or {}
    return definition.get("canonical_name_fields")


def _persisted_reducers(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not schema:
        return {}
    reducer = schema.get("reducer_config") or {}
    policies = reducer.get("merge_policies")
    return policies if isinstance(policies, dict) else {}


def _field_type(meta: Any) -> str:
    if isinstance(meta, dict):
        return str(meta.get("type") or "")
    return ""


def _field_required(meta: Any) -> bool:
    return bool(isinstance(meta, dict) and meta.get("required"))


def diff_schema(
    expected: dict[str, Any], persisted: dict[str, Any] | None
) -> list[str]:
    """Return human-readable mismatch lines; empty means contract matches."""
    problems: list[str] = []
    if persisted is None:
        problems.append(
            f"{expected['entity_type']}: schema {_SCHEMA_MISSING} "
            f"(expected version {expected['schema_version']})"
        )
        return problems

    entity_type = expected["entity_type"]
    want_fields = (expected["schema_definition"] or {}).get("fields") or {}
    got_fields = _persisted_fields(persisted)
    for name, meta in want_fields.items():
        if name not in got_fields:
            problems.append(
                f"{entity_type}.{name}: expected type={_field_type(meta)} "
                f"required={_field_required(meta)}; observed={_SCHEMA_MISSING}"
            )
            continue
        got = got_fields[name]
        if _field_type(got) and _field_type(got) != _field_type(meta):
            problems.append(
                f"{entity_type}.{name}: expected type={_field_type(meta)}; "
                f"observed type={_field_type(got)}"
            )
        if _field_required(meta) and not _field_required(got):
            problems.append(
                f"{entity_type}.{name}: expected required=true; observed required=false"
            )

    want_identity = (expected["schema_definition"] or {}).get("canonical_name_fields")
    got_identity = _persisted_identity(persisted)
    if got_identity != want_identity:
        problems.append(
            f"{entity_type}.canonical_name_fields: expected={want_identity!r}; "
            f"observed={got_identity!r}"
        )

    want_reducers = (expected.get("reducer_config") or {}).get("merge_policies") or {}
    got_reducers = _persisted_reducers(persisted)
    if want_reducers == {}:
        if got_reducers:
            problems.append(
                f"{entity_type}.reducer_config: expected empty merge_policies "
                f"(immutable); observed keys={sorted(got_reducers)}"
            )
    else:
        for field_name, policy in want_reducers.items():
            got = got_reducers.get(field_name)
            if got != policy:
                problems.append(
                    f"{entity_type}.reducer.{field_name}: expected={policy!r}; "
                    f"observed={got!r}"
                )

    if not persisted.get("active", True):
        problems.append(f"{entity_type}: expected active=true; observed active=false")

    return problems


def schemas_match_expected(store: ConnectorStore) -> tuple[bool, list[str]]:
    """Read-only contract check used to gate Fly writes."""
    problems: list[str] = []
    for expected in EXPECTED_CONNECTOR_SCHEMAS:
        try:
            persisted = store.read_schema(expected["entity_type"])
        except NeotomaUnavailable as exc:
            return False, [f"preflight: {exc}"]
        problems.extend(diff_schema(expected, persisted))
    return (not problems), problems


def _identity_summary(expected: dict[str, Any]) -> str:
    rules = (expected["schema_definition"] or {}).get("canonical_name_fields")
    return repr(rules)


def _mutable_summary(expected: dict[str, Any]) -> str:
    reducers = (expected.get("reducer_config") or {}).get("merge_policies") or {}
    if not reducers:
        return "immutable (append-only)"
    return f"mutable last_write ({len(reducers)} fields)"


#: Entity types whose names the server pluralization lint false-positives on.
#: "Status" is not a plural; registration still requires ``force: true``.
_FORCE_REGISTER_TYPES = frozenset({CONNECTOR_STATUS_TYPE})


def register_connector_schemas(store: ConnectorStore) -> RegistrationSummary:
    """Preflight → read → register-if-needed → read-back → verify."""
    summary = RegistrationSummary(ok=False)
    try:
        store.health()
    except NeotomaUnavailable as exc:
        summary.error = f"preflight: {exc} — no schema write attempted"
        return summary

    for expected in EXPECTED_CONNECTOR_SCHEMAS:
        entity_type = expected["entity_type"]
        identity = _identity_summary(expected)
        mutable = _mutable_summary(expected)
        reducer = (
            "empty"
            if not (expected.get("reducer_config") or {}).get("merge_policies")
            else "last_write+observed_at"
        )
        try:
            before = store.read_schema(entity_type)
        except NeotomaUnavailable as exc:
            summary.verdicts.append(
                SchemaVerdict(
                    entity_type=entity_type,
                    action="failed",
                    verified=False,
                    phase="preflight",
                    identity=identity,
                    mutable=mutable,
                    reducer=reducer,
                    problems=[str(exc)],
                )
            )
            summary.error = f"preflight: could not read {entity_type}"
            return summary

        before_problems = diff_schema(expected, before)
        action = "already_registered"
        if before_problems:
            action = "registered"
            try:
                store.register_schema(
                    entity_type,
                    expected["schema_definition"],
                    expected["reducer_config"],
                    schema_version=expected["schema_version"],
                    activate=True,
                    force=entity_type in _FORCE_REGISTER_TYPES,
                )
            except NeotomaUnavailable as exc:
                summary.verdicts.append(
                    SchemaVerdict(
                        entity_type=entity_type,
                        action="failed",
                        verified=False,
                        phase="register",
                        identity=identity,
                        mutable=mutable,
                        reducer=reducer,
                        problems=[str(exc), *before_problems],
                        schema_version=expected["schema_version"],
                    )
                )
                summary.error = f"register: {entity_type} failed"
                return summary

        try:
            after = store.read_schema(entity_type)
        except NeotomaUnavailable as exc:
            summary.verdicts.append(
                SchemaVerdict(
                    entity_type=entity_type,
                    action="failed",
                    verified=False,
                    phase="read_back",
                    identity=identity,
                    mutable=mutable,
                    reducer=reducer,
                    problems=[str(exc)],
                    schema_version=expected["schema_version"],
                )
            )
            summary.error = f"read_back: {entity_type} unavailable — registration is not verified"
            return summary

        after_problems = diff_schema(expected, after)
        summary.verdicts.append(
            SchemaVerdict(
                entity_type=entity_type,
                action=action if not after_problems else "failed",
                verified=not after_problems,
                phase="" if not after_problems else "read_back",
                identity=identity,
                mutable=mutable,
                reducer=reducer,
                problems=after_problems,
                read_back_at=_now_iso(),
                schema_version=str(
                    (after or {}).get("schema_version") or expected["schema_version"]
                ),
            )
        )
        if after_problems:
            summary.error = (
                f"read_back: {entity_type} contract mismatch — registration is not verified"
            )
            return summary

    # Empty-record note is informational only.
    notes: list[str] = []
    for entity_type in (CONNECTOR_STATUS_TYPE, DEPLOYMENT_OBSERVATION_TYPE):
        try:
            rows = store.query(entity_type, limit=1)
        except NeotomaUnavailable:
            continue
        if not rows:
            notes.append(
                f"{entity_type}: schema registered, no connector records observed yet"
            )
    summary.empty_records_note = "; ".join(notes)
    summary.ok = all(v.verified for v in summary.verdicts)
    return summary
