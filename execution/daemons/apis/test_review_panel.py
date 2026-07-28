"""Tests for review panel selection (neotoma#1640) and the shift-left
expectation pass (ateles#81)."""

from review_panel import LENSES, select_expectation_agents, select_panel


def _agents(panel):
    return [lens.agent for lens in panel]


# ── Panel selection ────────────────────────────────────────────────────────


def test_always_lenses_serve_on_minimal_panel():
    panel = select_panel(gate_contributors=set(), changed_files=[])
    assert "pavo" in _agents(panel)
    assert "phoenicurus" in _agents(panel)


def test_gate_contributor_joins_panel():
    panel = select_panel({"buteo"}, changed_files=[])
    assert "buteo" in _agents(panel)


def test_diff_surface_pulls_in_arch_lens():
    panel = select_panel(set(), ["server/openapi.yaml", "src/handler.py"])
    assert "waxwing" in _agents(panel)


def test_dependency_manifest_pulls_in_legal_lens():
    panel = select_panel(set(), ["package.json"], max_panel=6)
    assert "buteo" in _agents(panel)


def test_corvus_only_on_non_trivial_prs():
    small = select_panel(set(), ["a.py"], max_panel=6)
    assert "corvus" not in _agents(small)

    big_diff = [f"src/file{i}.py" for i in range(6)]
    big = select_panel(set(), big_diff, max_panel=6)
    assert "corvus" in _agents(big)


def test_panel_cap_prioritizes_blocking_lenses():
    big_diff = [
        "server/openapi.yaml",
        "docs/guide.md",
        "package.json",
        "src/a.py",
        "src/b.py",
        "src/c.py",
    ]
    panel = select_panel(set(), big_diff, max_panel=3)
    assert len(panel) == 3
    assert all(not lens.forward_looking for lens in panel)


def test_pending_gate_lens_gets_a_seat_under_the_cap():
    # ateles#230 regression: with a full diff and max_panel=2, the arch lens
    # (waxwing) would normally be at risk of being dropped. When arch is a
    # pending gate, its owning lens MUST be seated — otherwise the gate can
    # never clear because no re-review of it ever runs.
    big_diff = ["src/a.py", "src/b.py", "src/c.py", "server/openapi.yaml"]
    panel = select_panel(
        gate_contributors=set(),
        changed_files=big_diff,
        max_panel=2,
        pending_gates={"arch"},
    )
    assert "arch" in [lens.gate for lens in panel], "arch gate owner must be seated"
    # And it's prioritized first among the capped seats.
    assert panel[0].gate == "arch"


def test_pending_gate_pulls_in_a_lens_the_diff_would_not_match():
    # A gate can be pending even when the diff doesn't match that lens's
    # patterns and it didn't pre-register — the owning lens must still run.
    panel = select_panel(
        gate_contributors=set(),
        changed_files=["README.md"],  # matches nothing arch-y
        max_panel=6,
        pending_gates={"arch"},
    )
    assert "waxwing" in [lens.agent for lens in panel]


def test_no_pending_gates_preserves_prior_behavior():
    # Backward compat: omitting pending_gates behaves exactly as before.
    diff = ["server/openapi.yaml", "src/handler.py"]
    assert select_panel(set(), diff, max_panel=4) == select_panel(
        set(), diff, max_panel=4, pending_gates=set()
    )


def test_multiple_pending_gates_all_seated_before_cap():
    panel = select_panel(
        gate_contributors=set(),
        changed_files=["src/a.py"],
        max_panel=2,
        pending_gates={"arch", "ux"},
    )
    seated = {lens.gate for lens in panel}
    assert "arch" in seated and "ux" in seated


def test_panel_agents_have_skills_registered():
    # Every lens must point at a real T4 skill name (panel spawns by skill).
    from pathlib import Path

    skills_root = Path(__file__).resolve().parents[3] / ".claude" / "skills"
    for lens in LENSES:
        assert (skills_root / lens.agent / "SKILL.md").exists(), lens.agent


# ── Expectation pre-registration (ateles#81) ───────────────────────────────


def test_always_lenses_preregister_on_every_issue():
    lenses = select_expectation_agents("Tiny copy tweak", "", [])
    assert "pavo" in [l.agent for l in lenses]
    assert "phoenicurus" in [l.agent for l in lenses]


def test_api_issue_triggers_arch_expectations():
    lenses = select_expectation_agents(
        "Add new MCP tool endpoint", "expose a new API for retrieval", []
    )
    assert "waxwing" in [l.agent for l in lenses]


def test_auth_issue_triggers_legal_expectations():
    lenses = select_expectation_agents(
        "Guest token exposure", "auth token scope on public surface", []
    )
    assert "buteo" in [l.agent for l in lenses]


def test_forward_looking_lenses_never_preregister():
    lenses = select_expectation_agents("Huge content launch", "blog post", [])
    assert "corvus" not in [l.agent for l in lenses]


def test_forward_looking_gate_contributor_keeps_seat_on_small_diff():
    # Loxia review on PR #87: the size threshold is an opt-in path, not an
    # override — a forward-looking lens that pre-registered expectations on
    # the parent issue keeps its panel seat even when the diff is small.
    panel = select_panel({"corvus"}, ["a.py"], max_panel=6)
    assert "corvus" in _agents(panel)
