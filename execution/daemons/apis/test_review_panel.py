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


# ── Security lens (ateles#425) ─────────────────────────────────────────────


def test_security_lens_triggers_on_sensitive_paths():
    # The CONCERNS surfaces from neotoma's scripts/security/classify_diff.js,
    # which is what emits sensitive=true for the release security lane.
    for path in (
        "src/actions.ts",
        "src/middleware/admission.ts",
        "src/services/auth/bearer.ts",
        "src/services/aauth/verify.ts",
        "src/services/subscriptions/webhook_delivery.ts",
        "src/services/sync/sync_webhook_outbound.ts",
        "src/services/entity_submission/orchestrator.ts",
        "src/services/access_policy.ts",
        "src/services/local_auth.ts",
        "src/services/inspector_mount.ts",
        "openapi.yaml",
        "scripts/security/classify_diff.js",
    ):
        panel = select_panel(set(), [path], max_panel=8)
        assert "security" in [l.lens for l in panel], path


def test_security_lens_stays_off_non_sensitive_diffs():
    panel = select_panel(set(), ["README.md", "docs/guide.md"], max_panel=8)
    assert "security" not in [l.lens for l in panel]


def test_security_lens_survives_the_panel_cap():
    # Regression: the security lens owns no gate, so plain registry order let
    # the default cap of 4 drop it exactly on a BROAD security PR — the case
    # that most needs it. Once its trigger fires it must keep a seat.
    broad_security_diff = [
        "src/services/auth/token.ts",  # security
        "openapi.yaml",  # arch + security
        "package.json",  # legal
        "docs/guide.md",  # ux
        "src/a.ts",
        "src/b.ts",
        "src/c.ts",
    ]
    for cap in (2, 3, 4, 5):
        panel = select_panel(set(), broad_security_diff, max_panel=cap)
        assert "security" in [l.lens for l in panel], f"dropped at cap={cap}"


def test_pending_gate_owner_still_outranks_security():
    # Security is prioritized over other blocking lenses, but NOT over a lens
    # that owns a still-pending gate — that one must re-run or the gate can
    # never clear (ateles#230).
    panel = select_panel(
        set(),
        ["src/services/auth/token.ts", "openapi.yaml"],
        max_panel=2,
        pending_gates={"arch"},
    )
    assert "arch" in [l.lens for l in panel]


def test_security_lens_carries_a_refutation_mandate():
    # The whole point of the lens: it must ask "what fails open?", not
    # "is this adequate?". Guard the mandate against being softened away.
    security = next(l for l in LENSES if l.lens == "security")
    checks = security.checks.lower()
    for token in ("refute", "fails open", "incomplete", "confirmed", "plausible"):
        assert token in checks, token


def test_security_lens_owns_no_gate():
    # It reviews; it does not sign off a pre-impl gate on the issue.
    security = next(l for l in LENSES if l.lens == "security")
    assert security.gate == ""
    assert not security.forward_looking


def test_security_issue_patterns_preregister_expectations():
    lenses = select_expectation_agents(
        "Fix SSRF in outbound webhook delivery",
        "caller-supplied URL reaches fetch without the host guard",
        [],
    )
    assert "security" in [l.lens for l in lenses]


# ── Provider preference (model diversity) ──────────────────────────────────


def test_security_lens_prefers_a_non_authoring_provider():
    from review_panel import resolve_lens_provider

    security = next(l for l in LENSES if l.lens == "security")
    assert security.preferred_provider
    # Honored when the provider is actually usable.
    assert (
        resolve_lens_provider(security, {"claude", "codex", "cursor"})
        == security.preferred_provider
    )


def test_unavailable_preferred_provider_degrades_instead_of_skipping():
    # A hard pin would make run_skill find zero candidates and the security
    # review would silently not happen — strictly worse than reviewing on the
    # authoring model. The preference must degrade to normal routing.
    from review_panel import resolve_lens_provider

    security = next(l for l in LENSES if l.lens == "security")
    assert resolve_lens_provider(security, {"claude"}) is None


def test_lenses_without_a_preference_use_normal_routing():
    from review_panel import resolve_lens_provider

    for lens in LENSES:
        if lens.lens != "security":
            assert resolve_lens_provider(lens, {"claude", "codex"}) is None


def test_security_provider_preference_is_env_overridable(monkeypatch):
    from review_panel import resolve_lens_provider

    security = next(l for l in LENSES if l.lens == "security")
    monkeypatch.setenv("ATELES_SECURITY_LENS_PROVIDER", "cursor")
    assert resolve_lens_provider(security, {"claude", "cursor"}) == "cursor"
    # Empty disables the preference outright.
    monkeypatch.setenv("ATELES_SECURITY_LENS_PROVIDER", "")
    assert resolve_lens_provider(security, {"claude", "cursor"}) is None
