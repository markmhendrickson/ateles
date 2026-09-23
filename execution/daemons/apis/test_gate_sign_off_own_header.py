"""Sign-off round answering PR #1181's reviews at bf97b1a4.

One class per item, each written against the defect's exact shape and run RED
against bf97b1a4 before the fix:

1. ``TestVerdictCountsOnlyFromTheLensHeader`` — both security runs, BLOCKING.
   With no attribution header, a single quoted or copied clear verdict line
   (a blockquote, a fence, the spec section's copy of the issue body, the
   contract's own worked example) counted as the lens's own verdict. The gate
   prompts, including the pm prompts that asked for a "plain comment", now
   ask for the header and verdict line.
2. ``TestAFailedReadIsNotAnAbsentEntity`` — second security run. `load()`
   turns a transport failure into an empty state with `found == False`, so the
   pre-panel re-proof returned nothing and seating failed open.
3. ``TestARevertedGateIsSurfaced`` — ux review, REQUEST_CHANGES. A later run
   that walks an unproven `signed_off` back to pending said so only in the log.
4. ``TestQaAndLegalAreSigned`` — known gap. The dispatcher signed only the
   gates Lanius reported pending, so `qa` and `legal` could never clear once
   the lens's own `correct` was denied.
5. ``TestPreDeploySignOffsByServerTime`` — the operator's default decision:
   an unsigned `signed_off` counts only when Neotoma ingested it before
   `APIS_GATE_SIGNING_CUTOFF`, judged by the server's `created_at`.

Plus ``TestLoadReadsEnvelopeProvenance``: the live prod query row carries the
reducer's per-field provenance on the envelope, not inside `snapshot`.

Run: pytest execution/daemons/apis/test_gate_sign_off_own_header.py -v
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import gate_waive
import skill_runner
import swarm_dispatch
from gate_waive import GATE_STATUS_FIELD, IssueGateState, IssueGateStore
from skill_runner import SkillResult
from swarm_dispatch import SwarmDispatcher

import test_swarm_dispatch as tsd
from test_gate_sign_off_fail_closed import LENS_TP, _Record, _wire

FENCE = "```"
PAVO = "**🤖 Pavo — Ateles swarm, pm gate owner**"
HEAD = "b" * 40


@pytest.fixture(autouse=True)
def _no_ambient_github_token(monkeypatch):
    """No test here may reach real GitHub through a token in the shell: a
    test that needs one sets it explicitly."""
    for name in ("ATELES_AGENT_PAT", "NEOTOMA_AGENT_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("APIS_GATE_SIGNING_CUTOFF", raising=False)


def _run(coro):
    return asyncio.run(coro)


def _warranted(stdout: str, lens: str = "pavo") -> bool:
    return swarm_dispatch.sign_off_is_warranted(stdout, lens_agent=lens)


# ── 1. A verdict counts only from the lens's own header ─────────────────────


class TestVerdictCountsOnlyFromTheLensHeader:
    # Every input the two security runs at bf97b1a4 cited, verbatim in shape.
    @pytest.mark.parametrize(
        "stdout",
        [
            # dispatcher run: lone blockquoted clear verdict, objection in prose
            "> **APPROVE**\n\nThe migration still drops the index; I do not pass this.",
            # dispatcher run: fully blockquoted prior header + APPROVE, prose objection
            f"> {PAVO}\n> **APPROVE**\n\nScope has changed since that round; not passing.",
            # dispatcher run: own header, blockquoted-only APPROVE, prose objection
            f"{PAVO}\n> **APPROVE**\n\nNot passing this round.",
            # second run: fenced SIGNED_OFF, then the lens's own off-format COMMENT
            f"{FENCE}\n**SIGNED_OFF**\n{FENCE}\n\n"
            "Verdict: COMMENT — observations only; I am not signing the ux gate this round",
            # second run: pm spec section copying an issue-body **APPROVE**, prose decline
            "<<<SPEC_SECTION>>>\n## Acceptance criteria\n- [ ] endpoints listed\n"
            "**APPROVE**\n<<<END_SPEC_SECTION>>>\n\n"
            "The scope is not yet adequately bounded, so I asked the author to "
            "name the affected endpoints before I can pass it.",
            # second run: a header missing the comma after "Ateles swarm"
            "**🤖 Pavo — Ateles swarm pm gate owner**\n**APPROVE**\n\nlooks fine",
            # second run: the contract's own worked example, quoted in a fence
            f"{FENCE}\n{PAVO}\n**APPROVE**\n\n- [x] Acceptance criteria met\n{FENCE}\n\n"
            "Following that example is not enough here; scope is unclear.",
        ],
        ids=[
            "sole-blockquoted-approve",
            "blockquoted-header-and-approve",
            "own-header-blockquoted-approve",
            "fenced-signed-off-then-verdict-comment",
            "spec-section-copied-approve",
            "header-missing-comma",
            "quoted-worked-example",
        ],
    )
    def test_cited_inputs_do_not_clear(self, stdout):
        assert _warranted(stdout) is False

    @pytest.mark.parametrize(
        "own_line",
        [
            "**COMMENT** — observations only",
            "Verdict: COMMENT",
            "__COMMENT__",
            "**COMMENT.**",
            "### COMMENT",
            "** COMMENT **",
        ],
    )
    def test_an_off_format_second_verdict_is_refused(self, own_line):
        """Six off-format own verdicts after a clear verdict line: two
        verdicts, so no single own verdict."""
        stdout = f"{PAVO}\n**APPROVE**\n\nnotes\n\n{own_line}"
        assert _warranted(stdout) is False

    def test_the_contract_worked_example_quoted_verbatim_does_not_clear(self):
        """Taken from the injected contract itself, fences and all."""
        contract = skill_runner.SWARM_GITHUB_CONTRACT
        start = contract.index("### Worked example")
        fenced = contract[start:].split("### Checklists")[0]
        assert "**APPROVE**" in fenced
        assert _warranted(fenced + "\nI decline to pass this issue.") is False

    def test_headerless_single_verdict_no_longer_clears(self):
        assert _warranted("**SIGNED_OFF**\nno concerns") is False

    def test_own_header_and_verdict_clear(self):
        assert _warranted(f"{PAVO}\n**SIGNED_OFF**\n\n- [x] scoped") is True

    def test_own_header_after_the_spec_and_design_basis_sections_clears(self):
        stdout = (
            "<<<SPEC_SECTION>>>\n## PM scope\n- in scope: x\n<<<END_SPEC_SECTION>>>\n\n"
            "<<<DESIGN_BASIS>>>\nDesign basis: docs/foundation/x.md#y — z\n"
            "<<<END_DESIGN_BASIS>>>\n\n"
            f"{PAVO}\n**SIGNED_OFF**\n\npm gate passes: scope bounded"
        )
        assert _warranted(stdout) is True

    def test_panel_comment_shape_clears(self):
        stdout = (
            f"<!-- review:qa commit={HEAD} -->\nreview:qa\n"
            "**🤖 Phoenicurus — Ateles swarm, qa lens panelist**\n**SIGNED_OFF**\n\n"
            "eval green"
        )
        assert _warranted(stdout, "phoenicurus") is True

    def test_prompts_ask_for_the_header_and_verdict_line(self, monkeypatch):
        """Every prompt that asks a gate-owning lens for its verdict."""
        monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
        instruction = getattr(swarm_dispatch, "gate_verdict_instruction", None)
        assert instruction is not None, "no shared gate-verdict instruction"
        issue = tsd._trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
        pm_section = next(s for s in swarm_dispatch.SECTIONS if s.lens == "pm")
        pavo = SwarmDispatcher._pavo_prompt(issue)
        spec = SwarmDispatcher._spec_section_prompt(issue, pm_section, "")
        for prompt in (pavo, spec):
            assert "PLAIN GitHub comment" not in prompt
            assert instruction("pavo", "pm gate owner") in prompt
            assert "**SIGNED_OFF**" in prompt
        for lens_name in ("pm", "ux", "arch", "qa", "legal"):
            lens = swarm_dispatch.lens_by_name(lens_name)
            prompt = SwarmDispatcher._panelist_prompt(
                tsd._trigger(), lens, "", 80, reviewed_head=HEAD
            )
            assert instruction(lens.agent, f"{lens.lens} lens panelist") in prompt, lens_name

    def test_the_provisioned_account_is_still_asked_for_the_header(self, monkeypatch):
        monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo")
        prompt = SwarmDispatcher._pavo_prompt(
            tsd._trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
        )
        assert swarm_dispatch.attribution_header("pavo", "pm gate owner") in prompt
        assert "even if you post from your own GitHub account" in prompt

    def test_the_contract_says_gate_verdicts_come_from_the_header(self):
        contract = skill_runner.SWARM_GITHUB_CONTRACT
        assert "Gate verdicts are read from your header only" in contract


# ── 2. A failed read is not an absent entity ────────────────────────────────


class _Refusing:
    """httpx.AsyncClient whose every request fails at the transport."""

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise httpx.ConnectError("connection refused")

    async def get(self, *a, **k):
        raise httpx.ConnectError("connection refused")


def _failing_store(monkeypatch) -> IssueGateStore:
    monkeypatch.setattr(httpx, "AsyncClient", _Refusing)
    return IssueGateStore("https://neotoma.invalid", "tok")


class TestAFailedReadIsNotAnAbsentEntity:
    def test_load_marks_a_transport_failure_as_a_failed_read(self, monkeypatch):
        """The REAL failure shape: `_post` catches the transport error and
        returns None; `load` must not report that as a missing entity."""
        state = _run(_failing_store(monkeypatch).load("o/r", 795))
        assert state.found is False
        assert getattr(state, "read_failed", False) is True

    def test_load_reports_a_genuinely_absent_entity_as_read_ok(self, monkeypatch):
        store = IssueGateStore("https://neotoma.invalid", "tok")

        async def empty(path, payload):
            return {"entities": []}

        monkeypatch.setattr(store, "_post", empty)
        state = _run(store.load("o/r", 795))
        assert state.found is False
        assert getattr(state, "read_failed", False) is False

    def test_an_exhausted_fallback_scan_is_a_failed_read(self, monkeypatch):
        store = IssueGateStore("https://neotoma.invalid", "tok")
        store._MAX_SCAN_PAGES = 2

        async def paged(path, payload):
            if "snapshot_filters" in payload:
                return {"entities": []}
            return {"entities": [{"snapshot": {"repo": "o/r", "number": 1}}],
                    "next_cursor": "more"}

        monkeypatch.setattr(store, "_post", paged)
        state = _run(store.load("o/r", 795))
        assert getattr(state, "read_failed", False) is True

    def test_pre_panel_reproof_holds_every_gate_on_the_real_failure_shape(
        self, monkeypatch
    ):
        _failing_store(monkeypatch)
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config(neotoma_token="tok"))
        assert _run(d._unverified_signed_off_gates("o/r", 795)) == set(
            swarm_dispatch.PRE_IMPL_GATES
        )

    def test_pre_panel_reproof_is_empty_for_an_absent_entity(self, monkeypatch):
        async def empty(self, path, payload):
            return {"entities": []}

        monkeypatch.setattr(IssueGateStore, "_post", empty)
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config(neotoma_token="tok"))
        assert _run(d._unverified_signed_off_gates("o/r", 795)) == set()

    def test_decision_time_reread_holds_every_gate_on_a_failed_read(self, monkeypatch):
        _failing_store(monkeypatch)
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config(neotoma_token="tok"))
        result = _run(d._refresh_pending_gates("o/r", 795, {"arch"}))
        assert result == set(swarm_dispatch.PRE_IMPL_GATES)

    def test_a_failed_first_read_seats_the_owner_and_holds_the_gate_for_merge(
        self, monkeypatch
    ):
        """The second run's `_handle_pr` probe: Lanius clears, `ux` reads
        `signed_off` from a guest-tier write, and the FIRST read fails with
        the real shape (`_post` -> None). The ux owner must be seated and `ux`
        must reach merge authorization as pending."""
        seen: dict = {}
        captured: dict = {}
        reads = {"n": 0}

        class _Guest:
            found = True
            read_failed = False
            gate_status_unreadable = False
            gate_status = {"pm": "waived", "ux": "signed_off", "arch": "waived"}

        real_load = IssueGateStore.load

        async def load(self, repo, issue_number):
            reads["n"] += 1
            if reads["n"] == 1:
                async def none(path, payload):
                    return None

                self._post = none
                return await real_load(self, repo, issue_number)
            return _Guest()

        async def guest_unproven(self, state, owners):
            return {"ux"} & set(owners)

        async def no_surface(self, *a, **k):
            return None

        monkeypatch.setattr(IssueGateStore, "load", load)
        monkeypatch.setattr(IssueGateStore, "unverified_signed_off_gates", guest_unproven)
        monkeypatch.setattr(
            SwarmDispatcher, "_surface_unverified_signed_off_gates", no_surface,
            raising=False,
        )
        d = tsd._pr_dispatcher_with_stubs(
            monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[]
        )
        original = SwarmDispatcher._vanellus_prompt

        def spy(trigger, parent, lenses, reviews=None, pending_gates=None, reviewed_head=None):
            captured["pending"] = set(pending_gates or ())
            return original(trigger, parent, lenses, reviews,
                            pending_gates=pending_gates, reviewed_head=reviewed_head)

        async def fake_run_skill(skill, prompt, **kwargs):
            seen[skill] = True
            if skill == "lanius":
                return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
            return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        monkeypatch.setattr(SwarmDispatcher, "_vanellus_prompt", staticmethod(spy))
        _run(d._handle_pr(tsd._trigger(body="Closes #80.")))

        assert "accipiter" in seen, "the ux owner was not seated after a failed read"
        assert "ux" in captured.get("pending", set())


# ── 3. A reverted gate is surfaced on the PR ────────────────────────────────


class _Recording:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.posted: list[dict] = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        rows = self.existing

        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return rows

        return _R()

    async def post(self, url, json=None, headers=None):
        self.posted.append(json)
        self.existing.insert(0, json)

        class _R:
            def raise_for_status(self):
                pass

        return _R()


def _surface(d):
    method = getattr(d, "_surface_unverified_signed_off_gates", None)
    assert method is not None, "nothing surfaces a reverted signed_off gate"
    return method


class TestARevertedGateIsSurfaced:
    def test_posts_the_design_blocked_template(self, monkeypatch):
        client = _Recording()
        monkeypatch.setattr(httpx, "AsyncClient", client)
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())

        _run(_surface(d)(tsd._trigger(), 80, {"ux"}, HEAD))

        assert len(client.posted) == 1
        body = client.posted[0]["body"]
        assert "**BLOCKED**" in body
        assert "- reason: `gate_cleared_unverified`" in body
        assert "- gate: `ux` (lens: `accipiter`)" in body
        assert "attempted:" in body
        assert "reads `signed_off`" in body  # the gate READ cleared
        assert "Why it is not trusted" in body
        assert swarm_dispatch.sign_off_next_action("gate_cleared_unverified") in body
        assert "APIS_GATE_SIGNING_CUTOFF" in body

    def test_once_per_gate_per_head(self, monkeypatch):
        client = _Recording()
        monkeypatch.setattr(httpx, "AsyncClient", client)
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())
        surface = _surface(d)

        _run(surface(tsd._trigger(), 80, {"ux"}, HEAD))
        _run(surface(tsd._trigger(), 80, {"ux"}, HEAD))  # re-check, same head
        assert len(client.posted) == 1

        _run(surface(tsd._trigger(), 80, {"ux", "arch"}, HEAD))  # a new gate
        assert len(client.posted) == 2
        assert "gate: `arch`" in client.posted[1]["body"]
        assert "gate: `ux`" not in client.posted[1]["body"]

        _run(surface(tsd._trigger(), 80, {"ux"}, "c" * 40))  # a new head
        assert len(client.posted) == 3

    def _recorder(self, monkeypatch) -> list:
        calls: list = []

        async def record(self, trigger, parent, gates, head):
            calls.append((parent, set(gates), head))

        monkeypatch.setattr(
            SwarmDispatcher, "_surface_unverified_signed_off_gates", record,
            raising=False,
        )
        return calls

    def _over(self, monkeypatch, rec: _Record) -> SwarmDispatcher:
        _wire(monkeypatch, rec)

        async def load(self, repo, issue_number):
            return await rec.load(repo, issue_number)

        async def observations(self, entity_id, *, limit=100):
            return list(rec.observations)

        monkeypatch.setattr(IssueGateStore, "load", load)
        monkeypatch.setattr(IssueGateStore, "_observations", observations)
        return SwarmDispatcher(tsd._StubNotifier(), tsd._config())

    def _bearer_signed(self) -> _Record:
        rec = _Record({"pm": "waived", "ux": "pending", "arch": "waived"})
        rec.apply(
            {"field": GATE_STATUS_FIELD,
             "value": {"pm": "waived", "ux": "signed_off", "arch": "waived"}},
            "apis@ateles-swarm",
        )
        return rec

    def test_pre_panel_call_site_surfaces(self, monkeypatch):
        calls = self._recorder(monkeypatch)
        rec = self._bearer_signed()
        self._over(monkeypatch, rec)
        d = tsd._pr_dispatcher_with_stubs(
            monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[]
        )
        _run(d._handle_pr(tsd._trigger(body="Closes #80.", head_sha=HEAD)))
        assert (80, {"ux"}, HEAD) in calls

    def test_decision_time_call_site_surfaces(self, monkeypatch):
        calls = self._recorder(monkeypatch)
        d = self._over(monkeypatch, self._bearer_signed())
        try:
            result = _run(d._refresh_pending_gates(
                "o/r", 795, {"ux"}, trigger=tsd._trigger(), head=HEAD
            ))
        except TypeError as exc:
            pytest.fail(f"the decision-time re-read cannot surface a revert: {exc}")
        assert result == {"ux"}
        assert calls == [(795, {"ux"}, HEAD)]

    def test_issue_pipeline_call_site_surfaces(self, monkeypatch):
        calls = self._recorder(monkeypatch)
        d = self._over(monkeypatch, self._bearer_signed())

        class _Lanius:
            ok = True
            stdout = "GATE_INHERITANCE: clear"

        issue = tsd._trigger(kind="issue_opened", number=795)
        try:
            green = _run(d._gates_green(_Lanius(), "o/r", 795, trigger=issue, head=HEAD))
        except TypeError as exc:
            pytest.fail(f"the build hand-off check cannot surface a revert: {exc}")
        assert green is False
        assert calls == [(795, {"ux"}, HEAD)]

    def test_a_failed_read_surfaces_nothing(self, monkeypatch):
        calls = self._recorder(monkeypatch)
        _failing_store(monkeypatch)
        d = tsd._pr_dispatcher_with_stubs(
            monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[]
        )
        d.config.neotoma_token = "tok"
        _run(d._handle_pr(tsd._trigger(body="Closes #80.", head_sha=HEAD)))
        assert calls == [], "an unknown read must not claim a gate read cleared"


# ── 4. qa and legal are signed from the live record ─────────────────────────


class TestQaAndLegalAreSigned:
    def test_live_record_decides(self):
        awaits = getattr(swarm_dispatch, "gate_awaits_sign_off", None)
        assert awaits is not None, "which gates to sign is still Lanius's report"
        live = {"pm": "signed_off", "qa": "pending", "legal": "not_required"}
        assert awaits("qa", live) is True
        assert awaits("legal", live) is False
        assert awaits("pm", live) is False
        assert awaits("pm", live, unproven={"pm"}) is True
        assert awaits("arch", live) is True  # absent pre-impl gate reads pending
        assert awaits("legal", {"pm": "pending"}) is False  # absent, not in workflow
        assert awaits("qa", None) is False  # unknown record: sign nothing
        assert awaits("", live) is False

    @pytest.mark.parametrize(
        "lens_name,agent", [("qa", "phoenicurus"), ("legal", "buteo")]
    )
    def test_a_clean_verdict_signs_the_gate_with_that_lens_key(
        self, monkeypatch, lens_name, agent
    ):
        """Lanius reports nothing pending (it never reports qa/legal). The live
        record reads the gate pending; the lens's clean verdict must be
        signed, with that lens's own subject, by the real `sign_off`."""
        rec = _Record(
            {"pm": "waived", "ux": "waived", "arch": "waived",
             "qa": "pending", "legal": "pending"}
        )
        _wire(monkeypatch, rec)

        async def load(self, repo, issue_number):
            return await rec.load(repo, issue_number)

        async def observations(self, entity_id, *, limit=100):
            return list(rec.observations)

        async def no_worktree(*a, **k):
            return None

        monkeypatch.setattr(IssueGateStore, "load", load)
        monkeypatch.setattr(IssueGateStore, "_observations", observations)
        monkeypatch.setattr(swarm_dispatch, "prepare_pr_worktree", no_worktree)
        monkeypatch.setattr(swarm_dispatch, "cleanup_pr_worktree", no_worktree)
        lens = swarm_dispatch.lens_by_name(lens_name)
        monkeypatch.setattr(swarm_dispatch, "select_panel", lambda **kw: [lens])
        d = tsd._pr_dispatcher_with_stubs(
            monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[]
        )

        async def fake_run_skill(skill, prompt, **kwargs):
            if skill == "lanius":
                return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
            if skill == agent:
                return SkillResult(
                    skill, True, 0,
                    f"<!-- review:{lens_name} commit={HEAD} -->\nreview:{lens_name}\n"
                    f"**🤖 {agent.capitalize()} — Ateles swarm, {lens_name} lens panelist**\n"
                    "**SIGNED_OFF**\n\nno concerns",
                    "",
                )
            return SkillResult(skill, True, 0, "**APPROVE**\nlgtm", "")

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        monkeypatch.setattr(
            SwarmDispatcher, "_pr_head_sha", lambda self, t: tsd._async_return(HEAD)
        )
        _run(d._handle_pr(tsd._trigger(body="Closes #80.", head_sha=HEAD)))

        assert rec.gate_status[lens_name] == "signed_off", (
            f"a clean {agent} verdict did not sign the {lens_name} gate"
        )
        gate_subs = [sub for _value, sub in rec.gate_writes()]
        assert gate_subs == [f"{agent}@ateles-swarm"], gate_subs


# ── 5. Pre-deploy sign-offs count by SERVER ingestion time ──────────────────

EARLY = "2026-09-01T00:00:00+00:00"
LATE = "2026-10-01T00:00:00+00:00"
CUTOFF = "2026-09-24T00:00:00Z"


def _obs(oid: str, gates: dict, *, created_at: str | None, observed_at: str,
         sub: str = "apis@ateles-swarm") -> dict:
    row = {
        "id": oid,
        "observed_at": observed_at,
        "fields": {GATE_STATUS_FIELD: gates},
        "provenance": {
            "agent_sub": sub,
            "agent_thumbprint": "daemon-bearer",
            "attribution_tier": "unverified_client",
        },
    }
    if created_at is not None:
        row["created_at"] = created_at
    return row


def _unproven(monkeypatch, observations: list[dict], value: dict) -> set[str]:
    store = IssueGateStore("https://neotoma.invalid", "tok")
    monkeypatch.setattr(
        "gate_waive._ns.agent_identity",
        lambda agent, sub=None: {"key": "/k", "sub": sub, "kid": "k"},
    )
    monkeypatch.setattr(gate_waive, "_lens_key_thumbprint", lambda identity: LENS_TP)

    async def fetch(entity_id, *, limit=100):
        return list(observations)

    monkeypatch.setattr(store, "_observations", fetch)
    state = IssueGateState(
        repo="o/r", issue_number=795, entity_id="ent_1",
        gate_status=dict(value), field_provenance={GATE_STATUS_FIELD: observations[0]["id"]},
    )
    return _run(store.unverified_signed_off_gates(state, {"ux": "accipiter"}))


class TestPreDeploySignOffsByServerTime:
    def test_unsigned_value_ingested_before_the_cutoff_is_accepted(self, monkeypatch):
        monkeypatch.setenv("APIS_GATE_SIGNING_CUTOFF", CUTOFF)
        obs = [_obs("o1", {"ux": "signed_off"}, created_at=EARLY, observed_at=EARLY)]
        assert _unproven(monkeypatch, obs, {"ux": "signed_off"}) == set()

    def test_unsigned_value_ingested_after_the_cutoff_is_unproven(self, monkeypatch):
        monkeypatch.setenv("APIS_GATE_SIGNING_CUTOFF", CUTOFF)
        obs = [_obs("o1", {"ux": "signed_off"}, created_at=LATE, observed_at=LATE)]
        assert _unproven(monkeypatch, obs, {"ux": "signed_off"}) == {"ux"}

    def test_cutoff_unset_leaves_every_unsigned_value_unproven(self, monkeypatch):
        monkeypatch.delenv("APIS_GATE_SIGNING_CUTOFF", raising=False)
        obs = [_obs("o1", {"ux": "signed_off"}, created_at=EARLY, observed_at=EARLY)]
        assert _unproven(monkeypatch, obs, {"ux": "signed_off"}) == {"ux"}

    @pytest.mark.parametrize("raw", ["yesterday", "2026-09-24T00:00:00", "", "   "])
    def test_unparseable_or_zoneless_cutoff_grandfathers_nothing(self, monkeypatch, raw):
        monkeypatch.setenv("APIS_GATE_SIGNING_CUTOFF", raw)
        obs = [_obs("o1", {"ux": "signed_off"}, created_at=EARLY, observed_at=EARLY)]
        assert _unproven(monkeypatch, obs, {"ux": "signed_off"}) == {"ux"}

    def test_a_forged_early_observed_at_with_late_ingestion_is_unproven(self, monkeypatch):
        monkeypatch.setenv("APIS_GATE_SIGNING_CUTOFF", CUTOFF)
        obs = [_obs("o1", {"ux": "signed_off"}, created_at=LATE, observed_at=EARLY)]
        assert _unproven(monkeypatch, obs, {"ux": "signed_off"}) == {"ux"}

    def test_no_server_time_is_unproven(self, monkeypatch):
        monkeypatch.setenv("APIS_GATE_SIGNING_CUTOFF", CUTOFF)
        obs = [_obs("o1", {"ux": "signed_off"}, created_at=None, observed_at=EARLY)]
        assert _unproven(monkeypatch, obs, {"ux": "signed_off"}) == {"ux"}

    def test_a_post_cutoff_flip_from_pending_is_unproven(self, monkeypatch):
        """Pending before the cutoff, `signed_off` written unsigned after it:
        the value was not set before the cutoff."""
        monkeypatch.setenv("APIS_GATE_SIGNING_CUTOFF", CUTOFF)
        obs = [
            _obs("o2", {"ux": "signed_off"}, created_at=LATE, observed_at=LATE),
            _obs("o1", {"ux": "pending"}, created_at=EARLY, observed_at=EARLY),
        ]
        assert _unproven(monkeypatch, obs, {"ux": "signed_off"}) == {"ux"}

    def test_a_later_rewrite_that_keeps_a_legacy_value_stays_legacy(self, monkeypatch):
        monkeypatch.setenv("APIS_GATE_SIGNING_CUTOFF", CUTOFF)
        obs = [
            _obs("o2", {"ux": "signed_off", "pm": "pending"}, created_at=LATE,
                 observed_at=LATE),
            _obs("o1", {"ux": "signed_off"}, created_at=EARLY, observed_at=EARLY),
        ]
        assert _unproven(monkeypatch, obs, {"ux": "signed_off"}) == set()


# ── Envelope provenance (prod `/entities/query` shape) ──────────────────────


class TestLoadReadsEnvelopeProvenance:
    def test_field_provenance_comes_from_the_query_row_envelope(self, monkeypatch):
        """Shape read live from prod on 2026-09-23: `provenance` sits beside
        `snapshot`, and `snapshot` carries none."""
        store = IssueGateStore("https://neotoma.invalid", "tok")

        async def prod_shape(path, payload):
            return {
                "entities": [
                    {
                        "entity_id": "ent_1",
                        "provenance": {GATE_STATUS_FIELD: "obs-head", "repo": "obs-0"},
                        "snapshot": {
                            "repo": "o/r",
                            "number": 795,
                            GATE_STATUS_FIELD: {"ux": "signed_off"},
                        },
                    }
                ]
            }

        monkeypatch.setattr(store, "_post", prod_shape)
        state = _run(store.load("o/r", 795))
        assert state.field_provenance.get(GATE_STATUS_FIELD) == "obs-head"
