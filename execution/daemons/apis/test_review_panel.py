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


def test_late_enable_of_the_provider_preference_is_honored(monkeypatch):
    # Loxia review on PR #426: the env re-read ran AFTER an early
    # `if not preferred: return None` on the import-frozen field, so a
    # preference disabled at import and enabled later was silently ignored —
    # the disable-then-enable direction of the very staleness the re-read
    # exists to fix. Both directions must work at dispatch time.
    from dataclasses import replace

    from review_panel import resolve_lens_provider

    security = next(l for l in LENSES if l.lens == "security")
    frozen_disabled = replace(security, preferred_provider="")

    monkeypatch.setenv("ATELES_SECURITY_LENS_PROVIDER", "codex")
    assert resolve_lens_provider(frozen_disabled, {"claude", "codex"}) == "codex"

    monkeypatch.setenv("ATELES_SECURITY_LENS_PROVIDER", "")
    assert resolve_lens_provider(security, {"claude", "codex"}) is None


# ── Ateles-side security surfaces (ateles#426 follow-up) ───────────────────


def test_security_lens_fires_on_ateles_own_security_surfaces():
    # Regression: the lens shipped with diff_patterns mirroring neotoma's
    # classify_diff.js, which enumerates NEOTOMA's tree. Ateles has no
    # src/services/auth/ or src/middleware/, so in the repo the lens actually
    # lives in it fired on NOTHING — five consecutive real PRs after the merge
    # seated pm/qa and never security, including the PR that added the lens.
    for path in (
        ".claude/hooks/gmail_send_gate.py",
        "execution/scripts/secrets_lib.py",
        "lib/daemon_runtime/aauth_signer.py",
        "lib/daemon_runtime/aauth_httpsig.py",
        "lib/daemon_runtime/grant_checker.py",
        "lib/daemon_runtime/neotoma_signed.py",
        "lib/daemon_runtime/signed_fetch.mjs",
        "execution/daemons/monedula/monedula.py",
        "execution/daemons/monedula/handlers/payment_profile.py",
        "execution/mcp/ateles/server.py",
    ):
        panel = select_panel(set(), [path], max_panel=8)
        assert "security" in [l.lens for l in panel], path


def test_security_lens_stays_off_routine_ateles_changes():
    # The other half of the contract. A lens that fires on everything spends
    # the panel cap on routine work until it is ignored — the failure mode that
    # makes a security review worthless in a different way than not running.
    for path in (
        "execution/daemons/apis/apis.py",
        "execution/daemons/tyto/tyto.py",
        "execution/daemons/apis/test_routing.py",
        "docs/agents/pavo.md",
        "README.md",
    ):
        panel = select_panel(set(), [path], max_panel=8)
        assert "security" not in [l.lens for l in panel], path


def test_busiest_dispatch_files_are_not_matched_by_path_alone():
    # swarm_dispatch/skill_runner/harness_router ARE token-routing code, but
    # they are also the swarm's busiest files and most edits are routing or
    # bookkeeping. Matching them by path seated the lens on 76% of the last 30
    # merged PRs. They stay covered by the CONTENT patterns when an edit
    # actually touches auth/token/signature concerns; reachability-based
    # selection (neotoma#2174) is the real fix for the rest.
    for path in (
        "execution/daemons/apis/swarm_dispatch.py",
        "execution/daemons/apis/skill_runner.py",
        "execution/daemons/apis/harness_router.py",
    ):
        panel = select_panel(set(), [path], max_panel=8)
        assert "security" not in [l.lens for l in panel], path


# ── Gate-write invariant (ateles#762) ──────────────────────────────────────


def _agent_frontmatter(agent: str) -> dict:
    """Parse the YAML frontmatter of a rendered agent doc.

    The doc is a mirror of the `agent_definition` entity, kept byte-identical
    to Neotoma by `render_agent_docs.py --check`. Asserting against the mirror
    therefore asserts against the live grant, without a network call in tests.
    """
    import re
    from pathlib import Path

    import yaml

    doc = (
        Path(__file__).resolve().parents[3] / "docs" / "agents" / f"{agent}.md"
    )
    text = doc.read_text()
    # A do-not-edit banner precedes the fence, so locate it rather than
    # assuming the file opens with it.
    match = re.search(r"^---$(.*?)^---$", text, re.MULTILINE | re.DOTALL)
    assert match, f"{agent}.md has no frontmatter"
    return yaml.safe_load(match.group(1))


def test_gate_owning_lenses_can_write_the_issue_entity():
    """Every gate a lens is told to sign must be writable by that lens.

    A lens that owns a gate signs off by correcting `gate_status.<gate>` on
    the parent *issue* entity, and gate inheritance reads that entity as the
    source of truth. If `issue` is missing from the agent's
    `operational_entity_types`, the write is denied and the sign-off survives
    only as a PR comment no gate reader consults — the PR then blocks forever
    on a review that was clean (ateles#762, stalling neotoma#2040).

    Deriving the agent list from LENSES rather than hardcoding it is the
    point: a lens added or renamed later is covered automatically. Buteo was
    missing this grant too and was not named in the original filing.
    """
    for lens in LENSES:
        if not lens.gate:
            continue  # non-gating lens signs nothing on the issue
        operational = _agent_frontmatter(lens.agent).get(
            "operational_entity_types"
        ) or []
        assert "issue" in operational, (
            f"{lens.agent} owns the {lens.gate!r} gate and is instructed to "
            f"correct() gate_status on the issue entity, but 'issue' is not in "
            f"its operational_entity_types — the sign-off write will be denied "
            f"and the gate will never advance."
        )


def test_gate_owning_lens_skills_document_the_issue_writeback():
    """The other half of the invariant: the instruction actually exists.

    The test above proves a gate owner *may* write the issue. This one proves
    it is *told* to — so that if a skill ever drops the writeback recipe, the
    grant assertion above does not silently pass over a lens whose sign-off no
    longer reaches the entity at all.

    Cause #2 (ateles#762 / #769): a write without read-back is still a silent
    failure. The Gate handoff recipe must (a) call retrieve_entity_snapshot or
    retrieve_entity_by_identifier after correct(), (b) branch on mismatch to
    **BLOCKED** (never SIGNED_OFF), and (c) show a conditional — not only the
    happy-path correct() → store() sequence.
    """
    from pathlib import Path
    import re

    skills_root = Path(__file__).resolve().parents[3] / ".claude" / "skills"
    for lens in LENSES:
        if not lens.gate:
            continue
        skill = (skills_root / lens.agent / "SKILL.md").read_text()
        assert "issue_entity_id" in skill, (
            f"{lens.agent} owns the {lens.gate!r} gate but its SKILL.md no "
            f"longer references <issue_entity_id> — either restore the gate "
            f"writeback recipe or remove the gate from its lens."
        )
        # Isolate the Gate handoff section (first match) so consultation-protocol
        # retrieve calls elsewhere in the skill cannot satisfy this check.
        m = re.search(
            r"## Gate handoff.*?(?=\n## |\Z)", skill, flags=re.S | re.I
        )
        assert m, (
            f"{lens.agent} owns the {lens.gate!r} gate but has no "
            f"'## Gate handoff' section in SKILL.md"
        )
        handoff = m.group(0)
        assert (
            "retrieve_entity_snapshot" in handoff
            or "retrieve_entity_by_identifier" in handoff
        ), (
            f"{lens.agent} Gate handoff must read-back the issue entity after "
            f"correct() (retrieve_entity_snapshot / retrieve_entity_by_identifier)"
        )
        assert "**BLOCKED**" in handoff, (
            f"{lens.agent} Gate handoff must route read-back failure to "
            f"**BLOCKED**, never to SIGNED_OFF"
        )
        assert re.search(r"\bif\b", handoff), (
            f"{lens.agent} Gate handoff must branch on read-back result "
            f"(conditional), not only sequential correct() → store()"
        )


def test_gate_owning_lenses_have_agent_grant_admission_for_issue():
    """Admission is agent_grant.capabilities, not operational_entity_types.

    Mirrors can list `issue` while live grants still deny the write (ateles#769
    round-1). This sibling asserts a checked-in fixture — derived from LENSES
    with a non-empty gate — admits `issue` on retrieve+correct and never via
    store_structured (pavo's pre-existing pm grant is the documented exception
    for store_structured; it already stores issues as part of PM workflow).

    The fixture is a snapshot, so it can only prove the *shape* of admission
    offline. `execution/scripts/check_gate_lens_grants.py` is the other half:
    it compares this fixture against the live `agent_grant` rows and fails on a
    missing grant, an inactive one, or any capability drift. Keep both — this
    test guards the invariant in CI with no network, the script guards the
    fixture against becoming a comfortable fiction.
    """
    import json
    from pathlib import Path

    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "agent_grants"
        / "gate_lens_capabilities.json"
    )
    fixture = json.loads(fixture_path.read_text())
    by_agent = {entry["agent"]: entry for entry in fixture["lenses"]}

    for lens in LENSES:
        if not lens.gate:
            continue
        entry = by_agent.get(lens.agent)
        assert entry is not None, (
            f"{lens.agent} owns the {lens.gate!r} gate but is missing from "
            f"{fixture_path.name} — add its agent_grant row to the fixture"
        )
        assert entry.get("gate") == lens.gate, (
            f"{lens.agent} fixture gate {entry.get('gate')!r} != LENSES gate "
            f"{lens.gate!r}"
        )
        # entity_id + match_sub are what check_gate_lens_grants.py joins on to
        # reach the live grant. A row missing either is unverifiable against
        # Neotoma — it would pass here forever while the real grant is absent.
        assert entry.get("entity_id"), (
            f"{lens.agent} fixture row has no entity_id — the live grant check "
            f"cannot verify it against Neotoma"
        )
        assert entry.get("match_sub"), (
            f"{lens.agent} fixture row has no match_sub — the live grant check "
            f"cannot resolve its agent_grant in Neotoma"
        )
        caps = {c["op"]: set(c.get("entity_types") or []) for c in entry["capabilities"]}
        retrieve = caps.get("retrieve", set())
        correct = caps.get("correct", set())
        assert "issue" in retrieve or "*" in retrieve, (
            f"{lens.agent} agent_grant {entry.get('entity_id')} must admit "
            f"retrieve on issue (or *); got {sorted(retrieve)}"
        )
        assert "issue" in correct or "*" in correct, (
            f"{lens.agent} agent_grant {entry.get('entity_id')} must admit "
            f"correct on issue (or *); got {sorted(correct)}"
        )
        store_types = caps.get("store_structured", set())
        if lens.agent != "pavo":
            assert "issue" not in store_types, (
                f"{lens.agent} agent_grant must NOT admit store_structured on "
                f"issue (gate writeback is correct-only); got {sorted(store_types)}"
            )
