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
    assert "bombycilla" in _agents(panel)


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
    assert "bombycilla" in [l.agent for l in lenses]


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
