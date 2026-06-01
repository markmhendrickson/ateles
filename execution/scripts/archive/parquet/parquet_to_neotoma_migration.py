from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import which
from typing import Any

from parquet_client import ParquetMCPClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMAS_DIR = REPO_ROOT / "data" / "schemas"

PAGE_SIZE = 500
NEOTOMA_PAGE_SIZE = 200

PILOT_TYPES = ("tasks", "contacts", "locations")
WAVE_A_TYPES = (
    "task_stories",
    "transactions",
    "account_identifiers",
    "sets",
    "user_accounts",
    "companies",
    "transcriptions",
)

ENTITY_TYPE_OVERRIDES = {
    "contacts": "contact",
    "companies": "company",
    "people": "person",
    "posts": "post",
    "purchases": "purchase",
    "locations": "location",
    "messages": "message",
    "tasks": "task",
    "transactions": "transaction",
}

DISPLAY_FIELD_CANDIDATES = (
    "title",
    "name",
    "full_name",
    "item_name",
    "slug",
    "description",
)

PRIMARY_KEY_OVERRIDES = {
    "task_stories": "story_id",
    "transactions": "transaction_id",
    "contacts": "contact_id",
    "companies": "company_id",
    "people": "person_id",
    "messages": "message_id",
    "purchases": "purchase_id",
    "locations": "location_id",
    "posts": "slug",
}


@dataclass(frozen=True)
class MigrationMatrixEntry:
    data_type: str
    target_entity_type: str
    row_count: int
    primary_key: str | None
    display_field: str | None
    wave: str
    strategy: str


def get_parquet_client() -> ParquetMCPClient:
    return ParquetMCPClient()


def singularize_data_type(data_type: str) -> str:
    if data_type in ENTITY_TYPE_OVERRIDES:
        return ENTITY_TYPE_OVERRIDES[data_type]
    if data_type.endswith("ies") and len(data_type) > 3:
        return f"{data_type[:-3]}y"
    if data_type.endswith("ses") and len(data_type) > 3:
        return data_type[:-2]
    if data_type.endswith("s") and len(data_type) > 1:
        return data_type[:-1]
    return data_type


def infer_entity_type(data_type: str) -> str:
    return ENTITY_TYPE_OVERRIDES.get(data_type, singularize_data_type(data_type))


def infer_primary_key(data_type: str, schema: dict[str, Any]) -> str | None:
    fields = list(schema)
    singular = singularize_data_type(data_type)
    candidates = [
        PRIMARY_KEY_OVERRIDES.get(data_type),
        f"{singular}_id",
        f"{data_type}_id",
        "id",
        "slug",
        "name",
        "title",
    ]
    for candidate in candidates:
        if candidate and candidate in fields:
            return candidate
    for field in fields:
        if field.endswith("_id"):
            return field
    return None


def infer_display_field(schema: dict[str, Any]) -> str | None:
    for field in DISPLAY_FIELD_CANDIDATES:
        if field in schema:
            return field
    return next(iter(schema), None)


def classify_wave(data_type: str, row_count: int) -> str:
    if data_type in PILOT_TYPES:
        return "pilot"
    if data_type in WAVE_A_TYPES or row_count >= 500:
        return "wave_a"
    if row_count >= 25:
        return "wave_b"
    return "wave_c"


def strategy_for_type(data_type: str) -> str:
    if data_type in {"tasks", "transactions", "contacts", "companies", "people"}:
        return "custom"
    return "passthrough"


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: json_safe(value) for key, value in row.items() if value is not None}


def parquet_row_count(
    client: ParquetMCPClient, data_type: str, schema: dict[str, Any]
) -> int:
    first_column = next(iter(schema), None) or "id"
    result = client.call_tool_sync(
        "aggregate_parquet",
        {"data_type": data_type, "aggregations": {first_column: "count"}},
    )
    data = result.get("data") or []
    if not data:
        return 0
    count_key = f"{first_column}_count"
    return int(data[0].get(count_key) or 0)


def load_schema_definition(client: ParquetMCPClient, data_type: str) -> dict[str, Any]:
    try:
        schema_result = client.call_tool_sync("get_schema", {"data_type": data_type})
        schema = schema_result.get("schema") or {}
        if schema:
            return schema
    except Exception:
        pass

    schema_path = SCHEMAS_DIR / f"{data_type}_schema.json"
    if schema_path.exists():
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
        return payload.get("schema") or {}
    return {}


def build_migration_matrix(
    client: ParquetMCPClient | None = None,
) -> list[MigrationMatrixEntry]:
    client = client or get_parquet_client()
    result = client.call_tool_sync("list_data_types", {})
    data_types = sorted(result.get("data_types") or [])
    matrix: list[MigrationMatrixEntry] = []
    for data_type in data_types:
        schema = load_schema_definition(client, data_type)
        row_count = parquet_row_count(client, data_type, schema)
        matrix.append(
            MigrationMatrixEntry(
                data_type=data_type,
                target_entity_type=infer_entity_type(data_type),
                row_count=row_count,
                primary_key=infer_primary_key(data_type, schema),
                display_field=infer_display_field(schema),
                wave=classify_wave(data_type, row_count),
                strategy=strategy_for_type(data_type),
            )
        )
    return matrix


def matrix_to_json_rows(matrix: list[MigrationMatrixEntry]) -> list[dict[str, Any]]:
    return [asdict(entry) for entry in matrix]


def render_matrix_markdown(matrix: list[MigrationMatrixEntry]) -> str:
    lines = [
        "# Parquet to Neotoma Migration Matrix",
        "",
        "| Data type | Target entity type | Rows | Primary key | Display field | Wave | Strategy |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for entry in sorted(matrix, key=lambda item: (-item.row_count, item.data_type)):
        lines.append(
            "| {data_type} | {target_entity_type} | {row_count} | {primary_key} | {display_field} | {wave} | {strategy} |".format(
                data_type=entry.data_type,
                target_entity_type=entry.target_entity_type,
                row_count=entry.row_count,
                primary_key=entry.primary_key or "",
                display_field=entry.display_field or "",
                wave=entry.wave,
                strategy=entry.strategy,
            )
        )
    return "\n".join(lines) + "\n"


def read_all_parquet_rows(
    client: ParquetMCPClient,
    data_type: str,
    *,
    limit: int = 0,
    offset: int = 0,
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current_offset = offset
    remaining = limit
    while True:
        page_limit = PAGE_SIZE if limit <= 0 else min(PAGE_SIZE, remaining)
        if page_limit <= 0:
            break
        args: dict[str, Any] = {
            "data_type": data_type,
            "limit": page_limit,
            "offset": current_offset,
        }
        if columns:
            args["columns"] = columns
        result = client.call_tool_sync("read_parquet", args)
        page_rows = result.get("data") or []
        if not page_rows:
            break
        rows.extend(page_rows)
        current_offset += len(page_rows)
        if limit > 0:
            remaining -= len(page_rows)
        total_rows = result.get("total_rows")
        if limit <= 0:
            has_more = len(page_rows) == page_limit
        else:
            has_more = result.get("has_more")
            if total_rows is not None:
                has_more = current_offset < int(total_rows)
            elif has_more is None:
                has_more = len(page_rows) == page_limit
        if not has_more:
            break
    return rows


def deterministic_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()[:16]


def generic_idempotency_key(
    data_type: str, row: dict[str, Any], primary_key: str | None
) -> str:
    if primary_key and row.get(primary_key) not in (None, ""):
        return f"migrate-{data_type}-{row[primary_key]}"
    return f"migrate-{data_type}-hash-{deterministic_hash(row)}"


def map_contact_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_row(row)
    entity = {
        "entity_type": "contact",
        "full_name": normalized.get("name"),
        "name": normalized.get("name"),
        "contact_id": normalized.get("contact_id"),
        "contact_type": normalized.get("contact_type"),
        "category": normalized.get("category"),
        "platform": normalized.get("platform"),
        "email": normalized.get("email"),
        "phone": normalized.get("phone"),
        "address": normalized.get("address"),
        "country": normalized.get("country"),
        "website": normalized.get("website"),
        "language": normalized.get("language"),
        "birthday": normalized.get("birthday"),
        "notes": normalized.get("notes"),
        "first_contact_date": normalized.get("first_contact_date"),
        "last_contact_date": normalized.get("last_contact_date"),
        "created_date": normalized.get("created_date"),
        "updated_date": normalized.get("updated_date"),
        "data_source": "project-0-ateles-parquet/contacts",
        "source_record_id": normalized.get("contact_id"),
    }
    return {key: value for key, value in entity.items() if value not in (None, "")}


def map_company_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_row(row)
    entity = {
        "entity_type": "company",
        "name": normalized.get("name"),
        "company_id": normalized.get("company_id"),
        "website": normalized.get("website"),
        "description": normalized.get("description") or normalized.get("notes"),
        "notes": normalized.get("notes"),
        "data_source": "project-0-ateles-parquet/companies",
        "source_record_id": normalized.get("company_id"),
    }
    return {key: value for key, value in entity.items() if value not in (None, "")}


def map_person_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_row(row)
    entity = {
        "entity_type": "person",
        "full_name": normalized.get("name") or normalized.get("full_name"),
        "name": normalized.get("name"),
        "person_id": normalized.get("person_id"),
        "email": normalized.get("email"),
        "phone": normalized.get("phone"),
        "notes": normalized.get("notes"),
        "birthday": normalized.get("birthday"),
        "location": normalized.get("location"),
        "data_source": "project-0-ateles-parquet/people",
        "source_record_id": normalized.get("person_id"),
    }
    for key, value in normalized.items():
        if key not in entity and value not in (None, ""):
            entity[key] = value
    return {key: value for key, value in entity.items() if value not in (None, "")}


def map_generic_row(
    data_type: str,
    target_entity_type: str,
    row: dict[str, Any],
    primary_key: str | None,
) -> dict[str, Any]:
    normalized = normalize_row(row)
    entity = {
        "entity_type": target_entity_type,
        **normalized,
        "data_source": f"project-0-ateles-parquet/{data_type}",
    }
    if primary_key and normalized.get(primary_key) not in (None, ""):
        entity["source_record_id"] = normalized[primary_key]
    entity["parquet_data_type"] = data_type
    return entity


def map_row_to_entity(
    entry: MigrationMatrixEntry,
    row: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    if entry.data_type == "tasks":
        from migrate_tasks_parquet_to_neotoma import (
            idempotency_key_for as task_idempotency_key_for,
        )
        from migrate_tasks_parquet_to_neotoma import (
            map_parquet_to_neotoma,
        )

        return map_parquet_to_neotoma(row), task_idempotency_key_for(row)

    if entry.data_type == "transactions":
        from backfill_transactions_parquet_to_neotoma import row_to_entity_and_key

        parsed = row_to_entity_and_key(row)
        if parsed is None:
            raise ValueError("transaction row could not be mapped")
        return parsed

    if entry.data_type == "contacts":
        entity = map_contact_row(row)
        return entity, generic_idempotency_key(entry.data_type, row, entry.primary_key)

    if entry.data_type == "companies":
        entity = map_company_row(row)
        return entity, generic_idempotency_key(entry.data_type, row, entry.primary_key)

    if entry.data_type == "people":
        entity = map_person_row(row)
        return entity, generic_idempotency_key(entry.data_type, row, entry.primary_key)

    entity = map_generic_row(
        entry.data_type, entry.target_entity_type, row, entry.primary_key
    )
    return entity, generic_idempotency_key(entry.data_type, row, entry.primary_key)


def store_entities_batch(
    entities: list[dict[str, Any]],
    idempotency_key: str,
    *,
    source_file_path: str | None = None,
    timeout: int = 180,
) -> tuple[bool, str]:
    if not which("neotoma"):
        return False, "neotoma CLI not found on PATH"

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        json.dump(entities, handle, ensure_ascii=False, default=str)
        tmp_path = handle.name

    try:
        cmd = [
            "neotoma",
            "store",
            "--file",
            tmp_path,
            "--idempotency-key",
            idempotency_key,
            "--api-only",
        ]
        if source_file_path:
            cmd.extend(["--file-path", source_file_path])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return False, (
                result.stderr or result.stdout or "neotoma store failed"
            ).strip()
        return True, result.stdout.strip()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def run_neotoma_json_command(args: list[str], timeout: int = 180) -> dict[str, Any]:
    if not which("neotoma"):
        raise RuntimeError("neotoma CLI not found on PATH")
    cmd = ["neotoma", "--json", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "neotoma command failed"
        )
    return json.loads(result.stdout or "{}")


def list_neotoma_entities(
    entity_type: str,
    *,
    page_size: int = NEOTOMA_PAGE_SIZE,
    search: str | None = None,
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    offset = 0
    while True:
        args = [
            "entities",
            "list",
            "--type",
            entity_type,
            "--limit",
            str(page_size),
            "--offset",
            str(offset),
        ]
        if search:
            args.extend(["--search", search])
        payload = run_neotoma_json_command(args)
        batch = payload.get("entities") or []
        if not batch:
            break
        entities.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return entities


def flatten_entity_snapshot(entity: dict[str, Any]) -> dict[str, Any]:
    snapshot = entity.get("snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("snapshot"), dict):
        flat = dict(snapshot["snapshot"])
        for key, value in snapshot.items():
            if key not in {"snapshot"} and key not in flat:
                flat[key] = value
        return flat
    if isinstance(snapshot, dict):
        return dict(snapshot)
    return {}


def build_neotoma_index(
    entity_type: str, key_field: str | None
) -> dict[str, dict[str, Any]]:
    if not key_field:
        return {}
    index: dict[str, dict[str, Any]] = {}
    for entity in list_neotoma_entities(entity_type):
        snapshot = flatten_entity_snapshot(entity)
        key_value = snapshot.get(key_field)
        if key_value not in (None, ""):
            index[str(key_value)] = snapshot
    return index


def verify_migration(
    entry: MigrationMatrixEntry,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not entry.primary_key:
        return {
            "data_type": entry.data_type,
            "verified": False,
            "reason": "no primary key available for verification",
            "expected_rows": len(rows),
        }

    index = build_neotoma_index(entry.target_entity_type, entry.primary_key)
    missing: list[str] = []
    for row in rows:
        source_id = row.get(entry.primary_key)
        if source_id in (None, ""):
            continue
        if str(source_id) not in index:
            missing.append(str(source_id))
    return {
        "data_type": entry.data_type,
        "verified": len(missing) == 0,
        "expected_rows": len(rows),
        "missing_count": len(missing),
        "missing_ids_sample": missing[:20],
    }


def delete_parquet_rows(
    client: ParquetMCPClient,
    entry: MigrationMatrixEntry,
    rows: list[dict[str, Any]],
    *,
    batch_size: int = 100,
) -> int:
    if not entry.primary_key:
        return 0

    deleted = 0
    values = [
        row.get(entry.primary_key)
        for row in rows
        if row.get(entry.primary_key) not in (None, "")
    ]
    for index in range(0, len(values), batch_size):
        batch = values[index : index + batch_size]
        if not batch:
            continue
        client.call_tool_sync(
            "delete_records",
            {
                "data_type": entry.data_type,
                "filters": {entry.primary_key: {"$in": batch}},
            },
        )
        deleted += len(batch)
    return deleted


def ensure_parent_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
