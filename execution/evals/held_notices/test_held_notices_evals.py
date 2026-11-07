"""Held-notices eval: a daemon's overnight notices reach the operator, never a burst of email.

Collected by the existing ``pytest execution/evals/`` step. Offline: only the
``gws`` subprocess and the clock are faked; the Notifier and the UserPromptSubmit
hook are the real code. See ``held_notices_harness.py`` and ``scenarios.json``.

Run: pytest execution/evals/held_notices/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import held_notices_harness as harness  # noqa: E402

SCENARIOS = harness.load()["scenarios"]
IDS = [s["id"] for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_scenario(scenario):
    obs = harness.run_scenario(scenario)
    assert harness.check(scenario, obs) == [], scenario["why"]


def test_scenario_ids_are_unique_and_documented():
    assert len(set(IDS)) == len(IDS)
    assert all(s.get("why") and s.get("expect") and s.get("red_under") for s in SCENARIOS)
    assert all(m in harness.MUTATIONS for s in SCENARIOS for m in s["red_under"])


def test_the_eval_covers_every_required_behaviour():
    required = {
        "backlog_from_a_daemon_without_email_is_never_emailed_by_another_daemon",
        "an_email_armed_backlog_of_55_sends_exactly_5_in_the_first_hour",
        "an_email_ineligible_hold_is_never_emailed",
        "hostile_notice_text_never_reaches_the_hook_line",
        "clear_removes_only_what_was_shown_and_the_hook_follows_the_contents",
    }
    assert required <= set(IDS)


# An eval whose checks cannot fail is not an eval: each planted regression
# must turn its scenarios red.
PLANTED = [(s, m) for s in SCENARIOS for m in s["red_under"]]


@pytest.mark.parametrize("scenario,mutation", PLANTED, ids=[f"{s['id']}-{m}" for s, m in PLANTED])
def test_checks_go_red_under_the_planted_regression(scenario, mutation):
    obs = harness.run_scenario(scenario, mutation=mutation)
    assert harness.check(scenario, obs), (
        f"{mutation} passed {scenario['id']}: the eval cannot tell the designs apart"
    )


def test_the_original_incident_is_reproduced_by_the_old_design():
    """One shared queue and no budget: 55 emails, one per notice."""
    scenario = SCENARIOS[0]
    obs = harness.run_scenario(scenario, mutation="shared_queue_no_budget")
    assert obs["emails"] == 55
