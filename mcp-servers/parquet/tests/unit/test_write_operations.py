"""
Unit tests for parquet MCP server write operations.

Covers the add_record MCP tool handler, focused on issue #304: audit
logging and embedding generation were silently disabled on the add path
while returning a plausible success response.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd


def _run_add_record(data_type: str, record: dict):
    from parquet_mcp_server import call_tool

    result = asyncio.run(
        call_tool("add_record", {"data_type": data_type, "record": record})
    )
    return result


def _mock_openai_client(monkeypatch, embedding: list[float] | None = None):
    """Mock parquet_mcp_server.get_openai_client to avoid live OpenAI calls."""
    import parquet_mcp_server

    embedding = embedding or [0.1, 0.2, 0.3]
    client = MagicMock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=embedding)]
    )
    monkeypatch.setattr(parquet_mcp_server, "get_openai_client", lambda: client)
    return client


def _make_widgets_file(test_data_dir: Path, data_type: str) -> str:
    """Create a data type whose id field matches add_record's derivation
    (f"{data_type.rstrip('s')}_id") and includes a text field for embeddings.
    Returns the derived id_field name. Each test uses its own data_type so
    the embeddings parquet (never cleared by reset_data_dir) starts empty."""
    data_type_dir = test_data_dir / data_type
    data_type_dir.mkdir(parents=True, exist_ok=True)
    parquet_file = data_type_dir / f"{data_type}.parquet"
    id_field = f"{data_type.rstrip('s')}_id"

    df = pd.DataFrame(
        {
            id_field: ["w1", "w2"],
            "name": ["Existing Widget One", "Existing Widget Two"],
            "value": [1.0, 2.0],
        }
    )
    df.to_parquet(parquet_file, index=False)
    return id_field


class TestAddRecordAuditLog:
    """Effect-level tests: add_record must write a real audit_log row."""

    def test_add_record_writes_real_audit_entry(
        self, test_data_dir: Path, monkeypatch
    ):
        data_type = "widgets_audit_basic"
        id_field = _make_widgets_file(test_data_dir, data_type)
        _mock_openai_client(monkeypatch)

        record = {id_field: "w3", "name": "New Widget", "value": 3.0}

        result = _run_add_record(data_type, record)
        payload = json.loads(result[0].text)

        assert payload["success"] is True
        assert payload["audit_id"] != "skipped"

        audit_log_path = test_data_dir / "logs" / "audit_log.parquet"
        assert audit_log_path.exists()

        df_audit = pd.read_parquet(audit_log_path)
        matching = df_audit[
            (df_audit["operation"] == "add") & (df_audit["record_id"] == "w3")
        ]
        assert len(matching) == 1
        assert matching.iloc[0]["audit_id"] == payload["audit_id"]

        new_values = json.loads(matching.iloc[0]["new_values"])
        assert new_values["name"] == "New Widget"

    def test_add_record_duplicate_record_id_appends_independent_audit_rows(
        self, test_data_dir: Path, monkeypatch
    ):
        data_type = "widgets_audit_dup"
        id_field = _make_widgets_file(test_data_dir, data_type)
        _mock_openai_client(monkeypatch)

        record = {id_field: "w4", "name": "Duplicate Widget", "value": 4.0}

        _run_add_record(data_type, dict(record))
        _run_add_record(data_type, dict(record))

        audit_log_path = test_data_dir / "logs" / "audit_log.parquet"
        df_audit = pd.read_parquet(audit_log_path)
        matching = df_audit[
            (df_audit["operation"] == "add") & (df_audit["record_id"] == "w4")
        ]
        assert len(matching) == 2


class TestAddRecordEmbeddings:
    """Effect-level tests: add_record must generate a real embedding row."""

    def test_add_record_generates_real_embeddings(
        self, test_data_dir: Path, monkeypatch
    ):
        data_type = "widgets_emb_basic"
        id_field = _make_widgets_file(test_data_dir, data_type)
        client = _mock_openai_client(monkeypatch, embedding=[0.5, 0.6, 0.7])

        record = {id_field: "w5", "name": "Embedded Widget", "value": 5.0}

        result = _run_add_record(data_type, record)
        payload = json.loads(result[0].text)
        assert payload["embeddings_generated"] >= 1

        assert client.embeddings.create.called

        embeddings_path = (
            test_data_dir / "embeddings" / f"{data_type}_embeddings.parquet"
        )
        assert embeddings_path.exists()

        df_emb = pd.read_parquet(embeddings_path)
        matching = df_emb[df_emb[id_field] == "w5"]
        assert len(matching) == 1
        assert matching.iloc[0]["name_embedding"] is not None

    def test_add_record_embedding_failure_does_not_block_write(
        self, test_data_dir: Path, monkeypatch
    ):
        data_type = "widgets_emb_failure"
        id_field = _make_widgets_file(test_data_dir, data_type)
        import parquet_mcp_server

        def _raise_generate_embeddings(*args, **kwargs):
            raise RuntimeError("embedding service unavailable")

        monkeypatch.setattr(
            parquet_mcp_server,
            "generate_embeddings_for_records",
            _raise_generate_embeddings,
        )

        record = {id_field: "w6", "name": "Unembeddable Widget", "value": 6.0}

        result = _run_add_record(data_type, record)
        payload = json.loads(result[0].text)

        assert payload["success"] is True
        assert payload["audit_id"] != "skipped"
        assert payload["embeddings_generated"] == 0

        audit_log_path = test_data_dir / "logs" / "audit_log.parquet"
        df_audit = pd.read_parquet(audit_log_path)
        matching = df_audit[
            (df_audit["operation"] == "add") & (df_audit["record_id"] == "w6")
        ]
        assert len(matching) == 1

    def test_add_record_no_text_fields_generates_no_embeddings_without_raising(
        self, test_data_dir: Path, monkeypatch
    ):
        """Record with no text-bearing fields should not raise."""
        data_type = "numeric_only"
        data_type_dir = test_data_dir / data_type
        data_type_dir.mkdir(parents=True, exist_ok=True)
        parquet_file = data_type_dir / f"{data_type}.parquet"
        pd.DataFrame({"numeric_only_id": ["n1"], "amount": [10]}).to_parquet(
            parquet_file, index=False
        )

        _mock_openai_client(monkeypatch)

        result = _run_add_record(data_type, {"numeric_only_id": "n2", "amount": 20})
        payload = json.loads(result[0].text)

        assert payload["success"] is True
        assert payload["embeddings_generated"] == 0


class TestAddRecordResponseContract:
    """Contract test: add_record response shape is unchanged by the re-enable."""

    def test_response_key_set_unchanged(self, test_data_dir: Path, monkeypatch):
        data_type = "widgets_contract"
        id_field = _make_widgets_file(test_data_dir, data_type)
        _mock_openai_client(monkeypatch)

        record = {id_field: "w7", "name": "Contract Widget", "value": 7.0}

        result = _run_add_record(data_type, record)
        payload = json.loads(result[0].text)

        expected_keys = {
            "success",
            "audit_id",
            "snapshot_created",
            "total_records",
            "added_record",
            "embeddings_generated",
        }
        assert set(payload.keys()) == expected_keys
        assert isinstance(payload["audit_id"], str)
        assert payload["audit_id"] not in ("skipped", "")
        assert isinstance(payload["embeddings_generated"], int)


class TestWriteParquetWithSchemaUnchanged:
    """The ~line 875 dead-code deletion must be a behavior no-op."""

    def test_write_parquet_with_schema_uses_schemaless_path(self, test_data_dir: Path):
        from parquet_mcp_server import write_parquet_with_schema

        df = pd.DataFrame(
            {
                "id": ["1", "2"],
                "name": ["Alice", "Bob"],
                "value": [1.5, 2.5],
            }
        )

        out_dir = test_data_dir / "schema_write_check"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "schema_write_check.parquet"

        write_parquet_with_schema(df, out_path, "schema_write_check")

        assert out_path.exists()
        result = pd.read_parquet(out_path)
        pd.testing.assert_frame_equal(
            result.reset_index(drop=True), df.reset_index(drop=True)
        )
