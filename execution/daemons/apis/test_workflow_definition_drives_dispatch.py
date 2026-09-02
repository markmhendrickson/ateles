"""Changing a `workflow_definition` entity must change what the dispatcher does.

That sentence is the whole claim of this change, so it is what these tests
assert — end to end, through `_gates_green` and `_waive_gates`, with only the
entity's `gates` field varying between runs.

## Why the obvious test would be worthless

A test that exercised `resolve_pre_impl_gates` in isolation would pass today
against the OLD hardcoded path too: it would prove the resolver parses entities,
not that the dispatcher obeys them. The dispatcher could go on ignoring it
entirely and every such test would stay green.

This codebase has been bitten by exactly that shape twice — a Turdus test that
asserted only the two fields which survived a broken write, and a test asserting
`owning_agent == "gryllus"` that pinned a rename bug as expected behaviour. So
each test below drives a REAL dispatcher method and varies ONLY the stored
entity, which is the one input the hardcoded tuples could not have responded to.

## Verified to fail when the change is reverted

Confirmed by reverting both resolver call sites in `swarm_dispatch.py` to the
hardcoded `("pm", "ux", "arch")` and re-running: **6 failed, 2 passed.**

FAIL on revert — these are the claim:

  * `test_bug_workflow_waives_only_pm` — waives ("pm","ux","arch"), two of which
    `ateles|bug` does not declare.
  * `test_feature_workflow_waives_all_three` — the ordering comes from the
    entity's phases, not from the tuple's literal order.
  * `test_editing_the_entity_changes_the_waive` — the waived set is identical
    across two different stored entities: the defect, stated as an assertion.
  * `test_gates_green_uses_the_bug_workflows_single_gate` — False on a bug issue
    whose only declared gate (`pm`) is signed off, because it still judges
    against `ux`/`arch`.
  * `test_no_workflow_refuses_rather_than_defaulting` — the old path had no
    notion of an unresolvable workflow and waived the hardcoded set anyway.
  * `test_malformed_gate_sequence_refuses` — likewise waives regardless.

PASS before and after, deliberately — these prove the fix did not simply loosen
gating, which a change of this shape could easily do unnoticed:

  * `test_gates_green_still_blocks_on_a_feature_ux_gate` — a feature workflow's
    pending `ux` gate must still block (the ateles#460 production failure).
  * `test_unreachable_neotoma_fails_closed_for_gates_green` — an unreadable
    record is never green-by-default.

Run: pytest execution/daemons/apis/test_workflow_definition_drives_dispatch.py -v
"""

from __future__ import annotations

import json

import pytest

import swarm_dispatch as sd
from github_gateway import SwarmTrigger
from lib.daemon_runtime import workflow_resolver as wr

# Captured at import, before conftest's autouse fixture can replace it.
_REAL_FETCH = wr._fetch_definitions


# ── Fixtures shaped like the real stored entities ────────────────────────────
#
# `gates` is a JSON STRING here, not a list, because that is how 5 of the 8 live
# entities actually store it. A fixture using a real list would test a shape
# production does not have.


def _entity(entity_id: str, workflow_type: str, gates: list[dict]) -> dict:
    return {
        "entity_id": entity_id,
        "snapshot": {
            "project": "ateles",
            "workflow_type": workflow_type,
            "status": "active",
            "gates": json.dumps(gates),
        },
    }


def _gate(phase: int, name: str, owner: str) -> dict:
    return {
        "phase": phase,
        "gate_name": name,
        "owner_agent": owner,
        "parallel_group": None,
        "join_gate": None,
        "required": True,
    }


# Mirrors ent_1d20d557828ecd080b654367 (ateles|feature): pm, then ux+arch.
FEATURE_GATES = [
    _gate(1, "pm", "pavo"),
    _gate(2, "ux", "accipiter"),
    _gate(2, "arch", "waxwing"),
    _gate(3, "impl", "cicada"),
    _gate(4, "pr_review", "vanellus"),
]

# Mirrors ent_1b6d0acbdc436d3f0dad5a0d (ateles|bug): pm is the ONLY pre-impl
# gate. The hardcoded triple waived ux and arch here — gates this workflow
# never declares.
BUG_GATES = [
    _gate(1, "pm", "pavo"),
    _gate(3, "impl", "cicada"),
    _gate(4, "pr_review", "vanellus"),
]


def _install_entities(monkeypatch, entities: list[dict]) -> None:
    """Serve *entities* as Neotoma's workflow_definition query result.

    Patched at the HTTP boundary (`httpx.post`) rather than at the resolver's
    own seam, so the snapshot parsing, the JSON-string `gates` coercion, and the
    project/status filtering all run for real. Stubbing `load_workflows` instead
    would skip the code most likely to be wrong.

    `_fetch_definitions` is re-bound to the real implementation first, because
    conftest's `_default_feature_workflow` fixture replaces it for every test in
    this directory. Without this, these tests would silently assert against that
    fixture's workflow instead of the entities they installed — a test that
    passes while measuring the wrong thing, which is the failure mode this whole
    file exists to rule out.
    """

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"entities": entities, "total": len(entities)}

    def fake_post(url, **kwargs):  # noqa: ANN001, ARG001
        return _Resp()

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(wr, "_fetch_definitions", _REAL_FETCH)
    monkeypatch.setattr(wr.httpx, "post", fake_post)
    wr.clear_cache()


@pytest.fixture(autouse=True)
def _clear_resolver_cache():
    """The resolver caches for 30s; a leaked entry would make one test's stored
    entity decide another test's outcome — which is precisely the bug class."""
    wr.clear_cache()
    yield
    wr.clear_cache()


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg: str, priority=None, handler=None) -> None:  # noqa: ANN001
        self.sent.append(msg)


class _Ok:
    ok = True
    stdout = "Triage complete."
    error = None
    returncode = 0


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


def _trigger(labels: list[str]) -> SwarmTrigger:
    return SwarmTrigger(
        kind="issue_comment",
        repository="markmhendrickson/ateles",
        number=1234,
        title="t",
        body="b",
        author="markmhendrickson",
        html_url="",
        delivery_id="d",
        action="created",
        labels=labels,
    )


def _stub_gate_status(monkeypatch, gate_status: dict, found: bool = True) -> None:
    class _State:
        def __init__(self) -> None:
            self.found = found
            self.gate_status = gate_status

    async def fake_load(self, repo, issue_number):  # noqa: ANN001, ARG001
        return _State()

    monkeypatch.setattr(sd.IssueGateStore, "load", fake_load)


def _capture_waive(monkeypatch) -> list[tuple[str, ...]]:
    """Record the gate set each waive sweep targets."""
    seen: list[tuple[str, ...]] = []

    async def fake_waive(self, repo, issue_number, gates):  # noqa: ANN001, ARG001
        seen.append(tuple(gates))
        return sd.WaiveOutcome(
            entity_found=True, targeted=list(gates), waived=list(gates),
            verified=True,
        )

    async def fake_comment(self, trigger, outcome):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(sd.IssueGateStore, "waive", fake_waive)
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_post_gate_waive_comment", fake_comment
    )
    return seen


# ── The claim: the entity decides ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bug_workflow_waives_only_pm(monkeypatch):
    """A bug issue is waived against `ateles|bug`'s single pre-impl gate.

    The old hardcoded ("pm","ux","arch") waived two gates this workflow does not
    declare — writing `waived` onto gate_status keys the record never asked for.
    """
    _install_entities(
        monkeypatch,
        [
            _entity("ent_feature", "feature", FEATURE_GATES),
            _entity("ent_bug", "bug", BUG_GATES),
        ],
    )
    seen = _capture_waive(monkeypatch)

    await _dispatcher()._waive_gates(_trigger(labels=["bug"]))

    assert seen == [("pm",)], (
        "the bug workflow declares pm as its only pre-impl gate; waiving ux or "
        "arch means the hardcoded tuple is still in charge"
    )


@pytest.mark.asyncio
async def test_feature_workflow_waives_all_three(monkeypatch):
    """The same code path, same repo, different label — three gates.

    Paired with the test above: identical inputs except which workflow the
    labels select, so a difference in the result can only come from the entity.
    """
    _install_entities(
        monkeypatch,
        [
            _entity("ent_feature", "feature", FEATURE_GATES),
            _entity("ent_bug", "bug", BUG_GATES),
        ],
    )
    seen = _capture_waive(monkeypatch)

    await _dispatcher()._waive_gates(_trigger(labels=["enhancement"]))

    assert seen == [("pm", "arch", "ux")], (
        "feature declares pm (phase 1) then arch+ux (phase 2), ordered by "
        f"(phase, name); got {seen}"
    )


@pytest.mark.asyncio
async def test_editing_the_entity_changes_the_waive(monkeypatch):
    """THE test. One dispatcher, one trigger, two versions of one entity.

    Nothing varies between the two calls except the stored `gates` field. If the
    waived set is identical across them, the dispatcher is not reading the
    record — which is the defect, stated as an assertion.
    """
    trigger = _trigger(labels=["enhancement"])

    _install_entities(monkeypatch, [_entity("ent_x", "feature", FEATURE_GATES)])
    seen = _capture_waive(monkeypatch)
    await _dispatcher()._waive_gates(trigger)
    before = seen[-1]

    # The operator edits the entity: a new `security_review` gate at phase 2,
    # and `ux` removed. No code changes.
    edited = [
        _gate(1, "pm", "pavo"),
        _gate(2, "arch", "waxwing"),
        _gate(2, "security_review", "buteo"),
        _gate(3, "impl", "cicada"),
    ]
    _install_entities(monkeypatch, [_entity("ent_x", "feature", edited)])
    await _dispatcher()._waive_gates(trigger)
    after = seen[-1]

    assert before == ("pm", "arch", "ux")
    assert after == ("pm", "arch", "security_review"), (
        "editing the entity must change the waived set; a gate added in Neotoma "
        "is honoured with no code change, and a removed one stops being touched"
    )
    assert before != after, "the entity edit had no effect — the defect is back"


@pytest.mark.asyncio
async def test_gates_green_uses_the_bug_workflows_single_gate(monkeypatch):
    """A bug issue with `pm` signed off is green, even with ux/arch pending.

    Under the hardcoded triple this returned False forever: it demanded gates
    `ateles|bug` never defines, so no bug issue could ever hand off to build.
    """
    _install_entities(
        monkeypatch,
        [
            _entity("ent_feature", "feature", FEATURE_GATES),
            _entity("ent_bug", "bug", BUG_GATES),
        ],
    )
    _stub_gate_status(
        monkeypatch, {"pm": "signed_off", "ux": "pending", "arch": "pending"}
    )

    green = await _dispatcher()._gates_green(
        _Ok(), "markhendrickson/ateles", 1234, labels=["bug"]
    )
    assert green is True, (
        "ateles|bug declares only pm as pre-impl; pending ux/arch are not its "
        "gates and must not block it"
    )


@pytest.mark.asyncio
async def test_gates_green_still_blocks_on_a_feature_ux_gate(monkeypatch):
    """The fix must not merely loosen the gate.

    Same pending gate_status as the test above, but a feature issue — where the
    record DOES declare ux. This is the ateles#460 production failure and it
    must still fail closed.
    """
    _install_entities(
        monkeypatch,
        [
            _entity("ent_feature", "feature", FEATURE_GATES),
            _entity("ent_bug", "bug", BUG_GATES),
        ],
    )
    _stub_gate_status(
        monkeypatch, {"pm": "signed_off", "ux": "pending", "arch": "signed_off"}
    )

    green = await _dispatcher()._gates_green(
        _Ok(), "markhendrickson/ateles", 1234, labels=["enhancement"]
    )
    assert green is False, "a feature issue's pending ux gate must still block"


# ── No workflow matches: refuse, never default ───────────────────────────────


@pytest.mark.asyncio
async def test_no_workflow_refuses_rather_than_defaulting(monkeypatch):
    """With no workflow_definition, the waive is REFUSED and nothing is written.

    Falling back to a hardcoded set here would reintroduce the second source of
    truth — and would apply exactly when the record is not answering. Same
    posture as the Neotoma halt (#714): decline the decision, leave state
    untouched, tell the operator why.
    """
    _install_entities(monkeypatch, [])  # no definitions at all
    seen = _capture_waive(monkeypatch)

    notifier = _Notifier()
    d = sd.SwarmDispatcher(notifier=notifier)
    outcome = await d._waive_gates(_trigger(labels=["enhancement"]))

    assert seen == [], "nothing may be waived when no workflow governs the issue"
    assert outcome.ok is False
    assert any("REFUSED" in m for m in notifier.sent), (
        "a silent refusal is indistinguishable from an idle swarm — the "
        "operator must be told"
    )


@pytest.mark.asyncio
async def test_unreachable_neotoma_fails_closed_for_gates_green(monkeypatch):
    """An unreadable record means not-green, never green-by-default."""

    def boom(url, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("neotoma unreachable")

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(wr, "_fetch_definitions", _REAL_FETCH)
    monkeypatch.setattr(wr.httpx, "post", boom)
    wr.clear_cache()
    _stub_gate_status(monkeypatch, {"pm": "signed_off"})

    green = await _dispatcher()._gates_green(
        _Ok(), "markhendrickson/ateles", 1234, labels=["bug"]
    )
    assert green is False


@pytest.mark.asyncio
async def test_malformed_gate_sequence_refuses(monkeypatch):
    """A gate with no name fails the definition rather than shortening it.

    Silently dropping the bad gate would produce a SHORTER pre-impl set — "this
    issue needs fewer sign-offs than it does", the exact failure the divergent
    tuples caused.
    """
    broken = [
        {"phase": 1, "gate_name": "", "owner_agent": "pavo", "required": True},
        _gate(3, "impl", "cicada"),
    ]
    _install_entities(monkeypatch, [_entity("ent_broken", "feature", broken)])
    seen = _capture_waive(monkeypatch)

    outcome = await _dispatcher()._waive_gates(_trigger(labels=["enhancement"]))

    assert seen == []
    assert outcome.ok is False
