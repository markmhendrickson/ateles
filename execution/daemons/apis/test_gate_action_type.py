"""The gate reads what a task says it does; tiering and delivery do not (ateles#682).

Four things are pinned here, because each is a way this change could quietly do
harm:

  A. DECOUPLING. Model-tier selection and the build-delivery grant key on
     `apis._infer_action_type`, unchanged. `_GOLDEN` below was captured from
     origin/main BEFORE this change (resolver output, the action class derived
     from it, and `build_delivery.is_delivery_dispatch`), so "byte-for-byte
     unchanged" is asserted against recorded values, not against a function that
     could have moved with the code under test.

  B. NO LOOSENING. For every input in a cross-product corpus, the gate's blast
     verdict under the new resolver is the same or stricter than under the old
     one, under both a LOW-default and a HIGH-default policy, with ONE sanctioned
     exception that is enumerated and asserted exactly. The checker is itself
     tested against deliberately loosened resolvers ("planted red").

  C. CALIBRATION against tasks the gate really parked, with the control case
     that must keep gating.

  D. The operator sees the creator's `confidence_drivers` on the checkpoint
     brief and notification, or an explicit statement that none was supplied.

Run: pytest execution/daemons/apis/test_gate_action_type.py -v
"""

from __future__ import annotations

import inspect
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import build_delivery  # noqa: E402
from lib.daemon_runtime.action_type import (  # noqa: E402
    AGENT_ACTION_TYPE_PRIOR,
    infer_action_type_from_text,
)
from lib.daemon_runtime.gating import (  # noqa: E402
    BlastRadius,
    ExecutionPolicy,
    GateAction,
    evaluate_gate,
)
import test_producer_confidence_on_dispatch as _dispatch_world  # noqa: E402

# The dispatch harness (stubbed edges, real `dispatch_task`) and its fixtures are
# reused, not copied: one definition of "the world dispatch_task runs in".
_dispatch = _dispatch_world._dispatch
_only_brief = _dispatch_world._only_brief
_task = _dispatch_world._task
scorer = _dispatch_world.scorer
world = _dispatch_world.world

# ═══ A. Decoupling ═══════════════════════════════════════════════════════════

_SKILLS = ["cicada", "vanellus", "struthio", "monedula", "fringilla", "corvus",
           "Cicada", None, "", "apis"]
_SNAPSHOTS = {
    "empty": {},
    "declared_pr": {"action_type": "open_or_merge_pr"},
    "declared_upper_ws": {"action_type": "  Operator_Only "},
    "declared_typo": {"action_type": "open_pull_request"},
    "declared_low": {"action_type": "local_edit"},
    "report_only": {
        "title": "Add a read-only reconciler",
        "description": "Write NOTHING - this job's only output is a divergence report.",
    },
    "pr_text": {"title": "Implement the thing", "description": "PR #643 open."},
    "pay_text": {"title": "Pay the invoice", "description": "pay the invoice for rent"},
}

# (resolver output, action class tiering/delivery see, is_delivery_dispatch),
# recorded from origin/main at 77f1fc15 before this change.
_GOLDEN = {
    "''|declared_low": ('local_edit', 'local_edit', False),
    "''|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "''|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "''|declared_upper_ws": ('operator_only', 'operator_only', False),
    "''|empty": (None, 'task_dispatch', False),
    "''|pay_text": (None, 'task_dispatch', False),
    "''|pr_text": (None, 'task_dispatch', False),
    "''|report_only": (None, 'task_dispatch', False),
    "'Cicada'|declared_low": ('local_edit', 'local_edit', False),
    "'Cicada'|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'Cicada'|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "'Cicada'|declared_upper_ws": ('operator_only', 'operator_only', False),
    "'Cicada'|empty": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'Cicada'|pay_text": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'Cicada'|pr_text": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'Cicada'|report_only": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'apis'|declared_low": ('local_edit', 'local_edit', False),
    "'apis'|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'apis'|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "'apis'|declared_upper_ws": ('operator_only', 'operator_only', False),
    "'apis'|empty": (None, 'task_dispatch', False),
    "'apis'|pay_text": (None, 'task_dispatch', False),
    "'apis'|pr_text": (None, 'task_dispatch', False),
    "'apis'|report_only": (None, 'task_dispatch', False),
    "'cicada'|declared_low": ('local_edit', 'local_edit', False),
    "'cicada'|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'cicada'|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "'cicada'|declared_upper_ws": ('operator_only', 'operator_only', False),
    "'cicada'|empty": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'cicada'|pay_text": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'cicada'|pr_text": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'cicada'|report_only": ('open_or_merge_pr', 'open_or_merge_pr', True),
    "'corvus'|declared_low": ('local_edit', 'local_edit', False),
    "'corvus'|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'corvus'|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "'corvus'|declared_upper_ws": ('operator_only', 'operator_only', False),
    "'corvus'|empty": ('send_external_comms', 'send_external_comms', False),
    "'corvus'|pay_text": ('send_external_comms', 'send_external_comms', False),
    "'corvus'|pr_text": ('send_external_comms', 'send_external_comms', False),
    "'corvus'|report_only": ('send_external_comms', 'send_external_comms', False),
    "'fringilla'|declared_low": ('local_edit', 'local_edit', False),
    "'fringilla'|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'fringilla'|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "'fringilla'|declared_upper_ws": ('operator_only', 'operator_only', False),
    "'fringilla'|empty": ('compute_only_analysis', 'compute_only_analysis', False),
    "'fringilla'|pay_text": ('compute_only_analysis', 'compute_only_analysis', False),
    "'fringilla'|pr_text": ('compute_only_analysis', 'compute_only_analysis', False),
    "'fringilla'|report_only": ('compute_only_analysis', 'compute_only_analysis', False),
    "'monedula'|declared_low": ('local_edit', 'local_edit', False),
    "'monedula'|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'monedula'|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "'monedula'|declared_upper_ws": ('operator_only', 'operator_only', False),
    "'monedula'|empty": ('payment', 'payment', False),
    "'monedula'|pay_text": ('payment', 'payment', False),
    "'monedula'|pr_text": ('payment', 'payment', False),
    "'monedula'|report_only": ('payment', 'payment', False),
    "'struthio'|declared_low": ('local_edit', 'local_edit', False),
    "'struthio'|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'struthio'|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "'struthio'|declared_upper_ws": ('operator_only', 'operator_only', False),
    "'struthio'|empty": ('publish', 'publish', False),
    "'struthio'|pay_text": ('publish', 'publish', False),
    "'struthio'|pr_text": ('publish', 'publish', False),
    "'struthio'|report_only": ('publish', 'publish', False),
    "'vanellus'|declared_low": ('local_edit', 'local_edit', False),
    "'vanellus'|declared_pr": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'vanellus'|declared_typo": ('open_pull_request', 'open_pull_request', False),
    "'vanellus'|declared_upper_ws": ('operator_only', 'operator_only', False),
    "'vanellus'|empty": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'vanellus'|pay_text": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'vanellus'|pr_text": ('open_or_merge_pr', 'open_or_merge_pr', False),
    "'vanellus'|report_only": ('open_or_merge_pr', 'open_or_merge_pr', False),
    'None|declared_low': ('local_edit', 'local_edit', False),
    'None|declared_pr': ('open_or_merge_pr', 'open_or_merge_pr', False),
    'None|declared_typo': ('open_pull_request', 'open_pull_request', False),
    'None|declared_upper_ws': ('operator_only', 'operator_only', False),
    'None|empty': (None, 'task_dispatch', False),
    'None|pay_text': (None, 'task_dispatch', False),
    'None|pr_text': (None, 'task_dispatch', False),
    'None|report_only': (None, 'task_dispatch', False),
}


def _key(skill, name):
    return f"{skill!r}|{name}"


@pytest.mark.parametrize("skill", _SKILLS, ids=repr)
@pytest.mark.parametrize("name", sorted(_SNAPSHOTS))
def test_tiering_and_delivery_inputs_are_byte_for_byte_unchanged(skill, name):
    snapshot = _SNAPSHOTS[name]
    expected_type, expected_class, expected_delivery = _GOLDEN[_key(skill, name)]

    assert apis._infer_action_type(skill, snapshot) == expected_type
    action_class = apis._dispatch_action_class(skill, snapshot)
    assert action_class == expected_class
    assert build_delivery.is_delivery_dispatch(skill, action_class) is expected_delivery


def test_the_golden_table_covers_the_whole_corpus():
    assert set(_GOLDEN) == {_key(s, n) for s in _SKILLS for n in _SNAPSHOTS}


def test_a_report_only_cicada_task_keeps_the_delivery_class_but_gets_a_lower_gate_reading():
    """The seam, shown on one task: the gate lowers it, the dispatcher's class
    (tiering + delivery grant) does not move."""
    snap = _SNAPSHOTS["report_only"]
    assert apis._dispatch_action_class("cicada", snap) == "open_or_merge_pr"
    assert build_delivery.is_delivery_dispatch("cicada", "open_or_merge_pr")
    assert apis._resolve_gate_action_type("cicada", snap).value == "compute_only_analysis"


def test_the_dispatch_path_derives_its_class_from_the_old_resolver_only():
    src = inspect.getsource(apis._spawn_harness_skill)
    assert "_dispatch_action_class(" in src
    assert "resolve_gate_action_type" not in src
    assert "_infer_action_type(" in inspect.getsource(apis._dispatch_action_class)


def test_the_gate_paths_use_the_gate_resolver_and_not_the_old_one():
    for fn in (apis._task_gate_context, apis.dispatch_task):
        src = inspect.getsource(fn)
        assert "_resolve_gate_action_type(" in src, fn.__name__
        assert "_infer_action_type(" not in src, fn.__name__


def test_the_old_resolver_never_reads_the_tasks_text():
    assert apis._infer_action_type("cicada", _SNAPSHOTS["report_only"]) == "open_or_merge_pr"
    assert apis._infer_action_type(None, _SNAPSHOTS["pr_text"]) is None


# ═══ B. No loosening ═════════════════════════════════════════════════════════

_RANK = {BlastRadius.LOW: 0, BlastRadius.HIGH: 1, BlastRadius.NEVER: 2}

_TEXTS = [
    ("", ""),
    ("Plan the offsite", ""),
    ("Tidy the notes index", "Local file only."),
    ("Add a read-only reconciler", "Write NOTHING - this job's only output is a divergence report."),
    ("Extend the drift check", "PURE REPORTING. No behaviour change, nothing gated on the result."),
    ("Audit the queue", "Reporting only."),
    ("Audit", "The only output is a written report; also open a PR for it."),
    ("Write nothing, pure reporting", "Migrate the config and fix the importer."),
    ("Migrate swarm configuration", "PR #643 open."),
    ("Open a PR", ""),
    ("Pay the invoice", "pay the invoice for rent"),
    ("Send the email to the client", ""),
    ("Publish the post", "publish the post on the site"),
    ("Permanently delete the orphaned records", ""),
    ("Close the GitHub issue", "close the github issue after merge"),
    ("Pure reporting on PR #12", ""),
    ("Draft a reply", "draft a reply but do not send"),
    ("Make the endpoint read-only", ""),
    ("Analyse the logs", "Produce nothing else."),
]
_DECLARED = [None, "", "  ", "open_or_merge_pr", "local_edit", "compute_only_analysis",
             "operator_only", " Operator_Only ", "open_pull_request", "release", "payment"]


def _policies():
    low_default = ExecutionPolicy(entity_id="fallback", loaded=False)
    # The live policy's blast_radius_default was not readable when this was
    # written, so the property is checked under both defaults.
    high_default = ExecutionPolicy(
        entity_id="fallback-high-default",
        loaded=False,
        blast_radius_default=BlastRadius.HIGH,
    )
    return {"low_default": low_default, "high_default": high_default}


_SOURCES = [None, "a2a"]


def _corpus():
    for skill, (title, body), declared, source in itertools.product(
        _SKILLS, _TEXTS, _DECLARED, _SOURCES
    ):
        snap = {"title": title, "description": body}
        if declared is not None:
            snap["action_type"] = declared
        if source is not None:
            snap["source"] = source
        yield skill, snap


def _old(skill, snap):
    return apis._infer_action_type(skill, snap)


def _new(skill, snap):
    return apis._resolve_gate_action_type(skill, snap).value


def _sanctioned_lowering(skill, snap) -> bool:
    """The ONE place the new resolver may be less strict than the old one:
    Cicada, nothing declared, no provenance marker, and an explicit report-only
    statement."""
    declared = str(snap.get("action_type") or "").strip()
    return (
        (skill or "").lower() == "cicada"
        and not declared
        and not snap.get("source")
        and infer_action_type_from_text(snap.get("title"), snap.get("description"))
        == "compute_only_analysis"
    )


def loosening_violations(new_fn):
    """Every corpus input where `new_fn` is less strict than main, other than the
    sanctioned lowering. Empty means the property holds."""
    found = []
    for policy_name, policy in _policies().items():
        for skill, snap in _corpus():
            old_at, new_at = _old(skill, snap), new_fn(skill, snap)
            old_b = policy.blast_radius_for(old_at)
            new_b = policy.blast_radius_for(new_at)
            if _RANK[new_b] < _RANK[old_b] and not _sanctioned_lowering(skill, snap):
                found.append((policy_name, skill, snap, old_at, new_at))
            if old_b is BlastRadius.NEVER and new_b is not BlastRadius.NEVER:
                found.append((policy_name, skill, snap, old_at, new_at))
    return found


def test_the_new_resolver_never_loosens_anything_but_the_sanctioned_lowering():
    assert loosening_violations(_new) == []


def test_declared_values_are_identical_to_the_old_resolver():
    checked = 0
    for skill, snap in _corpus():
        if str(snap.get("action_type") or "").strip():
            checked += 1
            assert _new(skill, snap) == _old(skill, snap), (skill, snap)
    assert checked > 1000


def test_where_main_yields_never_the_new_code_yields_never_under_both_policies():
    checked = 0
    for policy in _policies().values():
        for skill, snap in _corpus():
            if policy.blast_radius_for(_old(skill, snap)) is BlastRadius.NEVER:
                checked += 1
                assert policy.blast_radius_for(_new(skill, snap)) is BlastRadius.NEVER, (
                    skill, snap,
                )
    assert checked > 100  # the corpus actually exercises the NEVER cases


def test_where_main_checkpoints_the_new_code_checkpoints_except_the_sanctioned_lowering():
    """Through the real `evaluate_gate`, at a confidence that auto-executes LOW
    work, so a blast-radius difference is the only thing that can change the
    action."""
    checked = lowered = 0
    for policy in _policies().values():
        for skill, snap in _corpus():
            old = evaluate_gate(confidence=0.99, action_type=_old(skill, snap), policy=policy)
            if old.action is GateAction.AUTO_EXECUTE:
                continue
            checked += 1
            new = evaluate_gate(confidence=0.99, action_type=_new(skill, snap), policy=policy)
            if new.action is GateAction.AUTO_EXECUTE:
                lowered += 1
                assert _sanctioned_lowering(skill, snap), (skill, snap)
    assert checked > 1000
    assert lowered > 0  # and the sanctioned lowering does happen


def test_the_sanctioned_lowering_is_exactly_cicada_report_only_and_still_needs_confidence():
    policy = _policies()["low_default"]
    snap = _SNAPSHOTS["report_only"]
    assert _new("cicada", snap) == "compute_only_analysis"
    held = evaluate_gate(confidence=0.40, action_type=_new("cicada", snap), policy=policy)
    assert held.action is not GateAction.AUTO_EXECUTE  # LOW blast, still two-axis
    for other in ("vanellus", "monedula", "struthio", "corvus"):
        assert _new(other, snap) == AGENT_ACTION_TYPE_PRIOR[other]


# ── planted red: the checker must fail on a resolver that loosens ────────────


def _planted_discards_declared(skill, snap):
    declared = str(snap.get("action_type") or "").strip().lower()
    if declared and declared not in {"local_edit", "compute_only_analysis", "payment", "open_or_merge_pr"}:
        return apis._resolve_gate_action_type(skill, {**snap, "action_type": None}).value
    return _new(skill, snap)


def _planted_lowers_every_agent(skill, snap):
    t = infer_action_type_from_text(snap.get("title"), snap.get("description"))
    if not str(snap.get("action_type") or "").strip() and t == "compute_only_analysis":
        return t
    return _new(skill, snap)


def _planted_lowers_an_externally_sourced_task(skill, snap):
    """Cicada-only like the real rule, but ignoring where the task came from:
    prose from outside the swarm loosening its own gate."""
    t = infer_action_type_from_text(snap.get("title"), snap.get("description"))
    if (
        (skill or "").lower() == "cicada"
        and not str(snap.get("action_type") or "").strip()
        and t == "compute_only_analysis"
    ):
        return t
    return _new(skill, snap)


def _planted_text_supplies_low_without_a_prior(skill, snap):
    resolved = _new(skill, snap)
    t = infer_action_type_from_text(snap.get("title"), snap.get("description"))
    if resolved is None and t == "compute_only_analysis":
        return t
    return resolved


def _planted_text_lowers_a_high_prior_on_a_pr_reference(skill, snap):
    resolved = _new(skill, snap)
    if (skill or "").lower() == "cicada" and "pr #" in (snap.get("title") or "").lower():
        return "compute_only_analysis"
    return resolved


def _planted_drops_the_prior(skill, snap):
    """The Cicada-prior removal this change explicitly does not make."""
    if not str(snap.get("action_type") or "").strip() and (skill or "").lower() == "cicada":
        return None
    return _new(skill, snap)


@pytest.mark.parametrize(
    "planted",
    [
        _planted_discards_declared,
        _planted_lowers_every_agent,
        _planted_lowers_an_externally_sourced_task,
        _planted_text_supplies_low_without_a_prior,
        _planted_text_lowers_a_high_prior_on_a_pr_reference,
        _planted_drops_the_prior,
    ],
    ids=lambda f: f.__name__,
)
def test_planted_loosening_resolvers_are_caught(planted):
    assert loosening_violations(planted) != [], (
        f"{planted.__name__} loosens the gate and the property check did not notice"
    )


def test_the_property_check_passes_the_old_resolver_trivially():
    assert loosening_violations(_old) == []


# ═══ C. Calibration against tasks the gate really parked ═════════════════════


def _gate(snapshot, skill, confidence):
    policy = ExecutionPolicy(entity_id="fallback", loaded=False)
    at = apis._resolve_gate_action_type(skill, snapshot).value
    return evaluate_gate(confidence=confidence, action_type=at, policy=policy), at


def test_the_hard_dependency_task_still_gates():
    """The control case: a config migration that puts a Neotoma read on daemon
    startup and cites an open PR. If a change lets this through, it is wrong."""
    snap = {
        "title": "Migrate swarm configuration from env files to Neotoma entities",
        "description": (
            "Moves ~250 configuration variables out of env files and into "
            "daemon_configuration entities. PR #643 open."
        ),
    }
    decision, at = _gate(snap, "cicada", 1.0)
    assert at == "open_or_merge_pr"
    assert decision.blast_radius is BlastRadius.HIGH
    assert decision.action is not GateAction.AUTO_EXECUTE


@pytest.mark.parametrize(
    "title, body",
    [
        ("Add a read-only reconciler comparing issue gate_status against participation_record",
         "Report through the existing daemon reporting path. Write NOTHING - this job's only output is a divergence report."),
        ("Extend the workflow drift check to compare declared gate sequence, not just owner names",
         "PURE REPORTING. No behaviour change, nothing gated on the result."),
    ],
)
def test_report_only_cicada_tasks_are_read_as_low_blast(title, body):
    decision, at = _gate({"title": title, "description": body}, "cicada", 0.0)
    assert at == "compute_only_analysis"
    assert decision.blast_radius is BlastRadius.LOW
    # Still two-axis: with no confidence it checkpoints, for the honest reason.
    assert decision.action is not GateAction.AUTO_EXECUTE


def test_analysis_that_also_opens_a_pr_is_pr_work():
    decision, at = _gate(
        {"title": "Analyse the drift", "description": "Write nothing locally, then open a PR."},
        "cicada", 1.0,
    )
    assert at == "open_or_merge_pr"
    assert decision.action is not GateAction.AUTO_EXECUTE


def test_a_low_prior_agent_asked_to_send_external_mail_is_now_gated():
    decision, at = _gate(
        {"title": "Send the email to the client", "description": "about the delay"},
        "fringilla", 1.0,
    )
    assert at == "send_external_comms"
    assert decision.action is not GateAction.AUTO_EXECUTE


# ═══ D. confidence_drivers reaches the operator ══════════════════════════════

_DRIVERS = "Only the calendar title is known; scope and owner are unverified."


def test_checkpoint_brief_and_notification_carry_the_creators_drivers(world, scorer):
    scorer('{"confidence": 0.40, "rationale": "ambiguous target"}')
    notifier = _dispatch(_task(confidence_drivers=_DRIVERS))
    assert _DRIVERS in _only_brief(world)["plan_summary"]
    assert f"drivers={_DRIVERS}" in notifier.sent[-1]


@pytest.mark.parametrize("blank", [None, "", "   ", [], ["", " "]])
def test_missing_drivers_are_stated_not_omitted(world, scorer, blank):
    scorer('{"confidence": 0.40, "rationale": "ambiguous target"}')
    task = _task()
    if blank is not None:
        task["confidence_drivers"] = blank
    notifier = _dispatch(task)
    assert apis.CONFIDENCE_DRIVERS_MISSING in _only_brief(world)["plan_summary"]
    assert apis.CONFIDENCE_DRIVERS_MISSING in notifier.sent[-1]


def test_a_list_of_drivers_is_joined_and_long_text_is_bounded():
    text = apis._confidence_drivers_text({"confidence_drivers": ["no spec", "no owner"]})
    assert text == "no spec; no owner"
    long = apis._confidence_drivers_text({"confidence_drivers": "word " * 1000})
    assert len(long) <= apis._CONFIDENCE_DRIVERS_MAX
    assert "\n" not in apis._confidence_drivers_text({"confidence_drivers": "a\nb\n\nc"})


# ═══ dispatch uses the gate resolver ═════════════════════════════════════════


def test_dispatch_reads_a_report_only_cicada_task_as_low_and_runs_it_on_confidence(world):
    task = _task(
        title="Add a read-only reconciler",
        body="Write NOTHING - this job's only output is a divergence report.",
        confidence=0.95,
    )
    del task["action_type"]
    _dispatch(task)
    assert world["briefs"] == []
    assert world["spawns"] == [("cicada", "ent_task_1")]


def test_dispatch_still_holds_a_cicada_task_with_no_signal_exactly_as_before(world):
    task = _task(title="Tidy the notes index", body="Re-sort it.", confidence=0.99)
    del task["action_type"]
    _dispatch(task)
    brief = _only_brief(world)
    assert brief["action_type"] == "open_or_merge_pr"
    assert brief["decision"].blast_radius is BlastRadius.HIGH
    assert world["spawns"] == []


def test_dispatch_holds_a_cicada_task_that_cites_a_pr_even_with_report_words(world):
    task = _task(
        title="Pure reporting on PR #643",
        body="Write nothing.",
        confidence=0.99,
    )
    del task["action_type"]
    _dispatch(task)
    assert _only_brief(world)["action_type"] == "open_or_merge_pr"
    assert world["spawns"] == []


def test_dispatch_never_discards_a_declared_typo(world):
    task = _task(
        action_type="open_pull_request",
        title="Write nothing, pure reporting",
        confidence=0.99,
    )
    _dispatch(task)
    brief = _only_brief(world)
    assert brief["action_type"] == "open_pull_request"
    assert brief["decision"].blast_radius is BlastRadius.NEVER
    assert world["spawns"] == []
