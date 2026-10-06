"""The durable record of a producer score that authorises a run (ateles#1142).

`write_producer_assessment` returns an id ONLY when the stored record reads back
with the exact summary written (which carries the unrounded value) and the task
link. Anything else is None, and the dispatcher must then not auto-execute.
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime import gating

KW = dict(
    task_entity_id="ent_task",
    value=0.8501,
    threshold=0.85,
    source="apis_producer_scorer",
    action_type="local_edit",
    policy_id="pol_1",
    tier="mid",
    model="m-mid",
    run_id="run123",
    rationale="well specified",
    gate_action="auto_execute",
    handler="apis",
)


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def neotoma(monkeypatch):
    state = {"posted": [], "stored": None, "post_error": None, "mutate": None}
    monkeypatch.setattr(gating, "NEOTOMA_BEARER_TOKEN", "t")
    monkeypatch.setattr(gating, "NEOTOMA_BASE_URL", "http://neotoma.invalid")

    def post(url, headers=None, json=None, timeout=None, **kw):
        if state["post_error"]:
            raise state["post_error"]
        state["posted"].append(json)
        state["stored"] = dict(json["entities"][0])
        return _Resp({"entities": [{"entity_id": "ent_ev"}]})

    def get(url, headers=None, timeout=None, **kw):
        snap = dict(state["stored"] or {})
        if state["mutate"]:
            snap = state["mutate"](snap)
        return _Resp({"snapshot": snap})

    monkeypatch.setattr(gating.httpx, "post", post)
    monkeypatch.setattr(gating.httpx, "get", get)
    return state


def test_a_proven_record_returns_its_id_and_carries_the_exact_value(neotoma):
    assert gating.write_producer_assessment(**KW) == "ent_ev"
    body = neotoma["posted"][0]
    entity = body["entities"][0]
    assert entity["entity_type"] == "harness_event"
    assert entity["event_type"] == gating.PRODUCER_ASSESSMENT_EVENT_TYPE
    assert "producer_confidence=0.8501 " in entity["output_summary"]
    assert "threshold=0.85" in entity["output_summary"]
    assert "source=apis_producer_scorer" in entity["output_summary"]
    assert "model=m-mid" in entity["output_summary"] and "run=run123" in entity["output_summary"]
    assert entity["confidence"] == 0.8501
    assert entity["rationale"] == "well specified"
    assert body["relationships"] == [
        {"relationship_type": "REFERS_TO", "source_index": 0, "target_entity_id": "ent_task"}
    ]


def test_the_summary_distinguishes_a_value_just_below_a_threshold():
    below = gating.producer_assessment_summary(
        value=0.8499, threshold=0.85, source="s", action_type="a", policy_id="p",
        tier="mid", model="m", run_id="r",
    )
    assert "producer_confidence=0.8499 " in below


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: {**s, "output_summary": "producer_confidence=0.85 altered"},
        lambda s: {**s, "task_entity_id": "ent_other"},
        lambda s: {k: v for k, v in s.items() if k != "output_summary"},
        lambda s: {**s, "event_type": "subprocess"},
        lambda s: {k: v for k, v in s.items() if k != "input_summary"},  # explanation missing
        lambda s: {**s, "input_summary": "rationale: something else"},   # explanation altered
        lambda s: {**s, "input_summary": ""},
        lambda s: {},
    ],
)
def test_a_record_that_does_not_read_back_as_written_is_not_proof(neotoma, mutate):
    neotoma["mutate"] = mutate
    assert gating.write_producer_assessment(**KW) is None


def test_a_failed_store_is_not_recorded(neotoma):
    neotoma["post_error"] = RuntimeError("down")
    assert gating.write_producer_assessment(**KW) is None


def test_no_token_or_no_task_is_not_recorded(neotoma, monkeypatch):
    monkeypatch.setattr(gating, "NEOTOMA_BEARER_TOKEN", "")
    assert gating.write_producer_assessment(**KW) is None
    monkeypatch.setattr(gating, "NEOTOMA_BEARER_TOKEN", "t")
    assert gating.write_producer_assessment(**{**KW, "task_entity_id": ""}) is None


def test_a_capped_score_records_both_numbers(neotoma):
    assert gating.write_producer_assessment(**{**KW, "value": 0.5, "capped_from": 0.99}) == "ent_ev"
    summary = neotoma["posted"][0]["entities"][0]["output_summary"]
    assert "producer_confidence=0.5 " in summary and "capped_from=0.99" in summary


def test_a_record_with_its_explanation_intact_is_proven(neotoma):
    assert gating.write_producer_assessment(**KW) == "ent_ev"
    assert gating.write_producer_assessment(**{**KW, "rationale": ""}) == "ent_ev"
    assert neotoma["stored"]["input_summary"] == "rationale: none supplied"
