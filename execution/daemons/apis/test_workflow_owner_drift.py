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

import json

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


# ---------------------------------------------------------------------------
# The check must actually RUN — a detector with no caller is not a guard
# ---------------------------------------------------------------------------


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg: str, priority=None, handler=None) -> None:  # noqa: ANN001
        self.sent.append(msg)


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


def test_startup_check_reports_and_escalates_drift(monkeypatch):
    """Loxia's finding on PR #442: the detector existed with no caller.

    A guard nobody runs is the same failure it guards against — a mechanism
    that looks like protection and never fires.
    """
    import asyncio

    d = _dispatcher()

    async def fake_post(self, path, payload):  # noqa: ANN001
        assert payload["entity_type"] == "workflow_definition"
        return {
            "entities": [
                {
                    "snapshot": {
                        "project": "ateles",
                        "workflow_type": "feature",
                        # JSON STRING, as /entities/query actually returns it.
                        # Mocking a parsed list here is what let the production
                        # crash ship green (#450).
                        "gates": json.dumps(
                            [
                                {"gate_name": "arch", "owner_agent": "bombycilla"},
                                {"gate_name": "pm", "owner_agent": "pavo"},
                            ]
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(sd.IssueSpecStore, "_post", fake_post)

    drift = asyncio.run(d.check_workflow_owner_drift())

    assert [row[2] for row in drift] == ["bombycilla"]
    assert d.notifier.sent, "drift must reach the operator, not only the log"
    assert "bombycilla" in d.notifier.sent[0]


def test_startup_check_is_quiet_when_clean(monkeypatch):
    import asyncio

    d = _dispatcher()

    async def fake_post(self, path, payload):  # noqa: ANN001
        return {
            "entities": [
                {
                    "snapshot": {
                        "project": "ateles",
                        "workflow_type": "bug",
                        "gates": json.dumps(
                            [
                                {"gate_name": "pm", "owner_agent": "pavo"},
                                {"gate_name": "impl", "owner_agent": "cicada"},
                            ]
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(sd.IssueSpecStore, "_post", fake_post)

    assert asyncio.run(d.check_workflow_owner_drift()) == []
    assert not d.notifier.sent, "a clean check must not page the operator"


def test_startup_check_fails_open(monkeypatch):
    """A check that cannot read Neotoma must never stop the daemon booting."""
    import asyncio

    d = _dispatcher()

    async def boom(self, path, payload):  # noqa: ANN001
        raise RuntimeError("neotoma unreachable")

    monkeypatch.setattr(sd.IssueSpecStore, "_post", boom)

    assert asyncio.run(d.check_workflow_owner_drift()) == []  # must not raise


def test_non_panel_owners_are_still_live_agents():
    """Loxia's second finding: the four hardcoded names can drift too.

    `dispatchable_agents()` appends triage/PR/release owners that sit outside
    the panel and spec rosters. That set is exactly the kind of list this guard
    exists to catch drifting, so pin it against the agent-facing roster.
    """
    roster = sd.dispatchable_agents()
    for agent in ("lanius", "vanellus", "struthio", "cicada"):
        assert agent in roster
    # lanius and vanellus are GitHub-facing, so they are independently anchored.
    assert {"lanius", "vanellus"} <= set(sd.GITHUB_FACING_AGENTS)
    # cicada is anchored by the spec sections it owns.
    from issue_spec import SECTION_BY_AGENT

    assert "cicada" in SECTION_BY_AGENT


# ---------------------------------------------------------------------------
# The shape the LIVE API actually returns
# ---------------------------------------------------------------------------


def test_gates_arriving_as_a_json_string():
    """Neotoma returns list-valued snapshot fields as JSON STRINGS.

    Every test above passed a parsed list, so all of them were green while the
    deployed check raised `'str' object has no attribute 'get'` on every
    startup — iterating the string yields characters. Fail-open swallowed it,
    so the guard reported nothing and never ran. Three consecutive startups in
    the daemon log before anyone looked.

    A test fixture that does not match the real payload is the same failure the
    guard exists to catch: something that looks like protection and is not.
    """
    import json

    entity = {
        "canonical_name": "workflow_definition:ateles|feature",
        "snapshot": {
            "project": "ateles",
            "workflow_type": "feature",
            "gates": json.dumps(
                [
                    {"gate_name": "pm", "owner_agent": "pavo"},
                    {"gate_name": "arch", "owner_agent": "bombycilla"},
                ]
            ),
        },
    }
    drift = sd.workflow_owner_drift([entity], {"pavo", "waxwing"})
    assert drift == [
        ("workflow_definition:ateles|feature", "arch", "bombycilla")
    ]


def test_malformed_gates_json_does_not_raise():
    entity = {"canonical_name": "x", "snapshot": {"gates": "not json at all"}}
    assert sd.workflow_owner_drift([entity], {"pavo"}) == []


def test_gates_of_an_unexpected_type_do_not_raise():
    for value in (42, {"gate_name": "pm"}, None):
        entity = {"canonical_name": "x", "snapshot": {"gates": value}}
        assert sd.workflow_owner_drift([entity], {"pavo"}) == []


def test_non_dict_entries_inside_gates_are_skipped():
    import json

    entity = {
        "canonical_name": "x",
        "snapshot": {
            "gates": json.dumps(["a string", {"gate_name": "arch", "owner_agent": "gryllus"}])
        },
    }
    assert sd.workflow_owner_drift([entity], {"cicada"}) == [
        ("x", "arch", "gryllus")
    ]


# ---------------------------------------------------------------------------
# Cross-surface parity (#450 blocker, pm lens on PR #449)
# ---------------------------------------------------------------------------


def test_the_shared_helper_handles_every_shape_seen_in_prod():
    """`parse_snapshot_list_field` is the list-shaped sibling of
    `parse_gate_status`, which gate_waive.py already had for dicts.

    Writing a second decode inline is exactly how #442 shipped a reader that
    crashed on every startup while its siblings handled the same payload
    correctly. One helper, one place to fix.
    """
    from gate_waive import parse_snapshot_list_field as parse

    rows = [{"gate_name": "pm", "owner_agent": "pavo"}]
    assert parse(json.dumps(rows)) == rows, "JSON string — the prod shape"
    assert parse(rows) == rows, "already-parsed list"
    assert parse("not json at all") == []
    assert parse("") == []
    assert parse(None) == []
    assert parse(42) == []
    assert parse({"gate_name": "pm"}) == [], "a dict is not a list of rows"
    # Partial malformation degrades to the rows that parse, not to nothing.
    assert parse(json.dumps(["junk", rows[0]])) == rows


def test_the_drift_check_routes_through_the_shared_helper():
    """Pin the wiring, not just the behaviour.

    A local re-implementation would pass the behavioural tests above while
    reintroducing the divergence this consolidation exists to remove.
    """
    from pathlib import Path

    source = Path(__file__).with_name("swarm_dispatch.py").read_text()
    assert "parse_snapshot_list_field(snap.get(\"gates\"))" in source, (
        "workflow_owner_drift must use the shared helper, not decode inline"
    )


def test_no_other_snapshot_reader_decodes_a_list_field_inline():
    """The audit result, pinned.

    gate_waive.py handles `gate_status` (parse_gate_status) and `owner_history`
    (parse_owner_history); issue_spec.py's `sequence_state` is a list of STRINGS
    with its own branch. The drift check was the only exposed reader. If a new
    one appears, it should use the shared helper rather than `json.loads` on a
    snapshot field.
    """
    from pathlib import Path

    for name in ("gate_waive.py", "issue_spec.py"):
        source = Path(__file__).with_name(name).read_text()
        # Each file may json.loads inside its OWN parse_* helper; what must not
        # appear is a decode applied straight to a snapshot lookup.
        assert "json.loads(snap.get(" not in source, (
            f"{name} decodes a snapshot field inline — use "
            "parse_snapshot_list_field instead"
        )
