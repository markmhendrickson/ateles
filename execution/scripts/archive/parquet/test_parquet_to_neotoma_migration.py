import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from parquet_to_neotoma_migration import (
    classify_wave,
    infer_entity_type,
    infer_primary_key,
    map_contact_row,
    singularize_data_type,
)


def test_singularize_irregular_types() -> None:
    assert singularize_data_type("contacts") == "contact"
    assert singularize_data_type("companies") == "company"
    assert singularize_data_type("locations") == "location"


def test_infer_primary_key_prefers_specific_identifier() -> None:
    schema = {
        "task_id": "string",
        "title": "string",
        "status": "string",
    }
    assert infer_primary_key("tasks", schema) == "task_id"


def test_infer_entity_type_uses_override() -> None:
    assert infer_entity_type("people") == "person"
    assert infer_entity_type("transactions") == "transaction"


def test_classify_wave() -> None:
    assert classify_wave("tasks", 100) == "pilot"
    assert classify_wave("task_stories", 100) == "wave_a"
    assert classify_wave("notes", 100) == "wave_b"
    assert classify_wave("movies", 1) == "wave_c"


def test_map_contact_row_preserves_source_id() -> None:
    row = {
        "contact_id": "abc123",
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "notes": "Test contact",
    }
    entity = map_contact_row(row)
    assert entity["entity_type"] == "contact"
    assert entity["full_name"] == "Ada Lovelace"
    assert entity["source_record_id"] == "abc123"
