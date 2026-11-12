"""Tests for review panel selection (neotoma#1640) and the shift-left
expectation pass (ateles#81)."""

from pathlib import Path

import pytest

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


def test_always_on_lenses_survive_a_constrained_broad_diff_panel():
    broad = [
        "execution/mcp/ateles/server.py",
        "docs/guide.md",
        "requirements.txt",
        "src/a.py",
        "src/b.py",
        "src/c.py",
        "src/d.py",
        "src/e.py",
    ]
    for cap in (4, 5, 6):
        seated = [lens.lens for lens in select_panel(set(), broad, max_panel=cap)]
        assert "qa" in seated, f"qa dropped at cap={cap}: {seated}"
        assert "pm" in seated, f"pm dropped at cap={cap}: {seated}"
        assert "security" in seated, f"security dropped at cap={cap}: {seated}"


def test_default_panel_seats_qa_and_content_on_broad_diff():
    broad = [
        "execution/mcp/ateles/server.py",
        "docs/guide.md",
        "requirements.txt",
        "src/a.py",
        "src/b.py",
        "src/c.py",
        "src/d.py",
        "src/e.py",
    ]
    seated = {lens.lens for lens in select_panel(set(), broad)}
    assert {"security", "pm", "qa", "ux", "legal", "content"} <= seated


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


def test_gate_owning_lens_skills_document_the_gate_handoff():
    """Every gate-owning lens is told how its verdict becomes the gate record.

    ateles#795 amended the ateles#762 / #769 recipe: a gate-owning lens no
    longer writes `gate_status` itself and reads it back. It states its
    verdict in the fixed position and the DISPATCHER records the gate via the
    lens-AAuth-signed `IssueGateStore.sign_off` (see `_run_pr_review_panel`
    and the pm gate block in `swarm_dispatch.py`). A lens told to `correct()`
    `gate_status` over the shared daemon bearer would reopen the
    unattributed-write sink Falco found on PR #1181.

    So the Gate handoff section must (a) exist, (b) still reference the issue
    entity it contributes to, (c) forbid the lens's own `gate_status` write,
    and (d) name the **BLOCKED** verdict that leaves the gate pending.
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
            f"handoff or remove the gate from its lens."
        )
        # Isolate the Gate handoff section (first match) so text elsewhere in
        # the skill cannot satisfy this check.
        m = re.search(
            r"## Gate handoff.*?(?=\n## |\Z)", skill, flags=re.S | re.I
        )
        assert m, (
            f"{lens.agent} owns the {lens.gate!r} gate but has no "
            f"'## Gate handoff' section in SKILL.md"
        )
        handoff = m.group(0)
        assert "the dispatcher records the gate" in handoff, (
            f"{lens.agent} Gate handoff must say the dispatcher, not the lens, "
            f"records the gate (ateles#795)"
        )
        assert re.search(r"Do not `correct\(\)` `gate_status`", handoff), (
            f"{lens.agent} Gate handoff must forbid the lens's own "
            f"correct() of gate_status (ateles#795)"
        )
        assert "**BLOCKED**" in handoff, (
            f"{lens.agent} Gate handoff must name **BLOCKED** as a verdict "
            f"that leaves the gate pending"
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


# ── Apis seats every lens the approval tool requires ───────────────────────
#
# `approve_pr_as_app.py --panel path` requires lenses by changed path from the
# repo's `.github/review_scope.json`, read from the PR's base branch. Apis used
# to seat only `select_panel`'s filename-keyword picks, so a change confined to
# `execution/daemons/apis/fly_runner.py` got pm and qa and rolled up to APPROVE
# while the tool required all five. These drive the real
# `SwarmDispatcher._path_required_lenses` (restored from the conftest default)
# against a scripted GitHub, the real scope file, and the tool's real reader.

import asyncio  # noqa: E402
import base64  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "execution" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import review_scope  # noqa: E402
import swarm_dispatch as sd  # noqa: E402
from github_gateway import SwarmTrigger  # noqa: E402

REPO = "owner/repo"
PR_NUMBER = 41
HEAD = "c" * 40
FULL = {"pm", "arch", "ux", "qa", "security"}
REAL_SCOPE = (_REPO_ROOT / ".github" / "review_scope.json").read_text(encoding="utf-8")
FLY_RUNNER = "execution/daemons/apis/fly_runner.py"


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _github(monkeypatch, files, *, scope=REAL_SCOPE, scope_status=200, scope_raises=False):
    """Script GitHub for `_path_required_lenses`: the PR, its base branch's scope
    file, its file list and the repository's default branch."""
    calls = []

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            calls.append((url, params))
            if url.endswith("/contents/.github/review_scope.json"):
                assert (params or {}).get("ref") == "main", "read from the BASE branch"
                if scope_raises:
                    raise ConnectionError("down")
                content = base64.b64encode((scope or "").encode()).decode()
                return _Resp(scope_status, {"type": "file", "encoding": "base64", "content": content})
            if url.endswith(f"/pulls/{PR_NUMBER}/files"):
                return _Resp(200, [{"filename": f} for f in files])
            if url.endswith(f"/repos/{REPO}"):
                return _Resp(200, {"default_branch": "main"})
            return _Resp(404, {})

    async def fetch_pr(self, repo, number):
        return {"number": number, "base": {"ref": "main"}, "head": {"sha": HEAD},
                "changed_files": len(files)}

    monkeypatch.setattr(sd.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_pr", fetch_pr)
    monkeypatch.setattr(sd.SwarmDispatcher, "_github_headers", lambda self, repo=None: {})
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_path_required_lenses", sd._REAL_PATH_REQUIRED_LENSES
    )
    return calls


class _Notifier:
    def send(self, *a, **k):
        pass

    def clear_dedupe(self, key):
        pass


def _dispatcher(**over):
    cfg = dict(neotoma_token="", github_token="", auto_merge=False)
    cfg.update(over)
    return sd.SwarmDispatcher(_Notifier(), sd.DispatchConfig(**cfg))


def _trigger():
    return SwarmTrigger(
        kind="pr_opened", repository=REPO, number=PR_NUMBER, title="t", body="",
        author="someone", html_url="u", delivery_id="d", action="opened",
        head_sha=HEAD, base_ref="main",
    )


def _seated(d, files):
    required = asyncio.run(d._path_required_lenses(_trigger()))
    panel = select_panel(set(), files, d.config.panel_max, required_lenses=required)
    return {lens.lens for lens in panel}, required


def test_a_daemon_only_change_seats_the_full_panel_the_tool_requires(monkeypatch):
    # Red without the change: Apis seated {pm, qa} (`select_panel` alone, below).
    _github(monkeypatch, [FLY_RUNNER])
    d = _dispatcher()
    seated, required = _seated(d, [FLY_RUNNER])
    assert set(required) == FULL
    assert seated == FULL
    assert {lens.lens for lens in select_panel(set(), [FLY_RUNNER])} == {"pm", "qa"}


@pytest.mark.parametrize(
    "case",
    ["missing", "server_error", "read_raises", "not_json", "wrong_version"],
)
def test_no_usable_scope_file_seats_the_full_panel_and_says_why(monkeypatch, caplog, case):
    # A docs-only change, so only the fail-closed rule can make the panel full.
    files = ["docs/notes.md"]
    kwargs = {
        "missing": {"scope_status": 404},
        "server_error": {"scope_status": 500},
        "read_raises": {"scope_raises": True},
        "not_json": {"scope": "{not json"},
        "wrong_version": {"scope": json.dumps({"version": 2, "full_panel_paths": ["a/**"],
                                               "reduced_paths": ["b/**"]})},
    }[case]
    _github(monkeypatch, files, **kwargs)
    d = _dispatcher()
    with caplog.at_level(logging.WARNING, logger="apis"):
        seated, required = _seated(d, files)
    assert set(required) == FULL and FULL <= seated
    lines = [r.getMessage() for r in caplog.records if "scope file unusable" in r.getMessage()]
    assert len(lines) == 1, lines


def test_a_docs_only_change_keeps_todays_seats_plus_the_one_scope_lens(monkeypatch):
    # docs/notes.md is on reduced_paths, not ux_paths: the tool requires arch.
    files = ["docs/notes.md"]
    _github(monkeypatch, files)
    d = _dispatcher()
    today = {lens.lens for lens in select_panel(set(), files, d.config.panel_max)}
    seated, required = _seated(d, files)
    assert set(required) == {"arch"}
    assert "arch" not in today, "red without the change: arch was never seated"
    assert seated == today | {"arch"}
    assert "security" not in seated, "the reduced route does not grow to the full panel"


def test_a_panel_cap_below_the_required_count_keeps_every_required_lens():
    # Red without the change: `select_panel` had no `required_lenses` and capped
    # at two seats, dropping arch, ux and security.
    panel = select_panel(
        set(), [FLY_RUNNER], max_panel=2, pending_gates={"pm"}, required_lenses=FULL
    )
    assert FULL <= {lens.lens for lens in panel}
    capped = select_panel(set(), ["src/x.py", "docs/a.md"], max_panel=1, required_lenses={"arch"})
    assert "arch" in {lens.lens for lens in capped}


def _lens_comment(lens, verdict="**SIGNED_OFF**"):
    agent = sd.lens_by_name(lens).agent
    reply = (
        f"{sd.compose_lens_review_marker(lens, HEAD)}\n"
        f"{sd.attribution_header(agent, f'{lens} lens panelist')}\n{verdict}\n\nlooks fine"
    )
    return {"id": hash(lens) & 0xFFFF, "created_at": "2026-11-06T00:00:01Z",
            "user": {"login": "ateles-agent"}, "body": reply}


def _aggregation():
    return {"id": 999, "created_at": "2026-11-06T00:00:09Z", "user": {"login": "ateles-agent"},
            "body": (f"<!-- vanellus-aggregation commit={HEAD} -->\n"
                     "**🤖 Vanellus — Ateles swarm, PR steward**\n**APPROVE**\n\nBlocking: 0")}


def test_a_panel_missing_only_the_security_verdict_is_not_finished(monkeypatch):
    # Red without the change: the aggregation at the head, or the pm/qa verdicts
    # `select_panel` alone required, read as a finished panel.
    _github(monkeypatch, [FLY_RUNNER])
    d = _dispatcher()
    comments = [_lens_comment(lens) for lens in sorted(FULL - {"security"})]

    async def scoped(self, repository, number, client):
        return state["comments"]

    async def files(self, trigger):
        return [FLY_RUNNER]

    state = {"comments": comments}
    monkeypatch.setattr(sd.SwarmDispatcher, "_lens_scoped_comments", scoped)
    monkeypatch.setattr(sd.SwarmDispatcher, "_changed_files", files)
    assert asyncio.run(d._panel_finished_for_pr(_trigger())) is False
    state["comments"] = comments + [_aggregation()]
    assert asyncio.run(d._panel_finished_for_pr(_trigger())) is False, (
        "an aggregation over a panel that never seated security is not finished"
    )
    state["comments"] = comments + [_lens_comment("security"), _aggregation()]
    assert asyncio.run(d._panel_finished_for_pr(_trigger())) is True


def test_the_missing_lens_sweep_reseats_a_required_lens_the_panel_never_had(monkeypatch):
    # Red without the change: no NOT RECEIVED line, so no candidate at all.
    _github(monkeypatch, [FLY_RUNNER])
    d = _dispatcher()
    comments = [_lens_comment("pm"), _lens_comment("qa"), _aggregation()]
    pr = {"number": PR_NUMBER, "draft": False, "head": {"sha": HEAD, "ref": "f"},
          "base": {"ref": "main"}, "title": "t", "user": {"login": "x"}}

    class _ListClient:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            return _Resp(200, [pr])

    async def scoped(self, repository, number, client):
        return comments

    async def required(self, trigger):
        return frozenset(FULL)

    monkeypatch.setattr(sd.httpx, "AsyncClient", _ListClient)
    monkeypatch.setattr(sd.SwarmDispatcher, "_lens_scoped_comments", scoped)
    monkeypatch.setattr(sd.SwarmDispatcher, "_path_required_lenses", required)
    out = asyncio.run(d._prs_with_missing_lens(REPO))
    assert [(p["number"], sorted(lenses)) for p, lenses in out] == [
        (PR_NUMBER, ["arch", "security", "ux"])
    ]


@pytest.mark.parametrize("lens", ["arch", "security", "ux"])
@pytest.mark.parametrize("path", ["fallback", "returned"])
def test_apis_posted_verdicts_read_as_clear_in_the_approval_tool(lens, path):
    # Red without the change (fallback): the comment carried a plain
    # `review:<lens>` line and a second, dispatcher-written header ahead of the
    # lens's own, and `evaluate_lens` said "no readable verdict at the fixed
    # header/verdict position".
    import approve_pr_as_app as tool

    agent = sd.lens_by_name(lens).agent
    header = sd.attribution_header(agent, f"{lens} lens panelist")
    review = f"{header}\n**SIGNED_OFF**\n\nNo blocking findings."
    if path == "fallback":
        reply = f"{sd.compose_lens_review_marker(lens, HEAD)}\n{review}"
        body = sd.compose_fallback_comment(lens, agent, reply, HEAD)
        assert f"\nreview:{lens}\n" not in body
    else:
        body = sd.verified_remote_comment(lens, agent, review, HEAD)
        assert body is not None
    comment = {"id": 1, "created_at": "2026-11-06T00:00:01Z",
               "user": {"login": "ateles-agent"}, "body": body, "html_url": "u"}
    outcome = asyncio.run(tool.evaluate_lens(
        None, repo=REPO, pr=PR_NUMBER, head_sha=HEAD, comments=[comment],
        lens=lens, authors=frozenset({"ateles-agent"}),
    ))
    assert outcome.passed is True, outcome.reason
    assert outcome.verdict == "signed_off"
    # The Apis-side reader agrees, so the finished-panel check counts it too.
    assert sd.posted_comment_own_verdict(body, lens_agent=agent) == "signed_off"


def test_a_fallback_for_an_unreadable_reply_still_reads_as_no_verdict():
    # The new shape is used only when it reads exactly as the reply did: a reply
    # with no verdict at the fixed position never becomes a pass.
    import approve_pr_as_app as tool

    body = sd.compose_fallback_comment("arch", "waxwing", "Looks fine to me. **SIGNED_OFF**", HEAD)
    comment = {"id": 1, "created_at": "x", "user": {"login": "ateles-agent"}, "body": body}
    outcome = asyncio.run(tool.evaluate_lens(
        None, repo=REPO, pr=PR_NUMBER, head_sha=HEAD, comments=[comment],
        lens="arch", authors=frozenset({"ateles-agent"}),
    ))
    assert outcome.passed is False


def test_the_approval_tool_and_apis_share_the_one_scope_reader():
    import approve_pr_as_app as tool
    import inspect

    assert tool.BOOTSTRAP_PANEL_LENSES == review_scope.FULL_PANEL_LENSES
    assert "review_scope.fetch_scope" in inspect.getsource(tool.fetch_review_scope)
    assert "review_scope.path_panel_for_pr" in inspect.getsource(tool.run)
    src = inspect.getsource(sd._REAL_PATH_REQUIRED_LENSES)
    assert "review_scope.fetch_scope" in src and "review_scope.path_panel_for_pr" in src


# ── Apis requires the approval tool's floor as well as the path decision ───
#
# The tool's `--panel path` requires, on a full-panel decision, the five core
# lenses AND its diff-derived floor (`review_panel.approval_floor`): `legal`
# for a `LICENSE` or `package.json` change, a pre-registered contributor's
# lens. Apis used to require only the core five, so at `APIS_PANEL_MAX=3` a
# `LICENSE` change seated pm/arch/ux/qa/security and capped legal away, and
# finished detection and the missing-lens sweep read that same short set.

CORE_AND_LEGAL = FULL | {"legal"}
PARENT = 7


def _scripted_client(files, *, scope=REAL_SCOPE, issue_comments=(), open_prs=()):
    """A GitHub stand-in for both Apis and the approval tool: the base-branch
    scope file, the PR's file list, the default branch, the parent issue's
    comments, and the open-PR list."""

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            if url.endswith("/contents/.github/review_scope.json"):
                content = base64.b64encode(scope.encode()).decode()
                return _Resp(200, {"type": "file", "encoding": "base64", "content": content})
            if url.endswith(f"/pulls/{PR_NUMBER}/files"):
                return _Resp(200, [{"filename": f} for f in files])
            if url.endswith(f"/issues/{PARENT}/comments"):
                return _Resp(200, [{"body": b} for b in issue_comments])
            if url.endswith(f"/repos/{REPO}/pulls"):
                return _Resp(200, list(open_prs))
            if url.endswith(f"/repos/{REPO}"):
                return _Resp(200, {"default_branch": "main"})
            return _Resp(404, {})

    return _Client


def _pr_data(files, body=""):
    return {"number": PR_NUMBER, "base": {"ref": "main"}, "head": {"sha": HEAD},
            "changed_files": len(files), "body": body}


def _script_apis(monkeypatch, files, *, body="", issue_comments=(), open_prs=()):
    client = _scripted_client(files, issue_comments=issue_comments, open_prs=open_prs)

    async def fetch_pr(self, repo, number):
        return _pr_data(files, body)

    monkeypatch.setattr(sd.httpx, "AsyncClient", client)
    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_pr", fetch_pr)
    monkeypatch.setattr(sd.SwarmDispatcher, "_github_headers", lambda self, repo=None: {})
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_path_required_lenses", sd._REAL_PATH_REQUIRED_LENSES
    )
    return client


def _tool_required(monkeypatch, files, *, body="", issue_comments=(), panel_max=6):
    """The lens set `approve_pr_as_app.py --panel path` requires, from the
    tool's own code against the same scripted GitHub."""
    import approve_pr_as_app as tool

    monkeypatch.setenv("APIS_PANEL_MAX", str(panel_max))
    monkeypatch.setattr(tool, "_github_headers", lambda repo=None: {})
    client = _scripted_client(files, issue_comments=issue_comments)()

    async def go():
        load = await tool.fetch_review_scope(client, REPO, "main")
        decision = await review_scope.path_panel_for_pr(
            client, api_base=tool.GITHUB_API, repo=REPO, pr=PR_NUMBER,
            pr_data=_pr_data(files, body), headers={}, load=load,
        )
        lenses, _required, _excluded = await tool.resolve_path_lenses(
            client, repo=REPO, pr=PR_NUMBER, pr_body=body, extra_lenses=[],
            decision=decision,
        )
        return set(lenses)

    return asyncio.run(go())


def test_a_license_change_at_panel_max_3_seats_the_core_five_and_legal(monkeypatch):
    # Red without the correction: required was the core five, and `select_panel`
    # at max 3 ranked legal after them and capped it away:
    # seated == {pm, arch, ux, qa, security}.
    _script_apis(monkeypatch, ["LICENSE"])
    d = _dispatcher(panel_max=3)
    seated, required = _seated(d, ["LICENSE"])
    assert set(required) == CORE_AND_LEGAL
    assert seated == CORE_AND_LEGAL


def test_a_package_json_change_seats_legal_with_the_core_five(monkeypatch):
    # Red without the correction: required == the core five, without legal.
    _script_apis(monkeypatch, ["package.json"])
    d = _dispatcher(panel_max=3)
    seated, required = _seated(d, ["package.json"])
    assert set(required) == CORE_AND_LEGAL
    assert CORE_AND_LEGAL <= seated


@pytest.mark.parametrize("panel_max", [6, 3])
@pytest.mark.parametrize(
    "files,body,issue_comments",
    [
        pytest.param([FLY_RUNNER], "", (), id="code"),
        pytest.param(["docs/notes.md"], "", (), id="docs-only"),
        pytest.param(["LICENSE"], "", (), id="license"),
        pytest.param(["package.json"], "", (), id="package-json"),
        pytest.param(
            ["docs/notes.md", "package.json", FLY_RUNNER, "LICENSE"], "", (), id="mixed"
        ),
        pytest.param(
            [f"execution/daemons/apis/m{i}.py" for i in range(6)], "", (), id="six-files"
        ),
        pytest.param(
            [FLY_RUNNER],
            f"Closes #{PARENT}",
            ("**review_expectation (legal)** — what buteo will verify",),
            id="preregistered-contributor",
        ),
    ],
)
def test_apis_required_set_equals_the_approval_tools(
    monkeypatch, files, body, issue_comments, panel_max
):
    # Red without the correction: license, package-json, mixed and
    # preregistered-contributor differed by `legal` (Apis: the core five only).
    _script_apis(monkeypatch, files, body=body, issue_comments=issue_comments)
    d = _dispatcher(panel_max=panel_max)
    trigger = _trigger()
    apis = set(asyncio.run(d._path_required_lenses(trigger)))
    tool = _tool_required(
        monkeypatch, files, body=body, issue_comments=issue_comments, panel_max=panel_max
    )
    assert apis == tool
    seated = {
        lens.lens
        for lens in select_panel(set(), files, panel_max, required_lenses=apis)
    }
    assert tool <= seated, "no required lens is capped away"


def test_core_verdicts_without_legal_are_not_finished_and_recovery_seats_legal(
    monkeypatch,
):
    # Red without the correction: legal was not required, so the five core
    # verdicts read as a finished panel and the sweep found nothing to re-run.
    comments = [_lens_comment(lens) for lens in sorted(FULL)]
    pr = {"number": PR_NUMBER, "draft": False, "head": {"sha": HEAD, "ref": "f"},
          "base": {"ref": "main"}, "title": "t", "user": {"login": "x"}, "body": ""}
    _script_apis(monkeypatch, ["LICENSE"], open_prs=[pr])
    d = _dispatcher(panel_max=3)
    state = {"comments": comments}

    async def scoped(self, repository, number, client):
        return state["comments"]

    async def files(self, trigger):
        return ["LICENSE"]

    monkeypatch.setattr(sd.SwarmDispatcher, "_lens_scoped_comments", scoped)
    monkeypatch.setattr(sd.SwarmDispatcher, "_changed_files", files)
    assert asyncio.run(d._panel_finished_for_pr(_trigger())) is False
    state["comments"] = comments + [_aggregation()]
    assert asyncio.run(d._panel_finished_for_pr(_trigger())) is False
    out = asyncio.run(d._prs_with_missing_lens(REPO))
    assert [(p["number"], lenses) for p, lenses in out] == [(PR_NUMBER, ["legal"])]
    state["comments"] = comments + [_lens_comment("legal"), _aggregation()]
    assert asyncio.run(d._panel_finished_for_pr(_trigger())) is True


def test_an_unreadable_required_set_seats_every_requirable_lens(monkeypatch):
    # Red without the correction: the fallback was the core five, without legal.
    async def fetch_pr(self, repo, number):
        return None

    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_pr", fetch_pr)
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_path_required_lenses", sd._REAL_PATH_REQUIRED_LENSES
    )
    required = asyncio.run(_dispatcher()._path_required_lenses(_trigger()))
    assert set(required) == CORE_AND_LEGAL
    assert "content" not in required


def test_the_approval_tool_and_apis_share_the_floor_and_contributor_reader():
    import approve_pr_as_app as tool
    import inspect

    import review_panel

    assert "review_panel.approval_floor" in inspect.getsource(tool.derive_required_lenses)
    assert "review_panel.fetch_gate_contributors" in inspect.getsource(
        tool._preregistered_gate_contributors
    )
    assert tool.BOOTSTRAP_EXCLUDED_LENSES is review_panel.APPROVAL_EXCLUDED_LENSES
    assert tool._EXPECTATION_MARKER_RE is review_panel._EXPECTATION_MARKER_RE
    # The shared marker reader matches what Apis posts.
    assert f"**{sd.EXPECTATION_MARKER} (" in "**review_expectation (legal)**"
    assert review_panel.gate_contributors_from_comments(
        [f"**{sd.EXPECTATION_MARKER} (legal)** — what Buteo will verify"]
    ) == {"buteo"}
    src = inspect.getsource(sd._REAL_PATH_REQUIRED_LENSES)
    assert "review_panel.approval_required_lenses" in src


# ── Security concerns end proven, disproven, or unsettled ───────────────

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _normalized(text):
    return " ".join(text.split())


def test_apis_security_brief_requires_the_three_concern_outcomes():
    security = next(lens for lens in LENSES if lens.lens == "security")
    brief = _normalized(security.checks)
    assert "Every concern must end in one of three states before your verdict" in brief
    # Proven: a closed code path counts as proof, and it blocks.
    assert "traced through a fully closed code path" in brief
    assert "is CONFIRMED: block on it" in brief
    # Disproven: dropped from the findings, with the checking shown.
    assert (
        "A concern you checked and refuted is dropped from the findings and "
        "listed with what you checked" in brief
    )
    # Unsettled: only after trying, as a COMMENT saying what would settle it.
    assert "tried and truly cannot settle" in brief
    assert "give a COMMENT verdict" in brief
    assert "what would settle it" in brief
    # The earlier rule let an argued break pass with no verdict consequence.
    assert "PLAUSIBLE and do not block" not in brief


@pytest.mark.parametrize(
    "document", ["docs/agents/falco.md", ".claude/skills/falco/SKILL.md"]
)
def test_falco_prompt_mirrors_map_each_outcome_to_its_verdict(document):
    text = _normalized((_REPO_ROOT / document).read_text(encoding="utf-8"))
    assert "must end in one of three states" in text
    assert "A CONFIRMED break is `[BLOCKING]`, and your verdict is `REQUEST_CHANGES`" in text
    assert "list it under **Checked and disproven**" in text
    assert "It doesn't stand in the way of `APPROVE`" in text
    assert "A PLAUSIBLE finding is marked `[NON-BLOCKING]`, and your verdict is `COMMENT`" in text
    assert "A `COMMENT` still holds the merge" in text
    assert (
        "`REQUEST_CHANGES` on any CONFIRMED break; otherwise `COMMENT` only if "
        "a concern truly can't be settled" in text
    )


def test_fixture_grants_admit_the_fly_result_write_for_every_fly_role():
    """Fly roles record `fly_runner_result` as themselves; a grant without
    store_structured on it gets HTTP 403 and the run reads as lost."""
    import json

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import check_gate_lens_grants as chk

    fixture = json.loads(chk.FIXTURE_PATH.read_text())
    caps = {
        e["agent"]: {c["op"]: sorted(c.get("entity_types") or []) for c in e["capabilities"]}
        for e in fixture["lenses"]
    }
    assert chk.fly_result_problems(caps) == []

    # A Fly role without it is flagged, and so is one with no grant at all.
    broken = {a: {op: [t for t in ts if t != "fly_runner_result"] for op, ts in c.items()}
              for a, c in caps.items()}
    problems = chk.fly_result_problems(broken)
    assert len(problems) == len(chk.FLY_ROLES)
    assert all("fly_runner_result" in p and "403" in p for p in problems)
    assert any("no agent_grant" in p for p in chk.fly_result_problems({}))
