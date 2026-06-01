from __future__ import annotations

from typing import Any

import pandas as pd
from parquet_client import ParquetMCPClient
from parquet_to_neotoma_migration import (
    flatten_entity_snapshot,
    list_neotoma_entities,
    read_all_parquet_rows,
)


def _records_to_dataframe(
    records: list[dict[str, Any]], date_fields: tuple[str, ...] = ()
) -> pd.DataFrame:
    df = pd.DataFrame(records)
    if df.empty:
        return df
    for field in date_fields:
        if field in df.columns:
            df[field] = pd.to_datetime(df[field], errors="coerce")
    return df


def load_normalized_dataframe(
    *,
    entity_type: str,
    parquet_data_type: str,
    date_fields: tuple[str, ...] = (),
    prefer_source: str = "auto",
) -> tuple[pd.DataFrame, str]:
    if prefer_source not in {"auto", "neotoma", "parquet"}:
        raise ValueError("prefer_source must be 'auto', 'neotoma', or 'parquet'")

    if prefer_source != "parquet":
        entities = list_neotoma_entities(entity_type)
        records = [flatten_entity_snapshot(entity) for entity in entities]
        df = _records_to_dataframe(records, date_fields=date_fields)
        if prefer_source == "neotoma" or not df.empty:
            return df, "neotoma"

    rows = read_all_parquet_rows(ParquetMCPClient(), parquet_data_type)
    df = _records_to_dataframe(rows, date_fields=date_fields)
    return df, "parquet"
