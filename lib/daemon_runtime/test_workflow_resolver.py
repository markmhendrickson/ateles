"""Unit coverage for the workflow resolver's own contract.

These tests deliberately do NOT stand in for
`execution/daemons/apis/test_workflow_definition_drives_dispatch.py`. Everything
here would have passed while the dispatcher went on ignoring the resolver
entirely — proving the parser parses is not proving the caller obeys. That
end-to-end claim is asserted there; this file covers the parsing, derivation,
caching, and refusal behaviour that file relies on.

Run: pytest lib/daemon_runtime/test_workflow_resolver.py -v
"""

from __future__ import annotations

import json

import pytest

from lib.daemon_runtime import workflow_resolver as wr
from lib.daemon_runtime.workflow_resolver import (
    ResolvedGate,
    ResolvedWorkflow,
    WorkflowUnresolvedError,
    project_from_repo,
    resolve_pre_impl_gates,
    select_workflow,
    unknown_owner_agents,
    validate_gates,
)


def _gate(phase: int, name: str, owner: str = "someone") -> dict:
    return {"phase": phase, "gate_name": name, "owner_agent": owner, "required": True}


def _wf(workflow_type: str, gates: list[dict]) -> ResolvedWorkflow:
    return ResolvedWorkflow(
        entity_id=f"ent_{workflow_type}",
        project="ateles",
        workflow_type=workflow_type,
        gates=validate_gates(gates, entity_id=f"ent_{workflow_type}"),
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    wr.clear_cache()
    yield
    wr.clear_cache()


# ── pre-impl is DERIVED from phases, never listed ────────────────────────────


def test_pre_impl_is_everything_before_the_impl_phase():
    wf = _wf("feature", [
        _gate(1, "pm"), _gate(2, "ux"), _gate(2, "arch"),
        _gate(3, "impl"), _gate(4, "pr_review"), _gate(4, "qa"),
    ])
    assert wf.pre_impl_gate_names() == ("pm", "arch", "ux")


def test_a_gate_added_in_neotoma_becomes_pre_impl_with_no_code_change():
    """The property the hardcoded tuples lacked, and why three copies drifted."""
    wf = _wf("feature", [
        _gate(1, "pm"), _gate(2, "arch"),
        _gate(2, "security_review"),  # newly added by an operator edit
        _gate(3, "impl"),
    ])
    assert "security_review" in wf.pre_impl_gate_names()


def test_post_impl_gates_are_never_pre_impl():
    wf = _wf("feature", [_gate(1, "pm"), _gate(3, "impl"), _gate(4, "qa")])
    assert wf.pre_impl_gate_names() == ("pm",)


def test_workflow_with_no_impl_gate_has_no_pre_impl_gates():
    """`ateles|release` is release-only — nothing precedes an implementation
    that does not exist. Inventing pre-impl gates here would block a manual
    release cut on gates its workflow never declares."""
    wf = _wf("release", [_gate(5, "release")])
    assert wf.pre_impl_gate_names() == ()


def test_gate_order_is_full_execution_order():
    wf = _wf("feature", [_gate(3, "impl"), _gate(1, "pm"), _gate(4, "qa")])
    assert wf.gate_order() == ("pm", "impl", "qa")


# ── parsing the shapes prod actually stores ──────────────────────────────────


def test_gates_stored_as_a_json_string_are_parsed():
    """5 of the 8 live entities store `gates` as a JSON string, not a list."""
    parsed = wr._coerce_gate_list(json.dumps([_gate(1, "pm")]), entity_id="ent_x")
    assert parsed == [_gate(1, "pm")]


def test_unparseable_gates_string_yields_no_gates_and_is_then_refused():
    """An empty gate list must never read as 'no pre-impl gates' (an all-clear)."""
    assert wr._coerce_gate_list("{not json", entity_id="ent_x") == []
    with pytest.raises(WorkflowUnresolvedError):
        validate_gates([], entity_id="ent_x")


def test_a_gate_without_a_name_fails_the_whole_definition():
    """Dropping it would SHORTEN the pre-impl set — 'fewer sign-offs than
    required', the exact failure the divergent tuples produced."""
    with pytest.raises(WorkflowUnresolvedError, match="no gate_name"):
        validate_gates(
            [{"phase": 1, "gate_name": "", "owner_agent": "pavo"}], entity_id="ent_x"
        )


def test_a_non_integer_phase_fails_the_whole_definition():
    with pytest.raises(WorkflowUnresolvedError, match="non-integer phase"):
        validate_gates(
            [{"phase": "soon", "gate_name": "pm"}], entity_id="ent_x"
        )


# ── selection ────────────────────────────────────────────────────────────────


def test_explicit_workflow_label_wins():
    wfs = [_wf("feature", [_gate(1, "pm"), _gate(3, "impl")]),
           _wf("bug", [_gate(1, "pm"), _gate(3, "impl")])]
    assert select_workflow(wfs, ["workflow:bug"]).workflow_type == "bug"


def test_a_label_matching_a_workflow_type_selects_it():
    wfs = [_wf("feature", [_gate(1, "pm"), _gate(3, "impl")]),
           _wf("security", [_gate(1, "pm"), _gate(3, "impl")])]
    assert select_workflow(wfs, ["security"]).workflow_type == "security"


def test_feature_is_the_default():
    wfs = [_wf("bug", [_gate(1, "pm"), _gate(3, "impl")]),
           _wf("feature", [_gate(1, "pm"), _gate(3, "impl")])]
    assert select_workflow(wfs, ["chore"]).workflow_type == "feature"


def test_no_match_and_no_feature_returns_none():
    wfs = [_wf("release", [_gate(5, "release")])]
    assert select_workflow(wfs, ["chore"]) is None


def test_project_is_the_repo_name():
    assert project_from_repo("markmhendrickson/ateles") == "ateles"
    assert project_from_repo("") == ""


# ── refusal, not fallback ────────────────────────────────────────────────────


def test_no_workflow_raises_rather_than_returning_a_default():
    """A default here would be the second source of truth all over again, and
    would apply precisely when the record is not answering."""
    with pytest.raises(WorkflowUnresolvedError, match="no active workflow_definition"):
        resolve_pre_impl_gates("o/unknown", [], fetcher=lambda project: [])


def test_missing_bearer_token_refuses_rather_than_guessing(monkeypatch):
    """Unlike the #714 reachability probe — which does NOT halt on a missing
    token because it has a safe default — there is no safe default gate
    sequence, so a config fault must refuse rather than pass permissively."""
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    with pytest.raises(WorkflowUnresolvedError, match="NEOTOMA_BEARER_TOKEN"):
        wr._fetch_definitions("ateles")


def test_transport_failure_becomes_a_refusal_not_an_exception_leak(monkeypatch):
    def boom(url, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("connection reset")

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "t")
    monkeypatch.setattr(wr.httpx, "post", boom)
    with pytest.raises(WorkflowUnresolvedError, match="could not read"):
        wr._fetch_definitions("ateles")


# ── caching: the backoff, never a fallback ───────────────────────────────────


def test_a_burst_of_lookups_issues_one_read():
    """The cache is the backoff (PR #714's shape) — the check must not become
    the retry pressure that manufactures the outage it tests for."""
    calls: list[str] = []

    def fetcher(project: str):
        calls.append(project)
        return [_wf("feature", [_gate(1, "pm"), _gate(3, "impl")])]

    for _ in range(10):
        wr.load_workflows("ateles", fetcher=fetcher)
    assert len(calls) == 1


def test_an_expired_cache_plus_an_unreachable_record_refuses(monkeypatch):
    """A cache that outlives the outage is a hardcoded default with extra steps."""
    monkeypatch.setattr(wr, "CACHE_TTL_SECONDS", -1.0)  # everything is expired

    def boom(project: str):
        raise WorkflowUnresolvedError("record unreachable")

    wr.load_workflows("ateles", fetcher=lambda p: [_wf("feature", [_gate(1, "pm"), _gate(3, "impl")])])
    with pytest.raises(WorkflowUnresolvedError):
        wr.load_workflows("ateles", fetcher=boom)


# ── stale owner_agent: reported, never substituted ───────────────────────────


def test_stale_owner_agents_are_reported_not_repaired():
    """Reading entities live turns a stale owner into a live misroute. Three of
    the eight definitions still name `gryllus` (renamed to Cicada 2026-06-12,
    filed as ent_875dee7675b0516f66a72220). This reports; it must not invent a
    substitute owner, because a guessed routing target is silent misdelivery.
    """
    wf = _wf("copy", [_gate(1, "pm", "pavo"), _gate(3, "impl", "gryllus")])
    stale = unknown_owner_agents([wf], known_agents=["pavo", "cicada", "vanellus"])
    assert stale == {"ateles|copy": ["gryllus"]}
    # The definition itself is still usable — a stale owner must not halt three
    # of eight workflows over a data-entry lag.
    assert wf.pre_impl_gate_names() == ("pm",)


def test_a_fully_current_roster_reports_nothing():
    wf = _wf("feature", [_gate(1, "pm", "pavo"), _gate(3, "impl", "cicada")])
    assert unknown_owner_agents([wf], known_agents=["pavo", "cicada"]) == {}


def test_resolved_workflow_carries_its_entity_id():
    """A dispatch decision can name the entity that produced it — the audit
    trail a hardcoded tuple could never have."""
    assert _wf("feature", [_gate(1, "pm"), _gate(3, "impl")]).entity_id == "ent_feature"
    assert isinstance(
        _wf("feature", [_gate(1, "pm"), _gate(3, "impl")]).gates[0], ResolvedGate
    )
