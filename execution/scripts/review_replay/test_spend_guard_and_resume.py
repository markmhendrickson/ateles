"""Spend guard and resumability."""

from __future__ import annotations

import json

from review_replay.review_replay import load_done, run_key
from review_replay.spend_guard import SpendGuard


def test_guard_gives_each_run_the_remaining_budget_and_stops_at_the_cap():
    g = SpendGuard(cap_usd=1.0)
    ok, _, limit = g.reserve("c")
    assert ok and limit == 1.0
    g.finish("c", "openrouter", 0.3)
    ok, _, limit = g.reserve("c")
    assert ok and abs(limit - 0.7) < 1e-9
    g.finish("c", "openrouter", 0.75)  # a run overshot: 1.05 spent
    ok, reason, _ = g.reserve("c")
    assert not ok and "cap reached" in reason


def test_guard_refuses_when_the_next_run_would_probably_pass_the_cap():
    g = SpendGuard(cap_usd=1.0)
    g.reserve("c")
    g.finish("c", "openrouter", 0.45)
    g.reserve("c")
    g.finish("c", "openrouter", 0.45)  # 0.90 spent, biggest run 0.45
    ok, reason, _ = g.reserve("c")
    assert not ok and "could pass" in reason


def test_guard_counts_runs_in_flight_against_the_cap():
    g = SpendGuard(cap_usd=1.0, max_run_cost={"c": 0.4})
    assert g.reserve("c")[0]
    assert g.reserve("c")[0]
    ok, _, _ = g.reserve("c")  # two in flight (0.8) + 0.4 projected > 1.0
    assert not ok


def test_metered_run_without_a_reported_cost_trips_the_guard():
    g = SpendGuard(cap_usd=100.0)
    g.reserve("c")
    g.finish("c", "openrouter", None)
    ok, reason, _ = g.reserve("c")
    assert not ok and "no cost" in reason


def test_unmetered_kinds_may_report_no_cost():
    g = SpendGuard(cap_usd=1.0)
    g.reserve("c")
    g.finish("c", "ollama", None)
    assert g.reserve("c")[0]


def test_guard_starts_from_what_the_results_file_already_spent(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"candidate": "c", "cost_usd": 0.7},
                {"candidate": "c", "cost_usd": 0.4},
                {"candidate": "c", "cost_usd": None},
            ]
        )
    )
    g = SpendGuard.from_results(1.0, p)
    assert round(g.spent_usd, 6) == 1.1
    assert not g.reserve("c")[0]


def _line(case, lens, cand, idx, **extra):
    return json.dumps(
        {"case_id": case, "lens": lens, "candidate": cand, "run_index": idx, **extra}
    )


def test_resume_skips_finished_runs_and_retries_incomplete_ones(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        "\n".join(
            [
                _line("a", "qa", "c", 0, verdict_valid=True),
                _line(
                    "a", "qa", "c", 1, timeout=True, verdict_valid=False
                ),  # a timeout is data, not a retry
                _line(
                    "a",
                    "qa",
                    "c",
                    2,
                    incomplete=True,
                    incomplete_reason="sandbox refused",
                ),
            ]
        )
    )
    done = load_done(p)
    assert ("a", "qa", "c", 0) in done and ("a", "qa", "c", 1) in done
    assert ("a", "qa", "c", 2) not in done
    assert run_key(
        {"case_id": "a", "lens": "qa", "candidate": "c", "run_index": 0}
    ) == ("a", "qa", "c", 0)


def test_resume_with_no_results_file_does_nothing(tmp_path):
    assert load_done(tmp_path / "missing.jsonl") == set()
