"""Tests for the measurement slice: schemas, definitions, store verification, parsers.

Run: python3 -m pytest execution/scripts/metrics/test_metrics.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution.scripts.metrics import (  # noqa: E402
    definitions,
    neotoma_io,
    probes,
    register_schemas,
    repo_baseline,
    schemas,
    swarm_ops_baseline as ops,
    weekly,
)

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def load_json(name):
    return json.loads((FIX / name).read_text())


def load_jsonl(name):
    return [json.loads(line) for line in (FIX / name).read_text().splitlines() if line.strip()]


# --- schemas and definitions ----------------------------------------------------------

def test_build_record_refuses_undeclared_and_missing_identity():
    with pytest.raises(ValueError, match="undeclared"):
        schemas.build_record("metric_reading", reading_key="k", not_a_field=1)
    with pytest.raises(ValueError, match="missing identity"):
        schemas.build_record("metric_reading", value=1)


def test_build_record_drops_none_rather_than_storing_null():
    rec = schemas.build_record("prediction", prediction_id="p", confidence=None)
    assert "confidence" not in rec


def test_register_payload_declares_every_field_and_identity():
    for entity_type, (fields, identity) in schemas.SCHEMAS.items():
        payload = schemas.register_payload(entity_type)
        assert set(payload["schema_definition"]["fields"]) == set(fields)
        assert payload["schema_definition"]["canonical_name_fields"] == identity
        assert set(payload["reducer_config"]["merge_policies"]) == set(fields)
        assert all(identity_field in fields for identity_field in identity)


def test_definitions_catalog_shape():
    records = definitions.build_definitions()
    ids = [r["metric_id"] for r in records]
    assert len(ids) == len(set(ids)) == 24
    assert all(r["status"] == "proposed" and r["default_for_kinds"] for r in records)
    # web, product-usage and newsletter definitions wait on keys that are not minted
    for r in records:
        if r["source_system"] in ("product_usage", "web", "newsletter", "social"):
            assert r["baseline_status"] == "awaiting_source", r["metric_id"]
    runnable = {r["metric_id"] for r in records if r["baseline_status"] == "exists"}
    assert {"D1_npm_downloads_weekly", "D2_github_stars", "S1_time_to_completion",
            "S2_dispatch_failure_rate", "S4_review_rounds_per_pr", "S5_cost_per_task",
            "S3p_operator_intervention_proxy"} <= runnable
    assert next(r for r in records if r["metric_id"].startswith("S3_"))["baseline_status"] == "capture"


def test_probes_populate_every_declared_field():
    for rec in probes.build_probes():
        fields, _ = schemas.SCHEMAS[rec["entity_type"]]
        assert set(rec) - {"entity_type"} == set(fields)
        assert rec["status"] == "probe"


# --- store and read-back ---------------------------------------------------------------------

class FakeNeotoma:
    """Stores at most ``cap`` entities per /store call while reporting success."""

    def __init__(self, cap=None, drop_field=None, never=False):
        self.cap, self.drop_field, self.never, self.rows, self.calls = cap, drop_field, never, {}, 0

    def __call__(self, method, path, body=None, **_):
        if path == "/store":
            self.calls += 1
            ents = [] if self.never else (body["entities"][: self.cap] if self.cap else body["entities"])
            for e in ents:
                snap = {k: v for k, v in e.items() if k not in ("entity_type", self.drop_field)}
                self.rows[(e["entity_type"], e["reading_key"])] = snap
            return {"success": True, "replayed": False, "observations_created": len(ents)}
        if path == "/entities/query":
            ents = [{"snapshot": s} for (t, _), s in self.rows.items() if t == body["entity_type"]]
            return {"entities": ents[body["offset"]: body["offset"] + body["limit"]], "total": len(ents)}
        raise AssertionError(path)


def reading(i):
    return schemas.build_record("metric_reading", reading_key=f"m|a|{i}", metric_ref="m", value=float(i),
                                unit="u", status="ok", detail={"n": i})


def test_store_and_verify_passes_when_everything_lands():
    fake = FakeNeotoma()
    ents = [reading(i) for i in range(20)]
    assert neotoma_io.store_and_verify(ents, "t", "reading_key", requester=fake) == []
    assert fake.calls == 3  # batched


def test_partial_batch_write_is_detected_and_repaired_under_a_fresh_key():
    fake = FakeNeotoma(cap=3)  # server reports success but keeps 3 per call
    ents = [reading(i) for i in range(8)]
    assert neotoma_io.store_and_verify(ents, "t", "reading_key", requester=fake) == []
    assert len(fake.rows) == 8


def test_unrepairable_write_is_reported_not_swallowed():
    fake = FakeNeotoma(never=True)
    problems = neotoma_io.store_and_verify([reading(1), reading(2)], "t", "reading_key", requester=fake)
    assert any("observations created" in p for p in problems)
    assert any("not found on read-back" in p for p in problems)


def test_store_and_verify_catches_dropped_field():
    fake = FakeNeotoma(drop_field="unit")
    problems = neotoma_io.store_and_verify([reading(1)], "t", "reading_key", requester=fake)
    assert any("unit" in p for p in problems)


def test_idempotency_key_changes_with_content_not_order_of_keys():
    a, b = [{"x": 1, "y": 2}], [{"y": 2, "x": 1}]
    assert neotoma_io.idempotency_key("l", a) == neotoma_io.idempotency_key("l", b)
    assert neotoma_io.idempotency_key("l", a) != neotoma_io.idempotency_key("l", [{"x": 1, "y": 3}])


def test_register_read_back_flags_missing_field_and_wrong_type():
    payload = schemas.register_payload("prediction")
    registered = {"schema_definition": payload["schema_definition"], "active": True}
    assert register_schemas.read_back_problems("prediction", registered) == []
    del registered["schema_definition"]["fields"]["horizon"]
    registered["schema_definition"]["fields"]["confidence"]["type"] = "string"
    problems = register_schemas.read_back_problems("prediction", registered)
    assert any("horizon" in p for p in problems) and any("confidence" in p for p in problems)


# --- weekly helpers ------------------------------------------------------------------------------

def test_weeks_and_summary():
    wins = weekly.weeks_back(NOW, 2, lag_days=1)
    assert [weekly.iso(s) for s, _ in wins] == ["2026-09-28T00:00:00Z", "2026-10-05T00:00:00Z"]
    assert weekly.iso(wins[-1][1]) == "2026-10-12T00:00:00Z"
    s = weekly.summarize([1, 2, 3, 4, 100])
    assert s["median"] == 3 and s["mad"] == 1 and s["noise_floor"] == 2 and s["total"] == 110
    assert weekly.summarize([])["median"] is None


# --- repo parsers -----------------------------------------------------------------------------------

WINDOWS = weekly.weeks_back(NOW, 2, lag_days=1)


def test_npm_weekly_sums_monday_to_sunday():
    rows = repo_baseline.weekly_npm(repo_baseline.parse_npm_range(load_json("npm_range.json")), WINDOWS)
    assert [r["value"] for r in rows] == [50, 128]
    assert all(r["coverage"] == 1 for r in rows)


def test_npm_weekly_reports_missing_days_as_coverage_not_zero():
    daily = repo_baseline.parse_npm_range(load_json("npm_range.json"))
    del daily["2026-10-11"]
    assert repo_baseline.weekly_npm(daily, WINDOWS)[1]["coverage"] == pytest.approx(6 / 7)


def test_stars_weekly_net_and_cumulative_with_exclusive_end():
    times = repo_baseline.parse_star_times(load_json("stargazers.json"))
    assert len(times) == 4  # the entry with no timestamp is ignored
    rows = repo_baseline.weekly_stars(times, WINDOWS)
    assert [(r["value"], r["cumulative"]) for r in rows] == [(2, 2), (1, 3)]  # 10-12T00:00 is next week


def test_issues_exclude_prs_owner_bots_and_swarm_accounts():
    issues = repo_baseline.parse_issues(load_json("issues.json"), "the-owner", {"agent-account"})
    assert len(issues) == 6  # one PR dropped
    rows = repo_baseline.weekly_issues(issues, WINDOWS)
    assert [(r["value"], r["opened_total"], r["closed_total"]) for r in rows] == [(1, 4, 1), (1, 2, 1)]


def test_contributors_are_distinct_case_insensitive_and_pooled_28_days():
    prs = repo_baseline.parse_merged_prs(load_json("pulls.json"), "the-owner", {"agent-account"})
    assert len(prs) == 6  # the unmerged PR is dropped
    rows = repo_baseline.weekly_contributors(prs, WINDOWS)
    assert [r["value"] for r in rows] == [1, 2]  # 28-day pool: the 09-20 merge counts in both
    assert rows[1]["merged_prs"] == 5


def test_collect_with_fake_fetch_builds_idempotent_readings():
    def fetch(url, accept="application/json"):
        if "api.npmjs.org" in url:
            return load_json("npm_range.json"), {}
        if "/stargazers" in url:
            return load_json("stargazers.json"), {}
        if "/issues" in url:
            return load_json("issues.json"), {}
        if "/pulls" in url:
            return load_json("pulls.json"), {}
        raise AssertionError(url)

    kwargs = dict(package="example-package", repos=["the-owner/repo"], weeks=2,
                  swarm_logins={"agent-account"}, now=NOW, fetch=fetch)
    readings = repo_baseline.collect(**kwargs)
    keys = [r["reading_key"] for r in readings]
    assert len(keys) == len(set(keys))
    assert keys == [r["reading_key"] for r in repo_baseline.collect(**kwargs)]  # replay-stable
    d1 = [r for r in readings if r["metric_ref"] == "D1_npm_downloads_weekly"]
    assert [r["value"] for r in d1] == [50, 128] and all(r["probe_ok"] for r in d1)
    assert d1[0]["reading_key"] == "D1_npm_downloads_weekly|npm:example-package|2026-10-05T00:00:00Z"
    d4 = [r for r in readings if r["metric_ref"] == "D4_external_issues_opened"]
    assert d4[0]["detail"]["excluded_logins"] == ["agent-account", "the-owner"]
    assert not {"user", "login"} & set(json.dumps(d4[0]).split('"'))  # no per-person fields stored


# --- swarm-ops parsers -------------------------------------------------------------------------------------

SWARM_WINDOWS = [(datetime(2026, 10, 5, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc))]


def test_completions_ignore_start_rows_and_keep_failures_without_duration():
    comps = ops.completions(load_jsonl("subprocess_obs.jsonl"))
    assert len(comps) == 6  # 7 rows minus the one start row
    assert [c["ok"] for c in comps].count(False) == 2
    assert any(c["minutes"] is None and not c["ok"] for c in comps)


def test_dispatch_week_rates_minutes_and_tokens():
    comps = ops.completions(load_jsonl("subprocess_obs.jsonl"))
    (row,) = ops.weekly_dispatch_rows(comps, SWARM_WINDOWS)
    assert row["n"] == 5 and row["failed"] == 2 and row["failure_rate"] == pytest.approx(0.4)
    assert row["minutes_per_dispatch"] == pytest.approx(10.75)  # 4 timed: 10, 2, 30, 1
    assert row["tasks_identified"] == 2 and row["minutes_per_identified_task"] == pytest.approx(21.0)
    assert row["by_role"] == {"alpha": {"dispatches": 2, "failed": 0}, "beta": {"dispatches": 3, "failed": 2}}
    assert row["tokens_reported_dispatches"] == 1 and row["total_tokens_reported"] == 5000


def test_empty_week_is_skipped_not_zero():
    other = [(datetime(2026, 9, 7, tzinfo=timezone.utc), datetime(2026, 9, 14, tzinfo=timezone.utc))]
    assert ops.weekly_dispatch_rows(ops.completions(load_jsonl("subprocess_obs.jsonl")), other) == []


def test_task_lifecycles_swarm_only_and_skip_created_done():
    lives = ops.task_lifecycles(load_jsonl("task_obs.jsonl"))
    # td has no swarm marker, te never done; tf carries a list-valued action_type
    assert sorted(t["kind"] for t in lives) == ["analysis", "analysis+review", "design", "unknown"]
    (row,) = ops.weekly_task_rows(lives, SWARM_WINDOWS)
    assert row["n"] == 3 and row["median_hours"] == pytest.approx(12.0) and row["p90_hours"] == pytest.approx(48.0)
    assert row["by_kind"]["analysis"] == {"n": 1, "median_hours": pytest.approx(48.0)}


def test_checkpoints_resolve_once_and_open_statuses_do_not_count():
    cps = ops.resolved_checkpoints(load_jsonl("checkpoint_obs.jsonl"))
    assert [c["status"] for c in cps] == ["approved"]
    (row,) = ops.weekly_intervention_rows(cps, load_jsonl("shape_obs.jsonl"), SWARM_WINDOWS)
    assert row["resolved_checkpoints"] == 1 and row["decision_shape_checks"] == 2


def test_review_commits_distinct_and_pending_excluded():
    assert ops.parse_review_commits(load_json("reviews.json")) == ["aaa", "bbb"]


def test_review_rounds_weekly_counts_unreviewed_separately():
    prs = [{"repo": "r", "number": 1, "merged_at": "2026-10-06T00:00:00Z", "review_commits": ["a", "b"]},
           {"repo": "r", "number": 2, "merged_at": "2026-10-07T00:00:00Z", "review_commits": ["a"]},
           {"repo": "r", "number": 3, "merged_at": "2026-10-08T00:00:00Z", "review_commits": []}]
    (row,) = ops.weekly_review_rows(prs, SWARM_WINDOWS)
    assert row["merged"] == 3 and row["reviewed"] == 2 and row["unreviewed"] == 1
    assert row["rounds_mean"] == pytest.approx(1.5) and row["rounds_max"] == 2


def test_swarm_readings_mark_thin_weeks_partial_and_stay_out_of_baselines():
    readings = ops.build_readings(
        subprocess_rows=load_jsonl("subprocess_obs.jsonl"), task_rows=load_jsonl("task_obs.jsonl"),
        checkpoint_rows=load_jsonl("checkpoint_obs.jsonl"), shape_rows=load_jsonl("shape_obs.jsonl"),
        review_prs=[], now=NOW, first_week=datetime(2026, 10, 5, tzinfo=timezone.utc))
    by_metric = {r["metric_ref"]: r for r in readings}
    assert set(by_metric) == {"S1_time_to_completion", "S2_dispatch_failure_rate", "S3p_operator_intervention_proxy",
                              "S5_cost_per_task"}
    assert by_metric["S2_dispatch_failure_rate"]["status"] == "partial"  # 5 dispatches < the minimum
    assert by_metric["S3p_operator_intervention_proxy"]["status"] == "ok"
    summary = weekly.summarize_readings(readings)
    assert ("S2_dispatch_failure_rate", "swarm:ateles") not in summary
    assert by_metric["S5_cost_per_task"]["detail"]["tokens"] == 5000
    assert by_metric["S1_time_to_completion"]["detail"]["tasks_done"] == 3
    assert all(r["reading_key"].count("|") == 2 for r in readings)
