"""Tests for connector schema registration — read-back gated."""

from __future__ import annotations

from typing import Any

from lib.connectors.schema_registration import (
    CONNECTOR_STATUS_SCHEMA,
    DEPLOYMENT_OBSERVATION_SCHEMA,
    diff_schema,
    register_connector_schemas,
    schemas_match_expected,
)
from lib.connectors.store import NeotomaUnavailable


class FakeSchemaStore:
    def __init__(self) -> None:
        self.schemas: dict[str, dict[str, Any]] = {}
        self.register_calls: list[dict[str, Any]] = []
        self.health_ok = True
        self.query_empty = True

    def health(self) -> dict[str, Any]:
        if not self.health_ok:
            raise NeotomaUnavailable("HTTP 502 from /health")
        return {"ok": True}

    def read_schema(self, entity_type: str) -> dict[str, Any] | None:
        return self.schemas.get(entity_type)

    def register_schema(
        self,
        entity_type: str,
        schema_definition: dict[str, Any],
        reducer_config: dict[str, Any],
        *,
        schema_version: str = "1.0",
        activate: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        self.register_calls.append(
            {
                "entity_type": entity_type,
                "schema_definition": schema_definition,
                "reducer_config": reducer_config,
                "schema_version": schema_version,
                "activate": activate,
                "force": force,
            }
        )
        self.schemas[entity_type] = {
            "entity_type": entity_type,
            "schema_version": schema_version,
            "active": activate,
            "schema_definition": schema_definition,
            "reducer_config": reducer_config,
        }
        return {"success": True, "entity_type": entity_type, "schema_version": schema_version}

    def query(self, entity_type: str, *, limit: int = 100, search: str | None = None):
        return [] if self.query_empty else [{"entity_id": "ent_x"}]


def _expected_as_persisted(expected: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_type": expected["entity_type"],
        "schema_version": expected["schema_version"],
        "active": True,
        "schema_definition": expected["schema_definition"],
        "reducer_config": expected["reducer_config"],
    }


def test_diff_reports_missing_schema():
    problems = diff_schema(CONNECTOR_STATUS_SCHEMA, None)
    assert problems
    assert "missing" in problems[0]


def test_diff_clean_when_contract_matches():
    persisted = _expected_as_persisted(DEPLOYMENT_OBSERVATION_SCHEMA)
    assert diff_schema(DEPLOYMENT_OBSERVATION_SCHEMA, persisted) == []


def test_diff_catches_wrong_identity():
    persisted = _expected_as_persisted(DEPLOYMENT_OBSERVATION_SCHEMA)
    persisted["schema_definition"] = {
        **persisted["schema_definition"],
        "canonical_name_fields": [{"composite": ["instance_ref", "version"]}],
    }
    problems = diff_schema(DEPLOYMENT_OBSERVATION_SCHEMA, persisted)
    assert any("canonical_name_fields" in p for p in problems)


def test_diff_rejects_mutable_reducer_on_deployment_observation():
    persisted = _expected_as_persisted(DEPLOYMENT_OBSERVATION_SCHEMA)
    persisted["reducer_config"] = {
        "merge_policies": {
            "status": {"strategy": "last_write", "tie_breaker": "observed_at"}
        }
    }
    problems = diff_schema(DEPLOYMENT_OBSERVATION_SCHEMA, persisted)
    assert any("immutable" in p for p in problems)


def test_preflight_failure_makes_no_register_call():
    store = FakeSchemaStore()
    store.health_ok = False
    summary = register_connector_schemas(store)  # type: ignore[arg-type]
    assert not summary.ok
    assert summary.error.startswith("preflight:")
    assert store.register_calls == []


def test_registers_then_verifies_by_read_back():
    store = FakeSchemaStore()
    summary = register_connector_schemas(store)  # type: ignore[arg-type]
    assert summary.ok
    assert len(store.register_calls) == 2
    assert all(v.verified for v in summary.verdicts)
    assert "no connector records observed yet" in summary.empty_records_note


def test_already_registered_skips_write():
    store = FakeSchemaStore()
    store.schemas[CONNECTOR_STATUS_SCHEMA["entity_type"]] = _expected_as_persisted(
        CONNECTOR_STATUS_SCHEMA
    )
    store.schemas[DEPLOYMENT_OBSERVATION_SCHEMA["entity_type"]] = _expected_as_persisted(
        DEPLOYMENT_OBSERVATION_SCHEMA
    )
    summary = register_connector_schemas(store)  # type: ignore[arg-type]
    assert summary.ok
    assert store.register_calls == []
    assert all(v.action == "already_registered" for v in summary.verdicts)


def test_dropped_field_on_read_back_fails_even_if_register_claimed_success():
    store = FakeSchemaStore()

    def bad_register(entity_type, schema_definition, reducer_config, **kwargs):
        store.register_calls.append({"entity_type": entity_type})
        # Persist a truncated schema — simulates silent field drop.
        truncated = {
            "entity_type": entity_type,
            "schema_version": kwargs.get("schema_version", "1.0"),
            "active": True,
            "schema_definition": {"fields": {}, "canonical_name_fields": []},
            "reducer_config": {"merge_policies": {}},
        }
        store.schemas[entity_type] = truncated
        return {"success": True}

    store.register_schema = bad_register  # type: ignore[method-assign]
    summary = register_connector_schemas(store)  # type: ignore[arg-type]
    assert not summary.ok
    assert "read_back" in summary.error
    assert any(not v.verified for v in summary.verdicts)


def test_schemas_match_expected_false_when_absent():
    store = FakeSchemaStore()
    ok, problems = schemas_match_expected(store)  # type: ignore[arg-type]
    assert not ok
    assert problems


def test_deployment_identity_is_instance_ref_plus_release_id():
    rules = DEPLOYMENT_OBSERVATION_SCHEMA["schema_definition"]["canonical_name_fields"]
    assert rules == [{"composite": ["instance_ref", "release_id"]}]


def test_connector_status_is_version_2():
    assert CONNECTOR_STATUS_SCHEMA["schema_version"] == "2.0"
    assert "connector_name" in CONNECTOR_STATUS_SCHEMA["schema_definition"]["fields"]
    assert "config_drift_detected" in CONNECTOR_STATUS_SCHEMA["schema_definition"]["fields"]
