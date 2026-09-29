"""The ``generation_record`` entity contract, defined once.

``GENERATION_RECORD_FIELDS`` is the single source of the record's field names.
``generation.py`` builds records from it and
``execution/scripts/register_generation_record_schema.py`` registers the schema
from it, so the two cannot drift. There are no secret fields: prompts are
stored (potentially personal data, hence ``visibility = private``); credentials
never are.
"""

from __future__ import annotations

from typing import Any

GENERATION_RECORD_ENTITY_TYPE = "generation_record"
GENERATION_RECORD_SCHEMA_VERSION = "1.0"
PRIVATE = "private"

GENERATION_RECORD_FIELDS: dict[str, tuple[str, str]] = {
    "generation_id": ("string", "Unique id minted before the vendor call; idempotency anchor"),
    "slot": ("string", "Capability slot requested (vendor_binding.capability)"),
    "prompt": ("string", "Prompt sent to the vendor (potentially personal data; private)"),
    "vendor": ("string", "Vendor that ACTUALLY produced the artifact (fallback-transparent)"),
    "model_tier": ("string", "Model that actually ran"),
    "cost_usd": ("number", "Cost attributed to the call in USD (list-price upper bound)"),
    "artifact_ref": ("string", "Operator-local path of the stored artifact"),
    "binding_entity_id": ("string", "vendor_binding entity that authorized the call"),
    "created_at": ("string", "ISO 8601 UTC timestamp"),
    "visibility": ("string", "Always 'private'"),
}
CANONICAL_NAME_FIELDS = ["generation_id"]
REQUIRED_ON_STORE = tuple(k for k in GENERATION_RECORD_FIELDS)


def build_generation_record(**values: Any) -> dict[str, Any]:
    """Build the entity dict, rejecting any field the schema does not declare."""
    unknown = set(values) - set(GENERATION_RECORD_FIELDS)
    if unknown:
        raise ValueError(f"undeclared generation_record fields: {sorted(unknown)}")
    missing = [k for k in GENERATION_RECORD_FIELDS if k not in values and k != "visibility"]
    if missing:
        raise ValueError(f"missing generation_record fields: {missing}")
    record = {"entity_type": GENERATION_RECORD_ENTITY_TYPE, **values}
    record["visibility"] = PRIVATE
    return record


def schema_definition() -> dict[str, Any]:
    return {
        "canonical_name_fields": list(CANONICAL_NAME_FIELDS),
        "fields": {
            name: {"type": ftype, "description": desc}
            for name, (ftype, desc) in GENERATION_RECORD_FIELDS.items()
        },
    }


def reducer_config() -> dict[str, Any]:
    return {
        "merge_policies": {
            name: {"strategy": "last_write"} for name in GENERATION_RECORD_FIELDS
        }
    }
