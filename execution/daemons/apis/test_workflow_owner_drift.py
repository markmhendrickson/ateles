"""
A workflow must not name a gate owner the swarm cannot dispatch (ateles#441).

## The failure

`workflow_definition` entities name the agent owning each gate. Dispatch picks
agents from hardcoded rosters in `review_panel.py` and `issue_spec.py`. Nothing
compared the two lists.

On 2026-06-12 two agents were renamed — Bombycilla → Waxwing, Gryllus → Cicada
(both agent_definitions say so in their own descriptions). The workflow
definitions kept naming the old ones. For two months no code noticed.

## Why it stayed invisible

The renamed agent still runs, still returns ok, still writes its spec section —
and the gate never signs, because the agent doing the work is not the owner the
workflow expects. `pending` is a legitimate state, so nothing errors and no
alert fires. ateles#416 sat at `arch: pending` for four days; #430 blocked PR
#438 the same way.

It also blocks unattended operation outright: the auto-build handoff is gated on
`_gates_green()`, which is False while any pre-impl gate is unsigned. A gate that
can never sign stalls every downstream PR forever, silently.

Run: pytest execution/daemons/apis/test_workflow_owner_drift.py -v
"""

from __future__ import annotations

import swarm_dispatch as sd


def _wf(name: str, gates: list[tuple[str, str]]) -> dict:
    return {
        "canonical_name": name,
        "gates": [
            {"gate_name": g, "owner_agent": o, "phase": 1, "required": True}
            for g, o in gates
        ],
    }


def test_clean_workflow_reports_no_drift():
    wf = _wf("workflow_definition:ateles|bug", [("pm", "pavo"), ("impl", "cicada")])
    assert sd.workflow_owner_drift([wf], {"pavo", "cicada"}) == []


def test_detects_the_june_rename_that_caused_this():
    """The historical case, verbatim: the pre-fix ateles|feature gate list."""
    wf = _wf(
        "workflow_definition:ateles|feature",
        [
            ("pm", "pavo"),
            ("ux", "accipiter"),
            ("arch", "bombycilla"),   # renamed to waxwing 2026-06-12
            ("impl", "gryllus"),      # renamed to cicada  2026-06-12
            ("pr_review", "vanellus"),
        ],
    )
    drift = sd.workflow_owner_drift([wf], sd.dispatchable_agents())

    owners = sorted(o for _, _, o in drift)
    assert owners == ["bombycilla", "gryllus"], (
        "both renamed agents must be flagged — gryllus would have blocked impl "
        "immediately after arch cleared"
    )
    gates = sorted(g for _, g, _ in drift)
    assert gates == ["arch", "impl"]


def test_reports_the_workflow_so_the_warning_is_actionable():
    wf = _wf("workflow_definition:ateles|feature", [("arch", "bombycilla")])
    drift = sd.workflow_owner_drift([wf], {"waxwing"})
    assert drift == [("workflow_definition:ateles|feature", "arch", "bombycilla")]


def test_handles_snapshot_wrapped_entities():
    """Neotoma returns entities with the fields under `snapshot`."""
    entity = {
        "entity_id": "ent_x",
        "snapshot": {
            "project": "ateles",
            "workflow_type": "feature",
            "gates": [{"gate_name": "arch", "owner_agent": "bombycilla"}],
        },
    }
    drift = sd.workflow_owner_drift([entity], {"waxwing"})
    assert len(drift) == 1
    assert drift[0][1:] == ("arch", "bombycilla")


def test_tolerates_malformed_rows():
    """A missing gates list or blank owner must not crash the check."""
    assert sd.workflow_owner_drift([{"canonical_name": "x"}], {"pavo"}) == []
    wf = _wf("y", [("pm", "")])
    assert sd.workflow_owner_drift([wf], {"pavo"}) == []


def test_current_rosters_contain_the_renamed_agents():
    """The roster must be derived, not a fourth hand-maintained copy.

    If this fails after a future rename, the guard itself has drifted — which is
    the failure mode it exists to catch.
    """
    roster = sd.dispatchable_agents()
    assert {"waxwing", "cicada"} <= roster
    assert not ({"bombycilla", "gryllus"} & roster), (
        "the retired names must NOT be dispatchable, or the guard cannot fire"
    )
