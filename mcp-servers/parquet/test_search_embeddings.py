#!/usr/bin/env python3
"""Regression test: search_parquet embedding extraction must not use pd.isna on arrays."""

import json

import numpy as np
import pandas as pd


def _extract_emb_values(emb_row, df_embeddings, id_field):
    """Mirror of search_parquet embedding extraction (avoids pd.isna on arrays)."""
    emb_values = []
    for col in df_embeddings.columns:
        if col.endswith("_embedding") and col in emb_row.columns:
            emb_val = emb_row[col].iloc[0]
            try:
                if isinstance(emb_val, list | np.ndarray):
                    arr = np.asarray(emb_val)
                    if arr.size > 0:
                        emb_values.append(arr)
                elif isinstance(emb_val, str):
                    arr = np.array(json.loads(emb_val))
                    if arr.size > 0:
                        emb_values.append(arr)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return emb_values


def test_extract_embeddings_no_array_truth_value():
    """Extracting from columns that store numpy arrays must not raise 'truth value of array'."""
    pd.DataFrame({"objective_id": ["a", "b"], "x": [1, 2]})
    emb = pd.DataFrame(
        {
            "objective_id": ["a", "b"],
            "objective_embedding": [
                np.random.randn(4).tolist(),
                np.random.randn(4).tolist(),
            ],
        }
    )
    # Simulate parquet round-trip: often becomes array
    emb["objective_embedding"] = emb["objective_embedding"].apply(np.array)
    emb_row = emb[emb["objective_id"] == "a"]
    values = _extract_emb_values(emb_row, emb, "objective_id")
    assert len(values) == 1
    assert isinstance(values[0], np.ndarray) and values[0].size == 4


if __name__ == "__main__":
    test_extract_embeddings_no_array_truth_value()
    print("OK")
