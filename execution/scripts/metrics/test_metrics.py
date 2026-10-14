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
SWARM = "swarm:test"  # an argument in production; the tests use a neutral label


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    """Sequential reads pause between pages in production; tests do not wait."""
    monkeypatch.setattr(neotoma_io, "PAGE_PAUSE", 0.0)


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
    (row,) = ops.weekly_intervention_rows(cps, load_jsonl("checkpoint_obs.jsonl"), load_jsonl("shape_obs.jsonl"),
                                          SWARM_WINDOWS)
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
        review_prs=[], now=NOW, swarm_artifact=SWARM, first_week=datetime(2026, 10, 5, tzinfo=timezone.utc))
    by_metric = {r["metric_ref"]: r for r in readings}
    assert set(by_metric) == {"S1_time_to_completion", "S2_dispatch_failure_rate", "S3p_operator_intervention_proxy",
                              "S5_cost_per_task"}
    assert by_metric["S2_dispatch_failure_rate"]["status"] == "partial"  # 5 dispatches < the minimum
    assert by_metric["S3p_operator_intervention_proxy"]["status"] == "ok"
    summary = weekly.summarize_readings(readings)
    assert ("S2_dispatch_failure_rate", SWARM) not in summary
    assert by_metric["S5_cost_per_task"]["detail"]["tokens_reported_sum"] == 5000
    assert by_metric["S1_time_to_completion"]["detail"]["tasks_done"] == 3
    assert all(r["reading_key"].count("|") == 2 for r in readings)


# =====================================================================================================
# Review round: units, absence-is-not-zero, store repair, thin weeks, token handling, bounded reads
# =====================================================================================================

import http.server
import io
import os
import threading
import urllib.error
from datetime import timedelta

UTC = timezone.utc
WEEK = (datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 12, tzinfo=UTC))


# --- a definition's unit is the unit its readings carry ---------------------------------------------

def _all_fixture_readings():
    swarm = ops.build_readings(
        subprocess_rows=load_jsonl("subprocess_obs.jsonl"), task_rows=load_jsonl("task_obs.jsonl"),
        checkpoint_rows=load_jsonl("checkpoint_obs.jsonl"), shape_rows=load_jsonl("shape_obs.jsonl"),
        review_prs=[{"repo": "o/r", "number": 1, "merged_at": "2026-10-07T00:00:00Z", "review_commits": ["a"]}],
        now=NOW, swarm_artifact=SWARM, first_week=WEEK[0])

    def fetch(url, accept="application/json"):
        for key, name in (("api.npmjs.org", "npm_range.json"), ("/stargazers", "stargazers.json"),
                          ("/issues", "issues.json"), ("/pulls", "pulls.json")):
            if key in url:
                return load_json(name), {}
        raise AssertionError(url)

    repo = repo_baseline.collect(package="example-package", repos=["the-owner/repo"], weeks=2,
                                 swarm_logins={"agent-account"}, now=NOW, fetch=fetch)
    return swarm + repo


def test_every_reading_unit_equals_its_definition_unit():
    units = {d["metric_id"]: d["unit"] for d in definitions.build_definitions()}
    readings = _all_fixture_readings()
    assert {r["metric_ref"] for r in readings} == {
        "D1_npm_downloads_weekly", "D2_github_stars", "D4_external_issues_opened", "D5_external_contributors",
        "S1_time_to_completion", "S2_dispatch_failure_rate", "S3p_operator_intervention_proxy",
        "S4_review_rounds_per_pr", "S5_cost_per_task"}  # every runnable metric is exercised
    for r in readings:
        assert r["unit"] == units[r["metric_ref"]], r["metric_ref"]


def test_s5_definition_says_per_dispatch_not_per_task():
    s5 = next(d for d in definitions.build_definitions() if d["metric_id"] == "S5_cost_per_task")
    assert s5["unit"] == "minutes_per_dispatch"
    assert "per task" not in s5["query"]["derive"] and "dispatch" in s5["query"]["derive"]
    assert "not per task" in s5["description"]


# --- S3p: absence is not zero; only decided statuses count ------------------------------------------

def cp(entity, at, status):
    return {"entity_id": entity, "observed_at": at, "fields": {"status": status}}


def s3p(rows, **kw):
    base = dict(subprocess_rows=[], task_rows=[], checkpoint_rows=rows, shape_rows=[], review_prs=[], now=NOW,
                swarm_artifact=SWARM)
    base.update(kw)
    return [r for r in ops.build_readings(**base) if r["metric_ref"] == "S3p_operator_intervention_proxy"]


def test_s3p_empty_leading_weeks_have_no_reading():
    rows = [cp("c1", "2026-10-06T10:00:00Z", "awaiting_operator"), cp("c1", "2026-10-07T10:00:00Z", "approved")]
    readings = s3p(rows, first_week=datetime(2026, 8, 3, tzinfo=UTC))  # 9 weeks before the first record
    assert [r["window_end"] for r in readings] == ["2026-10-12T00:00:00Z"]
    assert readings[0]["value"] == 1.0


def test_s3p_default_start_is_the_first_observed_checkpoint():
    rows = [cp("c1", "2026-09-22T10:00:00Z", "awaiting_operator"), cp("c1", "2026-10-07T10:00:00Z", "approved")]
    readings = s3p(rows)
    assert readings[0]["window_end"] == "2026-09-28T00:00:00Z"  # the week of 09-22, nothing earlier


def test_s3p_week_with_records_but_no_decision_is_a_measured_zero_and_a_gap_week_is_skipped():
    rows = [cp("c1", "2026-09-22T10:00:00Z", "awaiting_operator"),     # week ending 09-28: record, no decision
            cp("c2", "2026-10-07T10:00:00Z", "awaiting_operator"),     # week ending 10-12: record, no decision
            cp("c3", "2026-10-08T10:00:00Z", "approved")]
    readings = {r["window_end"]: r for r in s3p(rows)}
    assert "2026-10-05T00:00:00Z" not in readings                      # no record at all that week
    assert readings["2026-09-28T00:00:00Z"]["value"] == 0.0 and readings["2026-09-28T00:00:00Z"]["status"] == "ok"
    assert readings["2026-10-12T00:00:00Z"]["value"] == 1.0


def test_s3p_machine_approvals_and_pending_states_are_not_operator_decisions():
    rows = []
    for i, status in enumerate(["approved_via_auto_build", "approved_via_autobuild", "approved_by_dispatch",
                                "pending_review", "pending_operator_decision", "pending_operator_review",
                                "pending_operator_approval", "superseded", "resolved", "prepared", "open"]):
        rows.append(cp(f"m{i}", "2026-10-06T10:00:00Z", status))
    rows += [cp("d1", "2026-10-06T10:00:00Z", "approved"), cp("d2", "2026-10-06T11:00:00Z", "rejected"),
             cp("d3", "2026-10-06T12:00:00Z", "approved_no_release"), cp("d4", "2026-10-06T13:00:00Z", "redirected")]
    (reading,) = s3p(rows)
    assert reading["value"] == 4.0


def test_s3p_without_any_checkpoint_rows_has_no_reading_rather_than_zeros():
    assert s3p([], first_week=datetime(2026, 6, 15, tzinfo=UTC)) == []


def test_s3p_shape_check_count_is_not_recorded_before_that_source_existed():
    rows = [cp("c1", "2026-09-22T10:00:00Z", "awaiting_operator"), cp("c1", "2026-10-07T10:00:00Z", "approved")]
    shape = [{"entity_id": "s", "observed_at": "2026-10-06T10:00:00Z",
              "fields": {"event_type": "decision_shape_check", "event_at": "2026-10-06T10:00:00Z"}}]
    readings = {r["window_end"]: r for r in s3p(rows, shape_rows=shape)}
    assert readings["2026-09-28T00:00:00Z"]["detail"]["decision_shape_checks"] == "not recorded"
    assert readings["2026-10-12T00:00:00Z"]["detail"]["decision_shape_checks"] == 1


# --- stale caches: windows stop at the fetch, sourced_at is the fetch time, old caches are refused ----

def test_windows_stop_at_the_fetch_time_and_sourced_at_is_the_fetch_time():
    fetched = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)  # 13 days before NOW
    rows = [cp("c1", "2026-09-22T10:00:00Z", "approved"), cp("c2", "2026-10-07T10:00:00Z", "approved")]
    readings = s3p(rows, fetched_at={"checkpoints": fetched, "subprocess": fetched, "tasks": fetched,
                                     "shape_checks": fetched, "reviews": fetched})
    assert [r["window_end"] for r in readings] == ["2026-09-28T00:00:00Z"]  # nothing past the last complete week at fetch
    assert all(r["sourced_at"] == "2026-09-30T08:00:00Z" for r in readings)


def test_sourced_at_defaults_to_now_for_a_live_read():
    (reading,) = s3p([cp("c1", "2026-10-06T10:00:00Z", "approved")])
    assert reading["sourced_at"] == "2026-10-13T12:00:00Z"


def _write_cache_dir(tmp_path, fetched_at):
    for name, fixture in (("subprocess", "subprocess_obs.jsonl"), ("tasks", "task_obs.jsonl"),
                          ("checkpoints", "checkpoint_obs.jsonl"), ("shape_checks", "shape_obs.jsonl")):
        neotoma_io.write_cache(tmp_path / f"{name}.jsonl", load_jsonl(fixture), fetched_at=fetched_at)


def test_a_stale_cache_is_refused_and_with_allow_stale_every_reading_is_partial(tmp_path, monkeypatch, capsys):
    _write_cache_dir(tmp_path, "2026-09-01T00:00:00Z")
    monkeypatch.setattr(ops, "utcnow", lambda: NOW)
    assert ops.main(["compute", "--cache-dir", str(tmp_path), "--swarm-artifact", SWARM]) == 2
    assert "stale" in capsys.readouterr().err
    # a deliberately stale read: nothing in it can be an ok zero
    assert ops.main(["compute", "--cache-dir", str(tmp_path), "--swarm-artifact", SWARM, "--allow-stale",
                     "--first-week", "2026-08-24"]) in (0, 1)
    stamps = ops.stream_fetched_at(tmp_path)
    assert ops.stale_streams(stamps, NOW, 7) == ["checkpoints", "shape_checks", "subprocess", "tasks"]
    stale = ops.mark_stale(_all_fixture_readings(), "cache older than 7 days")
    assert {r["status"] for r in stale} == {"partial"} and max(r["coverage_ratio"] for r in stale) == 0.5
    assert weekly.summarize_readings(stale) == {}  # stale readings never enter a baseline


def test_a_fresh_cache_is_read_and_stamped_with_its_fetch_time(tmp_path, monkeypatch, capsys):
    _write_cache_dir(tmp_path, "2026-10-13T10:00:00Z")
    monkeypatch.setattr(ops, "utcnow", lambda: NOW)
    assert ops.main(["compute", "--cache-dir", str(tmp_path), "--swarm-artifact", SWARM]) == 0
    assert ops.stale_streams(ops.stream_fetched_at(tmp_path), NOW, 7) == []


def test_swarm_artifact_is_required_not_defaulted(tmp_path, monkeypatch):
    monkeypatch.delenv("METRICS_SWARM_ARTIFACT", raising=False)
    _write_cache_dir(tmp_path, "2026-10-13T10:00:00Z")
    with pytest.raises(SystemExit) as exc:
        ops.main(["compute", "--cache-dir", str(tmp_path)])
    assert exc.value.code == 2


def test_first_week_defaults_to_the_earliest_observation():
    readings = ops.build_readings(
        subprocess_rows=load_jsonl("subprocess_obs.jsonl"), task_rows=[], checkpoint_rows=[], shape_rows=[],
        review_prs=[], now=NOW, swarm_artifact=SWARM)
    assert readings and min(r["window_end"] for r in readings) == "2026-10-12T00:00:00Z"
    assert ops.first_observed([{"observed_at": "2026-03-04T00:00:00Z"}, {"observed_at": "2026-02-01T00:00:00Z"}]) \
        == datetime(2026, 2, 1, tzinfo=UTC)


def test_s1_surfaces_tasks_first_recorded_done():
    rows = done_tasks(5) + [
        {"entity_id": "x", "observed_at": "2026-10-08T08:00:00Z", "fields": {"status": "done", "assigned_to": "a"}}]
    r = build(task_rows=rows)["S1_time_to_completion|" + SWARM]
    assert r["detail"]["tasks_done"] == 5 and r["detail"]["first_recorded_done_excluded"] == 1


# --- store repair: a server that answers a seen idempotency key as replayed -------------------------------

class Srv:
    """Reports success on every /store; ``plan(call_no, chunk)`` says how many of the chunk are written.

    A key already seen is answered ``replayed`` and writes nothing, as the real server does.
    """

    def __init__(self, plan=lambda n, chunk: len(chunk), extra=None):
        self.plan, self.extra, self.keys, self.rows, self.calls = plan, extra or {}, set(), {}, []

    def __call__(self, method, path, body=None, **_):
        if path == "/store":
            ents, key = body["entities"], body["idempotency_key"]
            self.calls.append((key, [e["reading_key"] for e in ents]))
            if key in self.keys:
                return {"success": True, "replayed": True}
            self.keys.add(key)
            n = self.plan(len(self.calls), ents)
            for e in ents[:n]:
                self.rows[e["reading_key"]] = {k: v for k, v in e.items() if k != "entity_type"}
            return {"success": True, "replayed": False, "observations_created": n, **self.extra}
        if path == "/entities/query":
            ents = [{"snapshot": s} for s in self.rows.values()]
            return {"entities": ents[body["offset"]:body["offset"] + body["limit"]], "total": len(ents)}
        raise AssertionError(path)


def rd(i):
    return schemas.build_record("metric_reading", reading_key=f"m|a|{i}", metric_ref="m", value=float(i),
                                unit="u", status="ok")


def test_repair_resends_under_a_new_idempotency_key_even_when_the_chunk_is_identical():
    srv = Srv(plan=lambda n, chunk: 0 if n == 1 else len(chunk))  # first call: success reported, nothing written
    assert neotoma_io.store_and_verify([rd(1), rd(2)], "t", "reading_key", requester=srv) == []
    keys = [k for k, _ in srv.calls]
    assert len(keys) == len(set(keys)) and len(srv.rows) == 2


def test_a_second_repair_round_runs_when_the_first_repair_is_also_dropped():
    srv = Srv(plan=lambda n, chunk: 0 if n <= 2 else len(chunk))
    assert neotoma_io.store_and_verify([rd(1), rd(2)], "t", "reading_key", requester=srv) == []
    assert len(srv.calls) == 3


def test_repair_rounds_use_smaller_batches_than_the_first_pass():
    srv = Srv(plan=lambda n, chunk: 0 if len(chunk) > 4 else len(chunk))  # big batches silently dropped
    assert neotoma_io.store_and_verify([rd(i) for i in range(8)], "t", "reading_key", requester=srv) == []
    assert all(len(ids) <= 2 for _, ids in srv.calls[1:])


def test_repair_resends_only_what_the_read_back_could_not_find():
    srv = Srv(plan=lambda n, chunk: 3 if n == 1 else len(chunk))  # first call keeps 3 of 8
    assert neotoma_io.store_and_verify([rd(i) for i in range(8)], "t", "reading_key", requester=srv) == []
    resent = [i for _, ids in srv.calls[1:] for i in ids]
    assert sorted(resent) == sorted(f"m|a|{i}" for i in range(3, 8))


def test_repair_keys_differ_between_runs_of_the_same_payload(monkeypatch):
    """A repair key spent by an earlier run must not be replayed by an immediate rerun."""
    srv = Srv(plan=lambda n, chunk: 0 if n == 1 or n == 3 else len(chunk))
    ents = [rd(1)]
    assert neotoma_io.store_and_verify(ents, "t", "reading_key", requester=srv) == []
    srv.rows.clear()
    monkeypatch.setattr(neotoma_io, "_RUN_NONCE", "nonce-two")
    assert neotoma_io.store_and_verify(ents, "t", "reading_key", requester=srv) == []
    repair_keys = [k for k, _ in srv.calls if "-repair" in k]
    assert len(repair_keys) == len(set(repair_keys)) == 2


def test_store_reported_unknown_fields_is_surfaced_even_when_read_back_is_clean():
    srv = Srv(extra={"unknown_fields_count": 2})
    problems = neotoma_io.store_and_verify([rd(1)], "t", "reading_key", requester=srv)
    assert any("unknown_fields_count=2" in p for p in problems)


def test_read_back_pages_past_the_first_hundred_rows():
    srv = Srv()
    assert neotoma_io.store_and_verify([rd(i) for i in range(130)], "t", "reading_key", requester=srv) == []


def test_store_pauses_between_back_to_back_store_calls(monkeypatch):
    sleeps = []
    monkeypatch.setattr(neotoma_io.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(neotoma_io, "PAGE_PAUSE", 0.25)
    neotoma_io.store_and_verify([rd(i) for i in range(20)], "t", "reading_key", requester=Srv())  # 3 store calls
    assert sleeps.count(0.25) >= 2


# --- bounded, sequential reads ------------------------------------------------------------------------------

def test_query_all_advances_by_rows_returned_not_by_page_size():
    rows = [{"snapshot": {"k": i}} for i in range(7)]

    def short_pages(method, path, body=None, **_):  # the server never returns more than 3 rows
        return {"entities": rows[body["offset"]:body["offset"] + 3], "total": 7}

    assert len(neotoma_io.query_all("t", page=100, requester=short_pages)) == 7


def test_query_all_stops_when_total_is_absent():
    calls = []

    def no_total(method, path, body=None, **_):
        calls.append(body["offset"])
        return {"entities": [{"snapshot": {"k": 1}}] * 100}  # a server that ignores offset and omits total

    assert len(neotoma_io.query_all("t", page=100, requester=no_total)) == 100
    assert calls == [0]


def test_query_all_raises_at_the_page_cap_instead_of_looping():
    calls = []

    def endless(method, path, body=None, **_):
        calls.append(1)
        assert len(calls) < 50, "page cap not enforced"
        return {"entities": [{"snapshot": {}}], "total": 10_000}

    with pytest.raises(neotoma_io.ReadLimitError):
        neotoma_io.query_all("t", page=1, max_pages=5, requester=endless)
    assert len(calls) == 5


def test_query_all_pauses_between_pages(monkeypatch):
    sleeps = []
    monkeypatch.setattr(neotoma_io.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(neotoma_io, "PAGE_PAUSE", 0.4)
    neotoma_io.query_all("t", page=2, requester=Srv_pages(5))
    assert sleeps == [0.4, 0.4]


def Srv_pages(n):
    rows = [{"snapshot": {"k": i}} for i in range(n)]

    def fn(method, path, body=None, **_):
        return {"entities": rows[body["offset"]:body["offset"] + body["limit"]], "total": n}

    return fn


def test_a_read_back_that_hits_the_page_cap_is_reported_as_a_problem(monkeypatch):
    monkeypatch.setattr(neotoma_io, "MAX_PAGES", 1)
    problems = neotoma_io.store_and_verify([rd(i) for i in range(150)], "t", "reading_key", requester=Srv())
    assert any("read-back incomplete" in p for p in problems)


def test_observation_streams_advance_by_rows_returned_and_stop_at_the_cap():
    def short(method, path, body=None, **_):
        off = body["offset"] if body else int(path.split("offset=")[1])
        obs = [{"entity_id": "e", "observed_at": f"2026-10-0{i + 1}T00:00:00Z", "fields": {"status": "x"}}
               for i in range(off, min(off + 2, 6))]
        return {"observations": obs, "total": 6}

    assert len(neotoma_io.fetch_observation_stream("task", ("status",), page=200, requester=short)) == 6
    assert len(neotoma_io.fetch_entity_observations("e", ("status",), page=500, requester=short)) == 6

    seen = []

    def endless(method, path, body=None, **_):
        seen.append(1)
        assert len(seen) < 50, "page cap not enforced"
        return {"observations": [{"entity_id": "e", "observed_at": "x", "fields": {}}], "total": 10 ** 6}

    with pytest.raises(neotoma_io.ReadLimitError):
        neotoma_io.fetch_observation_stream("task", ("status",), page=1, max_pages=3, requester=endless)
    with pytest.raises(neotoma_io.ReadLimitError):
        neotoma_io.fetch_entity_observations("e", ("status",), page=1, max_pages=3, requester=endless)


def test_fetch_subprocess_and_shape_checks_page_through_every_entity():
    queries = []

    def srv(method, path, body=None, **_):
        if path == "/entities/query":
            queries.append(body)
            ents = [{"entity_id": f"e{i}"} for i in range(150)]
            return {"entities": ents[body["offset"]:body["offset"] + body["limit"]], "total": 150}
        return {"observations": [{"observed_at": "2026-10-06T00:00:00Z", "fields": {"success": "true"}}], "total": 1}

    assert len(ops.fetch_subprocess(requester=srv)) == 150
    assert len(ops.fetch_shape_checks(requester=srv)) == 150
    assert len(queries) == 4  # two pages each
    assert queries[0]["snapshot_filters"]["event_type"]["value"] == "subprocess"
    assert queries[2]["snapshot_filters"]["event_type"]["value"] == "decision_shape_check"


def test_an_empty_source_is_an_error_not_an_empty_cache():
    empty = lambda method, path, body=None, **_: {"entities": [], "total": 0}  # noqa: E731
    with pytest.raises(RuntimeError, match="no harness_event entities"):
        ops.fetch_subprocess(requester=empty)
    with pytest.raises(RuntimeError, match="no harness_event entities"):
        ops.fetch_shape_checks(requester=empty)


# --- the raw cache keeps only what the derivations read, mode 0600 ---------------------------------------------

def test_cache_files_are_private_and_record_a_fetch_time(tmp_path):
    path = tmp_path / "cache" / "tasks.jsonl"
    neotoma_io.write_cache(path, [{"entity_id": "e", "observed_at": "2026-10-06T00:00:00Z", "fields": {}}],
                           fetched_at="2026-10-13T09:00:00Z")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct((path.parent / "meta.json").stat().st_mode & 0o777) == "0o600"
    assert neotoma_io.read_meta(path.parent)["tasks"] == {
        "fetched_at": "2026-10-13T09:00:00Z", "rows": 1, "newest_observed_at": "2026-10-06T00:00:00Z"}
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_no_cache_keeps_titles_summaries_or_session_ids():
    kept = set(ops.SUBPROCESS_KEEP) | set(ops.TASK_KEEP) | set(ops.CHECKPOINT_KEEP) | set(ops.SHAPE_KEEP)
    assert not kept & {"title", "summary", "session_id", "harness", "model", "provider", "task_type"}

    def srv(method, path, body=None, **_):
        return {"observations": [{"entity_id": "e", "observed_at": "2026-10-06T00:00:00Z",
                                  "fields": {"status": "done", "title": "a personal errand"}}], "total": 1}

    (row,) = neotoma_io.fetch_observation_stream("task", ops.TASK_KEEP, requester=srv)
    assert row["fields"] == {"status": "done"}


# --- thin weeks and "never zero" ---------------------------------------------------------------------------------

def sub_rows(n, tokens=None):
    rows = []
    for i in range(n):
        f = {"event_type": "subprocess", "event_at": f"2026-10-06T{i % 24:02d}:{i % 60:02d}:00+00:00",
             "tool_name": "codex:alpha", "success": "true", "duration_ms": 60000}
        if tokens is not None:
            f["total_tokens"] = tokens
        rows.append({"entity_id": "e", "observed_at": "2026-10-06T10:00:00Z", "fields": f})
    return rows


def build(**kw):
    base = dict(subprocess_rows=[], task_rows=[], checkpoint_rows=[], shape_rows=[], review_prs=[], now=NOW,
                swarm_artifact=SWARM, first_week=WEEK[0])
    base.update(kw)
    return {r["metric_ref"] + "|" + r["artifact_ref"]: r for r in ops.build_readings(**base)}


def done_tasks(n):
    rows = []
    for i in range(n):
        rows.append({"entity_id": f"t{i}", "observed_at": "2026-10-06T08:00:00Z",
                     "fields": {"status": "open", "assigned_to": "x"}})
        rows.append({"entity_id": f"t{i}", "observed_at": "2026-10-07T08:00:00Z", "fields": {"status": "done"}})
    return rows


@pytest.mark.parametrize("n,status", [(19, "partial"), (20, "ok")])
def test_s2_thin_week_boundary_is_20_dispatches(n, status):
    r = build(subprocess_rows=sub_rows(n))["S2_dispatch_failure_rate|" + SWARM]
    assert r["status"] == status and r["coverage_ratio"] == (0.5 if status == "partial" else 1.0)


@pytest.mark.parametrize("n,status", [(4, "partial"), (5, "ok")])
def test_s1_thin_week_boundary_is_5_done_tasks(n, status):
    r = build(task_rows=done_tasks(n))["S1_time_to_completion|" + SWARM]
    assert r["status"] == status
    assert (r["coverage_ratio"], bool(r.get("coverage_note"))) == ((0.5, True) if status == "partial" else (1.0, False))
    summary = weekly.summarize_readings([r])
    assert (("S1_time_to_completion", SWARM) in summary) == (status == "ok")


def pr(n, rounds, day="2026-10-07T00:00:00Z"):
    return {"repo": "o/r", "number": n, "merged_at": day, "review_commits": [f"c{k}" for k in range(rounds)]}


@pytest.mark.parametrize("reviewed,status", [(2, "partial"), (3, "ok")])
def test_s4_reading_thin_boundary_artifact_and_detail(reviewed, status):
    prs = [pr(i, 1 + (i % 2)) for i in range(reviewed)] + [pr(99, 0)]
    r = build(review_prs=prs)["S4_review_rounds_per_pr|github:o/r"]
    assert r["status"] == status
    assert r["detail"]["reviewed_prs"] == reviewed and r["detail"]["unreviewed_prs"] == 1
    assert r["detail"]["counts_formal_reviews_only"] is True


def test_s4_week_with_only_unreviewed_prs_has_no_reading():
    assert "S4_review_rounds_per_pr|github:o/r" not in build(review_prs=[pr(1, 0), pr(2, 0)])


def test_s4_has_one_artifact_per_repository():
    prs = [{**pr(1, 1), "repo": "o/a"}, {**pr(2, 1), "repo": "o/b"}]
    assert {k for k in build(review_prs=prs) if k.startswith("S4")} == {
        "S4_review_rounds_per_pr|github:o/a", "S4_review_rounds_per_pr|github:o/b"}


def test_tokens_are_not_metered_not_zero_when_no_harness_reported_them():
    assert build(subprocess_rows=sub_rows(3))["S5_cost_per_task|" + SWARM]["detail"]["tokens_reported_sum"] == "not metered"
    r = build(subprocess_rows=sub_rows(3, tokens=7))["S5_cost_per_task|" + SWARM]
    assert r["detail"]["tokens_reported_sum"] == 21 and r["detail"]["tokens_reported_dispatches"] == 3


def test_s2_and_s5_cover_every_dispatch_the_s4_detail_names():
    r = build(subprocess_rows=sub_rows(20))
    assert r["S5_cost_per_task|" + SWARM]["value"] == pytest.approx(1.0)
    assert r["S2_dispatch_failure_rate|" + SWARM]["value"] == 0.0


# --- GitHub token handling in http_get -----------------------------------------------------------------------------

TOKEN = "tok-synthetic-" + "0123456789"  # not a credential; gitleaks:allow


class _Recorder(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep test output quiet
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        self.server.respond(self)


def _serve(respond):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    srv.seen, srv.respond = [], respond
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv


def _ok(h):
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.end_headers()
    h.wfile.write(b"[]")


@pytest.fixture
def two_hosts(monkeypatch):
    """A (the configured GitHub API) and B (any other host), both local."""
    b = _serve(_ok)
    a = _serve(lambda h: (h.send_response(307), h.send_header("Location", f"http://127.0.0.1:{b.server_port}/stolen"),
                          h.end_headers()))
    monkeypatch.setattr(repo_baseline, "GITHUB_API", f"http://127.0.0.1:{a.server_port}")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    yield a, b
    for srv in (a, b):
        srv.shutdown()
        srv.server_close()


def test_a_redirect_is_never_followed_and_the_token_never_reaches_another_host(two_hosts, capsys):
    a, b = two_hosts
    with pytest.raises(RuntimeError) as exc:
        repo_baseline.http_get(f"{repo_baseline.GITHUB_API}/repos/o/r/issues")
    assert a.seen and a.seen[0][1] == f"Bearer {TOKEN}"  # the configured API host did get it
    assert b.seen == []  # the redirect target got no request at all, so no header
    out = capsys.readouterr()
    assert TOKEN not in str(exc.value) and TOKEN not in out.out + out.err


def test_the_token_is_sent_only_to_the_github_api_host(two_hosts):
    a, b = two_hosts
    repo_baseline.http_get(f"http://127.0.0.1:{b.server_port}/downloads/range/a:b/p")
    assert b.seen == [("/downloads/range/a:b/p", None)]


def test_a_token_with_trailing_whitespace_is_stripped_before_use(two_hosts, monkeypatch):
    a, b = two_hosts
    a.respond = _ok
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN + "\r\n")
    repo_baseline.http_get(f"{repo_baseline.GITHUB_API}/repos/o/r/issues")
    assert a.seen[0][1] == f"Bearer {TOKEN}"


def test_a_malformed_token_never_appears_in_the_error(two_hosts, monkeypatch, capsys):
    a, b = two_hosts
    bad = "tok-synth\netic-0123456789"  # an embedded newline survives strip()
    monkeypatch.setenv("GITHUB_TOKEN", bad)
    with pytest.raises(Exception) as exc:
        repo_baseline.http_get(f"{repo_baseline.GITHUB_API}/repos/o/r/issues")
    out = capsys.readouterr()
    text = str(exc.value) + repr(exc.value) + out.out + out.err
    assert "tok-synth" not in text and "etic-0123456789" not in text
    assert a.seen == [] and b.seen == []


def test_a_server_error_body_that_echoes_the_token_is_redacted(two_hosts, capsys):
    a, b = two_hosts

    def echo(h):
        h.send_response(404)
        h.end_headers()
        h.wfile.write(f"no such thing for {h.headers.get('Authorization')}".encode())

    a.respond = echo
    with pytest.raises(RuntimeError) as exc:
        repo_baseline.http_get(f"{repo_baseline.GITHUB_API}/repos/o/r/issues")
    out = capsys.readouterr()
    assert "HTTP 404" in str(exc.value)
    assert TOKEN not in str(exc.value) + out.out + out.err


def test_an_empty_token_sends_no_authorization_header(two_hosts, monkeypatch):
    a, b = two_hosts
    a.respond = _ok
    monkeypatch.setenv("GITHUB_TOKEN", "  ")
    repo_baseline.http_get(f"{repo_baseline.GITHUB_API}/repos/o/r/issues")
    assert a.seen[0][1] is None


# --- pagination never ends a list quietly ---------------------------------------------------------------------------

def test_pagination_refuses_a_next_link_to_another_host():
    calls = []

    def fetch(url, accept):
        calls.append(url)
        return [{"n": 1}], {"link": '<https://evil.example/x?page=2>; rel="next"'}

    with pytest.raises(repo_baseline.PaginationError):
        list(repo_baseline.paginate("https://api.github.com/x", fetch=fetch))
    assert calls == ["https://api.github.com/x"]


def test_pagination_raises_at_the_page_cap():
    calls = []

    def fetch(url, accept):
        calls.append(url)
        assert len(calls) < 20, "page cap not enforced"  # a bounded failure instead of an endless loop
        return [{"n": 1}], {"link": '<https://api.github.com/x?page=2>; rel="next"'}

    with pytest.raises(repo_baseline.PaginationError):
        list(repo_baseline.paginate("https://api.github.com/x", fetch=fetch, max_pages=3))
    assert len(calls) == 3


def test_pagination_follows_same_host_links_to_the_end():
    pages = {"https://api.github.com/x": ([{"n": 1}], {"link": '<https://api.github.com/x?page=2>; rel="next"'}),
             "https://api.github.com/x?page=2": ([{"n": 2}], {})}
    assert [i["n"] for i in repo_baseline.paginate("https://api.github.com/x", fetch=lambda u, a: pages[u])] == [1, 2]


def test_an_npm_payload_without_downloads_is_an_error_not_26_zeros():
    with pytest.raises(ValueError):
        repo_baseline.parse_npm_range({"error": "package not found"})


def test_npm_probe_is_false_when_the_last_week_is_zero():
    def fetch(url, accept="application/json"):
        if "api.npmjs.org" in url:
            return {"downloads": [{"day": "2026-10-05", "downloads": 0}]}, {}
        return [], {}

    rows = repo_baseline.collect(package="p", repos=["o/r"], weeks=1, swarm_logins=set(), now=NOW, fetch=fetch)
    (d1,) = [r for r in rows if r["metric_ref"] == "D1_npm_downloads_weekly"]
    assert d1["probe_ok"] is False


def test_arguments_interpolated_into_urls_are_validated():
    for bad in ("o/r/../x", "o r", "o/r?x=1", "justone", "o/r#frag"):
        with pytest.raises(ValueError):
            repo_baseline.validate_repo(bad)
    with pytest.raises(ValueError):
        repo_baseline.validate_targets("pkg/../x", ["o/r"])
    repo_baseline.validate_targets("@scope/pkg", ["the-owner/repo.name"])


# --- command-line guards: swarm logins, dry run -------------------------------------------------------------------------

def _stub_collect(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return [reading(1) | {"metric_ref": "D1_npm_downloads_weekly", "window_end": "2026-10-12T00:00:00Z"}]

    monkeypatch.setattr(repo_baseline, "collect", fake)
    return seen


def test_an_empty_swarm_login_list_is_rejected(monkeypatch):
    monkeypatch.delenv("METRICS_SWARM_LOGINS", raising=False)
    _stub_collect(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        repo_baseline.main(["compute", "--npm-package", "p", "--repo", "o/r"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        repo_baseline.main(["compute", "--npm-package", "p", "--repo", "o/r", "--swarm-login", " "])


def test_swarm_logins_come_from_arguments_env_or_an_explicit_opt_out(monkeypatch):
    seen = _stub_collect(monkeypatch)
    monkeypatch.delenv("METRICS_SWARM_LOGINS", raising=False)
    assert repo_baseline.main(["compute", "--npm-package", "p", "--repo", "o/r", "--swarm-login", "Bot-A"]) == 0
    assert seen["swarm_logins"] == {"bot-a"}
    monkeypatch.setenv("METRICS_SWARM_LOGINS", "x, y")
    assert repo_baseline.main(["compute", "--npm-package", "p", "--repo", "o/r"]) == 0
    assert seen["swarm_logins"] == {"x", "y"}
    monkeypatch.delenv("METRICS_SWARM_LOGINS")
    assert repo_baseline.main(["compute", "--npm-package", "p", "--repo", "o/r", "--no-swarm-logins"]) == 0
    assert seen["swarm_logins"] == set()


def _forbid_writes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a dry run wrote")

    monkeypatch.setattr(neotoma_io, "store_and_verify", boom)
    monkeypatch.setattr(neotoma_io, "request", boom)


def test_repo_store_dry_run_names_the_target_and_writes_nothing(monkeypatch, capsys):
    _stub_collect(monkeypatch)
    _forbid_writes(monkeypatch)
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", TOKEN)
    assert repo_baseline.main(["store", "--dry-run", "--npm-package", "p", "--repo", "o/r", "--swarm-login", "a"]) == 0
    out = capsys.readouterr()
    assert "https://neotoma.example.test" in out.out and "nothing written" in out.out
    assert TOKEN not in out.out + out.err


def test_swarm_store_dry_run_names_the_target_and_writes_nothing(tmp_path, monkeypatch, capsys):
    _write_cache_dir(tmp_path, "2026-10-13T10:00:00Z")
    _forbid_writes(monkeypatch)
    monkeypatch.setattr(ops, "utcnow", lambda: NOW)
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", TOKEN)
    assert ops.main(["store", "--dry-run", "--cache-dir", str(tmp_path), "--swarm-artifact", SWARM]) == 0
    out = capsys.readouterr()
    assert "https://neotoma.example.test" in out.out and "nothing written" in out.out
    assert TOKEN not in out.out + out.err


def test_store_names_the_target_before_it_writes(monkeypatch, capsys):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", TOKEN)
    monkeypatch.setattr(neotoma_io, "store_and_verify", lambda *a, **k: [])
    assert neotoma_io.store_command([reading(1)], "t", "reading_key") == 0
    assert "https://neotoma.example.test" in capsys.readouterr().out


def test_store_refuses_without_a_configured_instance(monkeypatch, capsys):
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", TOKEN)
    _forbid_writes(monkeypatch)
    assert neotoma_io.store_command([reading(1)], "t", "reading_key") == 2


def test_metric_filter_limits_what_is_computed(tmp_path, monkeypatch, capsys):
    _write_cache_dir(tmp_path, "2026-10-13T10:00:00Z")
    monkeypatch.setattr(ops, "utcnow", lambda: NOW)
    assert ops.main(["compute", "--cache-dir", str(tmp_path), "--swarm-artifact", SWARM,
                     "--metric", "S3p_operator_intervention_proxy"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 readings;") and "S3p_operator_intervention_proxy" in out
    assert ops.main(["compute", "--cache-dir", str(tmp_path), "--swarm-artifact", SWARM]) == 0
    assert not capsys.readouterr().out.startswith("1 readings;")


def test_swarm_artifact_label_comes_from_the_argument():
    r = build(subprocess_rows=sub_rows(3), swarm_artifact="swarm:other")
    assert {k.split("|")[1] for k in r} == {"swarm:other"}


def test_first_week_default_reaches_back_before_any_fixed_date():
    early = [{"entity_id": "e", "observed_at": "2026-03-04T10:00:00Z",
              "fields": {"event_type": "subprocess", "event_at": "2026-03-04T10:00:00Z", "tool_name": "p:r",
                         "success": "true", "duration_ms": 60000}}]
    readings = ops.build_readings(subprocess_rows=early, task_rows=[], checkpoint_rows=[], shape_rows=[],
                                  review_prs=[], now=NOW, swarm_artifact=SWARM)
    assert min(r["window_end"] for r in readings) == "2026-03-09T00:00:00Z"


def test_an_existing_cache_directory_is_tightened_to_0700(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o755)
    os.chmod(cache, 0o755)
    neotoma_io.write_cache(cache / "tasks.jsonl", [])
    assert oct(cache.stat().st_mode & 0o777) == "0o700"
