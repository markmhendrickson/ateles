"""Probe records: one per measurement type, every declared field populated.

Storing these and reading them back proves each field is declared (store
reports ``unknown_fields_count`` 0) and that the snapshot holds the value
written. They are marked ``status: probe`` and are never deleted.
"""

from __future__ import annotations

from typing import Any

from execution.scripts.metrics import schemas

PROBE_AT = "2026-10-13T00:00:00Z"


def _sample(ftype: str, name: str) -> Any:
    if ftype == "string":
        return f"probe:{name}"
    if ftype == "number":
        return 1.5
    if ftype == "boolean":
        return True
    if ftype == "array":
        return [f"probe:{name}", 2]
    if ftype == "object":
        return {"probe": name, "n": 1}
    raise ValueError(ftype)


def build_probes() -> list[dict[str, Any]]:
    out = []
    for entity_type, (fields, _identity) in schemas.SCHEMAS.items():
        values = {name: _sample(ftype, name) for name, (ftype, _d) in fields.items()}
        values["status"] = "probe"
        if entity_type == "metric_definition":
            values["metric_id"] = "probe_metric_definition"
        elif entity_type == "metric_reading":
            values["reading_key"] = schemas.reading_key("probe_metric_definition", "probe", PROBE_AT)
            values["metric_ref"] = "probe_metric_definition"
            values["window_end"] = PROBE_AT
        elif entity_type == "prediction":
            values["prediction_id"] = "probe_prediction"
        elif entity_type == "outcome_score":
            values["score_id"] = "probe_outcome_score"
        out.append(schemas.build_record(entity_type, **values))
    return out
