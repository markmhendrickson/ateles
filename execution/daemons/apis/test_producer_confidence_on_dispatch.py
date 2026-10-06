"""A task nobody scored gets a real producer score before the gate (ateles#1142).

Before this change the gate read a mechanical estimate for almost every
dispatched task (0.76 for an owned, recognised task with no linked context, under
the 0.85 threshold), so the task was checkpointed with the reason "not scored by
a producer — score is a mechanical estimate". ~565 checkpoints were pending on
that reason alone.

These tests drive the REAL `apis.dispatch_task` — the gate, `_resolve_confidence`,
`evaluate_gate` and the producer scorer are all the production code. Only the
edges are stubbed: Neotoma writes, the agent spawn, and the model call itself
(the scorer's injected runner), because there is no model to call in a test.

What the headline tests looked like RED (apis.py without the producer call):

    test_unscored_low_blast_task_is_scored_then_gated_on_that_score_high
        AssertionError: the producer was never asked
    test_unscored_low_blast_task_is_scored_then_gated_on_that_score_low
        AssertionError: assert 0.76 == 0.4   (the gate decided on the mechanical
        estimate: reason='not scored by a producer — score is a mechanical
        estimate', confidence_unscored=True, confidence_source='')

The failure-path and high-blast tests pass both before and after: they pin the
behaviour that must NOT change.

Run: pytest execution/daemons/apis/test_producer_confidence_on_dispatch.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import model_tiering  # noqa: E402
import producer_confidence  # noqa: E402
from lib.daemon_runtime.gating import BlastRadius, ExecutionPolicy  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402

MECHANICAL = 0.76  # owner + recognised action, no context, no recurrences
BINDING = {"claude": {"local": "m-local", "mechanical": "m-mech", "mid": "m-mid", "top": "m-top"}}


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


class _SpawnResult:
    ok = True
    pr_url = None
    detail = "stubbed"


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Stub every edge dispatch_task touches; return what it recorded."""
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)

    rec = {"briefs": [], "spawns": [], "statuses": [], "runner_calls": []}

    def _set_status(entity_id, status, **kw):
        rec["statuses"].append((entity_id, status, kw.get("reason")))
        return True

    monkeypatch.setattr(apis, "set_task_status", _set_status)
    monkeypatch.setattr(apis, "fetch_task_record", lambda _id: {"entity_id": _id})
    monkeypatch.setattr(apis, "fetch_entity_user_id", lambda _id: "usr_test")
    monkeypatch.setattr(
        apis,
        "_record_snapshot",
        lambda record: {
            "status": "awaiting_approval",
            "blocked_reason": next(
                (r for _i, s, r in reversed(rec["statuses"]) if r), None
            ),
        },
    )
    monkeypatch.setattr(
        apis,
        "write_checkpoint_brief",
        lambda **kw: rec["briefs"].append(kw) or "ent_brief",
    )

    async def _spawn(skill, entity_id, *a, **kw):
        rec["spawns"].append((skill, entity_id))
        return _SpawnResult()

    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn)
    # The in-process fallback policy: the same thresholds and blast sets the
    # gate uses when Neotoma is unreachable.
    monkeypatch.setattr(
        apis,
        "resolve_policy_for_agent",
        lambda _skill: ExecutionPolicy(entity_id="fallback", loaded=False),
    )
    # Tiering config the scorer reads: nothing mapped (so the class default
    # applies) and a vendor binding present.
    monkeypatch.setattr(model_tiering, "configured_action_policy", lambda: {})
    monkeypatch.setattr(model_tiering, "configured_vendor_binding", lambda: BINDING)
    return rec


def _runner(world, reply: str | Exception, ok: bool = True):
    async def run(prompt: str):
        world["runner_calls"].append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return ok, reply, "claude"

    return run


@pytest.fixture
def scorer(monkeypatch, world):
    """Install a model reply as the scorer's runner."""

    def install(reply, ok=True):
        monkeypatch.setattr(producer_confidence, "_default_runner", _runner(world, reply, ok))

    return install


def _dispatch(snapshot: dict, entity_id: str = "ent_task_1"):
    asyncio.run(
        apis.dispatch_task(
            entity_id, snapshot, trigger="created", notifier=_Notifier(),
            snapshot_hydrated=True,
        )
    )


def _task(**over) -> dict:
    task = {
        "title": "Tidy the notes index",
        "body": "Re-sort the local notes index alphabetically. Local file only.",
        "assigned_to": "cicada",
        "action_type": "local_edit",  # LOW blast under the fallback policy
        "status": "pending",
    }
    task.update(over)
    return task


def _only_brief(world) -> dict:
    assert len(world["briefs"]) == 1, f"expected one checkpoint brief, got {world['briefs']}"
    return world["briefs"][0]


# ── 1. an unscored low-blast task is scored, then gated on that score ────────


def test_unscored_low_blast_task_is_scored_then_gated_on_that_score_high(world, scorer):
    scorer('{"confidence": 0.93, "rationale": "well specified, local only"}')
    _dispatch(_task())
    assert world["runner_calls"], "the producer was never asked"
    assert world["briefs"] == [], (
        "held for the operator; the gate decided on the mechanical estimate: "
        f"reason={world['statuses'][-1][2]!r}"
    )
    assert world["spawns"] == [("cicada", "ent_task_1")]


def test_unscored_low_blast_task_is_scored_then_gated_on_that_score_low(world, scorer):
    scorer('{"confidence": 0.40, "rationale": "ambiguous target"}')
    _dispatch(_task())
    brief = _only_brief(world)
    decision = brief["decision"]
    assert world["spawns"] == []
    assert decision.confidence == 0.4
    # A producer judged it low: the honest reason, not "never scored".
    assert decision.confidence_unscored is False
    assert decision.reason == "below confidence threshold"
    assert decision.blast_radius == BlastRadius.LOW


def test_producer_score_is_recorded_as_such(world, scorer):
    scorer('{"confidence": 0.40}')
    _dispatch(_task())
    brief = _only_brief(world)
    assert brief["decision"].confidence_source == producer_confidence.PRODUCER_SOURCE
    assert producer_confidence.PRODUCER_SOURCE in brief["plan_summary"]


def test_the_scorer_runs_on_the_cheap_tier_never_top(world, scorer):
    resolved = model_tiering.resolve_tier(model_tiering.ACTION_CONFIDENCE_SCORING)
    assert resolved.tier in producer_confidence.ALLOWED_TIERS
    assert resolved.tier != "top"


# ── 2. every scorer failure leaves the task checkpointed, as today ───────────


@pytest.mark.parametrize(
    "reply",
    [
        "I think this is fine",                  # no JSON
        '{"confidence": 1.5}',                   # out of range: rejected, not clamped
        '{"confidence": -0.1}',
        '{"confidence": "0.99"}',                # string, not a number
        '{"confidence": true}',                  # bool is not a score
        '{"confidence": NaN}',                   # non-finite
        '{"rationale": "no number"}',
        "",
    ],
)
def test_unusable_reply_never_auto_executes(world, scorer, reply):
    scorer(reply)
    _dispatch(_task())
    _assert_checkpointed_as_unscored(world)


def test_scorer_not_ok_means_checkpointed(world, scorer):
    # What run_skill returns when the usage/pace gate refuses: not ok.
    scorer('{"confidence": 0.99}', ok=False)
    _dispatch(_task())
    _assert_checkpointed_as_unscored(world)


def test_scorer_crash_means_checkpointed(world, scorer):
    scorer(RuntimeError("harness exploded"))
    _dispatch(_task())
    _assert_checkpointed_as_unscored(world)


def test_scorer_timeout_means_checkpointed(world, monkeypatch):
    async def wedged(prompt):
        world["runner_calls"].append(prompt)
        await asyncio.sleep(30)
        return True, '{"confidence": 0.99}', "claude"

    monkeypatch.setattr(producer_confidence, "_default_runner", wedged)
    monkeypatch.setattr(producer_confidence, "SCORER_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(producer_confidence, "TIMEOUT_GRACE_SECONDS", 0.05)
    _dispatch(_task())
    _assert_checkpointed_as_unscored(world)


def test_usage_gate_refusal_through_the_real_runner_means_checkpointed(world, monkeypatch):
    """The default runner, not a stub: run_skill refusing (pace gate) is a
    not-ok SkillResult, and the task stays unscored."""
    import skill_runner

    async def refused(skill, prompt, **kw):
        world["runner_calls"].append(prompt)
        assert kw["action_class"] == model_tiering.ACTION_CONFIDENCE_SCORING
        assert kw["local_review"] is True  # inference-only authority
        return skill_runner.SkillResult(
            skill, False, None, "", "", error="usage gate refused",
            cooled_until="2026-10-06T20:00:00+02:00",
        )

    monkeypatch.setattr(skill_runner, "run_skill", refused)
    _dispatch(_task())
    assert world["runner_calls"], "the default runner was never reached"
    _assert_checkpointed_as_unscored(world)


def _assert_checkpointed_as_unscored(world):
    brief = _only_brief(world)["decision"]
    assert world["spawns"] == [], "a failed scorer must never auto-execute"
    assert brief.confidence_unscored is True
    assert brief.confidence == MECHANICAL
    assert brief.confidence_source == ""
    assert "mechanical estimate" in brief.reason


# ── 3. where the scorer must not run, nothing changes ────────────────────────


@pytest.mark.parametrize(
    "action_type, assigned_to, expected_reason",
    [
        # high blast: gated on blast radius, reason text unchanged
        ("open_or_merge_pr", "cicada", "propose alternatives"),
        ("payment", "monedula", "propose alternatives"),
        # never-tier and unrecognised (resolves to NEVER, not LOW)
        ("operator_only", "cicada", "operator-only"),
        ("a_brand_new_action_nobody_classified", "cicada", "operator-only"),
    ],
)
def test_high_blast_and_unrecognised_actions_are_not_scored_and_stay_gated(
    world, scorer, action_type, assigned_to, expected_reason
):
    scorer('{"confidence": 0.99}')
    _dispatch(_task(action_type=action_type, assigned_to=assigned_to))
    assert world["runner_calls"] == [], "a high-blast task must not spend a scoring call"
    brief = _only_brief(world)["decision"]
    assert world["spawns"] == []
    assert expected_reason in brief.reason
    assert brief.confidence_source == ""
    if brief.blast_radius == BlastRadius.HIGH:
        assert brief.confidence == MECHANICAL
        assert brief.confidence_unscored is True


def test_a_task_that_already_carries_a_score_is_not_rescored(world, scorer):
    scorer('{"confidence": 0.10}')
    _dispatch(_task(confidence=0.95))
    assert world["runner_calls"] == []
    assert world["spawns"] == [("cicada", "ent_task_1")]


def test_a_gate_override_redispatch_is_not_scored(world, scorer, monkeypatch):
    scorer('{"confidence": 0.10}')
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *a, **k: True)
    asyncio.run(
        apis.dispatch_task(
            "ent_task_1", _task(), trigger="approved", notifier=_Notifier(),
            snapshot_hydrated=True, gate_override=True,
        )
    )
    assert world["runner_calls"] == []


# ── 4. tier guard: the scorer is never run on the top tier ───────────────────


def test_policy_that_maps_the_class_to_top_switches_the_scorer_off(world, scorer, monkeypatch):
    scorer('{"confidence": 0.99}')
    monkeypatch.setattr(
        model_tiering, "configured_action_policy",
        lambda: {model_tiering.ACTION_CONFIDENCE_SCORING: "top"},
    )
    _dispatch(_task())
    assert world["runner_calls"] == []
    _assert_checkpointed_as_unscored(world)


def test_no_vendor_binding_switches_the_scorer_off(world, scorer, monkeypatch):
    """No binding means the provider's ambient model — possibly the top one."""
    scorer('{"confidence": 0.99}')
    monkeypatch.setattr(model_tiering, "configured_vendor_binding", lambda: {})
    _dispatch(_task())
    assert world["runner_calls"] == []
    _assert_checkpointed_as_unscored(world)


def test_kill_switch(world, scorer, monkeypatch):
    scorer('{"confidence": 0.99}')
    monkeypatch.setenv("APIS_PRODUCER_SCORER", "0")
    _dispatch(_task())
    assert world["runner_calls"] == []
    _assert_checkpointed_as_unscored(world)


@pytest.mark.parametrize("tier", ["local", "mechanical", "mid"])
def test_policy_may_choose_any_cheap_tier(world, scorer, monkeypatch, tier):
    scorer('{"confidence": 0.93}')
    monkeypatch.setattr(
        model_tiering, "configured_action_policy",
        lambda: {model_tiering.ACTION_CONFIDENCE_SCORING: tier},
    )
    _dispatch(_task())
    assert world["runner_calls"]
    assert world["spawns"]


def test_other_unmapped_classes_still_fail_up_to_top():
    """The class default is not a general "unmapped means cheap" rule."""
    resolved = model_tiering.resolve_tier("a_class_nobody_wrote_down", policy={})
    assert (resolved.tier, resolved.source) == ("top", "unresolved_class")


# ── 5. the score is bounded by the rubric's own floors ───────────────────────


def test_a_producer_cannot_score_past_a_hard_floor(world, monkeypatch):
    monkeypatch.setattr(model_tiering, "configured_action_policy", lambda: {})
    monkeypatch.setattr(model_tiering, "configured_vendor_binding", lambda: BINDING)

    async def run(prompt):
        return True, '{"confidence": 0.99, "rationale": "trust me"}', "claude"

    produced = asyncio.run(
        producer_confidence.score_unscored_task(
            _task(),
            action_type="local_edit",
            policy=ExecutionPolicy(entity_id="fallback", loaded=False),
            mechanical_value=0.4,
            runner=run,
        )
    )
    assert produced is not None and produced.value == 0.4


# ── 6. the reply parser ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"confidence": 0.5}', 0.5),
        ('{"confidence": 0}', 0.0),
        ('{"confidence": 1}', 1.0),
        ('Sure!\n{"confidence": 0.7, "rationale": "ok"}\n', 0.7),
        ('{"confidence": 0.2} then {"confidence": 0.9}', 0.9),  # last wins
        ('{"result": "{\\"confidence\\": 0.6}"}', 0.6),         # harness envelope
    ],
)
def test_parse_reply_accepts_clean_scores(text, expected):
    parsed = producer_confidence.parse_reply(text)
    assert parsed is not None and parsed[0] == expected


@pytest.mark.parametrize(
    "text",
    ['{"confidence": 1.0001}', '{"confidence": "1"}', '{"confidence": null}',
     '{"confidence": [0.5]}', "confidence: 0.9", "{", None],
)
def test_parse_reply_rejects_everything_else(text):
    assert producer_confidence.parse_reply(text) is None
