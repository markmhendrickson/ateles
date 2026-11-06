"""The dispatcher stops manufacturing hold noise: two guards that only reduce.

Gate log, 22 Oct to 5 Nov 2026: 2,030 gate decisions for 974 distinct tasks.

  A. ALREADY HELD IS NOT RE-GATED. About half were the same task gated again:
     Neotoma redelivers `task.created` and `dispatch_task` had a terminal guard
     but none for a task already at AWAITING_APPROVAL, so it re-routed, re-held,
     made the previous signed envelope stale and filed another brief.

  B. A GUESSED OWNER IS NOT AN OWNER. About 54% of first decisions were tasks
     with no assignee and no stored routable tag, owned only by
     `_infer_tags_from_text` — then held or run on that guess. Such a task is
     now left as filed, unless the action the gate would judge is operator-only,
     never-tier, or an effect action, which keep today's hold.

These tests drive the real `apis.dispatch_task` with the shared stubbed world
from `test_producer_confidence_on_dispatch` (Neotoma writes, spawn and scorer
stubbed). Synthetic data only.

Run: pytest execution/daemons/apis/test_dispatch_hold_noise.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import test_producer_confidence_on_dispatch as _w  # noqa: E402
from lib.daemon_runtime.gating import ExecutionPolicy  # noqa: E402
from lib.daemon_runtime.task_lifecycle import TaskStatus  # noqa: E402

world = _w.world  # the stubbed dispatch world (fixture)


class _Notifier(_w._Notifier):
    pass


@pytest.fixture(autouse=True)
def _released(monkeypatch):
    """An approved release reads back as proven (its Neotoma read is stubbed)."""
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_a, **_k: True)


def _run(snapshot, *, trigger="created", override=False, hydrated=True, eid="ent_t1"):
    notifier = _Notifier()
    asyncio.run(
        apis.dispatch_task(
            eid,
            snapshot,
            trigger=trigger,
            notifier=notifier,
            gate_override=override,
            snapshot_hydrated=hydrated,
        )
    )
    return notifier


def _nothing_happened(world, notifier):
    assert world["statuses"] == [], f"status writes: {world['statuses']}"
    assert world["briefs"] == [], "a checkpoint brief was filed"
    assert world["spawns"] == [], "an agent was spawned"
    assert notifier.sent == [], f"notified: {notifier.sent}"


# A task whose words alone infer a routable skill ("finance_analysis" ->
# fringilla, a LOW-blast compute-only analysis); no assignee, no stored tags.
def _guessed(**over):
    task = {
        "title": "Reconcile the payments ledger",
        "body": "payments wage",
        "status": "pending",
        "confidence": 0.95,
    }
    task.update(over)
    return task


def _owned(**over):
    task = {"assigned_to": "cicada", "action_type": "local_edit"}
    task.update(over)
    return _guessed(**task)


# ── A. already held ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", ["awaiting_approval", "AWAITING_APPROVAL"])
def test_held_task_created_redelivery_writes_nothing(world, spelling):
    notifier = _run(_owned(status=spelling))
    _nothing_happened(world, notifier)


@pytest.mark.parametrize("trigger", ["created", "due_today", "watchdog_retry"])
def test_held_task_is_not_regated_on_any_non_override_trigger(world, trigger):
    notifier = _run(_owned(status="awaiting_approval"), trigger=trigger)
    _nothing_happened(world, notifier)


def test_held_task_with_gate_override_still_releases(world):
    _run(_owned(status="awaiting_approval"), trigger="approved", override=True)
    assert ("ent_t1", TaskStatus.ROUTED, "") in world["statuses"], (
        f"release did not route: {world['statuses']}"
    )
    assert world["spawns"] == [("cicada", "ent_t1")]


def test_pending_owned_task_is_still_gated_as_today(world):
    # Control: a not-yet-held task that the gate holds is still held and briefed.
    _run(_owned(status="pending", action_type="payment"))
    assert [s for _i, s, _r in world["statuses"]] == [
        TaskStatus.ROUTED,
        TaskStatus.AWAITING_APPROVAL,
    ]
    assert len(world["briefs"]) == 1


@pytest.mark.parametrize("status", ["done", "completed"])
def test_terminal_guard_unchanged(world, status):
    notifier = _run(_owned(status=status))
    _nothing_happened(world, notifier)


# ── B. a guessed owner is not an owner ───────────────────────────────────────


def test_guessed_owner_low_blast_is_left_as_filed(world, caplog):
    caplog.set_level("INFO")
    notifier = _run(_guessed())
    _nothing_happened(world, notifier)
    assert "left as filed" in caplog.text


def test_guessed_owner_left_as_filed_also_on_redelivery_spelling_open(world):
    notifier = _run(_guessed(status="open"))
    _nothing_happened(world, notifier)


def test_guessed_owner_left_as_filed_even_when_unscored(world):
    snap = _guessed()
    snap.pop("confidence")
    notifier = _run(snap)
    _nothing_happened(world, notifier)


# The gate's own vocabulary, read from the policy source the dispatch world
# installs rather than copied here, so a new high-blast action is covered the
# moment the policy gains it.
_POLICY = ExecutionPolicy(entity_id="fallback", loaded=False)
_HIGH_BLAST = sorted(_POLICY.high_blast_action_types)
_RESERVED = ["operator_only", "not_a_known_action", "correspondence"]


def test_high_blast_set_is_not_empty():
    # Instrument check: the parametrization below must actually cover something.
    assert {"payment", "wage", "git_push", "open_or_merge_pr"} <= set(_HIGH_BLAST)


@pytest.mark.parametrize("action", _HIGH_BLAST + _RESERVED)
def test_guessed_owner_with_any_non_low_action_still_reaches_the_gate(world, action):
    _run(_guessed(action_type=action))
    assert [s for _i, s, _r in world["statuses"]][-1] == TaskStatus.AWAITING_APPROVAL
    assert len(world["briefs"]) == 1
    assert world["spawns"] == []


@pytest.mark.parametrize("action", sorted(_POLICY.low_blast_action_types))
def test_guessed_owner_with_any_low_action_is_left_as_filed(world, action):
    notifier = _run(_guessed(action_type=action))
    _nothing_happened(world, notifier)


def test_guard_fails_closed_on_policy_read_failure(monkeypatch):
    def boom(_skill):
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(apis, "resolve_policy_for_agent", boom)
    assert apis._guessed_owner_is_low_stakes("fringilla", {}) is False


def test_guard_fails_closed_on_empty_action(monkeypatch):
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _s: _POLICY)

    class _Empty:
        value = ""

    monkeypatch.setattr(apis, "_resolve_gate_action_type", lambda *_a: _Empty())
    assert apis._guessed_owner_is_low_stakes("fringilla", {}) is False


def test_guard_fails_closed_on_resolver_exception(monkeypatch):
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _s: _POLICY)

    def boom(*_a):
        raise ValueError("bad snapshot")

    monkeypatch.setattr(apis, "_resolve_gate_action_type", boom)
    assert apis._guessed_owner_is_low_stakes("fringilla", {}) is False


@pytest.mark.parametrize("status", ["bogus_status", "approved", "ready", "blocked"])
def test_unknown_or_other_status_is_not_left_as_filed(world, status):
    # Allow-list of not-started statuses: anything else keeps its normal path.
    _run(_guessed(status=status))
    assert world["statuses"], "must reach routing/gating, not be left as filed"


def test_guessed_owner_with_effect_action_inferred_from_text_still_holds(world):
    # No declared action_type: the agent prior for the guessed skill is the
    # effect action `payment`.
    _run(
        {
            "title": "Pay the vendor invoice",
            "body": "payment transfer",
            "status": "pending",
        }
    )
    assert len(world["briefs"]) == 1


def test_named_assignee_is_gated_exactly_as_today(world):
    # Would be left as filed if assigned_to were treated as a guess.
    _run(_guessed(assigned_to="fringilla", action_type="compute_only_analysis"))
    statuses = [s for _i, s, _r in world["statuses"]]
    assert statuses[0] == TaskStatus.ROUTED


def test_sentinel_assignee_reads_as_absence(world):
    notifier = _run(_guessed(assigned_to="unassigned"))
    _nothing_happened(world, notifier)


def test_stored_routable_tag_is_gated_as_today(world):
    _run(_guessed(tags=["finance_analysis"], action_type="compute_only_analysis"))
    assert world["statuses"] and world["statuses"][0][1] == TaskStatus.ROUTED


def test_stored_tags_that_do_not_route_do_not_make_it_owned(world):
    notifier = _run(_guessed(tags=["bookkeeping", "ateles-1"]))
    _nothing_happened(world, notifier)


def test_unhydrated_snapshot_keeps_its_deferral(world):
    # Empty, unread snapshot: no owner can be inferred, nothing is escalated or
    # written, exactly as before (B must not swallow it or change the path).
    notifier = _run({}, hydrated=False)
    assert world["statuses"] == [] and world["briefs"] == []
    assert world["spawns"] == []
    assert notifier.sent == []


def test_unhydrated_guessable_snapshot_is_not_left_as_filed(world, caplog):
    caplog.set_level("INFO")
    _run(_guessed(), hydrated=False)
    assert "left as filed" not in caplog.text


@pytest.mark.parametrize("status", ["routed", "failed", "executing"])
def test_started_task_is_not_left_as_filed(world, status):
    # Dispatched on purpose (watchdog retry): B never applies.
    _run(_guessed(status=status), trigger="watchdog_retry")
    assert world["statuses"], "a started task must keep its existing path"


def test_correction_and_build_released_are_not_left_as_filed(world):
    _run(_guessed(task_type="existing_pr_correction"))
    assert world["statuses"], "correction lifecycle must keep its existing path"


def test_build_released_redispatch_keeps_existing_path(world):
    _run(_guessed(), trigger="build_released")
    assert world["statuses"], "deferred-task recovery must keep its existing path"


# ── B exception: a guessed builder's own default is not a reason to hold ─────


def _guessed_builder(**over):
    """Words alone route this to the builder; nothing is declared."""
    task = {
        "title": "Review the engineering work on the dispatcher",
        "body": "code change to the apis daemon",
        "status": "pending",
        "confidence": 0.95,
    }
    task.update(over)
    return task


def test_guessed_builder_default_action_is_left_as_filed(world, caplog):
    caplog.set_level("INFO")
    notifier = _run(_guessed_builder())
    _nothing_happened(world, notifier)
    assert "left as filed" in caplog.text


def test_declared_builder_action_still_holds(world):
    _run(_guessed_builder(action_type="open_or_merge_pr"))
    assert [s for _i, s, _r in world["statuses"]][-1] == TaskStatus.AWAITING_APPROVAL
    assert len(world["briefs"]) == 1


@pytest.mark.parametrize(
    "words",
    [
        "pay the vendor invoice by bank transfer",
        "send an email to the customer",
        "publish the release notes on the public blog",
    ],
)
def test_builder_task_whose_text_names_an_outward_action_still_holds(world, words):
    _run(_guessed_builder(body=f"code change to the apis daemon, then {words}"))
    assert len(world["briefs"]) == 1, world["statuses"]
    assert world["spawns"] == []


def test_exception_is_keyed_to_the_builder_by_source_not_value(monkeypatch):
    # Same value and prior, but the resolver says it did not come from the prior.
    from lib.daemon_runtime.action_type import ResolvedActionType

    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _s: _POLICY)
    for source in ("content", "declared_escalated", "declared", "none"):
        monkeypatch.setattr(
            apis,
            "_resolve_gate_action_type",
            lambda *_a, source=source: ResolvedActionType("open_or_merge_pr", source),
        )
        assert apis._guessed_owner_is_low_stakes("cicada", {}) is False, source
    monkeypatch.setattr(
        apis,
        "_resolve_gate_action_type",
        lambda *_a: ResolvedActionType("open_or_merge_pr", "agent_prior"),
    )
    assert apis._guessed_owner_is_low_stakes("cicada", {}) is True


_OTHER_PRIORS = sorted(
    (agent, action)
    for agent, action in apis._AGENT_ACTION_TYPE.items()
    if agent not in apis.build_delivery.BUILDER_SKILLS
    and _POLICY.blast_radius_for(action).value != "low"
)


def test_other_agent_priors_set_is_not_empty():
    assert {"monedula", "corvus", "struthio"} <= {a for a, _ in _OTHER_PRIORS}


@pytest.mark.parametrize("agent,action", _OTHER_PRIORS)
def test_every_other_agents_high_or_never_prior_still_holds(monkeypatch, agent, action):
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _s: _POLICY)
    # No declaration, no text: the resolved action is that agent's own prior.
    assert apis._guessed_owner_is_low_stakes(agent, {}) is False


# ── B, for a task assigned to the dispatcher ─────────────────────────────────
# The dispatcher's own name names no owner: it is what is asking who owns the
# task. So the words alone are still the only route, and the guard must read
# such a task exactly as it reads an unassigned one.


@pytest.mark.parametrize(
    "spelling", ["apis", "Apis", " APIS ", "apis@ateles-swarm", "Apis@ateles"]
)
def test_dispatcher_assigned_guessed_owner_is_left_as_filed(world, spelling):
    notifier = _run(_guessed(assigned_to=spelling))
    _nothing_happened(world, notifier)


def test_dispatcher_assigned_guessed_owner_with_high_action_still_reaches_the_gate(world):
    # The guard only reduces: the same action holds an unassigned task and this.
    _run(_guessed(assigned_to="apis", action_type="payment"))
    assert [s for _i, s, _r in world["statuses"]] == [
        TaskStatus.ROUTED,
        TaskStatus.AWAITING_APPROVAL,
    ]
    assert len(world["briefs"]) == 1


def test_dispatcher_assigned_with_a_stored_routable_tag_is_routed(world):
    # A stored tag is a real route, so the assignee being the dispatcher does
    # not make it a guess.
    _run(_guessed(assigned_to="apis", tags=["finance_analysis"]))
    assert world["statuses"], "a task with a stored route was left as filed"


# ── The readings after a hold name the skill dispatch_task runs ──────────────
# An approved release goes back through `dispatch_task`, so the gate context
# that judges it must resolve the same skill, however the task got its route.


@pytest.mark.parametrize(
    ("task", "skill"),
    [
        ({"title": "Pay the rent invoice", "body": "wire the payment"}, "monedula"),
        (
            {
                "title": "Pay the rent invoice",
                "body": "wire the payment",
                "tags": ["bookkeeping", "ateles-682"],
            },
            "monedula",
        ),
        (
            {"title": "Pay the rent invoice", "description": "wire the payment"},
            "monedula",
        ),
        (
            {
                "title": "Pay the rent invoice",
                "body": "wire the payment",
                "assigned_to": "apis",
            },
            "monedula",
        ),
        (
            {
                "title": "Pay the rent invoice",
                "body": "wire the payment",
                "tags": ["engineering"],
            },
            "cicada",
        ),
        (
            {
                "title": "Pay the rent invoice",
                "body": "wire the payment",
                "tags": '["engineering"]',
            },
            "cicada",
        ),
        (
            {
                "title": "Pay the rent invoice",
                "body": "wire the payment",
                "assigned_to": "fringilla",
            },
            "fringilla",
        ),
    ],
    ids=[
        "words_only",
        "unmapped_stored_tags",
        "words_in_description",
        "assigned_to_the_dispatcher",
        "stored_tag_outranks_words",
        "stored_tag_as_a_json_string",
        "named_agent",
    ],
)
def test_gate_context_names_the_skill_dispatch_runs(world, task, skill):
    snapshot = {"status": "awaiting_approval", "confidence": 0.95, **task}
    assert apis._task_gate_context(snapshot)[0] == skill

    _run(snapshot, trigger="approved", override=True)

    assert world["spawns"] == [(skill, "ent_t1")]


@pytest.mark.parametrize("tags", ['"finance"', '{"finance": 1}', "not json", 7])
def test_tags_that_are_not_a_list_read_as_no_tags(world, tags):
    # They used to raise when the words were added, losing the dispatch event.
    snapshot = _guessed(tags=tags)
    assert apis._task_gate_context(snapshot)[0] == "fringilla"
    notifier = _run(snapshot)
    _nothing_happened(world, notifier)
