"""The gate may read a task's text to be STRICTER; tiering and delivery do not (ateles#682).

Four things are pinned here, because each is a way this change could quietly do
harm:

  A. DECOUPLING. Model-tier selection and the build-delivery grant key on
     `apis._infer_action_type`, unchanged. `_GOLDEN` below was captured from
     origin/main BEFORE this change (resolver output, the action class derived
     from it, and `build_delivery.is_delivery_dispatch`), so "byte-for-byte
     unchanged" is asserted against recorded values, not against a function that
     could have moved with the code under test.

  B. NO LOOSENING, STRICTLY. For every input in a cross-product corpus, under
     policies that differ in their SETS and not only their default, the gate's
     blast verdict under the new resolver is the same or stricter than under the
     old one. There is no enumerated exception: nothing in the task's text can
     make a task less strict than its prior. The oracle is
     `policy.blast_radius_for` on both sides; it never calls the module under
     test to decide what is allowed. The checker is itself run against
     deliberately loosened resolvers ("planted red").

  C. CALIBRATION against tasks the gate really parked. Report-only language does
     NOT lower a Cicada task here (that needs independently labelled negative
     cases first); the control case that must keep gating still does.

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
    resolve_gate_action_type,
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

FALLBACK = ExecutionPolicy(entity_id="fallback", loaded=False)

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


def test_a_pr_phrased_task_gets_a_stricter_gate_reading_but_the_same_dispatch_class():
    """The seam, shown on one task: for an agent with a LOW prior the gate reads
    the text and escalates, while the dispatcher's class (tiering + delivery
    grant) does not move."""
    snap = {"title": "Send the email to the client", "description": "about the delay"}
    assert apis._dispatch_action_class("fringilla", snap) == "compute_only_analysis"
    assert apis._resolve_gate_action_type("fringilla", snap, FALLBACK).value == "send_external_comms"


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


def test_the_release_recheck_resolves_under_the_policy_it_then_evaluates_with():
    src = inspect.getsource(apis)
    assert "current_policy = resolve_policy_for_agent(safety_skill)" in src
    assert src.count("current_policy = resolve_policy_for_agent(safety_skill)") == 1
    assert "safety_skill or \"\", task_snapshot, current_policy" in src


def test_the_old_resolver_never_reads_the_tasks_text():
    assert apis._infer_action_type("cicada", _SNAPSHOTS["report_only"]) == "open_or_merge_pr"
    assert apis._infer_action_type(None, _SNAPSHOTS["pr_text"]) is None


# ═══ B. No loosening, strictly ═══════════════════════════════════════════════

_RANK = {BlastRadius.LOW: 0, BlastRadius.HIGH: 1, BlastRadius.NEVER: 2}

# (title, description) pairs: benign, report-only language in several forms,
# mixed and conflicting requests, high-blast phrases, spacing variants.
_TEXTS = [
    ("", ""),
    ("Plan the offsite", ""),
    ("Tidy the notes index", "Local file only."),
    ("Add a read-only reconciler", "Write NOTHING - this job's only output is a divergence report."),
    ("Extend the drift check", "PURE REPORTING. No behaviour change, nothing gated on the result."),
    ("Audit the queue", "Reporting only."),
    ("Audit", "The only output is a written report; also open a PR for it."),
    ("Write nothing, pure reporting", "Migrate the config and fix the importer."),
    ("Implement the exporter", "Nothing gated on the result."),
    ("Do not write nothing", "Implement it and report."),
    ("Write nothing", "pure reporting"),
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
    ("open a PR", ""),
    ("Quarterly review", "all fine\n\nthen send the email to the client"),
]
_DECLARED = [None, "", "  ", "open_or_merge_pr", "local_edit", "compute_only_analysis",
             "operator_only", " Operator_Only ", "open_pull_request", "release", "payment"]
# Where the text sits: the resolver must account for every task-text field.
_SHAPES = ("title+description", "title+body", "benign body, text in description")


def _policy(high, low, default=BlastRadius.LOW, name="p"):
    return ExecutionPolicy(
        entity_id=name,
        high_blast_action_types=frozenset(high),
        low_blast_action_types=frozenset(low),
        blast_radius_default=default,
        loaded=False,
    )


_ALL_HIGH = {"git_push", "open_or_merge_pr", "payment", "send_external_comms",
             "publish", "delete_entity_or_data", "external_api_write"}
_ALL_LOW = {"local_edit", "draft", "neotoma_read",
            "neotoma_internal_entity_update", "compute_only_analysis"}


def _policies():
    """Policies that differ in their SETS, not only in blast_radius_default."""
    return {
        "default_sets": FALLBACK,
        "high_default": _policy(_ALL_HIGH, _ALL_LOW, BlastRadius.HIGH, "high-default"),
        "never_default": _policy(_ALL_HIGH, _ALL_LOW, BlastRadius.NEVER, "never-default"),
        "narrow_sets": _policy({"open_or_merge_pr", "payment"},
                               {"compute_only_analysis", "local_edit"}, name="narrow"),
        # open_or_merge_pr unclassified: Cicada's and Vanellus's prior is NEVER
        # here, while the other high-blast candidates are classified HIGH.
        "prior_unclassified": _policy(_ALL_HIGH - {"open_or_merge_pr"},
                                      _ALL_LOW, name="prior-unclassified"),
        # Inverted: the prior types are LOW and several candidates are HIGH.
        "inverted_sets": _policy({"payment", "publish", "compute_only_analysis"},
                                 {"open_or_merge_pr", "send_external_comms", "local_edit"},
                                 name="inverted"),
        "empty_sets": _policy(set(), set(), name="empty"),
    }


def _snapshot(title, text, shape, declared):
    if shape == "title+description":
        snap = {"title": title, "description": text}
    elif shape == "title+body":
        snap = {"title": title, "body": text}
    else:
        snap = {"title": "Quarterly review", "body": "all fine", "description": f"{title} {text}"}
    if declared is not None:
        snap["action_type"] = declared
    return snap


def _corpus():
    for skill, (title, text), shape, declared in itertools.product(
        _SKILLS, _TEXTS, _SHAPES, _DECLARED
    ):
        yield skill, _snapshot(title, text, shape, declared)


def _old(skill, snap):
    return apis._infer_action_type(skill, snap)


def _new(skill, snap, policy):
    return apis._resolve_gate_action_type(skill, snap, policy).value


def _blast(policy, action_type):
    return _RANK[policy.blast_radius_for(action_type)]


def loosening_violations(new_fn):
    """Every (policy, input) where `new_fn` is less strict than main. The oracle
    is the policy's own classification of both values; nothing under test is
    consulted to decide what is allowed. Empty means the property holds."""
    found = []
    for policy_name, policy in _policies().items():
        for skill, snap in _corpus():
            old_at, new_at = _old(skill, snap), new_fn(skill, snap, policy)
            if _blast(policy, new_at) < _blast(policy, old_at):
                found.append((policy_name, skill, snap, old_at, new_at))
    return found


def test_the_new_resolver_never_loosens_anything():
    assert loosening_violations(_new) == []


def test_declared_values_are_identical_to_the_old_resolver():
    checked = 0
    for policy in _policies().values():
        for skill, snap in _corpus():
            if str(snap.get("action_type") or "").strip():
                checked += 1
                assert _new(skill, snap, policy) == _old(skill, snap), (skill, snap)
    assert checked > 10000


def test_where_main_yields_never_the_new_code_yields_never_under_every_policy():
    checked = 0
    for policy in _policies().values():
        for skill, snap in _corpus():
            if policy.blast_radius_for(_old(skill, snap)) is BlastRadius.NEVER:
                checked += 1
                assert policy.blast_radius_for(_new(skill, snap, policy)) is BlastRadius.NEVER, (
                    skill, snap,
                )
    assert checked > 10000  # the corpus actually exercises the NEVER cases


def test_where_main_checkpoints_the_new_code_checkpoints_under_every_policy():
    """Through the real `evaluate_gate`, at a confidence that auto-executes LOW
    work, so a blast-radius difference is the only thing that can change the
    action. No exception is enumerated."""
    checked = 0
    for policy in _policies().values():
        for skill, snap in _corpus():
            old = evaluate_gate(confidence=0.99, action_type=_old(skill, snap), policy=policy)
            if old.action is GateAction.AUTO_EXECUTE:
                continue
            checked += 1
            new = evaluate_gate(
                confidence=0.99, action_type=_new(skill, snap, policy), policy=policy
            )
            assert new.action is not GateAction.AUTO_EXECUTE, (skill, snap)
    assert checked > 10000


def test_the_corpus_exercises_both_an_escalation_and_a_blocked_escalation():
    """Without these the property above could pass vacuously."""
    escalated = blocked = 0
    for name, policy in _policies().items():
        for skill, snap in _corpus():
            if str(snap.get("action_type") or "").strip():
                continue
            old_at, new_at = _old(skill, snap), _new(skill, snap, policy)
            if _blast(policy, new_at) > _blast(policy, old_at):
                escalated += 1
            text_type = infer_action_type_from_text(
                snap.get("title"), snap.get("body"), snap.get("description")
            )
            if (
                text_type
                and policy.blast_radius_for(old_at) is BlastRadius.NEVER
                and policy.blast_radius_for(text_type) is BlastRadius.HIGH
                and new_at == old_at
            ):
                blocked += 1
    assert escalated > 100
    assert blocked > 50


def test_no_agents_report_only_task_is_lowered():
    for skill in AGENT_ACTION_TYPE_PRIOR:
        assert _new(skill, _SNAPSHOTS["report_only"], FALLBACK) == AGENT_ACTION_TYPE_PRIOR[skill]


# ── planted red: the checker must fail on a resolver that loosens ────────────


def _text_type(snap):
    return infer_action_type_from_text(snap.get("title"), snap.get("body"), snap.get("description"))


def _planted_discards_declared(skill, snap, policy):
    declared = str(snap.get("action_type") or "").strip().lower()
    if declared and declared not in {"local_edit", "compute_only_analysis", "payment", "open_or_merge_pr"}:
        return _new(skill, {**snap, "action_type": None}, policy)
    return _new(skill, snap, policy)


def _planted_lowers_on_report_only_language(skill, snap, policy):
    """The lowering this change used to make and no longer does."""
    text = f"{snap.get('title')} {snap.get('body')} {snap.get('description')}".lower()
    if (
        (skill or "").lower() == "cicada"
        and not str(snap.get("action_type") or "").strip()
        and ("write nothing" in text or "pure reporting" in text or "nothing gated" in text)
    ):
        return "compute_only_analysis"
    return _new(skill, snap, policy)


def _planted_text_supplies_low_without_a_prior(skill, snap, policy):
    resolved = _new(skill, snap, policy)
    if resolved is None and "write nothing" in f"{snap.get('title')} {snap.get('description')}".lower():
        return "compute_only_analysis"
    return resolved


def _planted_escalation_uses_fixed_fallback_tiers(skill, snap, policy):
    """Compares under the default vocabulary instead of the effective policy."""
    return resolve_gate_action_type(
        skill, snap, apis._AGENT_ACTION_TYPE,
        verdict=lambda at: FALLBACK.blast_radius_for(at).value,
    ).value


def _planted_escalation_replaces_never(skill, snap, policy):
    resolved = _new(skill, snap, policy)
    old_at = _old(skill, snap)
    text_type = _text_type(snap)
    if (
        not str(snap.get("action_type") or "").strip()
        and text_type
        and policy.blast_radius_for(old_at) is BlastRadius.NEVER
    ):
        return text_type
    return resolved


def _planted_drops_the_prior(skill, snap, policy):
    """The Cicada-prior removal this change explicitly does not make."""
    if not str(snap.get("action_type") or "").strip() and (skill or "").lower() == "cicada":
        return None
    return _new(skill, snap, policy)


@pytest.mark.parametrize(
    "planted",
    [
        _planted_discards_declared,
        _planted_lowers_on_report_only_language,
        _planted_text_supplies_low_without_a_prior,
        _planted_escalation_uses_fixed_fallback_tiers,
        _planted_escalation_replaces_never,
        _planted_drops_the_prior,
    ],
    ids=lambda f: f.__name__,
)
def test_planted_loosening_resolvers_are_caught(planted):
    assert loosening_violations(planted) != [], (
        f"{planted.__name__} loosens the gate and the property check did not notice"
    )


def test_the_property_check_passes_the_old_resolver_trivially():
    assert loosening_violations(lambda skill, snap, policy: _old(skill, snap)) == []


# ═══ C. Calibration against tasks the gate really parked ═════════════════════


def _gate(snapshot, skill, confidence):
    at = apis._resolve_gate_action_type(skill, snapshot, FALLBACK).value
    return evaluate_gate(confidence=confidence, action_type=at, policy=FALLBACK), at


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
def test_report_only_cicada_tasks_are_still_held_as_pr_work(title, body):
    """NOT FIXED here, on purpose: telling an affirmative report-only contract
    from negated, scoped, mixed or spoofed language needs independently labelled
    negative cases first. Until then these keep Cicada's prior, as on main."""
    decision, at = _gate({"title": title, "description": body}, "cicada", 0.99)
    assert at == "open_or_merge_pr"
    assert decision.blast_radius is BlastRadius.HIGH
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


def test_a_phrase_in_the_description_counts_even_when_the_body_is_present():
    decision, at = _gate(
        {"title": "Weekly note", "body": "all fine", "description": "then send the email to the client"},
        "fringilla", 1.0,
    )
    assert at == "send_external_comms"
    assert decision.action is not GateAction.AUTO_EXECUTE


def test_checkpoint_reissue_context_resolves_under_the_policy_it_evaluates_with(monkeypatch):
    """`_task_gate_context` feeds checkpoint re-issue and the approval-cannot-
    release check. Under a policy that does not classify Cicada's prior but does
    classify the payment the text names, 'escalating' to payment would turn a
    never-tier task into a high-tier one."""
    policy = _policies()["prior_unclassified"]
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _skill: policy)
    snapshot = {"assigned_to": "cicada", "title": "Pay the invoice for the rent", "confidence": 1.0}
    skill, action_type, used_policy, decision = apis._task_gate_context(snapshot)
    assert used_policy is policy
    assert action_type == "open_or_merge_pr"
    assert decision.blast_radius is BlastRadius.NEVER
    assert apis._approval_cannot_release(snapshot) == "unclassified"

    # And the other direction: under a narrower policy the candidate is
    # unclassified (never), which IS stricter than the HIGH prior, so it applies.
    narrow = _policies()["narrow_sets"]
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _skill: narrow)
    snapshot = {"assigned_to": "cicada", "title": "Publish the post on the site", "confidence": 1.0}
    _skill, action_type, _policy_used, decision = apis._task_gate_context(snapshot)
    assert action_type == "publish"
    assert decision.blast_radius is BlastRadius.NEVER


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


@pytest.mark.parametrize(
    "title, body",
    [
        ("Add a read-only reconciler", "Write NOTHING - this job's only output is a divergence report."),
        ("Extend the drift check", "PURE REPORTING. No behaviour change, nothing gated on the result."),
        ("Implement the exporter", "Nothing gated on the result."),
        ("Do not write nothing", "Implement it."),
        ("Tidy the notes index", "Re-sort it."),
        ("Pure reporting on PR #643", "Write nothing."),
    ],
)
def test_dispatch_holds_every_cicada_task_exactly_as_on_main_at_high_confidence(world, title, body):
    task = _task(title=title, body=body, confidence=0.99)
    del task["action_type"]
    _dispatch(task)
    brief = _only_brief(world)
    assert brief["action_type"] == "open_or_merge_pr"
    assert brief["decision"].blast_radius is BlastRadius.HIGH
    assert world["spawns"] == []


def test_dispatch_escalates_a_low_prior_agent_asked_to_send_external_mail(world):
    task = _task(
        assigned_to="fringilla",
        title="Send the email to the client",
        body="about the delay",
        confidence=0.99,
    )
    del task["action_type"]
    _dispatch(task)
    assert _only_brief(world)["action_type"] == "send_external_comms"
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
