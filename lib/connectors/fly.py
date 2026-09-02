"""
lib/connectors/fly.py — observe Fly releases and machine config into Neotoma.

Stage 2a: release history (one immutable ``deployment_observation`` per release)
and machine guest config compared against the committed ``[[vm]]`` block.
Read-only against Fly — never deploy, resize, or restart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .base import ConnectorResult
from .fly_config_drift import (
    MachineDriftResult,
    MachineGuest,
    compare_all_machines,
    parse_vm_want_from_path,
)
from .schema_registration import schemas_match_expected
from .store import ConnectorStore, NeotomaUnavailable, idempotency_key

log = logging.getLogger("connectors.fly")

CONNECTOR_NAME = "fly"
POLL_INTERVAL_SECONDS = 900
DEPLOYMENT_OBSERVATION_TYPE = "deployment_observation"
STATUS_ENTITY_TYPE = "connector_status"

_SKIP_BINDING_MSG = (
    "set FLY_APP env or create a deployment_configuration entity "
    "(DEPLOYMENT_CONFIGURATION_ID)"
)
_SCHEMA_GATE_MSG = (
    "connector schemas not verified — run "
    "python3 execution/daemons/connectors/register_schemas.py"
)


class StoreOutcome(str, Enum):
    STORED = "stored"
    DUPLICATE = "duplicate"
    REFUSED = "refused"


@dataclass(frozen=True)
class FlyBinding:
    """Runtime-resolved target. No app names committed in repo."""

    fly_app: str
    instance_ref: str
    config_path: Path


@dataclass(frozen=True)
class SkipBinding:
    reason: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _opaque_ref(label: str, value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:16]
    return f"{label}:{digest}"


def resolve_fly_binding(store: ConnectorStore) -> FlyBinding | SkipBinding:
    """Resolve Fly app + opaque instance_ref from env or one explicit entity."""
    config_id = os.environ.get("DEPLOYMENT_CONFIGURATION_ID", "").strip()
    if config_id:
        entity = _fetch_deployment_configuration(store, config_id)
        if entity is None:
            return SkipBinding(
                reason=f"deployment_configuration {config_id!r} not found"
            )
        return _binding_from_deployment_configuration(entity)

    fly_app = os.environ.get("FLY_APP", "").strip()
    if fly_app:
        config_path = _config_path_from_env()
        return FlyBinding(
            fly_app=fly_app,
            instance_ref=_opaque_ref("env", fly_app),
            config_path=config_path,
        )

    return SkipBinding(reason=_SKIP_BINDING_MSG)


def _config_path_from_env() -> Path:
    raw = os.environ.get("FLY_CONFIG_PATH", "fly.toml").strip() or "fly.toml"
    return Path(raw)


def _fetch_deployment_configuration(
    store: ConnectorStore, entity_id: str
) -> dict[str, Any] | None:
    try:
        for ent in store.query("deployment_configuration", limit=25, search=entity_id):
            if str(ent.get("entity_id") or "") == entity_id:
                return store._fields_of(ent)
            fields = store._fields_of(ent)
            if str(fields.get("entity_id") or "") == entity_id:
                return fields
    except NeotomaUnavailable as exc:
        log.warning("deployment_configuration lookup failed: %s", exc)
    return None


def _binding_from_deployment_configuration(
    fields: dict[str, Any],
) -> FlyBinding | SkipBinding:
    fly_app = (
        str(fields.get("fly_app") or fields.get("app_name") or fields.get("fly_app_name") or "")
        .strip()
    )
    if not fly_app:
        return SkipBinding(reason="deployment_configuration missing fly_app")

    entity_id = str(fields.get("entity_id") or "").strip()
    instance_ref = entity_id or _opaque_ref("cfg", fly_app)

    config_raw = (
        fields.get("fly_config_path")
        or fields.get("config_path")
        or fields.get("fly_config")
        or "fly.toml"
    )
    return FlyBinding(
        fly_app=fly_app,
        instance_ref=instance_ref,
        config_path=Path(str(config_raw)),
    )


def run_flyctl_json(args: list[str], *, app: str, timeout: int = 60) -> Any:
    """Read-only flyctl invocation. Raises on missing binary or non-zero exit."""
    if not shutil.which("flyctl"):
        raise RuntimeError("flyctl not on PATH")
    cmd = ["flyctl", *args, "--app", app, "--json"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        if "401" in stderr or "not logged in" in stderr.lower():
            raise RuntimeError("Fly API auth failed — check FLY_API_TOKEN")
        raise RuntimeError(f"flyctl failed ({proc.returncode}): {stderr[:200]}")
    return json.loads(proc.stdout or "null")


def parse_releases(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def parse_machines(raw: Any) -> list[MachineGuest]:
    out: list[MachineGuest] = []
    if not isinstance(raw, list):
        return out
    for machine in raw:
        if not isinstance(machine, dict):
            continue
        config = machine.get("config") or {}
        guest = config.get("guest") or {}
        # Match check_fly_config_drift.sh: ((.config.checks // {}) | length).
        # Also count checks nested under services when the top-level map is empty.
        checks = config.get("checks") or {}
        check_count = len(checks) if isinstance(checks, dict) else 0
        if check_count == 0:
            for service in config.get("services") or []:
                if not isinstance(service, dict):
                    continue
                svc_checks = service.get("checks") or []
                if isinstance(svc_checks, list):
                    check_count += len(svc_checks)
                elif isinstance(svc_checks, dict):
                    check_count += len(svc_checks)
        out.append(
            MachineGuest(
                memory_mb=int(guest.get("memory_mb") or 0),
                cpus=int(guest.get("cpus") or 0),
                cpu_kind=str(guest.get("cpu_kind") or "unknown"),
                health_check_count=check_count,
            )
        )
    return out


def release_observation_fields(
    release: dict[str, Any],
    *,
    instance_ref: str,
    observed_at: str,
    image_changed_from_previous: bool,
) -> dict[str, Any]:
    user = release.get("User") or release.get("user") or {}
    raw_actor = (
        user.get("email")
        or user.get("name")
        or user.get("Email")
        or user.get("Name")
        or ""
    )
    # Never persist a raw email — hash to an opaque actor ref (arch privacy).
    triggered_by = (
        _opaque_ref("actor", str(raw_actor)) if str(raw_actor).strip() else ""
    )
    image_ref = str(release.get("ImageRef") or release.get("imageRef") or "")
    version = str(release.get("Version") or release.get("version") or "")
    release_version = version
    deployed_at = str(release.get("CreatedAt") or release.get("createdAt") or "")
    status = str(release.get("Status") or release.get("status") or "")
    release_id = str(release.get("ID") or release.get("id") or version or image_ref)

    return {
        "entity_type": DEPLOYMENT_OBSERVATION_TYPE,
        "source": "fly",
        "instance_ref": instance_ref,
        "release_id": release_id,
        "version": version,
        "release_version": release_version,
        "image_ref": image_ref,
        "deployed_at": deployed_at,
        "status": status,
        "triggered_by": triggered_by,
        "observed_at": observed_at,
        "observed_by": CONNECTOR_NAME,
        "connector_name": CONNECTOR_NAME,
        "image_changed_from_previous_release": image_changed_from_previous,
    }


class FlyConnector:
    name = CONNECTOR_NAME
    poll_interval_seconds = POLL_INTERVAL_SECONDS
    ingestion_mode = "poll"

    def __init__(self, store: ConnectorStore | None = None) -> None:
        self._store = store or ConnectorStore()

    def observe(self) -> ConnectorResult:
        try:
            return self._observe_impl()
        except Exception as exc:  # noqa: BLE001 — contract: never raise
            log.exception("[%s] observe() failed", CONNECTOR_NAME)
            return ConnectorResult.failure(f"{type(exc).__name__}: {exc}")

    def _observe_impl(self) -> ConnectorResult:
        binding = resolve_fly_binding(self._store)
        if isinstance(binding, SkipBinding):
            return ConnectorResult.skipped(binding.reason)

        if not self._store.configured:
            return ConnectorResult.failure("no NEOTOMA_BEARER_TOKEN configured")

        schemas_ok, schema_problems = schemas_match_expected(self._store)
        if not schemas_ok:
            detail = "; ".join(schema_problems[:3]) if schema_problems else ""
            return ConnectorResult.failure(
                f"{_SCHEMA_GATE_MSG}" + (f" ({detail})" if detail else ""),
                schema_problems=schema_problems,
            )

        observed_at = _now_iso()
        partial_failures: list[str] = []
        records_written = 0
        detail: dict[str, Any] = {}

        releases_raw: list[dict[str, Any]] = []
        machines: list[MachineGuest] = []
        drift: MachineDriftResult | None = None

        try:
            releases_raw = parse_releases(
                run_flyctl_json(["releases"], app=binding.fly_app)
            )
        except Exception as exc:  # noqa: BLE001
            partial_failures.append(f"releases: {exc}")

        try:
            machines = parse_machines(
                run_flyctl_json(["machine", "list"], app=binding.fly_app)
            )
        except Exception as exc:  # noqa: BLE001
            partial_failures.append(f"machine_api: {exc}")

        if binding.config_path.is_file() and machines:
            try:
                want = parse_vm_want_from_path(binding.config_path)
                drift = compare_all_machines(want, machines)
                detail["config_drift_detected"] = drift.drift
            except Exception as exc:  # noqa: BLE001
                partial_failures.append(f"config_drift: {exc}")
        elif binding.config_path.is_file() and not machines:
            partial_failures.append("config_drift: no machines to compare")
        elif not binding.config_path.is_file():
            partial_failures.append(
                f"config_drift: config file missing ({binding.config_path})"
            )

        ordered = _releases_chronological(releases_raw)
        prev_image: str | None = None
        new_releases = 0
        refused_releases = 0
        duplicate_releases = 0
        for release in ordered:
            image_ref = str(release.get("ImageRef") or release.get("imageRef") or "")
            fields = release_observation_fields(
                release,
                instance_ref=binding.instance_ref,
                observed_at=observed_at,
                image_changed_from_previous=bool(
                    prev_image and image_ref and image_ref != prev_image
                ),
            )
            prev_image = image_ref or prev_image
            # observed_at / observed_by / connector_name are already stripped by
            # observation_payload(); deployed_at is a Fly-stable source timestamp.
            key = idempotency_key(
                CONNECTOR_NAME,
                str(fields["release_id"]),
                fields,
            )
            outcome = self._store_release(fields, key=key)
            if outcome is StoreOutcome.STORED:
                records_written += 1
                new_releases += 1
            elif outcome is StoreOutcome.DUPLICATE:
                duplicate_releases += 1
            else:
                refused_releases += 1
                rid = fields.get("release_id")
                partial_failures.append(
                    f"releases: store refused for release_id={rid}"
                )

        self._write_fly_status_fields(
            binding,
            observed_at=observed_at,
            machines=machines,
            drift=drift,
            release_count=len(releases_raw),
        )

        detail["partial_failures"] = partial_failures
        detail["new_releases"] = new_releases
        detail["release_count"] = len(releases_raw)
        detail["refused_releases"] = refused_releases
        # idempotent_note only for verified duplicates — never for refusals.
        if new_releases == 0 and duplicate_releases > 0 and refused_releases == 0:
            detail["idempotent_note"] = "0 new releases"

        if refused_releases > 0 and new_releases == 0:
            return ConnectorResult.failure(
                "; ".join(partial_failures[:3])
                or "release store/query refused",
                **detail,
            )

        ok = bool(releases_raw) or bool(machines) or drift is not None
        if not ok and partial_failures:
            return ConnectorResult.failure(
                "; ".join(partial_failures[:3]),
                **detail,
            )
        if partial_failures and (releases_raw or machines):
            detail["partial_failures"] = partial_failures
            if refused_releases > 0:
                return ConnectorResult.failure(
                    "; ".join(partial_failures[:3]),
                    **detail,
                )
        return ConnectorResult.success(records_written=records_written, **detail)

    def _store_release(self, fields: dict[str, Any], *, key: str) -> StoreOutcome:
        """Store one release observation and verify read-back."""
        already = self._release_already_stored(fields)
        if already is True:
            return StoreOutcome.DUPLICATE
        if already is None:
            # Query failed — do not invent a duplicate under a new key, and do
            # not present this as an idempotent no-op.
            log.warning(
                "skipping release store; cannot verify prior existence for %s",
                fields.get("release_id"),
            )
            return StoreOutcome.REFUSED
        try:
            ids = self._store.store_entities([fields], key=key)
        except NeotomaUnavailable as exc:
            log.warning("release store failed: %s", exc)
            return StoreOutcome.REFUSED
        if not ids:
            return StoreOutcome.REFUSED
        release_id = str(fields.get("release_id") or "")
        if release_id and self._store.verify_stored(
            DEPLOYMENT_OBSERVATION_TYPE,
            "release_id",
            release_id,
            search=release_id,
        ):
            return StoreOutcome.STORED
        log.warning(
            "release read-back verification failed for release_id=%s",
            fields.get("release_id"),
        )
        return StoreOutcome.REFUSED

    def _release_already_stored(self, fields: dict[str, Any]) -> bool | None:
        """True if present, False if absent, None if Neotoma could not be queried."""
        release_id = str(fields.get("release_id") or "")
        if not release_id:
            return False
        try:
            for ent in self._store.query(
                DEPLOYMENT_OBSERVATION_TYPE, limit=50, search=release_id
            ):
                stored = self._store._fields_of(ent)
                if stored.get("release_id") != release_id:
                    continue
                if stored.get("image_ref") == fields.get("image_ref") and stored.get(
                    "version"
                ) == fields.get("version"):
                    return True
        except NeotomaUnavailable:
            return None
        return False

    def _write_fly_status_fields(
        self,
        binding: FlyBinding,
        *,
        observed_at: str,
        machines: list[MachineGuest],
        drift: MachineDriftResult | None,
        release_count: int,
    ) -> None:
        primary = machines[0] if machines else MachineGuest()
        fields: dict[str, Any] = {
            "connector_name": CONNECTOR_NAME,
            "instance_ref": binding.instance_ref,
            "machine_memory_mb": primary.memory_mb,
            "machine_cpus": primary.cpus,
            "machine_cpu_kind": primary.cpu_kind,
            "machine_health_check_count": primary.health_check_count,
            "machine_count": len(machines),
            "release_count_observed": release_count,
            "fly_observed_at": observed_at,
        }
        if drift is not None:
            fields["config_drift_detected"] = drift.drift
            if drift.messages:
                fields["config_drift_messages"] = "; ".join(drift.messages)
            if drift.notes:
                fields["config_drift_notes"] = "; ".join(drift.notes)
            if drift.warnings:
                fields["config_drift_warnings"] = "; ".join(drift.warnings)

        existing = self._store.read_status(CONNECTOR_NAME)
        if existing is None or not existing.entity_id:
            payload = {"entity_type": STATUS_ENTITY_TYPE, **fields}
            try:
                self._store.store_entities(
                    [payload],
                    key=idempotency_key(CONNECTOR_NAME, "status-init", payload),
                )
            except NeotomaUnavailable as exc:
                log.warning("fly status create failed: %s", exc)
            return

        entity_id = existing.entity_id
        for field_name, value in fields.items():
            if field_name == "connector_name" or value is None:
                continue
            try:
                self._store.correct_field(
                    entity_id,
                    STATUS_ENTITY_TYPE,
                    field_name,
                    value,
                    key=self._store._status_field_key(CONNECTOR_NAME, field_name, value),
                )
            except NeotomaUnavailable as exc:
                log.warning("fly status field %r not written: %s", field_name, exc)


def _release_sort_key(release: dict[str, Any]) -> tuple[int, str]:
    version = release.get("Version") or release.get("version") or 0
    try:
        version_num = int(version)
    except (TypeError, ValueError):
        version_num = 0
    deployed = str(release.get("CreatedAt") or release.get("createdAt") or "")
    return (version_num, deployed)


def _releases_chronological(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(releases, key=_release_sort_key)
