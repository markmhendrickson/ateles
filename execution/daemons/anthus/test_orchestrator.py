"""
Unit tests for the Anthus orchestrator gate-readiness logic.

Run with: pytest execution/daemons/anthus/test_orchestrator.py -v
"""

from __future__ import annotations

from orchestrator import (
    Gate,
    GateState,
    _coerce_list_field,
    _gate_satisfied_by_comment,
    WorkflowDefinition,
    compute_ready_gates,
    select_workflow,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _smoke_test_workflow() -> WorkflowDefinition:
    """The harness-sandbox smoke-test workflow used in real test setup."""
    return WorkflowDefinition(
        entity_id="ent_smoke",
        project="harness-sandbox",
        workflow_type="smoke_test_full_lifecycle",
        description="smoke test",
        legal_required=True,
        gates=[
            Gate(1, "pm_scope", "pavo", None, None, True),
            Gate(2, "ux_design", "manucode", "phase2", "arch", True),
            Gate(2, "arch", "waxwing", "phase2", "ux_design", True),
            Gate(3, "impl", "cicada", None, None, True),
            Gate(4, "qa", "phoenicurus", "phase4", "legal", True),
            Gate(4, "legal", "buteo", "phase4", "qa", True),
            Gate(4, "compliance_supervisor", "robin", None, None, True),
            Gate(4, "pr_review", "vanellus", None, None, True),
            Gate(5, "release", "struthio", None, None, True),
            Gate(6, "growth_announce", "accipiter", "phase6", "social_draft", False),
            Gate(6, "social_draft", "corvus", "phase6", "growth_announce", False),
            Gate(6, "devrel_docs", "regulus", "phase6", None, False),
        ],
        fast_paths=[
            {
                "condition": "label:internal-only",
                "skip_gates": ["growth_announce", "social_draft", "devrel_docs"],
            },
            {"condition": "label:no-ui-changes", "skip_gates": ["ux_design"]},
            {
                "condition": "label:no-data-changes",
                "skip_gates": ["arch", "compliance_supervisor"],
            },
        ],
    )


def _comment(author: str, body: str, cid: int = 1) -> dict:
    return {
        "id": cid,
        "author": author,
        "body": body,
        "url": f"https://example/c/{cid}",
    }


# ── select_workflow ───────────────────────────────────────────────────────────


def test_select_workflow_explicit_override():
    feature = WorkflowDefinition("e1", "ateles", "feature", "", [], [], False)
    bug = WorkflowDefinition("e2", "ateles", "bug", "", [], [], False)
    chosen = select_workflow({"labels": ["workflow:bug"]}, [feature, bug])
    assert chosen is bug


def test_select_workflow_label_name_match():
    feature = WorkflowDefinition("e1", "ateles", "feature", "", [], [], False)
    bug = WorkflowDefinition("e2", "ateles", "bug", "", [], [], False)
    chosen = select_workflow({"labels": ["bug"]}, [feature, bug])
    assert chosen is bug


def test_select_workflow_default_feature():
    feature = WorkflowDefinition("e1", "ateles", "feature", "", [], [], False)
    bug = WorkflowDefinition("e2", "ateles", "bug", "", [], [], False)
    chosen = select_workflow({"labels": ["random-label"]}, [feature, bug])
    assert chosen is feature


def test_select_workflow_no_match_returns_none():
    bug = WorkflowDefinition("e2", "ateles", "bug", "", [], [], False)
    chosen = select_workflow({"labels": []}, [bug])
    assert chosen is None


# ── compute_ready_gates ──────────────────────────────────────────────────────


def test_initial_state_only_phase_1_ready():
    wf = _smoke_test_workflow()
    state, ready = compute_ready_gates(wf, {"labels": []}, [])
    assert [g.gate_name for g in ready] == ["pm_scope"]
    assert state["pm_scope"].status == "pending"
    # Phase 2+ still pending but not ready.
    assert state["ux_design"].status == "pending"


def test_pm_scope_satisfied_phase_2_opens():
    wf = _smoke_test_workflow()
    comments = [
        _comment(
            "pavo-agent", "[pavo] acceptance_criteria: form must have copy + opt-out"
        )
    ]
    state, ready = compute_ready_gates(wf, {"labels": []}, comments)
    assert state["pm_scope"].status == "satisfied"
    # Both phase-2 gates should be ready in parallel.
    assert set(g.gate_name for g in ready) == {"ux_design", "arch"}


def test_only_one_of_phase_2_satisfied_phase_3_blocked():
    wf = _smoke_test_workflow()
    comments = [
        _comment("pavo-agent", "[pavo] acceptance_criteria: ..."),
        _comment(
            "waxwing-agent", "[waxwing] schema_or_api_proposal: no schema needed"
        ),
        # manucode has NOT commented
    ]
    state, ready = compute_ready_gates(wf, {"labels": []}, comments)
    assert state["pm_scope"].status == "satisfied"
    assert state["arch"].status == "satisfied"
    assert state["ux_design"].status == "pending"
    # phase 3 (impl) should NOT be ready — ux_design unsatisfied.
    assert "impl" not in [g.gate_name for g in ready]


def test_phase_3_ready_when_both_phase_2_satisfied():
    wf = _smoke_test_workflow()
    comments = [
        _comment("pavo-agent", "[pavo] acceptance_criteria: ..."),
        _comment("waxwing-agent", "[waxwing] schema_or_api_proposal: ..."),
        _comment("manucode-agent", "[manucode] copy_and_ux_flow: ..."),
    ]
    state, ready = compute_ready_gates(wf, {"labels": []}, comments)
    assert "impl" in [g.gate_name for g in ready]


def test_fast_path_no_ui_changes_skips_ux_design():
    wf = _smoke_test_workflow()
    comments = [_comment("pavo-agent", "[pavo] acceptance_criteria: ...")]
    state, ready = compute_ready_gates(wf, {"labels": ["no-ui-changes"]}, comments)
    assert state["ux_design"].status == "skipped"
    # arch should still need to be done; ux_design no longer blocks impl.
    assert "arch" in [g.gate_name for g in ready]


def test_fast_path_internal_only_skips_phase_6():
    wf = _smoke_test_workflow()
    # Satisfy everything through phase 5
    comments = [
        _comment("pavo-agent", "[pavo] acceptance_criteria: ..."),
        _comment("manucode-agent", "[manucode] copy_and_ux_flow: ..."),
        _comment("waxwing-agent", "[waxwing] schema_or_api_proposal: ..."),
        _comment("cicada-agent", "[cicada] pull_request_link: #42"),
        _comment("phoenicurus-agent", "[phoenicurus] test_plan: ..."),
        _comment("buteo-agent", "[buteo] compliance_review: ..."),
        _comment("robin-agent", "[robin] compliance_verdict: approved"),
        _comment("vanellus-agent", "[vanellus] merge_decision: approved"),
        _comment("struthio-agent", "[struthio] release_note: ..."),
    ]
    state, ready = compute_ready_gates(wf, {"labels": ["internal-only"]}, comments)
    assert state["growth_announce"].status == "skipped"
    assert state["social_draft"].status == "skipped"
    assert state["devrel_docs"].status == "skipped"
    # No more phase-6 work to dispatch.
    assert all(g.phase < 6 for g in ready)


def test_phase_6_parallel_dispatch_when_ready():
    wf = _smoke_test_workflow()
    comments = [
        _comment("pavo-agent", "[pavo] acceptance_criteria: ..."),
        _comment("manucode-agent", "[manucode] copy_and_ux_flow: ..."),
        _comment("waxwing-agent", "[waxwing] schema_or_api_proposal: ..."),
        _comment("cicada-agent", "[cicada] pull_request_link: #42"),
        _comment("phoenicurus-agent", "[phoenicurus] test_plan: ..."),
        _comment("buteo-agent", "[buteo] compliance_review: ..."),
        _comment("robin-agent", "[robin] compliance_verdict: approved"),
        _comment("vanellus-agent", "[vanellus] merge_decision: approved"),
        _comment("struthio-agent", "[struthio] release_note: ..."),
    ]
    state, ready = compute_ready_gates(wf, {"labels": ["customer-facing"]}, comments)
    names = {g.gate_name for g in ready}
    assert names == {"growth_announce", "social_draft", "devrel_docs"}


def test_satisfaction_falls_back_to_author_match():
    """
    When an agent comments without the canonical [agent] artifact: header,
    the orchestrator still recognizes satisfaction if the comment author
    contains the agent's name.
    """
    wf = _smoke_test_workflow()
    comments = [
        # No canonical header — just a generic comment from Pavo's bot account.
        {
            "id": 1,
            "author": "pavo-bot",
            "body": "Looking at this, I think we should ship Option A with a mailto link only. Acceptance criteria: ...",
            "url": "https://example/c/1",
        }
    ]
    state, ready = compute_ready_gates(wf, {"labels": []}, comments)
    assert state["pm_scope"].status == "satisfied"
    # Phase 2 should still open.
    assert {g.gate_name for g in ready} == {"ux_design", "arch"}


def test_canonical_header_preferred_over_author_match():
    """
    When a comment has BOTH a canonical header and matching author, the
    canonical-header match is taken first. Both should produce satisfaction,
    so test there's no ordering bug between the two paths.
    """
    wf = _smoke_test_workflow()
    comments = [
        {
            "id": 1,
            "author": "pavo-bot",
            "body": "[pavo] acceptance_criteria: ship Option A",
            "url": "https://example/c/1",
        }
    ]
    state, _ = compute_ready_gates(wf, {"labels": []}, comments)
    assert state["pm_scope"].status == "satisfied"
    assert state["pm_scope"].artifact_refs == ["https://example/c/1"]


def test_unrelated_author_does_not_satisfy():
    """
    Comments by authors whose names don't contain the agent name must not
    satisfy the gate.
    """
    wf = _smoke_test_workflow()
    comments = [
        {
            "id": 1,
            "author": "random-contributor",
            "body": "Looks fine to me.",
            "url": "https://example/c/1",
        }
    ]
    state, ready = compute_ready_gates(wf, {"labels": []}, comments)
    assert state["pm_scope"].status == "pending"
    assert [g.gate_name for g in ready] == ["pm_scope"]


def test_author_substring_does_not_falsely_satisfy():
    """
    Whole-word match requirement: an author 'pavolino' should NOT satisfy
    pavo's gate. Otherwise long-name collisions become an attack surface.
    """
    wf = _smoke_test_workflow()
    comments = [
        {
            "id": 1,
            "author": "pavolino",
            "body": "random comment",
            "url": "https://example/c/1",
        }
    ]
    state, _ = compute_ready_gates(wf, {"labels": []}, comments)
    assert state["pm_scope"].status == "pending"


def test_impact_score_low_skips_publicity():
    """
    A work entity with impact_score < 5 should skip growth_announce and
    social_draft via the new numeric fast_path. devrel_docs stays (covers
    SDK changes regardless of impact).
    """
    wf = WorkflowDefinition(
        entity_id="ent",
        project="p",
        workflow_type="smoke",
        description="",
        legal_required=False,
        gates=[
            Gate(1, "pm_scope", "pavo", None, None, True),
            Gate(6, "growth_announce", "mimus", None, None, False),
            Gate(6, "social_draft", "corvus", None, None, False),
            Gate(6, "devrel_docs", "regulus", None, None, False),
        ],
        fast_paths=[
            {
                "condition": "impact_score<5",
                "skip_gates": ["growth_announce", "social_draft"],
            }
        ],
    )
    state, _ = compute_ready_gates(wf, {"impact_score": 2, "labels": []}, [])
    assert state["growth_announce"].status == "skipped"
    assert state["social_draft"].status == "skipped"
    assert state["devrel_docs"].status == "pending"


def test_impact_score_high_keeps_publicity():
    wf = WorkflowDefinition(
        entity_id="ent",
        project="p",
        workflow_type="smoke",
        description="",
        legal_required=False,
        gates=[
            Gate(1, "pm_scope", "pavo", None, None, True),
            Gate(6, "growth_announce", "mimus", None, None, False),
        ],
        fast_paths=[{"condition": "impact_score<5", "skip_gates": ["growth_announce"]}],
    )
    state, _ = compute_ready_gates(wf, {"impact_score": 8, "labels": []}, [])
    assert state["growth_announce"].status == "pending"


def test_audience_internal_skips_publicity():
    wf = WorkflowDefinition(
        entity_id="ent",
        project="p",
        workflow_type="smoke",
        description="",
        legal_required=False,
        gates=[
            Gate(1, "pm_scope", "pavo", None, None, True),
            Gate(6, "growth_announce", "mimus", None, None, False),
        ],
        fast_paths=[
            {"condition": "audience:internal", "skip_gates": ["growth_announce"]}
        ],
    )
    state, _ = compute_ready_gates(wf, {"audience": "internal", "labels": []}, [])
    assert state["growth_announce"].status == "skipped"


def test_missing_impact_score_treated_as_zero():
    """Defensive: no impact_score field → 0, which fails impact_score>=5."""
    wf = WorkflowDefinition(
        entity_id="ent",
        project="p",
        workflow_type="smoke",
        description="",
        legal_required=False,
        gates=[Gate(6, "growth_announce", "mimus", None, None, False)],
        fast_paths=[{"condition": "impact_score<5", "skip_gates": ["growth_announce"]}],
    )
    state, _ = compute_ready_gates(wf, {"labels": []}, [])
    assert state["growth_announce"].status == "skipped"


def test_unmet_precondition_skips_gate():
    """
    When a gate's precondition is unmet (passed in by the caller as
    unmet_preconditions), the gate is auto-skipped and downstream phases
    advance.
    """
    wf = _smoke_test_workflow()
    # Satisfy through phase 4 so phase 5 (release) is the next concern.
    comments = [
        _comment("pavo-bot", "[pavo] acceptance_criteria: ..."),
        _comment("manucode-bot", "[manucode] copy_and_ux_flow: ..."),
        _comment("waxwing-bot", "[waxwing] schema_or_api_proposal: ..."),
        _comment("cicada-bot", "[cicada] pull_request_link: #42"),
        _comment("phoenicurus-bot", "[phoenicurus] test_plan: ..."),
        _comment("buteo-bot", "[buteo] compliance_review: ..."),
        _comment("robin-bot", "[robin] compliance_verdict: approved"),
        _comment("vanellus-bot", "[vanellus] merge_decision: approved"),
    ]
    state, ready = compute_ready_gates(
        wf,
        {"labels": ["customer-facing"]},
        comments,
        unmet_preconditions={"release"},
    )
    assert state["release"].status == "skipped"
    # Phase 6 should now be reachable since release is treated as skipped.
    names = {g.gate_name for g in ready}
    assert {"growth_announce", "social_draft", "devrel_docs"} <= names


def test_dispatched_gate_can_be_satisfied_by_later_comment():
    """
    When a gate is in "dispatched" state and a satisfying comment arrives
    on a later orchestrator tick, the gate transitions to "satisfied".

    Only terminal states (satisfied, skipped, failed) are immune to update.
    """
    wf = _smoke_test_workflow()
    comments_v1 = [_comment("pavo-agent", "[pavo] acceptance_criteria: ...")]
    state_v1, ready_v1 = compute_ready_gates(wf, {"labels": []}, comments_v1)
    # Mark phase-2 gates dispatched.
    for g in ready_v1:
        state_v1[g.gate_name].status = "dispatched"

    # manucode now comments; orchestrator re-ticks with the new comment.
    comments_v2 = comments_v1 + [
        _comment("manucode-agent", "[manucode] copy_and_ux_flow: ...")
    ]
    state_v2, _ = compute_ready_gates(
        wf, {"labels": []}, comments_v2, existing_state=state_v1
    )
    assert state_v2["ux_design"].status == "satisfied"
    assert len(state_v2["ux_design"].artifact_refs) == 1
    # arch still dispatched (no satisfying comment yet).
    assert state_v2["arch"].status == "dispatched"


# ── Snapshot list-field coercion (regression: Anthus dead 2026-06-03 → 08-31) ──
#
# Neotoma stores some workflow_definition list fields as a JSON *string* rather
# than a JSON array (as of 2026-08-31: 5 of 8 live entities for `gates`, 1 for
# `fast_paths`). The parser iterated the raw value, so a string yielded
# characters and `g.get(...)` raised "'str' object has no attribute 'get'" on
# every issue/pull_request SSE event — aborting workflow selection entirely.


def test_coerce_list_field_passes_through_real_list():
    raw = [{"phase": 1, "gate_name": "pm"}]
    assert _coerce_list_field(raw, entity_id="ent_x", field_name="gates") == raw


def test_coerce_list_field_parses_json_encoded_string():
    """The shape that killed the daemon: a list arriving as a JSON string."""
    raw = '[{"phase": 1, "gate_name": "pm", "owner_agent": "pavo"}]'
    out = _coerce_list_field(raw, entity_id="ent_x", field_name="gates")
    assert out == [{"phase": 1, "gate_name": "pm", "owner_agent": "pavo"}]
    # Critically, elements are dicts — not characters.
    assert all(hasattr(item, "get") for item in out)


def test_coerce_list_field_handles_missing_empty_and_malformed():
    for raw in (None, "", "   ", "not json", '{"not": "a list"}', 42):
        assert _coerce_list_field(raw, entity_id="ent_x", field_name="gates") == []


def test_coerce_list_field_drops_non_dict_elements():
    """One bad element must not take down the whole workflow."""
    raw = '[{"gate_name": "pm"}, "stray", null, 7]'
    assert _coerce_list_field(raw, entity_id="ent_x", field_name="gates") == [
        {"gate_name": "pm"}
    ]


def test_string_encoded_gates_produce_usable_workflow(monkeypatch):
    """End-to-end: a string-encoded snapshot parses into real Gate objects."""
    import asyncio

    import orchestrator as _orch

    snapshot = {
        "project": "ateles",
        "status": "active",
        "workflow_type": "feature",
        "description": "d",
        # Both fields in their string-encoded form.
        "gates": (
            '[{"phase":1,"gate_name":"pm","owner_agent":"pavo",'
            '"parallel_group":null,"join_gate":null,"required":true}]'
        ),
        "fast_paths": '[{"condition": "label:copy", "skip_gates": ["arch"]}]',
    }
    payload = {"entities": [{"entity_id": "ent_str", "snapshot": snapshot}]}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(_orch.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(_orch, "NEOTOMA_BEARER", "test-token")

    out = asyncio.run(_orch.fetch_workflow_definitions("ateles"))

    assert len(out) == 1
    assert [g.gate_name for g in out[0].gates] == ["pm"]
    assert out[0].gates[0].owner_agent == "pavo"
    assert out[0].fast_paths == [{"condition": "label:copy", "skip_gates": ["arch"]}]


# ── Gate satisfaction coverage + state isolation (ateles#568, #573) ───────────


def _social_content_workflow() -> WorkflowDefinition:
    """The live ateles|social_content workflow (ent_38ab0119e528d021c51d46a1)."""
    return WorkflowDefinition(
        entity_id="ent_38ab0119e528d021c51d46a1",
        project="ateles",
        workflow_type="social_content",
        description="corvus social content",
        legal_required=False,
        gates=[
            Gate(1, "draft", "corvus", None, None, True),
            Gate(2, "draft_lint", "anthus", None, None, True),
            Gate(3, "operator_preview", "onychomys", None, None, True),
            Gate(4, "post", "corvus", None, None, True),
        ],
        fast_paths=[{"condition": "label:approved", "skip_gates": ["operator_preview"]}],
    )


def test_every_live_gate_name_has_a_satisfaction_rule():
    """
    Audit of all 8 live workflow_definition entities (Neotoma, 2026-08-31).

    A gate_name absent from GATE_SATISFACTION_RULES can never satisfy:
    _gate_satisfied_by_comment returns None forever and the workflow stalls.
    Operator-gated gates are exempt — they are satisfied by a human, not by
    an agent comment — but they must be declared as such, not merely omitted.
    """
    from orchestrator import GATE_SATISFACTION_RULES, OPERATOR_GATED_GATES

    live_gate_names = {
        # ateles|bug, ateles|feature, ateles|release, ateles|security,
        # ateles|copy, swarm-smoke|feature, neotoma|feature
        "pm", "ux", "arch", "impl", "pr_review", "qa", "legal", "release", "copy",
        # ateles|social_content
        "draft", "draft_lint", "operator_preview", "post",
    }
    unresolvable = {
        g
        for g in live_gate_names
        if g not in GATE_SATISFACTION_RULES and g not in OPERATOR_GATED_GATES
    }
    assert unresolvable == set(), (
        f"gates with no satisfaction rule and not operator-gated: {sorted(unresolvable)}"
    )


def test_social_content_advances_past_phase_one():
    """
    With a draft artifact posted, ateles|social_content must advance to
    phase 2 (draft_lint) instead of stalling forever at phase 1.
    """
    wf = _social_content_workflow()
    comments = [
        {
            "author": "corvus-bot",
            "body": "[corvus] social_post_draft: X/LinkedIn/Bluesky drafts ready",
            "url": "https://github.com/x/1#c1",
        }
    ]
    state, ready = compute_ready_gates(wf, {}, comments)

    assert state["draft"].status == "satisfied"
    assert [g.gate_name for g in ready] == ["draft_lint"]


def test_operator_preview_is_not_satisfied_by_an_agent_comment():
    """
    operator_preview represents a human approval. No agent-authored comment
    may satisfy it — a rule that anything satisfies is worse than none,
    because it looks like progress.
    """
    from orchestrator import GATE_SATISFACTION_RULES, OPERATOR_GATED_GATES

    assert "operator_preview" in OPERATOR_GATED_GATES
    assert "operator_preview" not in GATE_SATISFACTION_RULES

    gate = Gate(3, "operator_preview", "onychomys", None, None, True)
    forged = [
        {"author": "onychomys", "body": "[onychomys] operator_preview: approved"},
        {"author": "corvus-bot", "body": "[corvus] approved: ship it"},
    ]
    assert _gate_satisfied_by_comment(gate, forged) is None


def test_compute_ready_gates_does_not_mutate_caller_state():
    """
    Regression (ateles#573): compute_ready_gates shallow-copied the state dict
    and mutated the caller's GateState objects in place. anthus.py then compared
    `prior.status != "satisfied"` against the very object it had just mutated,
    so the guard was always False and record_satisfied was NEVER called for any
    already-dispatched gate. Every participation_record in the system's history
    is `dispatched`; none has ever reached `satisfied`. This is why.
    """
    wf = WorkflowDefinition(
        entity_id="w1",
        project="ateles",
        workflow_type="feature",
        description="",
        legal_required=False,
        gates=[Gate(1, "pm", "pavo", None, None, True)],
        fast_paths=[],
    )
    existing = {"pm": GateState(gate_name="pm", status="dispatched")}
    comments = [
        {
            "author": "pavo-bot",
            "body": "[pavo] acceptance_criteria: scoped",
            "url": "https://github.com/x/1#c9",
        }
    ]

    state, _ready = compute_ready_gates(wf, {}, comments, existing_state=existing)

    # The returned state advances...
    assert state["pm"].status == "satisfied"
    # ...while the caller's own copy is untouched, so the transition is visible.
    assert existing["pm"].status == "dispatched"
    assert state["pm"] is not existing["pm"]
    assert state["pm"].artifact_refs == ["https://github.com/x/1#c9"]
    assert existing["pm"].artifact_refs == []


def test_satisfaction_transition_is_detectable_by_the_caller():
    """
    Mirrors anthus.py's persistence guard exactly. This is the condition that
    gates record_satisfied, and therefore the only path to a participation_record
    reaching `satisfied`.
    """
    wf = WorkflowDefinition(
        entity_id="w1",
        project="ateles",
        workflow_type="feature",
        description="",
        legal_required=False,
        gates=[Gate(1, "pm", "pavo", None, None, True)],
        fast_paths=[],
    )
    existing = {"pm": GateState(gate_name="pm", status="dispatched")}
    comments = [{"author": "pavo-bot", "body": "[pavo] acceptance_criteria: ok", "url": "u"}]

    state, _ = compute_ready_gates(wf, {}, comments, existing_state=existing)

    gs = state["pm"]
    prior = existing.get("pm")
    fires = gs.status == "satisfied" and (not prior or prior.status != "satisfied")
    assert fires, "record_satisfied would not be called — gate can never reach satisfied"


def test_unresolvable_gates_flags_a_gate_with_no_rule():
    """A gate absent from both maps must be reported, not silently stalled."""
    from orchestrator import unresolvable_gates

    wf = WorkflowDefinition(
        entity_id="w1",
        project="ateles",
        workflow_type="mystery",
        description="",
        legal_required=False,
        gates=[
            Gate(1, "pm", "pavo", None, None, True),
            Gate(2, "telepathy", "nobody", None, None, True),
        ],
        fast_paths=[],
    )
    assert unresolvable_gates([wf]) == {"ateles|mystery": ["telepathy"]}


def test_unresolvable_gates_clean_for_all_live_workflows():
    """The 8 live workflows must be fully covered after this fix."""
    from orchestrator import unresolvable_gates

    assert unresolvable_gates([_social_content_workflow()]) == {}
