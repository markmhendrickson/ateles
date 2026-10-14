"""The four measurement entity types, defined once.

``metric_definition``, ``metric_reading``, ``prediction`` and ``outcome_score``
(measurement design, section 2). Field names live here and nowhere else: the
registration script, the record builders and the read-back checks all derive
from ``SCHEMAS`` so they cannot drift (a field a writer sends that the schema
does not declare is silently dropped by Neotoma, so every writer builds its
record through :func:`build_record`, which refuses undeclared fields).

Timestamps and periods are ISO-8601 strings, not Neotoma ``date`` fields, so a
value round-trips byte for byte (same choice as ``usage_digest``).
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.0"

# field -> (type, description). Types: string | number | boolean | array | object.
METRIC_DEFINITION_FIELDS: dict[str, tuple[str, str]] = {
    "metric_id": ("string", "Stable id, e.g. D1 npm_downloads_weekly (identity)"),
    "title": ("string", "Short human name"),
    "description": ("string", "What the metric counts, in one or two sentences"),
    "source_system": ("string", "product_usage | web | repo | swarm_ops | newsletter | social"),
    "artifact_kind": ("string", "Kind of artifact measured: package, repository, site, swarm, ..."),
    "query": ("object", "Declarative endpoint + parameters, or the name of a derived read"),
    "unit": ("string", "Unit of the value, e.g. downloads, stars, minutes, ratio"),
    "polarity": ("string", "up_good | down_good"),
    "strategy_weight": ("number", "Weight from the strategy entity; null until the operator sets it"),
    "cadence": ("string", "How often a reading is taken: daily | weekly | monthly"),
    "min_points": ("number", "Minimum readings before a baseline or post-window is usable"),
    "baseline_window": ("string", "Window the baseline median is taken over, e.g. 28d"),
    "noise_floor_rule": ("string", "Rule for the noise floor, e.g. max(2*MAD, min detectable change)"),
    "coverage_ratio": ("number", "Minimum share of the window that must be covered for a reading"),
    "aggregate_only": ("boolean", "True for anything derived from people: counts, never person rows"),
    "known_positive_probe": ("object", "A query whose result must be above zero before a zero is believed"),
    "default_for_kinds": ("array", "Kinds of work this is the default primary metric for (proposed mapping)"),
    "max_horizon": ("string", "Longest horizon a prediction on this metric may carry (ISO duration)"),
    "leading_indicator": ("string", "Higher-volume leading indicator to name beside the headline metric"),
    "pooling": ("string", "How a low-volume metric is scored: pooled window or horizon rule"),
    "baseline_status": ("string", "exists | capture | awaiting_source | measured"),
    "baseline_note": ("string", "Why the baseline has this status; what unblocks it"),
    "collector": ("string", "Script or adapter that produces readings; empty when none yet"),
    "status": ("string", "proposed | ratified | probe | retired"),
}
METRIC_DEFINITION_IDENTITY = ["metric_id"]

METRIC_READING_FIELDS: dict[str, tuple[str, str]] = {
    "reading_key": ("string", "metric_id|artifact|window_end (identity; makes a replay idempotent)"),
    "metric_ref": ("string", "metric_id of the metric_definition this reading belongs to"),
    "artifact_ref": ("string", "What was measured: package name, repository slug, site, ..."),
    "value": ("number", "The measured value for the window"),
    "unit": ("string", "Unit of the value"),
    "window_start": ("string", "ISO-8601 start of the window (inclusive)"),
    "window_end": ("string", "ISO-8601 end of the window"),
    "sourced_at": ("string", "ISO-8601 time the value was read from its source"),
    "coverage_ratio": ("number", "Share of the window the source actually covered (0-1)"),
    "coverage_note": ("string", "What is missing from the window, if anything"),
    "probe_ok": ("boolean", "Known-positive probe passed when this reading was taken"),
    "collector": ("string", "Script that produced the reading"),
    "status": ("string", "ok | probe | partial | invalid (superseded: do not read the value)"),
    "detail": ("object", "Extra context for the value (counts that sum to it, exclusions applied)"),
}
METRIC_READING_IDENTITY = ["reading_key"]

PREDICTION_FIELDS: dict[str, tuple[str, str]] = {
    "prediction_id": ("string", "Stable id (identity)"),
    "title": ("string", "What is predicted, in one line"),
    "subject": ("string", "Entity id of the tracked task (REFERS_TO)"),
    "metric_ref": ("string", "metric_id the change is predicted on"),
    "baseline_value": ("number", "Baseline median at write time"),
    "baseline_window": ("string", "Window the baseline was taken over"),
    "baseline_points": ("array", "Reading entity ids the baseline was computed from"),
    "baseline_status": ("string", "measured | unbaselined"),
    "expected_change": ("number", "Signed expected change, in the metric's unit"),
    "expected_change_basis": ("string", "One sentence plus evidence refs; none means author's guess"),
    "confidence": ("number", "0-1; at most 0.5 when the basis cites no evidence"),
    "horizon": ("string", "ISO duration counted from ship"),
    "guardrails": ("array", "Each: metric and allowed regression"),
    "experiment_class": ("string", "autonomous | reserved | n/a"),
    "written_at": ("string", "ISO-8601 record clock at write; before the item's first claim"),
    "status": ("string", "open | measured | scored | void | probe"),
    "supersedes": ("string", "Prediction id this one replaces, before the item started"),
}
PREDICTION_IDENTITY = ["prediction_id"]

OUTCOME_SCORE_FIELDS: dict[str, tuple[str, str]] = {
    "score_id": ("string", "Stable id (identity)"),
    "prediction": ("string", "Prediction entity id scored (REFERS_TO)"),
    "observed_value": ("number", "Median of the post-window readings"),
    "observed_change": ("number", "Observed value minus the baseline value"),
    "post_window": ("string", "ISO interval of the post-window"),
    "coverage": ("number", "Share of the post-window covered by readings (0-1)"),
    "noise_floor": ("number", "Noise floor F used for the verdict"),
    "controls": ("object", "Experiment control inputs, when the item had one"),
    "confounders": ("array", "Other items or events inside the post-window (derived)"),
    "guardrail_results": ("array", "Per guardrail: observed regression and pass/fail"),
    "verdict": ("string", "hit | partial | miss | inconclusive | void"),
    "reason": ("string", "Why this verdict, in one sentence"),
    "detectable_effect": ("number", "Smallest change the data could have shown"),
    "signed_relative_error": ("number", "(observed - expected) / expected, signed"),
    "attribution_caveat": (
        "string",
        "none | confounded_no_control | external_event | truncated_window | unbaselined",
    ),
    "signed_by": ("string", "Principal that signed; never the predictor or the measurer"),
    "signed_at": ("string", "ISO-8601 signing time"),
    "status": ("string", "draft | signed | probe"),
}
OUTCOME_SCORE_IDENTITY = ["score_id"]

SCHEMAS: dict[str, tuple[dict[str, tuple[str, str]], list[str]]] = {
    "metric_definition": (METRIC_DEFINITION_FIELDS, METRIC_DEFINITION_IDENTITY),
    "metric_reading": (METRIC_READING_FIELDS, METRIC_READING_IDENTITY),
    "prediction": (PREDICTION_FIELDS, PREDICTION_IDENTITY),
    "outcome_score": (OUTCOME_SCORE_FIELDS, OUTCOME_SCORE_IDENTITY),
}


def schema_definition(entity_type: str) -> dict[str, Any]:
    fields, identity = SCHEMAS[entity_type]
    return {
        "fields": {
            name: {"type": ftype, "required": False, "description": desc}
            for name, (ftype, desc) in fields.items()
        },
        "canonical_name_fields": list(identity),
        "name_collision_policy": "reject",
    }


def reducer_config(entity_type: str) -> dict[str, Any]:
    fields, _ = SCHEMAS[entity_type]
    return {"merge_policies": {name: {"strategy": "last_write"} for name in fields}}


def register_payload(entity_type: str) -> dict[str, Any]:
    """Body for POST /register_schema (RegisterSchemaRequestSchema)."""
    return {
        "entity_type": entity_type,
        "schema_version": SCHEMA_VERSION,
        "schema_definition": schema_definition(entity_type),
        "reducer_config": reducer_config(entity_type),
        "user_specific": False,
        "activate": True,
    }


def build_record(entity_type: str, **values: Any) -> dict[str, Any]:
    """Entity dict for a store call; refuses any field the schema does not declare.

    ``None`` values are dropped (an absent field is stored as absent, never as
    a null that could be read back as a value).
    """
    fields, identity = SCHEMAS[entity_type]
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(f"undeclared {entity_type} fields: {sorted(unknown)}")
    missing = [k for k in identity if values.get(k) in (None, "")]
    if missing:
        raise ValueError(f"missing identity fields for {entity_type}: {missing}")
    record: dict[str, Any] = {"entity_type": entity_type}
    record.update({k: v for k, v in values.items() if v is not None})
    return record


def reading_key(metric_id: str, artifact: str, window_end: str) -> str:
    return f"{metric_id}|{artifact}|{window_end}"
